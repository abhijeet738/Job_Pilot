"""Pydantic domain models that cross module boundaries (Section 7.1 of the plan)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .enums import ATS, AnswerSource, FieldType, Severity, Verdict


class FormField(BaseModel):
    """One field as extracted from the live page."""

    field_id: str  # stable id we assign: f0, f1, ...
    selector: str  # CSS selector matching exactly one element (or group)
    type: FieldType
    label: str
    name: str | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None
    accept: str | None = None
    section: str | None = None
    help_text: str | None = None


class FieldAnswer(BaseModel):
    """The decided value for one field, with provenance."""

    field_id: str
    canonical_key: str | None = None
    value: str | list[str] | None = None
    source: AnswerSource
    confidence: float = Field(ge=0, le=1)
    fact_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def has_value(self) -> bool:
        return self.value not in (None, "", [])


class FillPlan(BaseModel):
    job_id: int
    adapter: ATS
    url: str
    resume_key: str
    answers: list[FieldAnswer] = Field(default_factory=list)

    def unanswered_required(self, fields: list[FormField]) -> list[FormField]:
        """Required fields that have no non-empty answer in this plan."""
        answered = {a.field_id for a in self.answers if a.has_value}
        return [f for f in fields if f.required and f.field_id not in answered]


class VerifyIssue(BaseModel):
    field_id: str | None = None
    severity: Severity
    message: str


class VerifyResult(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    issues: list[VerifyIssue] = Field(default_factory=list)


class MatchResult(BaseModel):
    score: int = Field(ge=0, le=100)
    decision: Literal["apply", "skip"]
    reasons: list[str] = Field(default_factory=list, max_length=4)
    missing_requirements: list[str] = Field(default_factory=list)


class JobView(BaseModel):
    """Job plus its company name: what prompts and nodes need (Job only stores company_id)."""

    id: int
    company_id: int
    company: str
    company_slug: str
    ats: ATS
    external_id: str
    title: str
    location: str | None = None
    remote: bool | None = None
    description_text: str = ""
    apply_url: str
    posted_at: datetime | None = None
    salary_min: int | None = None
    salary_max: int | None = None