"""T1.6: onboarding questionnaire, driven by a scripted Prompter (no terminal needed)."""

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from jobpilot.profile import onboarding
from jobpilot.profile.answer_bank import canonical_keys
from jobpilot.profile.onboarding import (
    DECLINE,
    SECTIONS,
    STANDARD_QUESTIONS,
    RichPrompter,
    apply_section_json,
    clean_contact,
    collect_contact,
    missing_questions,
    normalize_phone,
    onboard,
    parse_keywords,
    register_resume,
    render_profile_summary,
    render_section,
    section_json,
    validate_answer,
)
from jobpilot.profile.resume_parser import ParsedResume
from jobpilot.profile.schema import Profile

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profile.json"
_MISSING = object()
QUESTIONS = {q.key: q for q in STANDARD_QUESTIONS}


# ---------------------------------------------------------------------- scripted prompter


class ScriptedPrompter:
    """Answers prompts from rules keyed by a substring of the prompt text.

    Each rule is a queue: responses are used in order and the last one repeats. A blank
    reply returns the prompt's default (like Rich). An unmatched ask is a blank reply;
    an unmatched confirm returns its default. More than 300 prompts means an endless loop.
    """

    def __init__(
        self,
        asks: dict[str, list[str]] | None = None,
        confirms: dict[str, list[bool]] | None = None,
        edits: list[str | None] | None = None,
    ) -> None:
        self._asks = {k: list(v) for k, v in (asks or {}).items()}
        self._confirms = {k: list(v) for k, v in (confirms or {}).items()}
        self._edits = list(edits or [])
        self.asked: list[str] = []
        self.confirmed: list[str] = []
        self.said: list[str] = []
        self.shown: list[object] = []
        self.edit_inputs: list[str] = []

    @staticmethod
    def _next(rules: dict[str, list[Any]], prompt: str) -> Any:
        for key, queue in rules.items():
            if key in prompt:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return _MISSING

    def _guard(self) -> None:
        if len(self.asked) + len(self.confirmed) > 300:
            raise AssertionError("more than 300 prompts: the flow is probably looping forever")

    @property
    def text(self) -> str:
        return "\n".join(self.said)

    def show(self, renderable: object) -> None:
        self.shown.append(renderable)

    def say(self, text: str) -> None:
        self.said.append(text)

    def ask(self, prompt: str, default: str | None = None) -> str:
        self.asked.append(prompt)
        self._guard()
        response = self._next(self._asks, prompt)
        if response is _MISSING:
            response = ""
        return default if (response == "" and default is not None) else response  # type: ignore[no-any-return]

    def confirm(self, prompt: str, default: bool = True) -> bool:
        self.confirmed.append(prompt)
        self._guard()
        response = self._next(self._confirms, prompt)
        return default if response is _MISSING else bool(response)

    def edit_json(self, text: str) -> str | None:
        self.edit_inputs.append(text)
        if not self._edits:
            raise AssertionError("the editor was opened but no edit was scripted")
        return self._edits.pop(0) if len(self._edits) > 1 else self._edits[0]


# ----------------------------------------------------------------------------- helpers

BASE_ASKS = {
    "Notice period": ["30 days"],
    "Current CTC": ["9 LPA"],
    "Expected CTC": ["14 LPA"],
    "authorized to work": ["y"],
    "visa sponsorship": ["n"],
    "willing to relocate": ["yes"],
}
EXPECTED_BASE_ANSWERS = {
    "notice_period": "30 days",
    "current_ctc": "9 LPA",
    "expected_ctc": "14 LPA",
    "work_authorization_india": "Yes",
    "requires_sponsorship": "No",
    "willing_to_relocate": "Yes",
    "how_did_you_hear": "Company website",
    "gender": DECLINE,
    "pronouns": DECLINE,
    "veteran_status": DECLINE,
    "disability_status": DECLINE,
    "ethnicity": DECLINE,
    "auto_consent_recruiting_privacy": "No",
}  # the three optional reusable texts were skipped, so they are absent


def _parsed(**overrides: Any) -> ParsedResume:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.update(overrides)
    return ParsedResume.model_validate(data)  # resumes and standard_answers are ignored


