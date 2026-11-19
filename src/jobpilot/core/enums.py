"""Shared enumerations. One member per line (ruff E70x)."""

from enum import StrEnum


class ATS(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    CUSTOM = "custom"


class JobStatus(StrEnum):
    """Lifecycle codes.

    Ownership: Job.status holds DISCOVERED..QUEUED; Application.status holds
    IN_PROGRESS onward (see docs/DECISIONS.md).
    """

    DISCOVERED = "DISCOVERED"
    REJECTED_FILTER = "REJECTED_FILTER"
    REJECTED_FIT = "REJECTED_FIT"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"


# Order used by the Tracker: statuses may only move forward along this chain.
FORWARD_CHAIN: tuple[JobStatus, ...] = (
    JobStatus.SUBMITTED,
    JobStatus.ACKNOWLEDGED,
    JobStatus.ASSESSMENT,
    JobStatus.INTERVIEW,
)


class FieldType(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    TEL = "tel"
    URL = "url"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"
    COMBOBOX = "combobox"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    CHECKBOX_GROUP = "checkbox_group"
    FILE = "file"
    UNKNOWN = "unknown"


class AnswerSource(StrEnum):
    ALIAS_PROFILE = "alias_profile"
    ANSWER_BANK = "answer_bank"
    LLM_GROUNDED = "llm_grounded"
    HUMAN = "human"
    NONE = "none"


class Verdict(StrEnum):
    PASS = "PASS"
    NEEDS_INPUT = "NEEDS_INPUT"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"


class Severity(StrEnum):
    BLOCK = "block"
    ASK = "ask"
    WARN = "warn"


class TaskKind(StrEnum):
    APPROVE = "approve"
    ANSWER = "answer"
    CAPTCHA = "captcha"
    LOGIN = "login"
    ERROR = "error"