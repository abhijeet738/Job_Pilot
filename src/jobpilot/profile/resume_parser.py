"""Resume PDF -> ParsedResume (plan Section 9.1.1).

Nothing parsed here is trusted until the user confirms it (onboarding.py).
Pre-flight checks run BEFORE the model is called, so a bad file costs nothing.
"""

import asyncio
import re
import unicodedata
from pathlib import Path
from typing import Any

import pdfplumber
from pydantic import BaseModel, EmailStr, Field, HttpUrl

from ..core.errors import ResumeError
from ..core.text import normalize
from ..llm import client
from .schema import Education, Experience, Profile, Project

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 15
MIN_CHARS = 200  # below this the PDF is almost certainly a scan with no text layer
MAX_CHARS = 30_000

_BLANK_RUNS = re.compile(r"\n{3,}")


class ParsedResume(BaseModel):
    """What a resume can contain. Contact fields are optional: the user fills any gaps."""

    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None  # validated as E.164 when the Profile is built
    current_location: str | None = None
    linkedin_url: HttpUrl | None = None
    github_url: HttpUrl | None = None
    portfolio_url: HttpUrl | None = None
    summary: str = ""
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- extraction


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")  # folds ligatures too
    lines = [line.rstrip() for line in text.splitlines()]
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def extract_resume_text(path: Path | str) -> str:
    """Read a resume PDF into clean text. Raises ResumeError with a fix for every failure."""
    path = Path(path)
    if not path.is_file():
        raise ResumeError(f"resume not found: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise ResumeError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ResumeError(f"{path.name} is not a PDF file")

    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ResumeError(f"{path.name} has more than {MAX_PAGES} pages: not a resume?")
            pages = [page.extract_text() or "" for page in pdf.pages]
    except ResumeError:
        raise
    except Exception as e:  # pdfplumber/pdfminer raise many types (encrypted, corrupt, ...)
        raise ResumeError(f"could not read {path.name}: {e}") from e

    text = _clean("\n".join(pages))
    if len(text) < MIN_CHARS:
        raise ResumeError(
            f"{path.name} has almost no text ({len(text)} characters). It looks like a "
            "scanned image. Export the resume as a text PDF from Word or Google Docs."
        )
    if len(text) > MAX_CHARS:
        raise ResumeError(f"{path.name} has too much text ({len(text)} characters) for a resume")
    return text


# ------------------------------------------------------------------------------- parsing


async def parse_resume(path: Path | str) -> ParsedResume:
    """Extract text, then ask the strong model to structure it. Does not save anything."""
    text = await asyncio.to_thread(extract_resume_text, path)
    return await client.ask("resume_parse", "resume_parse", ParsedResume, resume_text=text)


def suspect_values(parsed: ParsedResume, text: str) -> list[str]:
    """Values the model returned that do not appear in the resume text.

    The prompt forbids inventing anything, so a hit means a likely hallucination or a
    formatting difference. The confirmation step shows these first. Dates and bullets are
    not checked (they are reformatted by design).
    """
    haystack = normalize(text)
    digits = re.sub(r"\D", "", text)
    out: list[str] = []

    def check(label: str, value: str | None) -> None:
        if value and normalize(value) not in haystack:
            out.append(f"{label} {value!r} is not in the resume text")

    check("email", str(parsed.email) if parsed.email else None)
    if parsed.phone:
        phone_digits = re.sub(r"\D", "", parsed.phone)[-10:]
        if phone_digits and phone_digits not in digits:
            out.append(f"phone {parsed.phone!r} is not in the resume text")
    for job in parsed.experience:
        check("employer", job.company)
    for ed in parsed.education:
        check("institution", ed.institution)
    for skill in parsed.skills:
        check("skill", skill)
    return out


# ------------------------------------------------------------------------------ building


def to_profile(parsed: ParsedResume, **confirmed: Any) -> Profile:
    """Build a Profile from a confirmed parse plus the user's own values.

    `confirmed` overrides parsed values and supplies what a resume cannot contain
    (resumes, standard_answers) or what was missing (for example phone).
    Raises pydantic.ValidationError if a required field is still missing or invalid:
    the caller asks the user for exactly those fields.
    """
    data = parsed.model_dump(mode="json", exclude_none=True)
    data.update({k: v for k, v in confirmed.items() if v is not None})
    return Profile.model_validate(data)