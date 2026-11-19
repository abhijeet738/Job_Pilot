"""All database queries. No SQL anywhere else in the project (plan Section 15.1).

Part 1: companies, jobs, applications.
Datetimes: SQLite returns naive values; everything stored here is UTC.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select

from ..core.enums import ATS, JobStatus, TaskKind
from ..core.models import JobView
from ..core.text import normalize
from ..settings import get_settings
from .session import get_session
from .tables import (
    AnswerBankEntry,
    Application,
    Company,
    EmailEvent,
    HumanTask,
    Job,
    LLMCall,
    utcnow,
)

# Applications in these states do not count toward caps or duplicate checks.
_INACTIVE = (JobStatus.FAILED.value, JobStatus.SKIPPED.value)

_APPLICATION_FIELDS = frozenset(
    {
        "status",
        "resume_key",
        "plan",
        "verify",
        "confidence",
        "submitted_at",
        "error",
        "attempts",
        "artifacts_dir",
    }
)


# --------------------------------------------------------------------------- companies


def upsert_company(
    *, name: str, ats: ATS | str, slug: str, careers_url: str, priority: int = 3
) -> Company:
    """Insert a company, or update name/url/priority if (ats, slug) already exists."""
    ats_value = ATS(ats).value
    with get_session() as s:
        existing = s.exec(
            select(Company).where(col(Company.ats) == ats_value, col(Company.slug) == slug)
        ).first()
        if existing is not None:
            existing.name = name
            existing.careers_url = careers_url
            existing.priority = priority
            s.add(existing)
            s.flush()
            return existing
        company = Company(
            name=name, ats=ats_value, slug=slug, careers_url=careers_url, priority=priority
        )
        s.add(company)
        s.flush()
        return company


def get_company(company_id: int) -> Company | None:
    with get_session() as s:
        return s.get(Company, company_id)


def list_companies(*, active_only: bool = True) -> list[Company]:
    with get_session() as s:
        stmt = select(Company).order_by(col(Company.priority), col(Company.name))
        if active_only:
            stmt = stmt.where(col(Company.active).is_(True))
        return list(s.exec(stmt).all())


def record_feed_miss(company_id: int, deactivate_after: int = 2) -> bool:
    """Count one 404 for this company. Returns True if it was deactivated by this call."""
    with get_session() as s:
        company = s.get(Company, company_id)
        if company is None:
            raise LookupError(f"company {company_id} not found")
        company.feed_misses += 1
        deactivated = company.feed_misses >= deactivate_after
        if deactivated:
            company.active = False
        s.add(company)
        return deactivated


def reset_feed_misses(company_id: int) -> None:
    """Call after a successful feed fetch."""
    with get_session() as s:
        company = s.get(Company, company_id)
        if company is not None and company.feed_misses:
            company.feed_misses = 0
            s.add(company)


# --------------------------------------------------------------------------------- jobs


def job_exists(job_hash: str) -> bool:
    with get_session() as s:
        stmt = select(Job.id).where(col(Job.job_hash) == job_hash)
        return s.exec(stmt).first() is not None


def insert_job(
    *,
    company_id: int,
    external_id: str,
    job_hash: str,
    title: str,
    description_text: str,
    apply_url: str,
    location: str | None = None,
    remote: bool | None = None,
    posted_at: datetime | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    source: str = "feed",
) -> Job | None:
    """Insert a DISCOVERED job. Returns None if a job with the same hash already exists."""
    with get_session() as s:
        if s.exec(select(Job.id).where(col(Job.job_hash) == job_hash)).first() is not None:
            return None
        job = Job(
            company_id=company_id,
            external_id=external_id,
            job_hash=job_hash,
            title=title,
            location=location,
            remote=remote,
            description_text=description_text,
            apply_url=apply_url,
            posted_at=posted_at,
            salary_min=salary_min,
            salary_max=salary_max,
            source=source,
        )
        s.add(job)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            # Lost a race on the unique hash -> duplicate. Anything else (e.g. a bad
            # company_id) is a real error and must not be swallowed.
            if s.exec(select(Job.id).where(col(Job.job_hash) == job_hash)).first() is not None:
                return None
            raise
        return job


def get_job(job_id: int) -> Job | None:
    with get_session() as s:
        return s.get(Job, job_id)


def get_job_view(job_id: int) -> JobView:
    """Job joined with its company: what prompts and graph nodes need."""
    with get_session() as s:
        row = s.exec(
            select(Job, Company)
            .join(Company, col(Company.id) == col(Job.company_id))
            .where(col(Job.id) == job_id)
        ).first()
        if row is None:
            raise LookupError(f"job {job_id} not found")
        job, company = row
        assert job.id is not None and company.id is not None
        return JobView(
            id=job.id,
            company_id=company.id,
            company=company.name,
            company_slug=company.slug,
            ats=ATS(company.ats),
            external_id=job.external_id,
            title=job.title,
            location=job.location,
            remote=job.remote,
            description_text=job.description_text,
            apply_url=job.apply_url,
            posted_at=job.posted_at,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
        )


def list_jobs(status: JobStatus | str | None = None, limit: int = 100) -> list[Job]:
    with get_session() as s:
        stmt = select(Job).order_by(col(Job.discovered_at).desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(col(Job.status) == JobStatus(status).value)
        return list(s.exec(stmt).all())


def pending_jobs(limit: int = 500) -> list[Job]:
    """DISCOVERED jobs waiting for the matcher, oldest first."""
    with get_session() as s:
        stmt = (
            select(Job)
            .where(col(Job.status) == JobStatus.DISCOVERED.value)
            .order_by(col(Job.id))
            .limit(limit)
        )
        return list(s.exec(stmt).all())


def set_job_status(job_id: int, status: JobStatus | str) -> None:
    """Set Job.status (DISCOVERED..QUEUED only; later states belong to Application)."""
    value = JobStatus(status).value
    allowed = {
        JobStatus.DISCOVERED.value,
        JobStatus.REJECTED_FILTER.value,
        JobStatus.REJECTED_FIT.value,
        JobStatus.QUEUED.value,
    }
    if value not in allowed:
        raise ValueError(f"{value} is an Application status, not a Job status")
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        job.status = value
        s.add(job)


def save_match(
    job_id: int, *, status: JobStatus | str, score: int | None, reasons: list[str]
) -> None:
    """Store the matcher outcome. score is None when a hard filter rejected the job."""
    value = JobStatus(status).value
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        job.status = value
        job.match_score = score
        job.match_reasons = reasons
        s.add(job)


def next_queued_jobs(limit: int) -> list[Job]:
    """QUEUED jobs with no application yet, best first.

    Order: company priority (1 = dream), then match score, then oldest discovery.
    Jobs of deactivated companies are skipped.
    """
    with get_session() as s:
        stmt = (
            select(Job)
            .join(Company, col(Company.id) == col(Job.company_id))
            .outerjoin(Application, col(Application.job_id) == col(Job.id))
            .where(
                col(Job.status) == JobStatus.QUEUED.value,
                col(Company.active).is_(True),
                col(Application.id).is_(None),
            )
            .order_by(
                col(Company.priority).asc(),
                col(Job.match_score).desc(),
                col(Job.discovered_at).asc(),
            )
            .limit(limit)
        )
        return list(s.exec(stmt).all())


# --------------------------------------------------------------------------- applications


def create_application(job_id: int) -> Application | None:
    """Create the one application a job may ever have, in IN_PROGRESS.

    Returns None if the job already has an application: the caller must skip it.
    The unique constraint on job_id is the real guarantee; this just reports it.
    """
    with get_session() as s:
        if s.exec(select(Application.id).where(col(Application.job_id) == job_id)).first():
            return None
        app = Application(job_id=job_id, status=JobStatus.IN_PROGRESS.value)
        s.add(app)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            if s.exec(select(Application.id).where(col(Application.job_id) == job_id)).first():
                return None
            raise  # e.g. job_id does not exist: a real error, not a duplicate
        return app


def reset_application(job_id: int) -> Application | None:
    """Explicit retry path: put a FAILED application back to IN_PROGRESS.

    Returns None if the job has no application or it is not FAILED.
    """
    with get_session() as s:
        app = s.exec(select(Application).where(col(Application.job_id) == job_id)).first()
        if app is None or app.status != JobStatus.FAILED.value:
            return None
        app.status = JobStatus.IN_PROGRESS.value
        app.error = None
        app.attempts = 0
        app.plan = None
        app.verify = None
        app.confidence = None
        app.updated_at = utcnow()
        s.add(app)
        s.flush()
        return app


def get_application(application_id: int) -> Application | None:
    with get_session() as s:
        return s.get(Application, application_id)


def get_application_for_job(job_id: int) -> Application | None:
    with get_session() as s:
        return s.exec(select(Application).where(col(Application.job_id) == job_id)).first()


def update_application(application_id: int, **fields: Any) -> Application:
    """Update whitelisted columns and bump updated_at. Unknown names raise ValueError."""
    unknown = set(fields) - _APPLICATION_FIELDS
    if unknown:
        raise ValueError(f"unknown application fields: {sorted(unknown)}")
    if "status" in fields:
        fields["status"] = JobStatus(fields["status"]).value  # validates the status code
    with get_session() as s:
        app = s.get(Application, application_id)
        if app is None:
            raise LookupError(f"application {application_id} not found")
        for key, value in fields.items():
            setattr(app, key, value)
        app.updated_at = utcnow()
        s.add(app)
        s.flush()
        return app


def set_application_status(
    application_id: int, status: JobStatus | str, error: str | None = None
) -> Application:
    if error is not None:
        return update_application(application_id, status=status, error=error)
    return update_application(application_id, status=status)


def list_applications(status: JobStatus | str | None = None, limit: int = 100) -> list[Application]:
    with get_session() as s:
        stmt = select(Application).order_by(col(Application.updated_at).desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(col(Application.status) == JobStatus(status).value)
        return list(s.exec(stmt).all())


def stale_in_progress(minutes: int = 30) -> list[Application]:
    """IN_PROGRESS applications untouched for a while: a crash happened. Re-run them."""
    cutoff = utcnow() - timedelta(minutes=minutes)
    with get_session() as s:
        stmt = select(Application).where(
            col(Application.status) == JobStatus.IN_PROGRESS.value,
            col(Application.updated_at) < cutoff,
        )
        return list(s.exec(stmt).all())


# ------------------------------------------------------------------ caps and duplicate guard


def start_of_local_day(now: datetime | None = None) -> datetime:
    """Midnight today in the configured timezone, expressed in UTC. `now` must be aware."""
    tz = ZoneInfo(get_settings().jobpilot_timezone)
    local_now = (now or utcnow()).astimezone(tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def count_submitted_since(since: datetime) -> int:
    with get_session() as s:
        stmt = (
            select(func.count())
            .select_from(Application)
            .where(col(Application.submitted_at) >= since)
        )
        return int(s.exec(stmt).one())


def count_submitted_today(now: datetime | None = None) -> int:
    """Submissions since local midnight: the number the daily cap is checked against."""
    return count_submitted_since(start_of_local_day(now))


def count_company_applications(company_id: int, days: int = 30) -> int:
    """Applications to this company in the last N days (FAILED and SKIPPED excluded)."""
    since = utcnow() - timedelta(days=days)
    with get_session() as s:
        stmt = (
            select(func.count())
            .select_from(Application)
            .join(Job, col(Job.id) == col(Application.job_id))
            .where(
                col(Job.company_id) == company_id,
                col(Application.created_at) >= since,
                col(Application.status).not_in(_INACTIVE),
            )
        )
        return int(s.exec(stmt).one())


def already_applied(
    company_id: int, title_norm: str, days: int = 30, threshold: float = 95.0
) -> bool:
    """True if this company got an application with a fuzzy-equal title in the last N days.

    Second guard against duplicates, on top of the unique job_id.
    """
    since = utcnow() - timedelta(days=days)
    with get_session() as s:
        stmt = (
            select(Job.title)
            .join(Application, col(Application.job_id) == col(Job.id))
            .where(
                col(Job.company_id) == company_id,
                col(Application.created_at) >= since,
                col(Application.status).not_in(_INACTIVE),
            )
        )
        titles = list(s.exec(stmt).all())
    target = normalize(title_norm)
    return any(fuzz.ratio(target, normalize(t)) >= threshold for t in titles)

# ========================================================================= part 2 below


def _to_utc(dt: datetime) -> datetime:
    """Aware datetimes are converted to UTC; naive ones are assumed to be UTC already."""
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt


# ------------------------------------------------------------------------------ answer bank


def save_answer(
    *,
    question_raw: str,
    answer: str,
    field_type: str,
    canonical_key: str | None = None,
) -> AnswerBankEntry:
    """Store an answer. The same normalized question updates the existing entry in place.

    created_at is refreshed on update so "newest entry for a canonical key" stays correct.
    """
    question_norm = normalize(question_raw)
    with get_session() as s:
        entry = s.exec(
            select(AnswerBankEntry).where(col(AnswerBankEntry.question_norm) == question_norm)
        ).first()
        if entry is not None:
            entry.answer = answer
            entry.question_raw = question_raw
            entry.field_type = field_type
            entry.canonical_key = canonical_key or entry.canonical_key
            entry.created_at = utcnow()
        else:
            entry = AnswerBankEntry(
                question_norm=question_norm,
                question_raw=question_raw,
                canonical_key=canonical_key,
                answer=answer,
                field_type=field_type,
            )
        s.add(entry)
        s.flush()
        return entry


def get_answer(answer_id: int) -> AnswerBankEntry | None:
    with get_session() as s:
        return s.get(AnswerBankEntry, answer_id)


def find_answer_exact(question_norm: str) -> AnswerBankEntry | None:
    """Exact match on the normalized question (step 2 of the lookup)."""
    with get_session() as s:
        stmt = select(AnswerBankEntry).where(col(AnswerBankEntry.question_norm) == question_norm)
        return s.exec(stmt).first()


def find_answer_by_key(canonical_key: str) -> AnswerBankEntry | None:
    """Newest entry for a canonical key (step 3 of the lookup)."""
    with get_session() as s:
        stmt = (
            select(AnswerBankEntry)
            .where(col(AnswerBankEntry.canonical_key) == canonical_key)
            .order_by(col(AnswerBankEntry.created_at).desc(), col(AnswerBankEntry.id).desc())
        )
        return s.exec(stmt).first()


def answer_norms() -> list[tuple[int, str]]:
    """(id, normalized question) for every entry: the candidate set for fuzzy matching."""
    with get_session() as s:
        rows = s.exec(select(AnswerBankEntry.id, AnswerBankEntry.question_norm)).all()
        return [(i, q) for i, q in rows if i is not None]


def list_answers(search: str | None = None, limit: int = 500) -> list[AnswerBankEntry]:
    with get_session() as s:
        stmt = select(AnswerBankEntry).order_by(col(AnswerBankEntry.id).desc()).limit(limit)
        if search:
            like = f"%{normalize(search)}%"
            stmt = stmt.where(col(AnswerBankEntry.question_norm).like(like))
        return list(s.exec(stmt).all())


def bump_answer_use(answer_id: int) -> None:
    with get_session() as s:
        entry = s.get(AnswerBankEntry, answer_id)
        if entry is not None:
            entry.times_used += 1
            s.add(entry)


def delete_answer(answer_id: int) -> bool:
    with get_session() as s:
        entry = s.get(AnswerBankEntry, answer_id)
        if entry is None:
            return False
        s.delete(entry)
        return True


# ------------------------------------------------------------------------------ human tasks


def get_or_create_open_task(
    application_id: int, kind: TaskKind | str, payload: dict[str, Any]
) -> HumanTask:
    """Idempotent: one open task per (application, kind).

    human_review runs again from the top after an interrupt, so this must be safe to repeat.
    An existing open task gets its payload refreshed, so it reflects the latest verification.
    """
    kind_value = TaskKind(kind).value
    with get_session() as s:
        task = s.exec(
            select(HumanTask).where(
                col(HumanTask.application_id) == application_id,
                col(HumanTask.kind) == kind_value,
                col(HumanTask.status) == "open",
            )
        ).first()
        if task is None:
            task = HumanTask(application_id=application_id, kind=kind_value, payload=payload)
        else:
            task.payload = payload
        s.add(task)
        s.flush()
        return task


def get_human_task(task_id: int) -> HumanTask | None:
    with get_session() as s:
        return s.get(HumanTask, task_id)


def open_tasks(application_id: int | None = None) -> list[HumanTask]:
    with get_session() as s:
        stmt = (
            select(HumanTask)
            .where(col(HumanTask.status) == "open")
            .order_by(col(HumanTask.created_at))
        )
        if application_id is not None:
            stmt = stmt.where(col(HumanTask.application_id) == application_id)
        return list(s.exec(stmt).all())


def set_task_message_id(task_id: int, telegram_message_id: int) -> None:
    with get_session() as s:
        task = s.get(HumanTask, task_id)
        if task is None:
            raise LookupError(f"task {task_id} not found")
        task.telegram_message_id = telegram_message_id
        s.add(task)


def answer_task(task_id: int, response: dict[str, Any]) -> HumanTask | None:
    """Record the human's reply. Returns None if the task is not open (e.g. a double tap)."""
    with get_session() as s:
        task = s.get(HumanTask, task_id)
        if task is None or task.status != "open":
            return None
        task.status = "answered"
        task.response = response
        task.answered_at = utcnow()
        s.add(task)
        s.flush()
        return task


