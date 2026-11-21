"""The Profile: single source of truth about the candidate (plan Section 7.1).

Every fact has a deterministic id so answers and the Verifier can cite exactly which fact
supports a value. Pure models plus small helpers; no LLM or browser code lives here.
"""

import json
import os
import re
from datetime import date
from pathlib import Path

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from ..settings import get_settings

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_SLUG = re.compile(r"[^a-z0-9]+")
_DAYS_PER_YEAR = 365.25


class Fact(BaseModel):
    id: str  # e.g. "exp.0", "exp.0.b1", "skill.python", "std.notice_period"
    text: str  # human-readable fact, used in prompts


class Experience(BaseModel):
    company: str
    title: str
    start: date
    end: date | None = None  # None = current role
    location: str | None = None
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "Experience":
        if self.end is not None and self.end < self.start:
            raise ValueError(f"{self.title} at {self.company}: end date is before start date")
        return self


class Education(BaseModel):
    institution: str
    degree: str
    field: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    grade: str | None = None


class Project(BaseModel):
    name: str
    description: str
    tech: list[str] = Field(default_factory=list)
    url: HttpUrl | None = None


class ResumeVariant(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$")  # "backend", "fullstack"
    path: str  # data/resumes/resume_backend.pdf
    best_for: list[str] = Field(default_factory=list)  # role keywords this variant suits


class Profile(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: EmailStr
    phone: str  # E.164, e.g. +919876543210
    current_location: str = Field(min_length=1)
    linkedin_url: HttpUrl | None = None
    github_url: HttpUrl | None = None
    portfolio_url: HttpUrl | None = None
    summary: str = ""
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    resumes: list[ResumeVariant] = Field(default_factory=list)  # first one is the default
    standard_answers: dict[str, str] = Field(default_factory=dict)
    # e.g. {"notice_period": "30 days", "expected_ctc": "18 LPA", ...}

    @field_validator("phone")
    @classmethod
    def _phone_is_e164(cls, v: str) -> str:
        v = v.strip()
        if not _E164.match(v):
            raise ValueError("phone must be E.164, e.g. +919876543210 (no spaces or dashes)")
        return v

    @model_validator(mode="after")
    def _resume_keys_unique(self) -> "Profile":
        keys = [r.key for r in self.resumes]
        if len(keys) != len(set(keys)):
            raise ValueError("resume variant keys must be unique")
        return self

    # ------------------------------------------------------------------ experience

    def years_experience(self, today: date | None = None) -> float:
        """Total professional experience in years. Overlapping jobs are not double-counted.

        A current role (end=None) counts up to `today`; a future end date is clipped to it.
        """
        today = today or date.today()
        spans = sorted(
            (e.start, min(e.end or today, today)) for e in self.experience if e.start < today
        )
        total_days = 0
        cur_start: date | None = None
        cur_end: date | None = None
        for start, end in spans:
            if cur_start is None or cur_end is None:
                cur_start, cur_end = start, end
            elif start <= cur_end:  # overlaps or touches the current block
                cur_end = max(cur_end, end)
            else:
                total_days += (cur_end - cur_start).days
                cur_start, cur_end = start, end
        if cur_start is not None and cur_end is not None:
            total_days += (cur_end - cur_start).days
        return total_days / _DAYS_PER_YEAR

    # ----------------------------------------------------------------------- facts

    def facts(self, today: date | None = None) -> list[Fact]:
        """Flatten the profile into citable facts. Ids are deterministic across runs."""
        out: list[Fact] = [
            Fact(id="name", text=f"Name: {self.first_name} {self.last_name}"),
            Fact(id="email", text=f"Email: {self.email}"),
            Fact(id="phone", text=f"Phone: {self.phone}"),
            Fact(id="location", text=f"Current location: {self.current_location}"),
        ]
        for link_id, label, url in (
            ("link.linkedin", "LinkedIn", self.linkedin_url),
            ("link.github", "GitHub", self.github_url),
            ("link.portfolio", "Portfolio", self.portfolio_url),
        ):
            if url is not None:
                out.append(Fact(id=link_id, text=f"{label}: {url}"))
        if self.summary.strip():
            out.append(Fact(id="summary", text=f"Summary: {self.summary.strip()}"))

        years = self.years_experience(today)
        if self.experience:
            out.append(
                Fact(id="years", text=f"Total professional experience: {years:.1f} years")
            )

        for i, e in enumerate(self.experience):
            end = e.end.isoformat() if e.end else "present"
            out.append(
                Fact(id=f"exp.{i}", text=f"{e.title} at {e.company}, {e.start.isoformat()} to {end}")
            )
            if e.location:
                out.append(Fact(id=f"exp.{i}.location", text=f"Location of {e.title} role: {e.location}"))
            for j, bullet in enumerate(e.bullets):
                out.append(Fact(id=f"exp.{i}.b{j}", text=bullet))
            if e.tech:
                out.append(
                    Fact(id=f"exp.{i}.tech", text=f"Technologies used at {e.company}: {', '.join(e.tech)}")
                )

        for i, ed in enumerate(self.education):
            parts = [ed.degree, f"in {ed.field}" if ed.field else "", f"from {ed.institution}"]
            if ed.end_year:
                parts.append(f"({ed.end_year})")
            if ed.grade:
                parts.append(f"grade {ed.grade}")
            out.append(Fact(id=f"edu.{i}", text=" ".join(p for p in parts if p)))

        for i, p in enumerate(self.projects):
            text = f"Project {p.name}: {p.description}"
            if p.tech:
                text += f" Technologies: {', '.join(p.tech)}."
            out.append(Fact(id=f"proj.{i}", text=text))

        used: set[str] = set()
        seen_skills: set[str] = set()
        for skill in self.skills:
            name = skill.strip()
            if not name or name.lower() in seen_skills:
                continue
            seen_skills.add(name.lower())
            out.append(Fact(id=_unique_id("skill." + _skill_slug(name), used), text=f"Skill: {name}"))

        for key, value in self.standard_answers.items():
            if value.strip():
                out.append(Fact(id=f"std.{key}", text=f"{key}: {value}"))
        return out

    def facts_by_id(self, today: date | None = None) -> dict[str, Fact]:
        return {f.id: f for f in self.facts(today)}

    # --------------------------------------------------------------------- helpers

    def match_summary(self, today: date | None = None) -> str:
        """Name-free summary for the Matcher prompt: years, last 2 titles, top 15 skills, degree."""
        years = self.years_experience(today)
        lines = [
            f"{years:.1f} years of professional experience"
            if years > 0
            else "No professional experience yet"
        ]
        recent = sorted(self.experience, key=lambda e: (e.end or date.max, e.start), reverse=True)
        titles = list(dict.fromkeys(e.title for e in recent))[:2]
        if titles:
            lines.append("Recent titles: " + "; ".join(titles))
        if self.skills:
            lines.append("Skills: " + ", ".join(self.skills[:15]))
        if self.education:
            ed = self.education[0]
            lines.append("Education: " + " ".join(p for p in (ed.degree, ed.field or "") if p))
        return "\n".join(lines)

    def resume_variant(self, key: str) -> ResumeVariant | None:
        return next((r for r in self.resumes if r.key == key), None)

    @property
    def default_resume(self) -> ResumeVariant | None:
        return self.resumes[0] if self.resumes else None


# ------------------------------------------------------------------------------ id helpers


def _skill_slug(name: str) -> str:
    """'C++' -> 'c-plus-plus', 'C#' -> 'c-sharp', 'Node.js' -> 'node-js'."""
    s = name.lower().replace("++", " plus plus ").replace("#", " sharp ")
    return _SLUG.sub("-", s).strip("-") or "skill"


def _unique_id(base: str, used: set[str]) -> str:
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------- persistence


def profile_path() -> Path:
    return get_settings().jobpilot_data_dir / "profile" / "profile.json"


def load_profile() -> Profile:
    """Read data/profile/profile.json. Raises FileNotFoundError with the fix if it is missing."""
    path = profile_path()
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: jobpilot profile init --resume FILE")
    return Profile.model_validate_json(path.read_text(encoding="utf-8"))


def save_profile(profile: Profile) -> Path:
    """Write the profile atomically (temp file + rename), so a crash cannot corrupt it."""
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile.model_dump(mode="json"), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path