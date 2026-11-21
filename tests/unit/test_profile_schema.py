"""T1.6: Profile schema, facts(), years_experience(), match_summary() and persistence.

All year figures pin today=2026-11-19, because a current role counts up to "today".
"""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from jobpilot.profile import schema
from jobpilot.profile.schema import (
    Experience,
    Profile,
    ResumeVariant,
    load_profile,
    profile_path,
    save_profile,
)
from jobpilot.settings import get_settings

TODAY = date(2026, 11, 19)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profile.json"


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _data() -> dict[str, object]:
    loaded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _exp(start: date, end: date | None) -> Experience:
    return Experience(company="Co", title="Dev", start=start, end=end)


def _with_experience(profile: Profile, *jobs: Experience) -> Profile:
    return profile.model_copy(update={"experience": list(jobs)})


# --------------------------------------------------------------------------- the fixture


def test_fixture_profile_is_valid(profile: Profile) -> None:
    assert (profile.first_name, profile.last_name) == ("Asha", "Testcandidate")
    assert profile.phone == "+919000012345"
    assert profile.default_resume is not None
    assert profile.default_resume.key == "backend"
    assert profile.portfolio_url is None


# ------------------------------------------------------------------------------ fact ids


EXPECTED_IDS = [
    "name", "email", "phone", "location",
    "link.linkedin", "link.github",
    "summary", "years",
    "exp.0", "exp.0.location", "exp.0.b0", "exp.0.b1", "exp.0.b2", "exp.0.tech",
    "exp.1", "exp.1.location", "exp.1.b0", "exp.1.b1", "exp.1.tech",
    "exp.2", "exp.2.b0", "exp.2.tech",
    "edu.0",
    "proj.0", "proj.1",
    "skill.python", "skill.sql", "skill.fastapi", "skill.postgresql", "skill.docker",
    "skill.git", "skill.c-plus-plus", "skill.c-sharp", "skill.node-js",
    "std.notice_period", "std.current_ctc", "std.expected_ctc",
    "std.work_authorization_india", "std.requires_sponsorship", "std.willing_to_relocate",
    "std.how_did_you_hear", "std.gender", "std.pronouns", "std.veteran_status",
    "std.disability_status", "std.ethnicity", "std.auto_consent_recruiting_privacy",
    "std.why_this_role_template", "std.about_me_short", "std.biggest_project",
]  # fmt: skip


def test_fact_ids_are_stable_across_two_runs(profile: Profile) -> None:
    first = [f.id for f in profile.facts(TODAY)]
    second = [f.id for f in profile.facts(TODAY)]
    assert first == second


def test_fact_ids_match_the_golden_list(profile: Profile) -> None:
    assert [f.id for f in profile.facts(TODAY)] == EXPECTED_IDS


def test_fact_ids_are_unique(profile: Profile) -> None:
    ids = [f.id for f in profile.facts(TODAY)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    ("fact_id", "text"),
    [
        ("name", "Name: Asha Testcandidate"),
        ("email", "Email: asha.testcandidate@example.com"),
        ("phone", "Phone: +919000012345"),
        ("location", "Current location: Pune, India"),
        ("link.github", "GitHub: https://github.com/asha-testcandidate"),
        ("years", "Total professional experience: 3.8 years"),
        ("exp.0", "Software Engineer at Northwind Labs, 2024-01-15 to present"),
        ("exp.1", "Junior Developer at Contoso Apps, 2023-02-01 to 2024-03-31"),
        ("exp.0.location", "Location of Software Engineer role: Pune"),
        ("exp.0.b1", "Reduced nightly report generation time from 40 minutes to 12 minutes."),
        ("exp.0.tech", "Technologies used at Northwind Labs: Python, FastAPI, PostgreSQL, Docker"),
        (
            "edu.0",
            "B.Tech in Computer Science from Test Institute of Technology (2023) grade 8.1 CGPA",
        ),
        (
            "proj.0",
            "Project Expense Tracker: A command-line tool that categorises bank statements. "
            "Technologies: Python, SQLite.",
        ),
        ("proj.1", "Project Chess Clock: A small web app with a configurable chess clock."),
        ("skill.c-plus-plus", "Skill: C++"),
        ("std.notice_period", "notice_period: 30 days"),
    ],
)
def test_fact_texts(profile: Profile, fact_id: str, text: str) -> None:
    assert profile.facts_by_id(TODAY)[fact_id].text == text


