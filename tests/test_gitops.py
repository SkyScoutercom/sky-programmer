"""The git engine, against throwaway repos with a local "remote". It never touches this repo or the network."""
import os
import subprocess
from pathlib import Path

import pytest

from sky_programmer import gitops


def _run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _project(path: Path) -> Path:
    """Make this folder look like a checkout of the project."""
    (path / "src" / "sky_programmer").mkdir(parents=True)
    (path / "src" / "sky_programmer" / "__init__.py").write_text("")
    return path


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """(work, other): two clones of one bare remote. `other` is how new commits reach GitHub."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _run(seed, "config", "user.email", "t@t")
    _run(seed, "config", "user.name", "t")
    _project(seed)
    (seed / "f.txt").write_text("one\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-qm", "one")
    _run(seed, "remote", "add", "origin", str(bare))
    _run(seed, "push", "-q", "-u", "origin", "main")
    work, other = tmp_path / "work", tmp_path / "other"
    for clone in (work, other):
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        _run(clone, "config", "user.email", "t@t")
        _run(clone, "config", "user.name", "t")
    return work, other


def _commit(repo, name="new.txt", text="x\n", message="next"):
    (repo / name).write_text(text)
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", message)


def _publish(other, **kw):
    _commit(other, **kw)
    _run(other, "push", "-q")


# ---------------------------------------------------------------------- which folder
def test_only_a_checkout_of_this_project_is_accepted(tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert gitops.is_app_repo(other) is False
    assert gitops.is_app_repo(tmp_path / "missing") is False
    _project(other)
    assert gitops.is_app_repo(other) is True


def test_a_project_folder_without_git_is_refused(tmp_path):
    assert gitops.is_app_repo(_project(tmp_path)) is False


@pytest.mark.parametrize("call", [
    lambda p: gitops.pull(p),
    lambda p: gitops.reset_preview(p),
    lambda p: gitops.reset_hard(p, {"commit": "abc", "target": "HEAD", "state": ""}),
])
def test_operations_refuse_a_folder_that_is_not_this_project(tmp_path, call):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / "precious.txt").write_text("work\n")
    result = call(other)
    assert result["ok"] is False
    assert "not a checkout" in result["output"]
    assert (other / "precious.txt").read_text() == "work\n"


def test_reset_refuses_an_unrelated_repo_even_with_a_valid_commit(tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    _run(other, "config", "user.email", "t@t")
    _run(other, "config", "user.name", "t")
    _commit(other, "a.txt", "one\n")
    (other / "a.txt").write_text("uncommitted work\n")
    result = gitops.reset_hard(other, {"commit": _run(other, "rev-parse", "HEAD"), "target": "HEAD", "state": ""})
    assert result["ok"] is False
    assert (other / "a.txt").read_text() == "uncommitted work\n"


def test_running_from_is_true_only_for_the_folder_holding_this_code(tmp_path):
    assert gitops.running_from(Path(gitops.__file__).parents[2]) is True
    assert gitops.running_from(tmp_path) is False
    assert gitops.running_from(Path(gitops.__file__).parents[3]) is False  # a folder that merely contains it


# ---------------------------------------------------------------------- status
def test_report_says_where_the_folder_stands(repos):
    work, _ = repos
    text = gitops.report(work)
    assert "branch     main @" in text
    assert "origin/main: 0 behind, 0 ahead" in text
    assert "uncommitted: none" in text
    assert "one" in text


def test_report_lists_uncommitted_files(repos):
    work, _ = repos
    (work / "f.txt").write_text("changed\n")
    assert "uncommitted (1):\n   M f.txt" in gitops.report(work)


def test_status_never_touches_the_network(repos):
    work, other = repos
    _publish(other)
    assert gitops.status(work)["behind"] == 0  # the remote moved, and status hasn't fetched


def test_report_says_how_old_behind_and_ahead_are(repos):
    work, _ = repos
    os.remove(work / ".git" / "FETCH_HEAD") if (work / ".git" / "FETCH_HEAD").exists() else None
    assert "fetched    never" in gitops.report(work)
    _run(work, "fetch", "-q")
    assert "ago (behind and ahead were measured then)" in gitops.report(work)


def test_report_scrubs_control_characters_from_commit_subjects(repos):
    work, _ = repos
    _commit(work, message="evil\x1b[31m subject")
    text = gitops.report(work)
    assert "\x1b" not in text
    assert "evil·[31m subject" in text


def test_remote_url_never_shows_an_embedded_password(repos):
    work, _ = repos
    _run(work, "remote", "set-url", "origin", "https://user:secret@github.com/x/y.git")
    st = gitops.status(work)
    assert st["remote"] == "https://github.com/x/y.git"
    assert "secret" not in gitops.report(work, st)


def test_a_failed_status_does_not_become_a_healthy_report():
    text = gitops.report("/nowhere", {"is_repo": True, "branch": "main", "error": "git broke"})
    assert "Status incomplete: git broke" in text
    assert "uncommitted" not in text


def test_a_detached_head_or_no_upstream_is_reported(repos):
    work, _ = repos
    _run(work, "checkout", "-q", "-b", "local-only")
    assert "upstream   none" in gitops.report(work)


# ---------------------------------------------------------------------- pull
def test_pull_brings_in_new_commits_and_says_the_code_changed(repos):
    work, other = repos
    _publish(other, name="fresh.txt")
    result = gitops.pull(work)
    assert result["ok"] is True
    assert result["changed"] is True
    assert (work / "fresh.txt").exists()


def test_pull_with_nothing_new_changes_nothing(repos):
    work, _ = repos
    result = gitops.pull(work)
    assert result["ok"] is True
    assert result["changed"] is False


def test_pull_refuses_to_merge_a_diverged_branch(repos):
    work, other = repos
    _publish(other, name="theirs.txt")
    _commit(work, name="mine.txt")
    head = _run(work, "rev-parse", "HEAD")
    result = gitops.pull(work)
    assert result["ok"] is False
    assert result["changed"] is False
    assert "nothing was merged" in result["output"]
    assert _run(work, "rev-parse", "HEAD") == head


def test_pull_keeps_uncommitted_work_it_would_overwrite(repos):
    work, other = repos
    _publish(other, name="f.txt", text="theirs\n")
    (work / "f.txt").write_text("my work\n")
    result = gitops.pull(work)
    assert result["ok"] is False
    assert (work / "f.txt").read_text() == "my work\n"


def test_pull_never_waits_for_a_password(repos, monkeypatch):
    work, _ = repos
    _run(work, "remote", "set-url", "origin", "https://127.0.0.1:9/x/y.git")
    monkeypatch.setenv("GIT_ASKPASS", "/bin/false-window")
    result = gitops.pull(work)
    assert result["ok"] is False
    assert result["changed"] is False


def test_pull_is_logged_with_no_url_password(repos, tmp_path):
    work, _ = repos
    gitops.pull(work)
    log = (tmp_path / "data" / "sky-programmer" / "git_ops.log").read_text()
    assert " pull dir=" in log and "ok=True" in log


# ---------------------------------------------------------------------- reset
def test_reset_preview_lists_what_would_be_lost_and_changes_nothing(repos):
    work, other = repos
    _publish(other, name="theirs.txt")
    _commit(work, name="mine.txt", message="my local commit")
    (work / "f.txt").write_text("my edit\n")
    (work / "scratch.txt").write_text("untracked\n")
    head = _run(work, "rev-parse", "HEAD")
    preview = gitops.reset_preview(work)
    assert preview["target"] == "origin/main"
    assert any("f.txt" in line for line in preview["lost_files"])
    assert any("my local commit" in line for line in preview["dropped"])
    assert preview["untracked"] == 1
    assert (work / "f.txt").read_text() == "my edit\n"
    assert _run(work, "rev-parse", "HEAD") == head


def test_reset_preview_fetches_so_the_target_is_current(repos):
    work, other = repos
    _publish(other, name="theirs.txt")
    preview = gitops.reset_preview(work)
    assert preview["commit"] == _run(other, "rev-parse", "HEAD")
    assert preview["note"] == ""


def test_reset_preview_says_when_github_could_not_be_reached(repos):
    work, _ = repos
    _run(work, "remote", "set-url", "origin", "/no/such/remote.git")
    preview = gitops.reset_preview(work)
    assert preview["ok"] is True
    assert "last fetched" in preview["note"]


def test_reset_makes_the_folder_match_upstream_and_keeps_untracked_files(repos):
    work, other = repos
    _publish(other, name="theirs.txt")
    _commit(work, name="mine.txt")
    (work / "f.txt").write_text("my edit\n")
    (work / "scratch.txt").write_text("untracked\n")
    old_head = _run(work, "rev-parse", "--short=12", "HEAD")
    preview = gitops.reset_preview(work)
    result = gitops.reset_hard(work, preview)
    assert result["ok"] is True
    assert result["changed"] is True
    assert _run(work, "rev-parse", "HEAD") == _run(other, "rev-parse", "HEAD")
    assert (work / "f.txt").read_text() == "one\n"
    assert not (work / "mine.txt").exists()
    assert (work / "scratch.txt").exists()
    assert f"git reset --hard {old_head}" in result["output"]


def test_reset_can_be_undone_with_the_command_it_prints(repos):
    work, _ = repos
    _commit(work, name="mine.txt")
    preview = gitops.reset_preview(work)
    result = gitops.reset_hard(work, preview)
    undo = result["output"].rsplit("git ", 1)[1].split()
    _run(work, *undo)
    assert (work / "mine.txt").exists()


def test_reset_with_no_upstream_goes_to_head_and_says_so(repos):
    work, _ = repos
    _run(work, "checkout", "-q", "-b", "local-only")
    (work / "f.txt").write_text("my edit\n")
    preview = gitops.reset_preview(work)
    assert preview["target"] == "HEAD"
    result = gitops.reset_hard(work, preview)
    assert result["ok"] is True
    assert "reset --hard HEAD" in result["output"]
    assert (work / "f.txt").read_text() == "one\n"


def test_reset_refuses_if_the_target_moved_since_the_review(repos):
    work, other = repos
    (work / "f.txt").write_text("my edit\n")
    preview = gitops.reset_preview(work)
    _publish(other, name="later.txt")
    _run(work, "fetch", "-q")
    result = gitops.reset_hard(work, preview)
    assert result["ok"] is False
    assert "changed since you reviewed" in result["output"]
    assert (work / "f.txt").read_text() == "my edit\n"


def test_reset_is_logged(repos, tmp_path):
    work, _ = repos
    preview = gitops.reset_preview(work)
    gitops.reset_hard(work, preview)
    assert " reset dir=" in (tmp_path / "data" / "sky-programmer" / "git_ops.log").read_text()


def test_reset_lists_untracked_and_ignored_files_it_would_replace(repos):
    work, other = repos
    (other / "theirs.txt").write_text("upstream\n")
    (other / "ignored.txt").write_text("upstream\n")
    _run(other, "add", ".")
    _run(other, "commit", "-qm", "adds two files")
    _run(other, "push", "-q")
    (work / ".git" / "info" / "exclude").write_text("ignored.txt\n")
    (work / "ignored.txt").write_text("my ignored work\n")
    (work / "theirs.txt").write_text("my untracked work\n")
    (work / "elsewhere.txt").write_text("kept\n")
    preview = gitops.reset_preview(work)
    assert set(preview["overwritten"]) == {"ignored.txt", "theirs.txt"}
    assert preview["untracked"] == 1
    assert preview["lost_files"] == []
    gitops.reset_hard(work, preview)
    assert (work / "theirs.txt").read_text() == "upstream\n"  # what the dialog said would be replaced
    assert (work / "elsewhere.txt").exists()


def test_reset_lists_an_untracked_folder_where_the_target_has_a_file(repos):
    work, other = repos
    _publish(other, name="thing", text="a file upstream\n")
    (work / "thing").mkdir()
    (work / "thing" / "mine.txt").write_text("my work\n")
    preview = gitops.reset_preview(work)
    assert "thing/mine.txt" in preview["overwritten"]


def test_reset_refuses_if_the_folder_changed_since_the_review(repos):
    work, _ = repos
    (work / "f.txt").write_text("first edit\n")
    preview = gitops.reset_preview(work)
    (work / "f.txt").write_text("a later edit that was never shown\n")
    result = gitops.reset_hard(work, preview)
    assert result["ok"] is False
    assert "changed since you reviewed" in result["output"]
    assert (work / "f.txt").read_text() == "a later edit that was never shown\n"


def test_reset_refuses_if_a_commit_was_made_since_the_review(repos):
    work, _ = repos
    preview = gitops.reset_preview(work)
    _commit(work, name="later.txt")
    assert gitops.reset_hard(work, preview)["ok"] is False
    assert (work / "later.txt").exists()


def test_inherited_git_location_cannot_send_a_reset_to_another_repo(repos, tmp_path, monkeypatch):
    work, other = repos
    (other / "f.txt").write_text("someone else's work\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    preview = gitops.reset_preview(work)
    assert preview["lost_files"] == []
    gitops.reset_hard(work, preview)
    assert (other / "f.txt").read_text() == "someone else's work\n"


def test_an_empty_git_folder_does_not_send_a_reset_to_the_outer_repo(repos):
    work, _ = repos
    (work / "f.txt").write_text("outer repo's work\n")
    inner = work / "copy"
    _project(inner)
    (inner / ".git").mkdir()
    assert gitops.is_app_repo(inner) is True
    result = gitops.reset_preview(inner)
    assert result["ok"] is False
    assert (work / "f.txt").read_text() == "outer repo's work\n"


def test_a_failed_git_status_is_not_reported_as_clean(repos):
    work, _ = repos
    (work / "f.txt").write_text("dirty\n")
    (work / ".git" / "index").write_bytes(b"not an index")
    assert "uncommitted: none" not in gitops.report(work)
    preview = gitops.reset_preview(work)
    assert preview["ok"] is False
    assert "nothing was done" in preview["output"]


def test_a_pull_that_times_out_after_moving_head_still_says_the_code_changed(repos, monkeypatch):
    work, other = repos
    _publish(other, name="fresh.txt")
    real = gitops._git

    def pull_then_time_out(repo, *args, **kw):
        result = real(repo, *args, **kw)
        if args[0] == "pull":
            raise subprocess.TimeoutExpired("git", 1)
        return result

    monkeypatch.setattr(gitops, "_git", pull_then_time_out)
    result = gitops.pull(work)
    assert result["ok"] is False
    assert result["changed"] is True


def test_askpass_configured_in_git_is_not_run(repos, tmp_path):
    work, _ = repos
    marker = tmp_path / "ran"
    script = tmp_path / "askpass.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    _run(work, "config", "core.askPass", str(script))
    _run(work, "remote", "set-url", "origin", "https://127.0.0.1:9/x/y.git")
    gitops.pull(work)
    assert not marker.exists()


@pytest.mark.parametrize("url, shown", [
    ("https://user:pa@ss@github.com/x/y.git", "https://github.com/x/y.git"),
    ("https://github.com/foo@v1/repo.git", "https://github.com/foo@v1/repo.git"),
])
def test_urls_hide_credentials_and_nothing_else(url, shown):
    assert gitops._hide_credentials(f"fatal: {url} failed") == f"fatal: {shown} failed"


def test_git_being_missing_is_an_answer_not_a_crash(repos, monkeypatch):
    work, _ = repos

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(gitops.subprocess, "run", no_git)
    assert gitops.status(work)["error"]
