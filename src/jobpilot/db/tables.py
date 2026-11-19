"""SQLModel tables. Declarations only: all queries live in db/repo.py.

Status ownership (see docs/DECISIONS.md):
  Job.status         -> DISCOVERED, REJECTED_FILTER, REJECTED_FIT, QUEUED
  Application.status -> IN_PROGRESS onward (NEEDS_HUMAN, READY, SUBMITTED, ...)
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from ..core.enums import JobStatus


def utcnow() -> datetime:
    """Timezone-aware 'now'. Never use datetime.utcnow (deprecated in 3.12)."""
    return datetime.now(UTC)


class Company(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("ats", "slug"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ats: str
    slug: str
    careers_url: str
    priority: int = 3  # 1 = dream company; tie-breaker in the apply queue
    active: bool = True
    feed_misses: int = 0  # consecutive 404s; deactivated at 2


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    external_id: str  # id from the ATS feed
    job_hash: str = Field(index=True, unique=True)  # dedupe key
    title: str
    location: str | None = None
    remote: bool | None = None
    description_text: str  # plain text, truncated to ~20k chars
    apply_url: str
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utcnow)
    salary_min: int | None = None
    salary_max: int | None = None
    status: str = Field(default=JobStatus.DISCOVERED.value, index=True)
    match_score: int | None = None
    match_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    source: str = "feed"  # feed | jsonld | alert_email | page


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", unique=True)  # one per job, ever
    status: str = Field(index=True)
    resume_key: str | None = None
    plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # FillPlan dump
    verify: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # VerifyResult
    confidence: float | None = None
    submitted_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    artifacts_dir: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AnswerBankEntry(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    question_norm: str = Field(index=True)  # normalized question text
    question_raw: str
    canonical_key: str | None = Field(default=None, index=True)
    answer: str
    field_type: str  # for select/radio, `answer` is the option text
    times_used: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class HumanTask(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: str  # approve | answer | captcha | login | error
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "open"  # open | answered | expired
    response: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    telegram_message_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None


class EmailEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(unique=True)
    application_id: int | None = Field(default=None, foreign_key="application.id")
    from_addr: str
    subject: str
    received_at: datetime
    category: str  # ack | rejection | assessment | interview | otp | alert | other
    deadline: datetime | None = None
    summary: str = ""


class LLMCall(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: datetime = Field(default_factory=utcnow, index=True)