def _existing() -> Profile:
    return Profile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _pdf(tmp_path: Path, name: str = "resume.pdf", body: bytes = b"first") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n" + body)
    return path


# ------------------------------------------------------------------------- the questions


def test_question_keys_are_unique_and_canonical() -> None:
    keys = [q.key for q in STANDARD_QUESTIONS]
    assert len(keys) == len(set(keys))
    canonical = set(canonical_keys())
    assert {k for k in keys if k not in canonical} == {"why_this_role_template"}


@pytest.mark.parametrize("key", [q.key for q in STANDARD_QUESTIONS if q.default is not None])
def test_every_default_is_a_valid_answer(key: str) -> None:
    q = QUESTIONS[key]
    assert validate_answer(q, "") == q.default


def test_the_fixture_profile_answers_every_question() -> None:
    assert missing_questions(_existing().standard_answers) == []


def test_missing_questions_skips_answered_and_blank_ones() -> None:
    left = missing_questions({"notice_period": "30 days", "current_ctc": "   "})
    keys = {q.key for q in left}
    assert "notice_period" not in keys
    assert "current_ctc" in keys  # a blank answer counts as unanswered
    assert len(left) == len(STANDARD_QUESTIONS) - 1


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("notice_period", "  30 days ", "30 days"),
        ("how_did_you_hear", "", "Company website"),
        ("gender", "1", "Male"),
        ("gender", "4", DECLINE),
        ("gender", "prefer NOT to say", DECLINE),
        ("gender", "", DECLINE),
        ("work_authorization_india", "y", "Yes"),
        ("work_authorization_india", "YES", "Yes"),
        ("work_authorization_india", "true", "Yes"),
        ("requires_sponsorship", "No", "No"),
        ("requires_sponsorship", "n", "No"),
        ("auto_consent_recruiting_privacy", "", "No"),
        ("expected_ctc", "14 LPA", "14 LPA"),
        ("about_me_short", "", ""),
        ("about_me_short", "x" * 1200, "x" * 1200),
    ],
)
def test_valid_answers_are_cleaned(key: str, raw: str, expected: str) -> None:
    assert validate_answer(QUESTIONS[key], raw) == expected


@pytest.mark.parametrize(
    ("key", "raw", "message"),
    [
        ("notice_period", "", "required"),
        ("notice_period", "   ", "required"),
        ("gender", "5", "choose one of"),
        ("gender", "0", "choose one of"),
        ("gender", "other", "choose one of"),
        ("work_authorization_india", "maybe", "yes or no"),
        ("work_authorization_india", "", "required"),
        ("expected_ctc", "Fourteen lakh", "include a number"),
        ("current_ctc", "negotiable", "include a number"),
        ("about_me_short", "x" * 1201, "too long"),
    ],
)
def test_invalid_answers_say_why(key: str, raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_answer(QUESTIONS[key], raw)


# -------------------------------------------------------------------------------- phones


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91 98765 43210", "+919876543210"),
        ("98765 43210", "+919876543210"),
        ("98765-43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("0091 98765 43210", "+919876543210"),
        ("+1 (415) 555-2671", "+14155552671"),
        (" +919000012345 ", "+919000012345"),
    ],
)
def test_phone_numbers_are_normalized_to_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "1234567890", "abc", "", "+12", "+0123456789", "98765"])
def test_bad_phone_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="phone"):
        normalize_phone(raw)


# ------------------------------------------------------------------------------- contact


def test_clean_contact_keeps_only_valid_values() -> None:
    parsed = _parsed(first_name="  Asha ", phone="123", current_location=None)
    assert clean_contact(parsed) == {
        "first_name": "Asha",
        "last_name": "Testcandidate",
        "email": "asha.testcandidate@example.com",
    }


def test_clean_contact_normalizes_a_local_phone_number() -> None:
    assert clean_contact(_parsed(phone="98765 43210"))["phone"] == "+919876543210"


