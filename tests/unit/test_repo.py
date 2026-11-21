"""Repo tests run against a real temporary SQLite database (tmp_db fixture), never mocks."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from jobpilot.core.enums import ATS, JobStatus, TaskKind
from jobpilot.core.text import job_hash, normalize
from jobpilot.db import repo
from jobpilot.db.session import get_session
from jobpilot.db.tables import Application, Company, HumanTask, Job, LLMCall, utcnow

pytestmark = pytest.mark.usefixtures("tmp_db")


# ------------------------------------------------------------------------------- helpers


def _id(obj: Company | Job | Application | HumanTask) -> int:
    assert obj.id is not None
    return obj.id


def _company(
    name: str = "Acme", slug: str = "acme", priority: int = 3, ats: ATS = ATS.GREENHOUSE
) -> Company:
    return repo.upsert_company(
        name=name, ats=ats, slug=slug, careers_url=f"https://x/{slug}", priority=priority
    )


def _job(company_id: int, title: str = "Backend Engineer", location: str = "Pune") -> Job:
    job = repo.insert_job(
        company_id=company_id,
        external_id=f"ext-{company_id}-{title}",
        job_hash=job_hash(str(company_id), title, location),
        title=title,
        location=location,
        description_text="desc",
        apply_url="https://x/apply",
    )
    assert job is not None
    return job


def _application(title: str = "Backend Engineer", company: Company | None = None) -> Application:
    company = company or _company()
    app = repo.create_application(_id(_job(_id(company), title)))
    assert app is not None
    return app


# ------------------------------------------------------------------------------- database


def test_wal_and_foreign_keys_are_enabled(tmp_db: Engine) -> None:
    with tmp_db.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_session_rolls_back_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), get_session() as s:
        s.add(Company(name="X", ats="lever", slug="x", careers_url="https://x"))
        raise RuntimeError("boom")
    assert repo.list_companies(active_only=False) == []


# ------------------------------------------------------------------------------ companies


def test_upsert_company_updates_instead_of_duplicating() -> None:
    first = _company(name="Acme", priority=3)
    again = _company(name="Acme Inc", priority=1)
    assert _id(first) == _id(again)
    assert len(repo.list_companies()) == 1
    stored = repo.get_company(_id(first))
    assert stored is not None
    assert (stored.name, stored.priority) == ("Acme Inc", 1)


def test_list_companies_orders_by_priority_and_hides_inactive() -> None:
    _company("Low", "low", priority=3)
    top = _company("Top", "top", priority=1)
    gone = _company("Gone", "gone", priority=2)
    repo.record_feed_miss(_id(gone), deactivate_after=1)
    assert [c.name for c in repo.list_companies()] == ["Top", "Low"]
    assert len(repo.list_companies(active_only=False)) == 3
    assert _id(top) in {_id(c) for c in repo.list_companies()}


def test_feed_misses_deactivate_after_two_and_reset_on_success() -> None:
    cid = _id(_company())
    assert repo.record_feed_miss(cid) is False
    repo.reset_feed_misses(cid)
    assert repo.record_feed_miss(cid) is False  # counter was reset
    assert repo.record_feed_miss(cid) is True
    company = repo.get_company(cid)
    assert company is not None
    assert company.active is False


# ---------------------------------------------------------------------------------- jobs


def test_duplicate_job_hash_is_rejected() -> None:
    cid = _id(_company())
    kwargs = {
        "company_id": cid,
        "external_id": "1",
        "job_hash": "same-hash",
        "title": "SDE",
        "description_text": "d",
        "apply_url": "https://x",
    }
    assert repo.insert_job(**kwargs) is not None  # type: ignore[arg-type]
    assert repo.insert_job(**kwargs) is None  # type: ignore[arg-type]
    assert repo.job_exists("same-hash")
    assert len(repo.list_jobs()) == 1


def test_insert_job_with_unknown_company_is_a_real_error_not_a_duplicate() -> None:
    with pytest.raises(IntegrityError):
        repo.insert_job(
            company_id=999,
            external_id="1",
            job_hash="h",
            title="SDE",
            description_text="d",
            apply_url="https://x",
        )


def test_get_job_view_joins_company() -> None:
    company = _company("Acme", "acme", ats=ATS.LEVER)
    job = _job(_id(company), "Platform Engineer")
    view = repo.get_job_view(_id(job))
    assert (view.company, view.company_slug, view.ats) == ("Acme", "acme", ATS.LEVER)
    assert view.title == "Platform Engineer"
    with pytest.raises(LookupError):
        repo.get_job_view(999)


def test_set_job_status_rejects_application_states() -> None:
    job = _job(_id(_company()))
    repo.set_job_status(_id(job), JobStatus.QUEUED)
    for bad in (JobStatus.SUBMITTED, JobStatus.NEEDS_HUMAN, JobStatus.FAILED):
        with pytest.raises(ValueError, match="Application status"):
            repo.set_job_status(_id(job), bad)
    with pytest.raises(LookupError):
        repo.set_job_status(999, JobStatus.QUEUED)


def test_save_match_stores_score_and_reasons() -> None:
    job = _job(_id(_company()))
    repo.save_match(
        _id(job), status=JobStatus.REJECTED_FIT, score=42, reasons=["no python", "senior"]
    )
    stored = repo.get_job(_id(job))
    assert stored is not None
    assert (stored.status, stored.match_score) == ("REJECTED_FIT", 42)
    assert stored.match_reasons == ["no python", "senior"]


def test_pending_jobs_returns_only_discovered() -> None:
    cid = _id(_company())
    a, b = _job(cid, "A"), _job(cid, "B")
    repo.set_job_status(_id(b), JobStatus.REJECTED_FILTER)
    assert [_id(j) for j in repo.pending_jobs()] == [_id(a)]


def test_next_queued_jobs_orders_by_priority_then_score() -> None:
    low = _id(_company("A", "a", priority=2))
    dream = _id(_company("B", "b", priority=1))
    ja, jb1, jb2 = _job(low, "SDE A"), _job(dream, "SDE B1"), _job(dream, "SDE B2")
    for job, score in ((ja, 90), (jb1, 60), (jb2, 80)):
        repo.save_match(_id(job), status=JobStatus.QUEUED, score=score, reasons=[])
    assert [_id(j) for j in repo.next_queued_jobs(10)] == [_id(jb2), _id(jb1), _id(ja)]
    assert [_id(j) for j in repo.next_queued_jobs(1)] == [_id(jb2)]


def test_next_queued_jobs_skips_taken_unqueued_and_inactive() -> None:
    live = _id(_company("Live", "live"))
    dead = _id(_company("Dead", "dead"))
    taken, free, unqueued, orphan = (
        _job(live, "Taken"),
        _job(live, "Free"),
        _job(live, "Unqueued"),
        _job(dead, "Orphan"),
    )
    for job in (taken, free, orphan):
        repo.save_match(_id(job), status=JobStatus.QUEUED, score=80, reasons=[])
    assert unqueued.status == "DISCOVERED"
    repo.create_application(_id(taken))
    repo.record_feed_miss(dead, deactivate_after=1)
    assert [_id(j) for j in repo.next_queued_jobs(10)] == [_id(free)]


# ------------------------------------------------------------------------- applications


def test_second_create_application_for_the_same_job_returns_none() -> None:
    job_id = _id(_job(_id(_company())))
    first = repo.create_application(job_id)
    assert first is not None
    assert first.status == "IN_PROGRESS"
    assert repo.create_application(job_id) is None
    assert len(repo.list_applications()) == 1


def test_create_application_for_unknown_job_is_a_real_error() -> None:
    with pytest.raises(IntegrityError):
        repo.create_application(999)


def test_reset_application_only_works_on_failed() -> None:
    app = _application()
    assert repo.reset_application(app.job_id) is None  # still IN_PROGRESS
    assert repo.reset_application(999) is None
    repo.update_application(_id(app), status=JobStatus.FAILED, error="boom", attempts=2)
    reset = repo.reset_application(app.job_id)
    assert reset is not None
    assert (reset.status, reset.error, reset.attempts) == ("IN_PROGRESS", None, 0)


def test_update_application_validates_fields_and_bumps_updated_at() -> None:
    app = _application()
    with pytest.raises(ValueError, match="unknown application fields"):
        repo.update_application(_id(app), confidnce=0.9)  # typo must not pass silently
    with pytest.raises(ValueError):
        repo.update_application(_id(app), status="BOGUS")
    before = repo.get_application(_id(app))
    assert before is not None
    time.sleep(0.01)
    repo.update_application(_id(app), confidence=0.9, plan={"answers": []})
    after = repo.get_application(_id(app))
    assert after is not None
    assert after.updated_at > before.updated_at
    assert (after.confidence, after.plan) == (0.9, {"answers": []})


def test_stale_in_progress_finds_only_old_runs() -> None:
    old, fresh = _application("Old"), _application("Fresh")
    with get_session() as s:
        row = s.get(Application, _id(old))
        assert row is not None
        row.updated_at = utcnow() - timedelta(hours=1)
        s.add(row)
    assert [_id(a) for a in repo.stale_in_progress(30)] == [_id(old)]
    assert _id(fresh) not in {_id(a) for a in repo.stale_in_progress(30)}


# ------------------------------------------------------------------------ caps and guards


def test_start_of_local_day_uses_ist_midnight() -> None:
    now = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)  # 01:30 on 20 Nov in IST
    assert repo.start_of_local_day(now) == datetime(2026, 11, 19, 18, 30, tzinfo=UTC)


def test_daily_cap_counts_since_local_midnight_not_utc() -> None:
    now = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
    company = _company()
    before = _application("Before", company)  # 23:59 IST yesterday
    after = _application("After", company)  # 00:01 IST today
    never = _application("Never", company)  # not submitted
    repo.update_application(_id(before), submitted_at=datetime(2026, 11, 19, 18, 29, tzinfo=UTC))
    repo.update_application(_id(after), submitted_at=datetime(2026, 11, 19, 18, 31, tzinfo=UTC))
    assert repo.count_submitted_today(now) == 1
    assert never.submitted_at is None


def test_company_application_count_ignores_failed_and_skipped() -> None:
    company = _company()
    live, failed, skipped = (
        _application("Live", company),
        _application("Failed", company),
        _application("Skipped", company),
    )
    repo.update_application(_id(failed), status=JobStatus.FAILED)
    repo.update_application(_id(skipped), status=JobStatus.SKIPPED)
    assert repo.count_company_applications(_id(company)) == 1
    assert live.status == "IN_PROGRESS"


def test_already_applied_matches_fuzzy_titles_per_company() -> None:
    company = _company()
    app = _application("Backend Engineer", company)
    cid = _id(company)
    assert repo.already_applied(cid, "backend engineer")
    assert repo.already_applied(cid, "Backend  Engineer *")  # normalization applies
    assert not repo.already_applied(cid, "Frontend Designer")
    assert not repo.already_applied(_id(_company("Other", "other")), "Backend Engineer")
    repo.update_application(_id(app), status=JobStatus.FAILED)  # failed attempts don't count
    assert not repo.already_applied(cid, "Backend Engineer")


# ----------------------------------------------------------------------------- answer bank


def test_save_answer_updates_the_same_normalized_question() -> None:
    repo.save_answer(question_raw="Notice Period *", answer="30 days", field_type="text")
    repo.save_answer(question_raw="notice period?", answer="60 days", field_type="text")
    assert len(repo.list_answers()) == 1
    found = repo.find_answer_exact(normalize("Notice period"))
    assert found is not None
    assert found.answer == "60 days"


def test_find_answer_by_key_returns_the_newest_entry() -> None:
    repo.save_answer(
        question_raw="Notice period", answer="30 days", field_type="text", canonical_key="np"
    )
    repo.save_answer(
        question_raw="When can you join", answer="45 days", field_type="text", canonical_key="np"
    )
    newest = repo.find_answer_by_key("np")
    assert newest is not None
    assert newest.answer == "45 days"
    time.sleep(0.01)
    repo.save_answer(
        question_raw="Notice period", answer="15 days", field_type="text", canonical_key="np"
    )
    newest = repo.find_answer_by_key("np")
    assert newest is not None
    assert newest.answer == "15 days"  # re-answering makes it the newest again
    assert repo.find_answer_by_key("missing") is None


def test_answer_norms_search_bump_and_delete() -> None:
    entry = repo.save_answer(question_raw="Expected CTC", answer="18 LPA", field_type="text")
    repo.save_answer(question_raw="Github link", answer="https://g", field_type="url")
    assert {q for _, q in repo.answer_norms()} == {"expected ctc", "github link"}
    assert [e.answer for e in repo.list_answers(search="ctc")] == ["18 LPA"]
    repo.bump_answer_use(_id_of(entry))
    bumped = repo.get_answer(_id_of(entry))
    assert bumped is not None
    assert bumped.times_used == 1
    assert repo.delete_answer(_id_of(entry)) is True
    assert repo.delete_answer(_id_of(entry)) is False


def _id_of(entry: object) -> int:
    value = getattr(entry, "id", None)
    assert isinstance(value, int)
    return value


# ----------------------------------------------------------------------------- human tasks


def test_get_or_create_open_task_is_idempotent_per_kind() -> None:
    app_id = _id(_application())
    first = repo.get_or_create_open_task(app_id, TaskKind.ANSWER, {"q": ["a"]})
    again = repo.get_or_create_open_task(app_id, TaskKind.ANSWER, {"q": ["a", "b"]})
    other = repo.get_or_create_open_task(app_id, TaskKind.APPROVE, {})
    assert _id(first) == _id(again)
    assert _id(other) != _id(first)
    refreshed = repo.get_human_task(_id(first))
    assert refreshed is not None
    assert refreshed.payload == {"q": ["a", "b"]}  # payload reflects the latest verification
    assert len(repo.open_tasks(app_id)) == 2


def test_answer_task_is_one_shot() -> None:
    app_id = _id(_application())
    task = repo.get_or_create_open_task(app_id, TaskKind.ANSWER, {})
    done = repo.answer_task(_id(task), {"action": "answers"})
    assert done is not None
    assert done.status == "answered"
    assert done.answered_at is not None
    assert repo.answer_task(_id(task), {"action": "again"}) is None  # double tap ignored
    fresh = repo.get_or_create_open_task(app_id, TaskKind.ANSWER, {})
    assert _id(fresh) != _id(task)  # a new open task is created after the old one closed


def test_expire_and_stale_tasks() -> None:
    app_id = _id(_application())
    old = repo.get_or_create_open_task(app_id, TaskKind.ANSWER, {})
    new = repo.get_or_create_open_task(app_id, TaskKind.APPROVE, {})
    with get_session() as s:
        row = s.get(HumanTask, _id(old))
        assert row is not None
        row.created_at = utcnow() - timedelta(hours=30)
        s.add(row)
    assert [_id(t) for t in repo.stale_open_tasks(24)] == [_id(old)]
    assert repo.expire_task(_id(old)) is True
    assert repo.expire_task(_id(old)) is False
    assert repo.stale_open_tasks(24) == []
    repo.set_task_message_id(_id(new), 4242)
    updated = repo.get_human_task(_id(new))
    assert updated is not None
    assert updated.telegram_message_id == 4242


# ------------------------------------------------------------------------------------ emails


def test_insert_email_event_dedupes_and_validates_category() -> None:
    received = datetime(2026, 11, 19, 15, 30, tzinfo=timezone_ist())
    kwargs = {
        "message_id": "<m1@x>",
        "from_addr": "hr@acme.com",
        "subject": "Thanks for applying",
        "received_at": received,
        "category": "ack",
    }
    assert repo.insert_email_event(**kwargs) is not None  # type: ignore[arg-type]
    assert repo.insert_email_event(**kwargs) is None  # type: ignore[arg-type]
    assert repo.email_seen("<m1@x>")
    assert not repo.email_seen("<other>")
    with pytest.raises(ValueError, match="category"):
        repo.insert_email_event(**{**kwargs, "message_id": "<m2@x>", "category": "spam"})  # type: ignore[arg-type]


def timezone_ist():  # type: ignore[no-untyped-def]
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Kolkata")


def test_latest_email_received_is_stored_as_utc() -> None:
    assert repo.latest_email_received() is None
    repo.insert_email_event(
        message_id="<a>",
        from_addr="a@x.com",
        subject="s",
        received_at=datetime(2026, 11, 19, 15, 30, tzinfo=timezone_ist()),  # 10:00 UTC
        category="other",
    )
    repo.insert_email_event(
        message_id="<b>",
        from_addr="b@x.com",
        subject="s",
        received_at=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        category="other",
    )
    assert repo.latest_email_received() == datetime(2026, 11, 19, 10, 0)
    assert len(repo.list_email_events()) == 2


# ----------------------------------------------------------------------------------- LLM cost


def test_month_cost_is_zero_with_no_calls() -> None:
    assert repo.month_llm_cost() == 0.0


def test_month_start_is_ist_midnight_on_the_first() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    assert repo.start_of_local_month(now) == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)


def test_month_cost_sums_only_this_local_month() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    with get_session() as s:
        s.add(  # 23:30 IST on 31 Oct: last month
            LLMCall(
                agent="a", model="m", input_tokens=1, output_tokens=1, cost_usd=5.0,
                created_at=datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
            )
        )
        s.add(  # 00:30 IST on 1 Nov: this month
            LLMCall(
                agent="a", model="m", input_tokens=1, output_tokens=1, cost_usd=0.25,
                created_at=datetime(2026, 10, 31, 19, 0, tzinfo=UTC),
            )
        )
        s.add(
            LLMCall(
                agent="a", model="m", input_tokens=1, output_tokens=1, cost_usd=0.5,
                created_at=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
            )
        )
    assert repo.month_llm_cost(now) == pytest.approx(0.75)


def test_log_llm_call_is_counted_in_the_current_month() -> None:
    repo.log_llm_call(
        agent="match_score", model="m", input_tokens=100, output_tokens=10, cost_usd=0.01
    )
    repo.log_llm_call(
        agent="verify", model="m", input_tokens=200, output_tokens=20, cost_usd=0.02
    )
    assert repo.month_llm_cost() == pytest.approx(0.03)