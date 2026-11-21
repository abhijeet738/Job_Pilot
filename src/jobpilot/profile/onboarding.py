"""Interactive profile setup for `jobpilot profile init` (plan Section 9.1.2).

Questions, validation and screens are pure functions. All terminal access goes through the
Prompter protocol, so tests drive the whole flow with scripted answers.
"""

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import typer
from pydantic import EmailStr, TypeAdapter, ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..core.text import normalize
from ..settings import get_settings
from .resume_parser import ParsedResume, to_profile
from .schema import Profile, ResumeVariant

DECLINE = "Prefer not to say"
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_KEY = re.compile(r"^[a-z0-9_]+$")
_EMAIL = TypeAdapter(EmailStr)


# --------------------------------------------------------------------------- questions


@dataclass(frozen=True)
class Question:
    key: str  # the standard_answers key (plan Appendix 17.1)
    prompt: str
    kind: Literal["text", "choice", "yesno", "long"]
    choices: tuple[str, ...] = ()
    default: str | None = None
    required: bool = True
    needs_digit: bool = False
    max_len: int = 1200
    help: str = ""


STANDARD_QUESTIONS: tuple[Question, ...] = (
    Question("notice_period", "Notice period", "text", help='e.g. "30 days" or "Immediate"'),
    Question("current_ctc", "Current CTC", "text", needs_digit=True, help='e.g. "9 LPA"'),
    Question("expected_ctc", "Expected CTC", "text", needs_digit=True, help='e.g. "14 LPA"'),
    Question("work_authorization_india", "Are you authorized to work in India?", "yesno"),
    Question("requires_sponsorship", "Do you require visa sponsorship?", "yesno"),
    Question("willing_to_relocate", "Are you willing to relocate?", "yesno"),
    Question("how_did_you_hear", "How did you hear about jobs (default answer)", "text",
             default="Company website"),
    Question("gender", "Gender (voluntary self-identification)", "choice",
             ("Male", "Female", "Non-binary", DECLINE), default=DECLINE),
    Question("pronouns", "Pronouns (voluntary)", "choice",
             ("He/Him", "She/Her", "They/Them", DECLINE), default=DECLINE),
    Question("veteran_status", "Veteran status (voluntary)", "choice",
             ("Yes", "No", DECLINE), default=DECLINE),
    Question("disability_status", "Disability status (voluntary)", "choice",
             ("Yes", "No", DECLINE), default=DECLINE),
    Question("ethnicity", "Ethnicity (voluntary)", "text", default=DECLINE),
    Question("auto_consent_recruiting_privacy",
             "Tick recruiting privacy / data-processing consent boxes automatically?",
             "yesno", default="No",
             help="Marketing and newsletter boxes are never ticked, whatever you answer"),
    Question("about_me_short", "About you in 2 sentences (reusable)", "long", required=False),
    Question("biggest_project", "Your proudest project in 3 sentences (reusable)", "long",
             required=False),
    Question("why_this_role_template", "Why you want a role like this, 3 to 4 sentences "
             "(reusable)", "long", required=False),
)  # fmt: skip


def _pick(value: str, choices: tuple[str, ...]) -> str:
    if value.isdigit() and 1 <= int(value) <= len(choices):
        return choices[int(value) - 1]
    for choice in choices:
        if normalize(choice) == normalize(value):
            return choice
    raise ValueError("choose one of: " + ", ".join(choices))


def _yes_no(value: str) -> str:
    v = value.strip().lower()
    if v in {"y", "yes", "true"}:
        return "Yes"
    if v in {"n", "no", "false"}:
        return "No"
    raise ValueError("answer yes or no")


def validate_answer(q: Question, raw: str) -> str:
    """The cleaned answer ("" for a skipped optional question). Raises ValueError with why."""
    value = raw.strip()
    if not value:
        if q.default is not None:
            value = q.default
        elif q.required:
            raise ValueError("an answer is required")
        else:
            return ""
    if q.kind == "choice":
        return _pick(value, q.choices)
    if q.kind == "yesno":
        return _yes_no(value)
    if q.kind == "long" and len(value) > q.max_len:
        raise ValueError(f"too long ({len(value)} characters, the limit is {q.max_len})")
    if q.needs_digit and not re.search(r"\d", value):
        raise ValueError("include a number, for example 14 LPA")
    return value