def test_collect_contact_asks_only_for_what_is_missing() -> None:
    p = ScriptedPrompter(asks={"Last name": ["", "Testcandidate"], "Current city": ["Pune"]})
    values = collect_contact(_parsed(last_name=None, current_location=None), p)
    assert p.asked == ["Last name", "Last name", "Current city and country"]
    assert "required" in p.text  # the blank answer was refused
    assert values["last_name"] == "Testcandidate"
    assert values["current_location"] == "Pune"
    assert values["first_name"] == "Asha"


# -------------------------------------------------------------------------- section edits


def test_section_json_round_trips() -> None:
    parsed = _parsed()
    for section in SECTIONS:
        assert apply_section_json(parsed, section, section_json(parsed, section)) == parsed


def test_the_contact_section_holds_all_eight_contact_fields() -> None:
    assert len(json.loads(section_json(_parsed(), "contact"))) == 8


def test_an_edit_replaces_only_its_own_section_and_never_mutates_the_original() -> None:
    parsed = _parsed()
    before = parsed.model_copy(deep=True)
    edited = apply_section_json(parsed, "skills", json.dumps({"skills": ["Rust"]}))
    assert edited.skills == ["Rust"]
    assert edited.experience == parsed.experience
    assert parsed == before


def test_an_experience_edit_is_validated() -> None:
    parsed = _parsed()
    data = json.loads(section_json(parsed, "experience"))
    data["experience"][0]["title"] = "Staff Engineer"
    assert apply_section_json(parsed, "experience", json.dumps(data)).experience[0].title == (
        "Staff Engineer"
    )
    data["experience"][0]["end"] = "1999-01-01"  # before the start date
    with pytest.raises(ValueError, match="before start"):
        apply_section_json(parsed, "experience", json.dumps(data))


