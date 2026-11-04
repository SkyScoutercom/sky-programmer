"""Errors the app reports to the user. Each maps to its own exit code."""


class SkyError(Exception):
    exit_code = 1


class SafetyRefusal(SkyError):
    """The app refused to do something. Nothing was sent to the board."""

    exit_code = 2


class CommsError(SkyError):
    """The board did not answer, or the port could not be used."""

    exit_code = 3


class VerifyFailed(SkyError):
    """A write was sent and the board does not hold what was expected."""

    exit_code = 4