def missing_questions(existing: dict[str, str]) -> list[Question]:
    """Questions not answered yet, so re-running profile init never repeats itself."""
    return [q for q in STANDARD_QUESTIONS if not existing.get(q.key, "").strip()]


# ------------------------------------------------------------------------------ contact

CONTACT_FIELDS = ("first_name", "last_name", "email", "phone", "current_location")


def normalize_phone(raw: str, default_country: str = "+91") -> str:
    """Return E.164. A bare Indian mobile number (10 digits starting 6-9) gets +91."""
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        candidate = "+" + digits
    elif s.startswith("00"):
        candidate = "+" + digits[2:]
    elif len(digits) == 10 and digits[0] in "6789":
        candidate = default_country + digits
    elif len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        candidate = default_country + digits[1:]
    elif len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        candidate = "+" + digits
    else:
        raise ValueError("enter a phone number like +91 98765 43210")
    if not _E164.match(candidate):
        raise ValueError("that does not look like a valid phone number")
    return candidate


def _validate_email(raw: str) -> str:
    try:
        return str(_EMAIL.validate_python(raw.strip()))
    except ValidationError as e:
        raise ValueError("enter a valid email address") from e


def _validate_text(raw: str) -> str:
    if not raw.strip():
        raise ValueError("an answer is required")
    return raw.strip()


_CONTACT_PROMPTS: dict[str, tuple[str, Callable[[str], str]]] = {
    "first_name": ("First name", _validate_text),
    "last_name": ("Last name", _validate_text),
    "email": ("Email address", _validate_email),
    "phone": ("Phone number", normalize_phone),
    "current_location": ("Current city and country", _validate_text),
}


def clean_contact(parsed: ParsedResume) -> dict[str, str]:
    """Contact values that are already valid. Invalid or missing ones are left out (asked later)."""
    out: dict[str, str] = {}
    for key, (_, validate) in _CONTACT_PROMPTS.items():
        raw = getattr(parsed, key)
        if raw:
            try:
                out[key] = validate(str(raw))
            except ValueError:
                continue
    return out


# ----------------------------------------------------------------------------- sections

SECTIONS = ("contact", "summary", "experience", "education", "projects", "skills")
_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "contact": ("first_name", "last_name", "email", "phone", "current_location",
                "linkedin_url", "github_url", "portfolio_url"),
    "summary": ("summary",),
    "experience": ("experience",),
    "education": ("education",),
    "projects": ("projects",),
    "skills": ("skills",),
}  # fmt: skip


def section_json(parsed: ParsedResume, section: str) -> str:
    """One section as editable JSON."""
    data = parsed.model_dump(mode="json")
    return json.dumps({k: data[k] for k in _SECTION_KEYS[section]}, indent=2, ensure_ascii=False)


def apply_section_json(parsed: ParsedResume, section: str, text: str) -> ParsedResume:
    """Return a copy of `parsed` with this section replaced by the edited JSON.

    Raises ValueError (pydantic's ValidationError is one) with a readable reason.
    """
    try:
        edited = json.loads(text)
    except ValueError as e:
        raise ValueError(f"not valid JSON: {e}") from e
    if not isinstance(edited, dict):
        raise ValueError("expected a JSON object")
    unknown = set(edited) - set(_SECTION_KEYS[section])
    if unknown:
        raise ValueError(f"unknown keys for {section}: {sorted(unknown)}")
    data = parsed.model_dump(mode="json")
    data.update(edited)
    return ParsedResume.model_validate(data)


def _cell(value: object) -> str:
    return "-" if value in (None, "", []) else str(value)


