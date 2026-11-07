"""Status, pull and reset for this app's own source folder.

The same idea as the SKY kiosk's OTA pane (skyscouter/gitops.py in the SKY repo). As there:
- pull is --ff-only, so a diverged branch is reported, never silently merged, and reset is the explicit recovery;
- reset goes to the upstream branch when there is one, else to HEAD, and says which;
- every operation that changes something adds one line to a log;
- git can never hang waiting for a prompt, and remote URLs are shown without any embedded password.

Unlike the kiosk this runs on the operator's own desktop, so it keeps their normal git credentials instead of a token
file. It also refuses any folder that isn't this project, because reset --hard in the wrong folder would destroy work.

The callers enforce the run-state gate: no pull or reset while a board is connected.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from .paths import data_dir

GIT_TIMEOUT_S = 30.0
NET_TIMEOUT_S = 120.0
_MAX_LISTED = 200

# These would point git at a different repo from the folder named on screen, e.g. when started from a git hook.
_ENV_NOT_INHERITED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
                      "GIT_ASKPASS", "SSH_ASKPASS")  # a graphical password window would sit behind the app  # fmt: skip


class GitFailed(Exception):
    """A query failed, so its answer is unknown. It must never be read as an empty answer."""


def audit_path() -> Path:
    return data_dir() / "git_ops.log"


def is_app_repo(path) -> bool:
    """Whether this folder looks like a checkout of this project. _problem() checks what git itself makes of it."""
    path = Path(path).expanduser()
    return (path / ".git").exists() and (path / "src" / "sky_programmer" / "__init__.py").is_file()


def running_from(repo) -> bool:
    """Whether the code running now is this folder's own. Only then does a pull change this app at its next start."""
    try:
        return Path(__file__).resolve() == (Path(repo).expanduser() / "src/sky_programmer/gitops.py").resolve()
    except OSError:
        return False


def find_checkout():
    """The source folder this app runs from, or the usual place for it. None if neither is one."""
    candidates = list(Path(__file__).resolve().parents) + [Path.home() / "Desktop" / "sky-programmer"]
    for path in candidates:
        if is_app_repo(path):
            return path
    return None


def _git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_NOT_INHERITED}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["SSH_ASKPASS_REQUIRE"] = "never"
    env["LC_ALL"] = "C"
    return subprocess.run(["git", "-C", str(repo), "-c", "core.askPass=", "-c", "submodule.recurse=false", *args],
                          capture_output=True, text=True, errors="replace", timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)  # fmt: skip