@pytest.mark.parametrize(
    ("section", "text", "message"),
    [
        ("skills", "{nope", "not valid JSON"),
        ("skills", "[1, 2]", "expected a JSON object"),
        ("skills", '{"nope": 1}', "unknown keys"),
        ("summary", '{"skills": []}', "unknown keys"),  # cannot reach another section
        ("skills", '{"skills": "Python"}', "skills"),
    ],
)
def test_bad_edits_are_rejected_with_a_reason(section: str, text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        apply_section_json(_parsed(), section, text)


# --------------------------------------------------------------------------- rendering


def test_every_section_renders_even_for_an_empty_parse() -> None:
    for section in SECTIONS:
        assert isinstance(render_section(section, _parsed()), Table)
        assert isinstance(render_section(section, ParsedResume()), Table)


def test_row_counts_match_the_data() -> None:
    parsed = _parsed()
    assert render_section("contact", parsed).row_count == 8
    assert render_section("experience", parsed).row_count == len(parsed.experience) == 3
    assert render_section("education", parsed).row_count == 1
    assert render_section("projects", parsed).row_count == 2
    assert render_section("skills", parsed).row_count == 1
    assert render_section("experience", ParsedResume()).row_count == 0


def test_the_profile_summary_has_seven_rows() -> None:
    assert render_profile_summary(_existing()).row_count == 7


def test_rich_prompter_writes_to_its_console() -> None:
    buffer = StringIO()
    p = RichPrompter(Console(file=buffer, width=100))
    p.say("hello there")
    p.show(render_section("skills", _parsed()))
    out = buffer.getvalue()
    assert "hello there" in out
    assert "Python" in out


# ---------------------------------------------------------------------------- resumes


def test_parse_keywords() -> None:
    assert parse_keywords("Backend, Python , backend") == ["backend", "python"]
    assert parse_keywords("") == []
    assert parse_keywords(" , ,") == []


def test_register_resume_copies_the_pdf(tmp_path: Path) -> None:
    src = _pdf(tmp_path)
    variant = register_resume(src, "backend", ["backend", "python"])
    assert (variant.key, variant.best_for) == ("backend", ["backend", "python"])
    assert variant.path.endswith("resume_backend.pdf")
    assert Path(variant.path).read_bytes() == src.read_bytes()


def test_registering_a_file_that_is_already_in_place_works(tmp_path: Path) -> None:
    first = register_resume(_pdf(tmp_path), "backend", [])
    again = register_resume(first.path, "backend", [])
    assert again.path == first.path


@pytest.mark.parametrize(
    ("key", "name", "message"),
    [
        ("My Resume", "resume.pdf", "lowercase letters"),
        ("backend", "missing.pdf", "file not found"),
        ("backend", "notes.txt", "not a PDF"),
    ],
)
def test_register_resume_rejects_bad_input(
    tmp_path: Path, key: str, name: str, message: str
) -> None:
    if name == "notes.txt":
        (tmp_path / name).write_text("just text", encoding="utf-8")
    elif name == "resume.pdf":
        _pdf(tmp_path)
    with pytest.raises(ValueError, match=message):
        register_resume(tmp_path / name, key, [])


# ---------------------------------------------------------------------------- full runs


def test_a_clean_run_builds_a_profile(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    p = ScriptedPrompter(asks=BASE_ASKS)
    profile = onboard(_parsed(), [], p, resume_pdf=pdf)

    assert isinstance(profile, Profile)
    assert profile.standard_answers == EXPECTED_BASE_ANSWERS
    assert [r.key for r in profile.resumes] == ["backend"]
    assert profile.resumes[0].best_for == ["backend", "python"]
    assert Path(profile.resumes[0].path).read_bytes() == pdf.read_bytes()
    assert (profile.first_name, profile.phone) == ("Asha", "+919000012345")
    assert len(p.confirmed) == 8  # six sections, 'add another?' and 'save?'
    assert sum(isinstance(x, Table) for x in p.shown) == 7  # six sections plus the summary


def test_suspect_values_are_shown_first(tmp_path: Path) -> None:
    p = ScriptedPrompter(asks=BASE_ASKS)
    onboard(
        _parsed(),
        ["skill 'Kubernetes' is not in the resume text"],
        p,
        resume_pdf=_pdf(tmp_path),
    )
    assert "Check these first" in p.said[0]
    assert "Kubernetes" in p.said[1]


def test_a_bad_edit_is_rejected_and_the_review_reopens(tmp_path: Path) -> None:
    good = json.dumps({"skills": ["Python", "Rust"]})
    p = ScriptedPrompter(
        asks=BASE_ASKS,
        confirms={"skills section": [False, False, True]},
        edits=["{nope", good],
    )
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path))
    assert profile is not None
    assert profile.skills == ["Python", "Rust"]
    assert "Could not use that edit" in p.text
    assert "not valid JSON" in p.text
    assert len(p.edit_inputs) == 2
    assert p.edit_inputs[0] == p.edit_inputs[1]  # the rejected edit did not leak into the next


def test_closing_the_editor_without_saving_changes_nothing(tmp_path: Path) -> None:
    p = ScriptedPrompter(
        asks=BASE_ASKS,
        confirms={"experience section": [False, True]},
        edits=[None],
    )
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path))
    assert profile is not None
    assert profile.experience == Profile.model_validate_json(FIXTURE.read_text("utf-8")).experience
    assert "No changes saved" in p.text


def test_an_edit_to_the_contact_section_takes_effect(tmp_path: Path) -> None:
    contact = json.loads(section_json(_parsed(), "contact"))
    contact["current_location"] = "Hyderabad, India"
    p = ScriptedPrompter(
        asks=BASE_ASKS,
        confirms={"contact section": [False, True]},
        edits=[json.dumps(contact)],
    )
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path))
    assert profile is not None
    assert profile.current_location == "Hyderabad, India"


def test_a_missing_phone_is_asked_for_and_bad_ones_are_refused(tmp_path: Path) -> None:
    asks = {**BASE_ASKS, "Phone number": ["12345", "98765 43210"]}
    p = ScriptedPrompter(asks=asks)
    profile = onboard(_parsed(phone=None), [], p, resume_pdf=_pdf(tmp_path))
    assert profile is not None
    assert profile.phone == "+919876543210"
    assert p.asked.count("Phone number") == 2
    assert "phone" in p.text


def test_rerunning_with_an_existing_profile_asks_nothing(tmp_path: Path) -> None:
    existing = _existing()
    p = ScriptedPrompter()
    profile = onboard(_parsed(), [], p, existing=existing)
    assert profile is not None
    assert p.asked == []  # every answer and resume was already there
    assert profile.standard_answers == existing.standard_answers
    assert profile.resumes == existing.resumes