def expire_task(task_id: int) -> bool:
    with get_session() as s:
        task = s.get(HumanTask, task_id)
        if task is None or task.status != "open":
            return False
        task.status = "expired"
        s.add(task)
        return True


def stale_open_tasks(older_than_hours: int) -> list[HumanTask]:
    """Open tasks older than N hours: reminded at 24 h, expired at 72 h."""
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    with get_session() as s:
        stmt = select(HumanTask).where(
            col(HumanTask.status) == "open", col(HumanTask.created_at) < cutoff
        )
        return list(s.exec(stmt).all())


# ------------------------------------------------------------------------------------ emails

EMAIL_CATEGORIES = frozenset(
    {"ack", "rejection", "assessment", "interview", "otp", "alert", "other"}
)


def email_seen(message_id: str) -> bool:
    with get_session() as s:
        stmt = select(EmailEvent.id).where(col(EmailEvent.message_id) == message_id)
        return s.exec(stmt).first() is not None


def insert_email_event(
    *,
    message_id: str,
    from_addr: str,
    subject: str,
    received_at: datetime,
    category: str,
    application_id: int | None = None,
    deadline: datetime | None = None,
    summary: str = "",
) -> EmailEvent | None:
    """Store a classified email. Returns None if this message_id was already stored."""
    if category not in EMAIL_CATEGORIES:
        raise ValueError(f"unknown email category: {category}")
    with get_session() as s:
        if s.exec(select(EmailEvent.id).where(col(EmailEvent.message_id) == message_id)).first():
            return None
        event = EmailEvent(
            message_id=message_id,
            application_id=application_id,
            from_addr=from_addr,
            subject=subject,
            received_at=_to_utc(received_at),
            category=category,
            deadline=_to_utc(deadline) if deadline else None,
            summary=summary,
        )
        s.add(event)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            if s.exec(
                select(EmailEvent.id).where(col(EmailEvent.message_id) == message_id)
            ).first():
                return None
            raise
        return event


