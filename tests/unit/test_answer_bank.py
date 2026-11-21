"""T1.7: answer bank lookup (exact -> key -> fuzzy -> None) and option safety.

Uses a real temporary database and the real config/field_aliases.yaml.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from jobpilot.core.enums import FieldType
from jobpilot.core.models import FormField
from jobpilot.db import repo
from jobpilot.profile import answer_bank
from jobpilot.profile.answer_bank import (
    canonical_key_for,
    canonical_keys,
    lookup,
    match_option,
    save,
)
from jobpilot.settings import get_settings

pytestmark = pytest.mark.usefixtures("tmp_db")

TEXT = FieldType.TEXT


def _field(
    label: str = "Notice period",
    type_: FieldType = TEXT,
    options: list[str] | None = None,
    max_length: int | None = None,
) -> FormField:
    return FormField(
        field_id="f0",
        selector="#f0",
        type=type_,
        label=label,
        options=options or [],
        max_length=max_length,
    )


def _lookup(question: str, **kwargs: Any) -> answer_bank.AnswerHit | None:
    return lookup(question, _field(question, **kwargs))


def _use_alias_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    """Point settings at a temporary config dir holding only field_aliases.yaml."""
    path = tmp_path / "field_aliases.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    return path


# --------------------------------------------------------------------- the three steps


def test_exact_match_ignores_case_asterisks_and_spacing() -> None:
    save("Notice period", "30 days", TEXT)
    hit = _lookup("  NOTICE   Period *")
    assert hit is not None
    assert (hit.value, hit.matched_by) == ("30 days", "exact")


def test_canonical_key_match_shares_one_answer_across_wordings() -> None:
    save("Notice period", "30 days", TEXT)
    hit = _lookup("When can you join?")
    assert hit is not None
    assert (hit.value, hit.matched_by) == ("30 days", "key")
    assert hit.entry.canonical_key == "notice_period"


def test_key_match_returns_the_newest_answer_for_the_key() -> None:
    save("Notice period", "30 days", TEXT)
    save("When can you start", "45 days", TEXT)  # same key, newer
    hit = _lookup("Earliest start date")
    assert hit is not None
    assert hit.value == "45 days"


def test_exact_beats_the_newest_key_match() -> None:
    save("Notice period", "30 days", TEXT)
    save("Earliest start", "45 days", TEXT)  # newest for the key
    hit = _lookup("Notice period")
    assert hit is not None
    assert (hit.value, hit.matched_by) == ("30 days", "exact")


def test_fuzzy_match_accepts_a_near_identical_question() -> None:
    save("What is your favourite programming language", "Python", TEXT)
    hit = _lookup("What is your favorite programming language?")
    assert hit is not None
    assert (hit.value, hit.matched_by) == ("Python", "fuzzy")


def test_fuzzy_match_rejects_a_question_below_the_threshold() -> None:
    save("What is your favourite programming language", "Python", TEXT)
    assert _lookup("What is your favourite database") is None


def test_nothing_stored_means_ask_the_human() -> None:
    assert _lookup("Anything at all") is None
    save("Notice period", "30 days", TEXT)
    assert _lookup("Favourite colour") is None


@pytest.mark.parametrize("question", ["", "   ", " * ", "?"])
def test_an_empty_question_finds_nothing(question: str) -> None:
    save("Notice period", "30 days", TEXT)
    assert lookup(question, _field(question)) is None


# --------------------------------------------------------------------- fuzzy-match traps


def test_a_different_number_in_the_question_never_matches() -> None:
    save("Do you have 3 years of Java experience", "Yes", TEXT)
    assert _lookup("Do you have 5 years of Java experience") is None
    hit = _lookup("do you have 3 years of java experience?")
    assert hit is not None
    assert hit.matched_by == "exact"


def test_an_added_negation_in_the_question_never_matches() -> None:
    # token_set_ratio scores 100 when one question's words are a subset of the other's.
    save("Do you enjoy working with legacy code", "Yes", TEXT)
    assert _lookup("Do you not enjoy working with legacy code") is None


@pytest.mark.parametrize(
    ("answer", "options"),
    [
        ("Within 30 days", ["Within 60 days"]),  # 93% similar, different number
        ("Yes, willing to relocate", ["Yes, not willing to relocate"]),  # 92% similar, negated
        ("Yes", ["No"]),
        ("Immediate", ["30 days", "60 days"]),
    ],
)
def test_match_option_refuses_lookalike_options(answer: str, options: list[str]) -> None:
    assert match_option(answer, options) is None


# -------------------------------------------------------------------------- option safety


def test_a_stored_answer_not_among_the_options_is_refused() -> None:
    save("Notice period", "30 days", TEXT)
    field = _field(
        "Notice period",
        FieldType.SELECT,
        ["Immediate", "Within 30 days", "Within 60 days"],
    )
    assert lookup("Notice period", field) is None  # asked, never guessed


def test_a_stored_answer_among_the_options_is_used() -> None:
    save("Notice period", "30 days", TEXT)
    field = _field("Notice period", FieldType.SELECT, ["Immediate", "30 days", "60 days"])
    hit = lookup("Notice period", field)
    assert hit is not None
    assert hit.value == "30 days"


def test_the_options_own_text_is_returned_not_the_stored_wording() -> None:
    save("Willing to relocate", "yes", FieldType.RADIO)
    field = _field("Willing to relocate", FieldType.RADIO, ["Yes", "No"])
    hit = lookup("Willing to relocate", field)
    assert hit is not None
    assert hit.value == "Yes"


@pytest.mark.parametrize("type_", [FieldType.SELECT, FieldType.COMBOBOX, FieldType.RADIO])
def test_every_choice_type_checks_the_options(type_: FieldType) -> None:
    save("Willing to relocate", "Yes", FieldType.RADIO)
    assert (
        lookup("Willing to relocate", _field("Willing to relocate", type_, ["No", "Maybe"]))
        is None
    )
    hit = lookup("Willing to relocate", _field("Willing to relocate", type_, ["Yes", "No"]))
    assert hit is not None
    assert hit.value == "Yes"


def test_a_near_identical_option_is_accepted() -> None:
    assert match_option("Within 30 days", ["Immediate", "Within 30 days."]) == "Within 30 days."


def test_exact_entry_that_does_not_fit_falls_through_to_the_key_step() -> None:
    save("Notice period", "30 days", TEXT)
    save("Joining time", "Within 30 days", TEXT, canonical_key="notice_period")  # newer
    field = _field("Notice period", FieldType.SELECT, ["Immediate", "Within 30 days"])
    hit = lookup("Notice period", field)
    assert hit is not None
    assert (hit.value, hit.matched_by) == ("Within 30 days", "key")


def test_a_fuzzy_hit_is_also_checked_against_the_options() -> None:
    save("What is your favourite programming language", "Python", TEXT)
    field = _field("What is your favorite programming language", FieldType.SELECT, ["Java", "Go"])
    assert lookup("What is your favorite programming language", field) is None


def test_match_option_edge_cases() -> None:
    assert match_option("30 days", ["Immediate", "30 days", "60 days"]) == "30 days"
    assert match_option("yes", ["Yes", "YES"]) == "Yes"  # duplicates: the first one wins
    assert match_option("x", ["", "  "]) is None
    assert match_option("x", []) is None
    assert match_option("  ", ["Yes"]) is None


# --------------------------------------------------------------------------- checkbox groups


def test_checkbox_group_round_trip() -> None:
    entry = save("Which cloud platforms do you use", ["AWS", "GCP"], FieldType.CHECKBOX_GROUP)
    assert entry is not None
    assert json.loads(entry.answer) == ["AWS", "GCP"]
    field = _field(
        "Which cloud platforms do you use",
        FieldType.CHECKBOX_GROUP,
        ["AWS", "Azure", "GCP"],
    )
    hit = lookup("Which cloud platforms do you use", field)
    assert hit is not None
    assert hit.value == ["AWS", "GCP"]


def test_checkbox_group_is_refused_if_any_choice_is_missing() -> None:
    save("Which cloud platforms do you use", ["AWS", "GCP"], FieldType.CHECKBOX_GROUP)
    field = _field("Which cloud platforms do you use", FieldType.CHECKBOX_GROUP, ["AWS", "Azure"])
    assert lookup("Which cloud platforms do you use", field) is None


def test_a_plain_text_answer_works_as_a_one_item_group() -> None:
    save("Cloud platforms used", "AWS", FieldType.CHECKBOX_GROUP)
    hit = lookup(
        "Cloud platforms used",
        _field("Cloud platforms used", FieldType.CHECKBOX_GROUP, ["AWS", "GCP"]),
    )
    assert hit is not None
    assert hit.value == ["AWS"]


# ------------------------------------------------------------------------ length and type


@pytest.mark.parametrize(("max_length", "usable"), [(20, False), (50, True), (None, True)])
def test_max_length_is_respected_and_never_truncated(max_length: int | None, usable: bool) -> None:
    save("Tell me about your hobbies", "x" * 50, FieldType.TEXTAREA)
    hit = _lookup("Tell me about your hobbies", type_=FieldType.TEXTAREA, max_length=max_length)
    assert (hit is not None) is usable
    if hit is not None:
        assert hit.value == "x" * 50


def test_file_fields_never_come_from_the_bank() -> None:
    save("Resume", "resume.pdf", FieldType.FILE)
    assert lookup("Resume", _field("Resume", FieldType.FILE)) is None


# ------------------------------------------------------------------------- usage counting


def test_a_hit_counts_as_a_use_unless_bump_is_off() -> None:
    entry = save("Notice period", "30 days", TEXT)
    assert entry is not None
    assert entry.id is not None
    lookup("Notice period", _field(), bump=False)
    assert repo.get_answer(entry.id).times_used == 0  # type: ignore[union-attr]
    lookup("Notice period", _field())
    lookup("Notice period", _field())
    assert repo.get_answer(entry.id).times_used == 2  # type: ignore[union-attr]


def test_a_miss_is_not_counted() -> None:
    entry = save("Notice period", "30 days", TEXT)
    assert entry is not None
    assert entry.id is not None
    _lookup("Favourite colour")
    assert repo.get_answer(entry.id).times_used == 0  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------ save


def test_saving_again_replaces_the_answer() -> None:
    save("Notice period", "30 days", TEXT)
    save("notice period?", "60 days", TEXT)
    assert len(repo.list_answers()) == 1
    hit = _lookup("Notice period")
    assert hit is not None
    assert hit.value == "60 days"


def test_save_derives_the_canonical_key_and_an_explicit_key_wins() -> None:
    derived = save("Mobile number", "+919000012345", TEXT)
    assert derived is not None
    assert derived.canonical_key == "phone"
    explicit = save("Joining time", "30 days", TEXT, canonical_key="notice_period")
    assert explicit is not None
    assert explicit.canonical_key == "notice_period"
    unknown = save("Favourite colour", "Blue", TEXT)
    assert unknown is not None
    assert unknown.canonical_key is None


def test_save_strips_whitespace_and_accepts_a_field_type_enum_or_string() -> None:
    entry = save("Notice period", "  30 days  ", "text")
    assert entry is not None
    assert entry.answer == "30 days"
    assert entry.field_type == "text"


@pytest.mark.parametrize(
    ("question", "answer"),
    [("", "x"), ("  *  ", "x"), ("Notice period", ""), ("Notice period", "   "), ("Cloud", [])],
)
def test_save_rejects_empty_input(question: str, answer: str | list[str]) -> None:
    with pytest.raises(ValueError, match="empty"):
        save(question, answer, TEXT)
    assert repo.list_answers() == []


def test_save_rejects_an_unknown_field_type() -> None:
    with pytest.raises(ValueError):
        save("Notice period", "30 days", "bogus")


# --------------------------------------------------------------- job-specific answers


@pytest.mark.parametrize("key", [None, "why_this_role"])
def test_why_this_role_is_never_stored(key: str | None) -> None:
    assert save("Why do you want to work here?", "I admire Acme.", TEXT, canonical_key=key) is None
    assert repo.list_answers() == []


def test_a_job_specific_key_is_never_reused_by_key() -> None:
    repo.save_answer(
        question_raw="Why Acme?",
        answer="Because Acme",
        field_type="text",
        canonical_key="why_this_role",
    )
    assert _lookup("Why do you want this role?") is None


# ----------------------------------------------------------------- canonical key mapping


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("First Name *", "first_name"),
        ("Last name", "last_name"),
        ("Your name", "full_name"),
        ("Name", "full_name"),
        ("Email address", "email"),
        ("Mobile number", "phone"),
        ("Contact number", "phone"),
        ("LinkedIn profile", "linkedin_url"),
        ("GitHub", "github_url"),
        ("Portfolio website", "portfolio_url"),
        ("Website", "portfolio_url"),
        ("Please attach your resume or CV", "resume"),
        ("Cover letter", "cover_letter"),
        ("Current location", "current_location"),
        ("Where are you based?", "current_location"),
        ("Current employer", "current_company"),
        ("Current designation", "current_title"),
        ("Notice period", "notice_period"),
        ("When can you join?", "notice_period"),
        ("Earliest start date", "notice_period"),
        ("Current CTC", "current_ctc"),
        ("Expected CTC (INR)", "expected_ctc"),
        ("Years of experience", "years_experience"),
        ("Total experience", "years_experience"),
        ("Are you authorized to work in India?", "work_authorization_india"),
        ("Do you require visa sponsorship?", "requires_sponsorship"),
        ("Are you open to relocating?", "willing_to_relocate"),
        ("How did you hear about us?", "how_did_you_hear"),
        ("Source", "how_did_you_hear"),
        ("Gender", "gender"),
        ("Preferred pronouns", "pronouns"),
        ("Are you a veteran?", "veteran_status"),
        ("Do you have a disability?", "disability_status"),
        ("Race / Ethnicity", "ethnicity"),
        ("I agree to the privacy policy", "auto_consent_recruiting_privacy"),
        ("Why do you want to work here?", "why_this_role"),
        ("Tell us about yourself", "about_me_short"),
        ("Describe a project you're proud of", "biggest_project"),
    ],
)
def test_real_labels_map_to_the_right_key(label: str, key: str) -> None:
    assert canonical_key_for(label) == key


@pytest.mark.parametrize(
    "label", ["Favourite colour", "Resource planning", "Advisable steps", "", "  *  "]
)
def test_unrelated_labels_map_to_nothing(label: str) -> None:
    # Word boundaries prevent labels such as "resource" from matching "source".
    assert canonical_key_for(label) is None


def test_canonical_keys_cover_the_plan_appendix() -> None:
    keys = canonical_keys()
    assert keys[0] == "first_name"
    appendix = {
        "first_name", "last_name", "full_name", "email", "phone", "linkedin_url", "github_url",
        "portfolio_url", "resume", "cover_letter", "current_location", "current_company",
        "current_title", "years_experience", "notice_period", "current_ctc", "expected_ctc",
        "work_authorization_india", "requires_sponsorship", "willing_to_relocate",
        "how_did_you_hear", "gender", "pronouns", "veteran_status", "disability_status",
        "ethnicity", "auto_consent_recruiting_privacy", "why_this_role", "about_me_short",
        "biggest_project",
    }  # fmt: skip
    # earliest_start_date is in the appendix but is handled by notice_period's patterns.
    assert appendix <= set(keys)
    assert len(keys) == len(set(keys))


# ------------------------------------------------------------------------- the alias file


def test_the_first_matching_key_in_the_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_alias_file(tmp_path, monkeypatch, "a: ['^x']\nb: ['^x']\n")
    assert canonical_key_for("x marks the spot") == "a"


def test_edits_to_the_alias_file_apply_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _use_alias_file(tmp_path, monkeypatch, "foo: ['^foo']\n")
    assert canonical_key_for("Foo bar") == "foo"
    path.write_text("bar: ['^foo']\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert canonical_key_for("Foo bar") == "bar"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("k: ['(unclosed']\n", "bad regex under 'k'"),
        ("k: '^x'\n", "must be a list"),
        ("k: [1, 2]\n", "must be a list"),
        ("- a\n- b\n", "must be a mapping"),
    ],
)
def test_a_broken_alias_file_gives_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, message: str
) -> None:
    _use_alias_file(tmp_path, monkeypatch, content)
    with pytest.raises(ValueError, match=message):
        canonical_key_for("anything")


def test_a_missing_alias_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))  # an empty folder
    get_settings.cache_clear()
    with pytest.raises(FileNotFoundError):
        canonical_key_for("anything")