def test_missing_optional_values_produce_no_facts(profile: Profile) -> None:
    ids = {f.id for f in profile.facts(TODAY)}
    assert "link.portfolio" not in ids  # portfolio_url is null
    assert "exp.2.location" not in ids  # the freelance job has no location
    blank = profile.model_copy(
        update={"summary": "   ", "standard_answers": {"notice_period": " "}}
    )
    blank_ids = {f.id for f in blank.facts(TODAY)}
    assert "summary" not in blank_ids
    assert "std.notice_period" not in blank_ids


def test_no_years_fact_without_experience(profile: Profile) -> None:
    fresher = _with_experience(profile)
    assert "years" not in {f.id for f in fresher.facts(TODAY)}


# ----------------------------------------------------------------------------- skill ids


def test_skill_ids_are_safe_slugs(profile: Profile) -> None:
    ids = {f.id for f in profile.facts(TODAY)}
    assert {"skill.c-plus-plus", "skill.c-sharp", "skill.node-js"} <= ids
    assert "skill.c" not in ids  # C++ and C# must never collapse into plain "c"


def test_repeated_skills_appear_once_and_clashing_slugs_get_a_suffix(profile: Profile) -> None:
    messy = profile.model_copy(update={"skills": ["Python", "python", " PYTHON ", "C", "c!"]})
    skill_ids = [f.id for f in messy.facts(TODAY) if f.id.startswith("skill.")]
    assert skill_ids == ["skill.python", "skill.c", "skill.c-2"]


def test_facts_by_id_returns_the_same_facts(profile: Profile) -> None:
    by_id = profile.facts_by_id(TODAY)
    assert list(by_id) == EXPECTED_IDS
    assert by_id["skill.python"].text == "Skill: Python"


# ------------------------------------------------------------------- years of experience


def test_overlapping_jobs_are_not_double_counted(profile: Profile) -> None:
    assert profile.years_experience(TODAY) == pytest.approx(1387 / 365.25)
    assert profile.years_experience(TODAY) < 4.0  # the naive sum would be 4.59


def test_a_current_job_counts_up_to_today(profile: Profile) -> None:
    only = _with_experience(profile, _exp(date(2026, 1, 1), None))
    assert only.years_experience(TODAY) == pytest.approx(322 / 365.25)


def test_future_dates_are_clipped_or_ignored(profile: Profile) -> None:
    clipped = _with_experience(profile, _exp(date(2026, 1, 1), date(2027, 6, 1)))
    assert clipped.years_experience(TODAY) == pytest.approx(322 / 365.25)
    future = _with_experience(profile, _exp(date(2027, 1, 1), None))
    assert future.years_experience(TODAY) == 0.0


def test_jobs_sharing_a_boundary_day_are_merged(profile: Profile) -> None:
    touching = _with_experience(
        profile,
        _exp(date(2020, 1, 1), date(2020, 6, 30)),
        _exp(date(2020, 6, 30), date(2020, 12, 31)),
    )
    assert touching.years_experience(TODAY) == pytest.approx(365 / 365.25)


def test_a_gap_between_jobs_is_not_counted(profile: Profile) -> None:
    gap = _with_experience(
        profile,
        _exp(date(2020, 1, 1), date(2020, 1, 31)),
        _exp(date(2020, 3, 1), date(2020, 3, 31)),
    )
    assert gap.years_experience(TODAY) == pytest.approx((30 + 30) / 365.25)


def test_no_experience_means_zero_years(profile: Profile) -> None:
    assert _with_experience(profile).years_experience(TODAY) == 0.0


# ----------------------------------------------------------------------------- validators


