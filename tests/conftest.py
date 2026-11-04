import pytest

from sky_programmer.errors import SafetyRefusal
from sky_programmer.link import Link
from sky_programmer.programmer import Programmer
from sky_programmer.simulator import FakeVesc


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