def latest_email_received() -> datetime | None:
    """Newest stored email date (UTC): where the next inbox poll should start."""
    with get_session() as s:
        return s.exec(select(func.max(EmailEvent.received_at))).one()


def list_email_events(application_id: int | None = None, limit: int = 100) -> list[EmailEvent]:
    with get_session() as s:
        stmt = select(EmailEvent).order_by(col(EmailEvent.received_at).desc()).limit(limit)
        if application_id is not None:
            stmt = stmt.where(col(EmailEvent.application_id) == application_id)
        return list(s.exec(stmt).all())


# ------------------------------------------------------------------------------- LLM cost


def log_llm_call(
    *, agent: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float
) -> LLMCall:
    with get_session() as s:
        call = LLMCall(
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        s.add(call)
        s.flush()
        return call


def start_of_local_month(now: datetime | None = None) -> datetime:
    """Midnight on the 1st of this month in the configured timezone, as UTC."""
    tz = ZoneInfo(get_settings().jobpilot_timezone)
    local_now = (now or utcnow()).astimezone(tz)
    first = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(UTC)


def llm_cost_since(since: datetime) -> float:
    with get_session() as s:
        stmt = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
            col(LLMCall.created_at) >= since
        )
        return float(s.exec(stmt).one())


def month_llm_cost(now: datetime | None = None) -> float:
    """USD spent this calendar month (local time). The budget guard checks this before a call."""
    return llm_cost_since(start_of_local_month(now))