def render_section(section: str, parsed: ParsedResume) -> Table:
    """A Rich table showing one section for review."""
    table = Table(title=section.capitalize(), show_lines=True)
    if section == "contact":
        table.add_column("Field")
        table.add_column("Value")
        for key in _SECTION_KEYS["contact"]:
            table.add_row(key, _cell(getattr(parsed, key)))
    elif section == "summary":
        table.add_column("Summary")
        table.add_row(_cell(parsed.summary))
    elif section == "experience":
        for col in ("Role", "Dates", "Bullets", "Tech"):
            table.add_column(col)
        for e in parsed.experience:
            dates = f"{e.start} to {e.end or 'present'}"
            table.add_row(f"{e.title} at {e.company}", dates, "\n".join(e.bullets) or "-",
                          ", ".join(e.tech) or "-")  # fmt: skip
    elif section == "education":
        for col in ("Degree", "Institution", "Years", "Grade"):
            table.add_column(col)
        for ed in parsed.education:
            years = "-".join(str(y) for y in (ed.start_year, ed.end_year) if y) or "-"
            table.add_row(f"{ed.degree} {ed.field or ''}".strip(), ed.institution, years,
                          _cell(ed.grade))  # fmt: skip
    elif section == "projects":
        for col in ("Name", "Description", "Tech", "URL"):
            table.add_column(col)
        for p in parsed.projects:
            table.add_row(p.name, p.description, ", ".join(p.tech) or "-", _cell(p.url))
    else:
        table.add_column("Skills")
        table.add_row(", ".join(parsed.skills) or "-")
    return table


def render_profile_summary(profile: Profile) -> Table:
    """The final screen shown before saving."""
    table = Table(title="Profile to save", show_lines=True)
    table.add_column("Item")
    table.add_column("Value")
    table.add_row("Name", f"{profile.first_name} {profile.last_name}")
    table.add_row("Email / phone", f"{profile.email}  {profile.phone}")
    table.add_row("Location", profile.current_location)
    table.add_row("Experience", f"{profile.years_experience():.1f} years, "
                  f"{len(profile.experience)} roles")  # fmt: skip
    table.add_row("Skills", str(len(profile.skills)))
    table.add_row("Resumes", ", ".join(r.key for r in profile.resumes) or "-")
    table.add_row("Standard answers", str(len(profile.standard_answers)))
    return table


# ----------------------------------------------------------------------------- resumes


def parse_keywords(raw: str) -> list[str]:
    """'Backend, Python , backend' -> ['backend', 'python']."""
    seen: dict[str, None] = {}
    for part in raw.split(","):
        word = part.strip().lower()
        if word:
            seen.setdefault(word)
    return list(seen)


def register_resume(src: Path | str, key: str, best_for: list[str]) -> ResumeVariant:
    """Check the PDF and copy it to data/resumes/resume_<key>.pdf. Raises ValueError."""
    src = Path(src).expanduser()
    if not _KEY.match(key):
        raise ValueError("the key may only contain lowercase letters, digits and underscores")
    if not src.is_file():
        raise ValueError(f"file not found: {src}")
    with src.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError(f"{src.name} is not a PDF file")
    dest_dir = get_settings().jobpilot_data_dir / "resumes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"resume_{key}.pdf"
    if not dest.exists() or src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return ResumeVariant(key=key, path=dest.as_posix(), best_for=best_for)


# ------------------------------------------------------------------------------ prompter


class Prompter(Protocol):
    """All terminal access. Tests supply a scripted implementation."""

    def show(self, renderable: object) -> None: ...
    def say(self, text: str) -> None: ...
    def ask(self, prompt: str, default: str | None = None) -> str: ...
    def confirm(self, prompt: str, default: bool = True) -> bool: ...
    def edit_json(self, text: str) -> str | None:
        """Open `text` in an editor. None means the user closed it without saving."""
        ...