def _text(repo: Path, *args: str) -> str:
    """Trimmed output of a query that may fairly have no answer, such as a branch with no upstream."""
    r = _git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def _must(repo: Path, *args: str) -> str:
    """Output of a query that has to work."""
    r = _git(repo, *args)
    if r.returncode != 0:
        raise GitFailed((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout


def _names(repo: Path, *args: str) -> set:
    return {name for name in _must(repo, *args, "-z").split("\0") if name}


def _problem(repo: Path) -> str:
    """Why nothing should be done in this folder, or "". Git's own view counts: an empty .git makes it climb upwards."""
    if not is_app_repo(repo):
        return f"{repo} is not a checkout of SKY programmer, so nothing was done there."
    r = _git(repo, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return f"Git can't use {repo}, so nothing was done there: {_scrub(r.stderr.strip())}"
    top = Path(r.stdout.strip()).resolve()
    if top != repo.resolve():
        return f"Git treats {top} as the repo here, not {repo}, so nothing was done."
    return ""


def _state(repo: Path) -> str:
    """A fingerprint of the commit and the tracked files, to tell whether either has changed."""
    parts = (_must(repo, "rev-parse", "HEAD"), _must(repo, "status", "--porcelain"), _must(repo, "diff", "HEAD"))
    return hashlib.sha256("\0".join(parts).encode(errors="replace")).hexdigest()


def _state_or_none(repo: Path):
    try:
        return _state(repo)
    except (GitFailed, OSError, subprocess.SubprocessError):
        return None


def _audit(op: str, repo: Path, ok: bool, detail: str = "") -> None:
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {op} dir={repo} ok={ok} "
                    f"{_hide_credentials(detail)[:200].replace(chr(10), ' ')}\n")  # fmt: skip
    except OSError:
        pass


def _hide_credentials(text: str) -> str:
    return re.sub(r"(\w+://)[^/\s]*@", r"\1", text)


def _scrub(text: str) -> str:
    """Commit subjects and filenames come from other people, so control characters become '·'."""
    text = _hide_credentials(text)
    return "".join(ch if ch == "\n" or (" " <= ch != "\x7f") else "·" for ch in text)


def _trim(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… (trimmed)"


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h = m // 60
    return f"{h} h" if h < 48 else f"{h // 24} days"


def _fail(op: str, text: str, changed: bool = False) -> dict:
    return {"ok": False, "op": op, "output": text, "changed": changed}


def status(repo) -> dict:
    """Facts for the page. Local only: it never touches the network."""
    repo = Path(repo).expanduser()
    out = {"dir": str(repo), "is_repo": is_app_repo(repo)}
    if not out["is_repo"]:
        return out
    try:
        problem = _problem(repo)
        if problem:
            out["error"] = problem
            return out
        out["branch"] = _text(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        out["head"] = _text(repo, "rev-parse", "--short=12", "HEAD")
        dirty = [line for line in _must(repo, "status", "--porcelain").splitlines() if line.strip()]
        out["dirty"] = len(dirty)
        out["dirty_files"] = [line[:100] for line in dirty[:30]]
        out["remote"] = _hide_credentials(_text(repo, "remote", "get-url", "origin"))
        out["upstream"] = _text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        counts = _text(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
        if len(counts) == 2:
            out["behind"], out["ahead"] = int(counts[0]), int(counts[1])
        fetch_head = _text(repo, "rev-parse", "--git-path", "FETCH_HEAD")
        try:
            out["fetched_ago"] = time.time() - (repo / fetch_head).stat().st_mtime if fetch_head else None
        except OSError:
            out["fetched_ago"] = None
    except (GitFailed, OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
    return out


def report(repo, st: dict | None = None) -> str:
    """The Status button's answer: where the folder stands, and how old that knowledge is."""
    repo = Path(repo).expanduser()
    st = st if st is not None else status(repo)
    if not st.get("is_repo"):
        return f"{repo} is not a checkout of SKY programmer."
    if st.get("error"):  # missing keys must not turn into a healthy-looking report
        return _scrub(f"branch     {st.get('branch', '?')}\n\nStatus incomplete: {st['error']}")
    lines = [f"branch     {st['branch']} @ {st['head']}"]
    if st["upstream"]:
        lines.append(f"upstream   {st['upstream']}: {st.get('behind', 0)} behind, {st.get('ahead', 0)} ahead")
    else:
        lines.append("upstream   none, so this branch has nothing to pull from or reset to")
    if st["remote"]:
        lines.append(f"remote     {st['remote']}")
    if st["fetched_ago"] is None:
        lines.append("fetched    never, so behind and ahead are unknown until the first pull")
    else:
        lines.append(f"fetched    {_age(st['fetched_ago'])} ago (behind and ahead were measured then)")
    lines.append("")
    if st["dirty"]:
        lines.append(f"uncommitted ({st['dirty']}):")
        lines.extend(f"  {f}" for f in st["dirty_files"])
        if st["dirty"] > len(st["dirty_files"]):
            lines.append(f"  … and {st['dirty'] - len(st['dirty_files'])} more")
    else:
        lines.append("uncommitted: none")
    lines.extend(["", "recent commits:"])
    try:
        log = _must(repo, "log", "--oneline", "--no-color", "--no-decorate", "-8")
    except (GitFailed, OSError, subprocess.SubprocessError) as e:
        log = f"could not read the log: {e}"
    lines.extend(f"  {line[:100]}" for line in log.splitlines() if line.strip())
    return _scrub("\n".join(lines))


def pull(repo) -> dict:
    """--ff-only: a diverged branch is reported, not merged. `changed` means the code differs, however it ended."""
    repo = Path(repo).expanduser()
    problem = _problem(repo)
    if problem:
        return _fail("pull", problem)
    before = _state_or_none(repo)
    ok, output = False, ""
    try:
        r = _git(repo, "pull", "--ff-only", timeout=NET_TIMEOUT_S)
        ok = r.returncode == 0
        output = _scrub(_trim(r.stdout + ("\n" + r.stderr if r.stderr else "")))
    except subprocess.TimeoutExpired:
        output = f"Timed out after {NET_TIMEOUT_S:.0f} s. Is the network up?"
    except (OSError, subprocess.SubprocessError) as e:
        output = f"Could not run git: {e}"
    _audit("pull", repo, ok, output.splitlines()[-1] if output else "")
    if not ok and ("fast-forward" in output or "diverg" in output):
        output += "\n\nThis folder and GitHub have both changed, so nothing was merged. Reset makes it match GitHub."
    if not ok and ("could not read Username" in output or "Authentication failed" in output):
        output += "\n\nGit has no saved login for GitHub here. Run `gh auth setup-git` or pull once in a terminal."
    after = _state_or_none(repo)
    return {"ok": ok, "op": "pull", "output": output, "changed": before is None or after != before}


def _would_overwrite(repo: Path, target: str) -> set:
    """Untracked and ignored paths the reset would replace. Reset only spares them if the target has nothing there."""
    others = _names(repo, "ls-files", "--others")
    wanted = _names(repo, "ls-tree", "-r", "--name-only", target)
    hit = others & wanted
    for name in others:  # an untracked folder where the target has a file
        if any(str(parent) in wanted for parent in Path(name).parents):
            hit.add(name)
    for name in wanted:  # an untracked file where the target has a folder
        hit.update(str(parent) for parent in Path(name).parents if str(parent) in others)
    return hit


def reset_preview(repo) -> dict:
    """Everything a reset would throw away. Fetches first when it can, so the target is what GitHub has now."""
    repo = Path(repo).expanduser()
    problem = _problem(repo)
    if problem:
        return _fail("reset", problem)
    note = ""
    try:
        if _git(repo, "fetch", timeout=NET_TIMEOUT_S).returncode != 0:
            note = "GitHub couldn't be reached, so this uses what was last fetched."
    except (OSError, subprocess.SubprocessError):
        note = "GitHub couldn't be reached, so this uses what was last fetched."
    try:
        upstream = _text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        target = upstream or "HEAD"
        commit = _must(repo, "rev-parse", target).strip()
        dirty = [line for line in _must(repo, "status", "--porcelain").splitlines() if line.strip()]
        dropped = _must(repo, "log", "--oneline", "--no-decorate", f"{target}..HEAD").splitlines()
        overwritten = _would_overwrite(repo, target)
        kept = _names(repo, "ls-files", "--others", "--exclude-standard") - overwritten
        return {
            "ok": True, "op": "reset", "output": "", "dir": str(repo), "target": target, "commit": commit,
            "head": _must(repo, "rev-parse", "--short=12", "HEAD").strip(),
            "lost_files": [_scrub(line[:100]) for line in dirty if not line.startswith("??")][:_MAX_LISTED],
            "overwritten": [_scrub(name[:100]) for name in sorted(overwritten)][:_MAX_LISTED],
            "dropped": [_scrub(line[:100]) for line in dropped][:_MAX_LISTED],
            "merge": _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0,
            "untracked": len(kept),
            "state": _state(repo),
            "note": note,
        }  # fmt: skip
    except (GitFailed, OSError, subprocess.SubprocessError) as e:
        return _fail("reset", _scrub(f"Couldn't work out what a reset would change, so nothing was done: {e}"))


def reset_hard(repo, preview: dict) -> dict:
    """reset --hard to the commit the operator reviewed. If the target or folder moved since, nothing happens."""
    repo = Path(repo).expanduser()
    problem = _problem(repo)
    if problem:
        return _fail("reset", problem)
    commit, target = preview["commit"], preview["target"]
    try:
        if _must(repo, "rev-parse", target).strip() != commit:
            return _fail("reset", f"{target} has changed since you reviewed this, so nothing was reset.")
        if _state(repo) != preview["state"]:
            return _fail("reset", "This folder has changed since you reviewed this, so nothing was reset.")
        before = _must(repo, "rev-parse", "--short=12", "HEAD").strip()
        r = _git(repo, "reset", "--hard", commit)
    except (GitFailed, OSError, subprocess.SubprocessError) as e:
        _audit("reset", repo, False, str(e))
        return _fail("reset", _scrub(f"Could not reset: {e}"), _state_or_none(repo) != preview["state"])
    ok = r.returncode == 0
    output = _scrub(_trim(r.stdout + ("\n" + r.stderr if r.stderr else "")))
    _audit("reset", repo, ok, f"{before} -> {target} {commit[:12]}")
    if ok:
        output = f"reset --hard {target}\n{output}\n\nThe folder was at {before}. To go back: git reset --hard {before}"
    return {"ok": ok, "op": "reset", "output": output, "changed": ok or _state_or_none(repo) != preview["state"]}
