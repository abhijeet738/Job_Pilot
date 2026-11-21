"""T1.6: resume PDF -> ParsedResume. PDFs are built in code with reportlab; no model is called."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from jobpilot.core.errors import ResumeError
from jobpilot.profile import resume_parser
from jobpilot.profile.resume_parser import (
    ParsedResume,
    _clean,
    extract_resume_text,
    parse_resume,
    suspect_values,
    to_profile,
)
from jobpilot.profile.schema import Education, Experience, Profile, ResumeVariant

RESUME_LINES = [
    "Asha Testcandidate",
    "asha.testcandidate@example.com | +91 90000 12345 | Pune, India",
    "SUMMARY",
    "Backend engineer who builds Python APIs and data pipelines.",
    "EXPERIENCE",
    "Software Engineer, Northwind Labs (Jan 2024 - Present)",
    "- Built REST APIs in FastAPI serving internal analytics dashboards.",
    "- Reduced nightly report generation time from 40 minutes to 12 minutes.",
    "Junior Developer, Contoso Apps (Feb 2023 - Mar 2024)",
    "- Maintained a Django web application used by 2,000 customers.",
    "EDUCATION",
    "B.Tech Computer Science, Test Institute of Technology, 2023",
    "SKILLS",
    "Python, SQL, FastAPI, PostgreSQL, Docker, Git",
]


# ------------------------------------------------------------------------------- helpers


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Write a PDF with the given lines of text per page."""
    c = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(50, y, line)
            y -= 16
        c.showPage()
    c.save()
    return path


@pytest.fixture
def resume_pdf(tmp_path: Path) -> Path:
    return make_pdf(tmp_path / "resume.pdf", [RESUME_LINES])


def _parsed(**overrides: Any) -> ParsedResume:
    base: dict[str, Any] = {
        "first_name": "Asha",
        "last_name": "Testcandidate",
        "email": "asha.testcandidate@example.com",
        "phone": "+919000012345",
        "current_location": "Pune, India",
        "summary": "Backend engineer who builds Python APIs and data pipelines.",
        "experience": [
            Experience(company="Northwind Labs", title="Software Engineer", start=date(2024, 1, 1)),
            Experience(
                company="Contoso Apps",
                title="Junior Developer",
                start=date(2023, 2, 1),
                end=date(2024, 3, 1),
            ),
        ],
        "education": [
            Education(institution="Test Institute of Technology", degree="B.Tech", end_year=2023)
        ],
        "skills": ["Python", "SQL", "FastAPI"],
    }
    return ParsedResume.model_validate({**base, **overrides})


# ----------------------------------------------------------------------------- cleaning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ﬁnance ﬂow", "finance flow"),  # ligatures from PDF fonts
        ("a\x00b", "ab"),
        ("one\n\n\n\ntwo", "one\n\ntwo"),
        ("  x  \n y ", "x\n y"),
        ("", ""),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert _clean(raw) == expected


# --------------------------------------------------------------------------- extraction


def test_extract_returns_the_resume_text(resume_pdf: Path) -> None:
    text = extract_resume_text(resume_pdf)
    assert "Northwind Labs" in text
    assert "asha.testcandidate@example.com" in text
    assert "Test Institute of Technology" in text
    assert "\n\n\n" not in text


def test_extract_accepts_a_string_path(resume_pdf: Path) -> None:
    assert "Contoso Apps" in extract_resume_text(str(resume_pdf))


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ResumeError, match="not found"):
        extract_resume_text(tmp_path / "nope.pdf")


def test_a_text_file_named_pdf_is_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "resume.pdf"
    fake.write_text("This is plain text, not a PDF.", encoding="utf-8")
    with pytest.raises(ResumeError, match="not a PDF"):
        extract_resume_text(fake)


def test_a_corrupt_pdf_is_rejected(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\n" + b"\x00garbage that is not a real pdf structure" * 20)
    with pytest.raises(ResumeError, match=r"could not read|almost no text"):
        extract_resume_text(corrupt)


def test_an_oversized_file_is_rejected(resume_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume_parser, "MAX_BYTES", 100)
    with pytest.raises(ResumeError, match="larger than"):
        extract_resume_text(resume_pdf)


def test_too_many_pages_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "long.pdf", [RESUME_LINES, RESUME_LINES, RESUME_LINES])
    monkeypatch.setattr(resume_parser, "MAX_PAGES", 2)
    with pytest.raises(ResumeError, match="more than 2 pages"):
        extract_resume_text(pdf)


def test_a_scanned_pdf_with_no_text_layer_is_rejected(tmp_path: Path) -> None:
    scan = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(scan), pagesize=A4)
    c.rect(50, 50, 400, 600, fill=1)  # drawn content only, no text
    c.showPage()
    c.save()
    with pytest.raises(ResumeError, match="scanned image"):
        extract_resume_text(scan)


def test_too_much_text_is_rejected(resume_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume_parser, "MAX_CHARS", 250)
    with pytest.raises(ResumeError, match="too much text"):
        extract_resume_text(resume_pdf)


