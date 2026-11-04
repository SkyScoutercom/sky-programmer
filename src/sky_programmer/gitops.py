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

import os
import subprocess
import time
from pathlib import Path

GIT_TIMEOUT_S = 30.0
NET_TIMEOUT_S = 120.0
_MAX_LISTED = 30


def audit_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "sky-programmer" / "git_ops.log"


def is_app_repo(path) -> bool:
    """A git checkout of this project, and nothing else."""
    path = Path(path).expanduser()
    return (path / ".git").exists() and (path / "src" / "sky_programmer" / "__init__.py").is_file()


def running_from(repo) -> bool:
    """Whether the code running now is in this folder. If so, a pull changes this app the next time it starts."""
    try:
        return Path(__file__).resolve().is_relative_to(Path(repo).expanduser().resolve())
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
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["LC_ALL"] = "C"
    env.pop("GIT_ASKPASS", None)  # a graphical password window would sit behind the app with no explanation
    env.pop("SSH_ASKPASS", None)
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL)  # fmt: skip


def _text(repo: Path, *args: str) -> str:
    """Trimmed output of a local query, or "" if it failed."""
    r = _git(repo, *args)
    return r.stdout.strip() if r.returncode == 0 else ""


def _audit(op: str, repo: Path, ok: bool, detail: str = "") -> None:
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {op} dir={repo} ok={ok} "
                    f"{detail[:200].replace(chr(10), ' ')}\n")  # fmt: skip
    except OSError:
        pass


def _scrub(text: str) -> str:
    """Commit subjects and filenames come from other people, so control characters become '·'."""
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


def _clean_url(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return scheme + "://" + rest.split("@", 1)[-1]
    return url


def _fail(op: str, repo, text: str) -> dict:
    return {"ok": False, "op": op, "output": text, "changed": False}


def _not_this_project(repo) -> str:
    return f"{repo} is not a checkout of SKY programmer, so nothing was done there."


def status(repo) -> dict:
    """Facts for the page. Local only: it never touches the network."""
    repo = Path(repo).expanduser()
    out = {"dir": str(repo), "is_repo": is_app_repo(repo)}
    if not out["is_repo"]:
        return out
    try:
        out["branch"] = _text(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        out["head"] = _text(repo, "rev-parse", "--short=12", "HEAD")
        dirty = [line for line in _git(repo, "status", "--porcelain").stdout.splitlines() if line.strip()]
        out["dirty"] = len(dirty)
        out["dirty_files"] = [line[:100] for line in dirty[:_MAX_LISTED]]
        out["remote"] = _clean_url(_text(repo, "remote", "get-url", "origin"))
        out["upstream"] = _text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        counts = _text(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
        if len(counts) == 2:
            out["behind"], out["ahead"] = int(counts[0]), int(counts[1])
        fetch_head = _text(repo, "rev-parse", "--git-path", "FETCH_HEAD")
        try:
            out["fetched_ago"] = time.time() - (repo / fetch_head).stat().st_mtime
        except OSError:
            out["fetched_ago"] = None
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
    return out


def report(repo, st: dict | None = None) -> str:
    """The Status button's answer: where the folder stands, and how old that knowledge is."""
    repo = Path(repo).expanduser()
    st = st if st is not None else status(repo)
    if not st.get("is_repo"):
        return _not_this_project(repo)
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
        log = _git(repo, "log", "--oneline", "--no-color", "--no-decorate", "-8").stdout
    except (OSError, subprocess.SubprocessError) as e:
        log = f"could not read the log: {e}"
    lines.extend(f"  {line[:100]}" for line in log.splitlines() if line.strip())
    return _scrub("\n".join(lines))


def pull(repo) -> dict:
    """--ff-only: a diverged branch is reported, not merged."""
    repo = Path(repo).expanduser()
    if not is_app_repo(repo):
        return _fail("pull", repo, _not_this_project(repo))
    before = _text(repo, "rev-parse", "HEAD")
    try:
        r = _git(repo, "pull", "--ff-only", timeout=NET_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _audit("pull", repo, False, "timeout")
        return _fail("pull", repo, f"Timed out after {NET_TIMEOUT_S:.0f} s. Is the network up?")
    except (OSError, subprocess.SubprocessError) as e:
        _audit("pull", repo, False, str(e))
        return _fail("pull", repo, f"Could not run git: {e}")
    ok = r.returncode == 0
    output = _scrub(_trim(r.stdout + ("\n" + r.stderr if r.stderr else "")))
    _audit("pull", repo, ok, output.splitlines()[-1] if output else "")
    if not ok and ("fast-forward" in output or "diverg" in output):
        output += "\n\nThis folder and GitHub have both changed, so nothing was merged. Reset makes it match GitHub."
    if not ok and ("could not read Username" in output or "Authentication failed" in output):
        output += "\n\nGit has no saved login for GitHub here. Run `gh auth setup-git` or pull once in a terminal."
    return {"ok": ok, "op": "pull", "output": output, "changed": _text(repo, "rev-parse", "HEAD") != before}


def reset_preview(repo) -> dict:
    """Everything a reset would throw away. Fetches first when it can, so the target is what GitHub has now."""
    repo = Path(repo).expanduser()
    if not is_app_repo(repo):
        return _fail("reset", repo, _not_this_project(repo))
    note = ""
    try:
        fetched = _git(repo, "fetch", timeout=NET_TIMEOUT_S)
        if fetched.returncode != 0:
            note = "GitHub couldn't be reached, so this uses what was last fetched."
    except (OSError, subprocess.SubprocessError):
        note = "GitHub couldn't be reached, so this uses what was last fetched."
    upstream = _text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    target = upstream or "HEAD"
    dirty = [line for line in _git(repo, "status", "--porcelain").stdout.splitlines() if line.strip()]
    dropped = _git(repo, "log", "--oneline", "--no-decorate", f"{target}..HEAD").stdout.splitlines()
    return {
        "ok": True, "op": "reset", "output": "", "target": target,
        "commit": _text(repo, "rev-parse", target),
        "head": _text(repo, "rev-parse", "--short=12", "HEAD"),
        "lost_files": [_scrub(line[:100]) for line in dirty if not line.startswith("??")],
        "untracked": sum(line.startswith("??") for line in dirty),
        "dropped": [_scrub(line[:100]) for line in dropped],
        "note": note,
    }  # fmt: skip


def reset_hard(repo, commit: str, target: str) -> dict:
    """reset --hard to the commit the operator reviewed. If the target has moved since, nothing happens."""
    repo = Path(repo).expanduser()
    if not is_app_repo(repo):
        return _fail("reset", repo, _not_this_project(repo))
    if not commit or _text(repo, "rev-parse", target) != commit:
        return _fail("reset", repo, f"{target} has changed since you reviewed this, so nothing was reset.")
    before = _text(repo, "rev-parse", "--short=12", "HEAD")
    try:
        r = _git(repo, "reset", "--hard", commit)
    except (OSError, subprocess.SubprocessError) as e:
        _audit("reset", repo, False, str(e))
        return _fail("reset", repo, f"Could not run git: {e}")
    ok = r.returncode == 0
    output = _scrub(_trim(r.stdout + ("\n" + r.stderr if r.stderr else "")))
    _audit("reset", repo, ok, f"{before} -> {target} {commit[:12]}")
    if ok:
        output = f"reset --hard {target}\n{output}\n\nThe folder was at {before}. To go back: git reset --hard {before}"
    return {"ok": ok, "op": "reset", "output": output, "changed": ok}
