import pytest

from sky_programmer.errors import SafetyRefusal
from sky_programmer.link import Link
from sky_programmer.programmer import Programmer
from sky_programmer.simulator import FakeVesc


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch):
    """A test run started from a git hook mustn't point these tests' git commands at the real repo."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def board(tmp_path):
    """Returns make(**knobs) -> (fake board, Programmer). Every test fails if a forbidden command is sent."""
    made = []

    def make(baseline=True, **knobs):
        fake = FakeVesc(**knobs)
        made.append(fake)
        link = Link(fake, clock=fake.clock, sleep=fake.clock.sleep)
        prog = Programmer(link, tmp_path / "backups", say=lambda *_: None)
        if baseline:
            try:
                prog.save_backup()
                fake.commands.clear()
            except SafetyRefusal:
                pass  # tests of unsupported firmware
        return fake, prog

    yield make
    for fake in made:
        assert fake.violations == []
