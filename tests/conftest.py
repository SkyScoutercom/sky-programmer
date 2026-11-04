import pytest

from fake_vesc import FakeVesc
from sky_programmer.link import Link
from sky_programmer.programmer import Programmer


@pytest.fixture
def board(tmp_path):
    """Returns make(**knobs) -> (fake board, Programmer). Every test fails if a forbidden command is sent."""
    made = []

    def make(**knobs):
        fake = FakeVesc(**knobs)
        made.append(fake)
        link = Link(fake, clock=fake.clock, sleep=fake.clock.sleep)
        return fake, Programmer(link, tmp_path / "backups", say=lambda *_: None)

    yield make
    for fake in made:
        assert fake.violations == []