@pytest.mark.parametrize("phone", ["+919876543210", "+14155552671", " +919000012345 "])
def test_valid_phone_numbers_are_accepted_and_stripped(phone: str) -> None:
    p = Profile.model_validate({**_data(), "phone": phone})
    assert p.phone == phone.strip()


@pytest.mark.parametrize(
    "phone",
    ["9876543210", "+91 98765 43210", "+91-9876543210", "919876543210", "+0123456789", "", "+91"],
)
def test_invalid_phone_numbers_are_rejected(phone: str) -> None:
    with pytest.raises(ValidationError, match="E.164"):
        Profile.model_validate({**_data(), "phone": phone})


def test_end_date_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="before start"):
        _exp(date(2024, 5, 1), date(2024, 4, 1))


def test_duplicate_or_malformed_resume_keys_are_rejected() -> None:
    resumes = [
        {"key": "backend", "path": "a.pdf", "best_for": []},
        {"key": "backend", "path": "b.pdf", "best_for": []},
    ]
    with pytest.raises(ValidationError, match="unique"):
        Profile.model_validate({**_data(), "resumes": resumes})
    with pytest.raises(ValidationError):
        ResumeVariant(key="Backend Resume", path="a.pdf")


@pytest.mark.parametrize(
    ("field", "value"),
    [("email", "not-an-email"), ("first_name", ""), ("last_name", ""), ("current_location", "")],
)
def test_required_fields_are_validated(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({**_data(), field: value})


# --------------------------------------------------------------------------- match summary


def test_match_summary_is_name_free_and_lists_recent_titles(profile: Profile) -> None:
    summary = profile.match_summary(TODAY)
    assert "Asha" not in summary
    assert "Testcandidate" not in summary
    assert "example.com" not in summary
    lines = summary.splitlines()
    assert lines[0] == "3.8 years of professional experience"
    assert lines[1] == "Recent titles: Software Engineer; Junior Developer"
    assert lines[2].startswith("Skills: Python, SQL, FastAPI")
    assert lines[3] == "Education: B.Tech Computer Science"


def test_match_summary_keeps_only_the_first_15_skills(profile: Profile) -> None:
    many = profile.model_copy(update={"skills": [f"S{i}" for i in range(20)]})
    skills_line = next(x for x in many.match_summary(TODAY).splitlines() if x.startswith("Skills"))
    assert "S14" in skills_line
    assert "S15" not in skills_line


def test_match_summary_for_a_fresher(profile: Profile) -> None:
    fresher = _with_experience(profile)
    assert fresher.match_summary(TODAY).splitlines()[0] == "No professional experience yet"


def test_resume_variant_lookup(profile: Profile) -> None:
    assert profile.resume_variant("fullstack") is not None
    assert profile.resume_variant("nope") is None
    assert profile.default_resume == profile.resumes[0]
    assert _with_experience(profile).model_copy(update={"resumes": []}).default_resume is None


# ----------------------------------------------------------------------------- persistence


def test_save_and_load_round_trip(profile: Profile) -> None:
    path = save_profile(profile)
    assert path == profile_path()
    assert path.parent.is_dir()
    assert load_profile() == profile
    assert list(path.parent.glob("*.tmp")) == []  # no temp file is left behind


def test_saving_again_replaces_the_file(profile: Profile) -> None:
    save_profile(profile)
    changed = profile.model_copy(update={"current_location": "Hyderabad, India"})
    save_profile(changed)
    assert load_profile().current_location == "Hyderabad, India"


def test_a_failed_save_leaves_the_original_intact(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_profile(profile)
    before = profile_path().read_text(encoding="utf-8")

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(schema.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        save_profile(profile.model_copy(update={"current_location": "Elsewhere"}))
    assert profile_path().read_text(encoding="utf-8") == before


def test_loading_a_missing_profile_says_how_to_fix_it() -> None:
    assert not profile_path().exists()
    with pytest.raises(FileNotFoundError, match="jobpilot profile init"):
        load_profile()


def test_profile_lives_under_the_data_dir() -> None:
    assert profile_path() == get_settings().jobpilot_data_dir / "profile" / "profile.json"