# ------------------------------------------------------------------------------ parsing


async def test_parse_resume_sends_the_extracted_text_to_the_strong_model(
    resume_pdf: Path, fake_llm: Any
) -> None:
    expected = _parsed()
    fake_llm.set("resume_parse", expected)
    result = await parse_resume(resume_pdf)
    assert result == expected
    (call,) = fake_llm.calls_for("resume_parse")
    assert call.agent == "resume_parse"
    assert "Northwind Labs" in call.variables["resume_text"]
    assert "Northwind Labs" in call.user


@pytest.mark.parametrize("problem", ["missing", "text_file", "scan"])
async def test_a_bad_file_never_reaches_the_model(
    problem: str, tmp_path: Path, fake_llm: Any
) -> None:
    path = tmp_path / "bad.pdf"
    if problem == "text_file":
        path.write_text("not a pdf", encoding="utf-8")
    elif problem == "scan":
        c = canvas.Canvas(str(path), pagesize=A4)
        c.rect(50, 50, 300, 300, fill=1)
        c.showPage()
        c.save()
    with pytest.raises(ResumeError):
        await parse_resume(path)
    assert fake_llm.calls == []


def test_an_empty_parse_is_valid() -> None:
    parsed = ParsedResume()
    assert parsed.experience == []
    assert parsed.email is None


# ----------------------------------------------------------------------- suspect values


def test_a_faithful_parse_has_no_suspect_values(resume_pdf: Path) -> None:
    assert suspect_values(_parsed(), extract_resume_text(resume_pdf)) == []


def test_matching_ignores_case_spacing_and_line_breaks() -> None:
    text = "NORTHWIND\nLABS   is great. Skills: python"
    parsed = _parsed(
        email=None,
        phone=None,
        experience=[Experience(company="Northwind Labs", title="Dev", start=date(2024, 1, 1))],
        education=[],
        skills=["Python"],
    )
    assert suspect_values(parsed, text) == []


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"email": "someone.else@example.com"}, "email 'someone.else@example.com'"),
        ({"phone": "+919111122222"}, "phone '+919111122222'"),
        (
            {"experience": [Experience(company="Initech", title="Dev", start=date(2020, 1, 1))]},
            "employer 'Initech'",
        ),
        (
            {"education": [Education(institution="Hogwarts", degree="B.Tech")]},
            "institution 'Hogwarts'",
        ),
        ({"skills": ["Python", "Kubernetes"]}, "skill 'Kubernetes'"),
    ],
)
def test_invented_values_are_flagged(
    resume_pdf: Path, overrides: dict[str, Any], expected_fragment: str
) -> None:
    warnings = suspect_values(_parsed(**overrides), extract_resume_text(resume_pdf))
    assert len(warnings) == 1
    assert expected_fragment in warnings[0]
    assert "not in the resume text" in warnings[0]


def test_a_phone_written_with_spaces_still_matches(resume_pdf: Path) -> None:
    # The PDF says "+91 90000 12345"; the model returned E.164.
    assert suspect_values(_parsed(phone="+919000012345"), extract_resume_text(resume_pdf)) == []


def test_several_problems_are_all_reported() -> None:
    parsed = _parsed(email="x@y.com", skills=["Cobol"])
    assert len(suspect_values(parsed, "Northwind Labs Contoso Apps Test Institute of Technology")) >= 3


# ------------------------------------------------------------------------------ to_profile


def test_to_profile_builds_a_valid_profile() -> None:
    profile = to_profile(
        _parsed(),
        resumes=[ResumeVariant(key="backend", path="data/resumes/backend.pdf", best_for=["python"])],
        standard_answers={"notice_period": "30 days"},
    )
    assert isinstance(profile, Profile)
    assert (profile.first_name, profile.phone) == ("Asha", "+919000012345")
    assert profile.experience[0].end is None  # a current role stays current
    assert profile.experience[1].end == date(2024, 3, 1)
    assert profile.standard_answers == {"notice_period": "30 days"}
    assert profile.default_resume is not None
    assert profile.default_resume.key == "backend"


def test_to_profile_names_the_missing_required_field() -> None:
    with pytest.raises(ValidationError, match="phone"):
        to_profile(_parsed(phone=None))
    with pytest.raises(ValidationError, match="first_name"):
        to_profile(_parsed(first_name=None))


def test_confirmed_values_fill_gaps_and_override_the_parse() -> None:
    profile = to_profile(_parsed(phone=None), phone="+919888877777", current_location="Hyderabad")
    assert profile.phone == "+919888877777"
    assert profile.current_location == "Hyderabad"


def test_none_never_wipes_a_parsed_value() -> None:
    profile = to_profile(_parsed(), phone=None, current_location=None)
    assert profile.phone == "+919000012345"
    assert profile.current_location == "Pune, India"


def test_to_profile_rejects_a_badly_formatted_phone() -> None:
    with pytest.raises(ValidationError, match="E.164"):
        to_profile(_parsed(), phone="98765 43210")