class RichPrompter:
    """The real terminal implementation (Rich + the user's $EDITOR)."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def show(self, renderable: object) -> None:
        self.console.print(renderable)

    def say(self, text: str) -> None:
        self.console.print(text)

    def ask(self, prompt: str, default: str | None = None) -> str:
        if default is None:
            return Prompt.ask(prompt, console=self.console, default="", show_default=False)
        return Prompt.ask(prompt, console=self.console, default=default)

    def confirm(self, prompt: str, default: bool = True) -> bool:
        return Confirm.ask(prompt, console=self.console, default=default)

    def edit_json(self, text: str) -> str | None:
        return typer.edit(text, extension=".json")


# --------------------------------------------------------------------------------- flow


def review_parsed(parsed: ParsedResume, suspects: list[str], p: Prompter) -> ParsedResume:
    """Show every section; the user confirms it or edits it until it is right."""
    if suspects:
        p.say("[bold yellow]Check these first. They were not found in your resume text:[/]")
        for s in suspects:
            p.say(f"  - {s}")
    for section in SECTIONS:
        while True:
            p.show(render_section(section, parsed))
            if p.confirm(f"Is the {section} section correct?"):
                break
            edited = p.edit_json(section_json(parsed, section))
            if edited is None:
                p.say("No changes saved.")
                continue
            try:
                parsed = apply_section_json(parsed, section, edited)
            except ValueError as e:
                p.say(f"[red]Could not use that edit:[/] {e}")
    return parsed


def collect_contact(parsed: ParsedResume, p: Prompter) -> dict[str, str]:
    """Valid contact values from the resume, plus answers for whatever is missing."""
    values = clean_contact(parsed)
    for key in CONTACT_FIELDS:
        if key in values:
            continue
        label, validate = _CONTACT_PROMPTS[key]
        while True:
            try:
                values[key] = validate(p.ask(label))
                break
            except ValueError as e:
                p.say(f"[red]{e}[/]")
    return values


def collect_standard_answers(existing: dict[str, str], p: Prompter) -> dict[str, str]:
    """Ask every question that is not answered yet. Returns existing plus the new answers."""
    answers = dict(existing)
    for q in missing_questions(existing):
        if q.help:
            p.say(f"[dim]{q.help}[/]")
        if q.kind == "choice":
            p.say("  " + "   ".join(f"[{i}] {c}" for i, c in enumerate(q.choices, 1)))
        while True:
            try:
                value = validate_answer(q, p.ask(q.prompt, q.default))
                break
            except ValueError as e:
                p.say(f"[red]{e}[/]")
        if value:
            answers[q.key] = value
    return answers


def collect_resumes(first_pdf: Path | None, p: Prompter) -> list[ResumeVariant]:
    """Register the resume used for parsing, then any other variants. The first is the default."""
    variants: list[ResumeVariant] = []
    pending = first_pdf
    while True:
        if pending is None:
            if variants and not p.confirm("Add another resume variant?", default=False):
                break
            path_raw = p.ask("Path to the resume PDF")
        else:
            path_raw, pending = str(pending), None
        key = p.ask("Short name for this variant (letters, digits, underscore)",
                    "backend" if not variants else None)  # fmt: skip
        keywords = parse_keywords(p.ask("Role keywords it suits, comma-separated", "backend, python"
                                        if not variants else None))  # fmt: skip
        key = key.strip()
        if any(v.key == key for v in variants):  # check BEFORE copying: a copy would overwrite
            p.say("[red]That name is already used.[/]")
            continue
        try:
            variant = register_resume(path_raw, key, keywords)
        except ValueError as e:
            p.say(f"[red]{e}[/]")
            pending = None if variants else first_pdf
            continue
        variants.append(variant)
        p.say(f"Saved {variant.path}")
    return variants


def onboard(
    parsed: ParsedResume,
    suspects: list[str],
    p: Prompter,
    *,
    resume_pdf: Path | None = None,
    existing: Profile | None = None,
) -> Profile | None:
    """Run the whole questionnaire. Returns the Profile, or None if the user declines to save.

    Saving is left to the caller. `existing` keeps its standard answers and resume variants,
    so re-running init only asks for what is missing.
    """
    parsed = review_parsed(parsed, suspects, p)
    contact = collect_contact(parsed, p)
    answers = collect_standard_answers(existing.standard_answers if existing else {}, p)
    resumes = existing.resumes if existing and existing.resumes else collect_resumes(resume_pdf, p)

    while True:
        try:
            profile = to_profile(parsed, **contact, standard_answers=answers, resumes=resumes)
            break
        except ValidationError as e:  # a field the review let through is still invalid
            p.say(f"[red]The profile is not valid yet:[/] {e}")
            parsed = review_parsed(parsed, [], p)
            contact = collect_contact(parsed, p)
    p.show(render_profile_summary(profile))
    return profile if p.confirm("Save this profile?") else None


def _unused(_: Any) -> None:  # keeps `Any` imported for tests that build prompters
    return None