def test_a_partial_rerun_asks_only_the_missing_questions() -> None:
    existing = _existing().model_copy(update={"standard_answers": {"notice_period": "60 days"}})
    asks = {k: v for k, v in BASE_ASKS.items() if k != "Notice period"}
    p = ScriptedPrompter(asks=asks)
    profile = onboard(_parsed(), [], p, existing=existing)
    assert profile is not None
    assert profile.standard_answers["notice_period"] == "60 days"  # kept, not asked again
    assert not any("Notice period" in q for q in p.asked)
    assert profile.standard_answers["current_ctc"] == "9 LPA"


def test_declining_to_save_returns_none(tmp_path: Path) -> None:
    p = ScriptedPrompter(asks=BASE_ASKS, confirms={"Save this profile?": [False]})
    assert onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path)) is None


def test_a_late_validation_failure_reopens_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = onboarding.to_profile
    calls: list[int] = []

    def flaky(parsed: ParsedResume, **confirmed: Any) -> Profile:
        calls.append(1)
        if len(calls) == 1:
            Profile.model_validate({})  # raises ValidationError, as a real late failure would
        return real(parsed, **confirmed)

    monkeypatch.setattr(onboarding, "to_profile", flaky)
    p = ScriptedPrompter(asks=BASE_ASKS)
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path))
    assert isinstance(profile, Profile)
    assert len(calls) == 2
    assert "not valid yet" in p.text
    assert p.confirmed.count("Is the contact section correct?") == 2
    with pytest.raises(ValidationError):
        Profile.model_validate({})


# ---------------------------------------------------------------------- resume variants


def test_a_second_resume_variant_can_be_added(tmp_path: Path) -> None:
    second = _pdf(tmp_path, "full.pdf", b"second")
    p = ScriptedPrompter(
        asks={
            **BASE_ASKS,
            "Path to the resume PDF": [str(second)],
            "Short name": ["", "fullstack"],  # the first reply takes the default 'backend'
            "Role keywords": ["", "react, frontend"],
        },
        confirms={"Add another resume variant?": [True, False]},
    )
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path, "one.pdf", b"first"))
    assert profile is not None
    assert [r.key for r in profile.resumes] == ["backend", "fullstack"]
    assert profile.resumes[1].best_for == ["react", "frontend"]
    assert profile.default_resume is not None
    assert profile.default_resume.key == "backend"


def test_a_duplicate_variant_name_never_overwrites_the_first_file(tmp_path: Path) -> None:
    second = _pdf(tmp_path, "full.pdf", b"second")
    p = ScriptedPrompter(
        asks={
            **BASE_ASKS,
            "Path to the resume PDF": [str(second)],
            "Short name": ["", "backend", "fullstack"],  # the second reply repeats a name
            "Role keywords": ["", "react"],
        },
        confirms={"Add another resume variant?": [True, True, False]},
    )
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path, "one.pdf", b"first"))
    assert profile is not None
    assert "already used" in p.text
    by_key = {r.key: Path(r.path).read_bytes() for r in profile.resumes}
    assert by_key["backend"].endswith(b"first")  # still the first resume, not overwritten
    assert by_key["fullstack"].endswith(b"second")


def test_a_bad_variant_name_reopens_the_same_pdf(tmp_path: Path) -> None:
    p = ScriptedPrompter(asks={**BASE_ASKS, "Short name": ["My Resume", "backend"]})
    profile = onboard(_parsed(), [], p, resume_pdf=_pdf(tmp_path))
    assert profile is not None
    assert "lowercase letters" in p.text
    assert [r.key for r in profile.resumes] == ["backend"]


def test_without_a_resume_pdf_the_path_is_asked(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path)
    p = ScriptedPrompter(asks={**BASE_ASKS, "Path to the resume PDF": [str(pdf)]})
    profile = onboard(_parsed(), [], p)
    assert profile is not None
    assert "Path to the resume PDF" in p.asked
    assert len(profile.resumes) == 1