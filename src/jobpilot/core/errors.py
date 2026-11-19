"""Typed exceptions. Graph nodes catch these and set status; they never crash the daemon."""


class JobPilotError(Exception):
    """Base class for all project errors."""


class FillError(JobPilotError):
    """A field could not be filled (e.g. combobox option not found)."""


class BlockerDetected(JobPilotError):
    """A captcha, login wall or OTP prompt needs a human."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


class FeedError(JobPilotError):
    """A job feed could not be fetched or parsed."""


class BudgetExceeded(JobPilotError):
    """The monthly LLM budget has been reached."""


class DuplicateApplication(JobPilotError):
    """An application for this job already exists."""