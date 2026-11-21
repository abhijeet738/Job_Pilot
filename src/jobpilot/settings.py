"""Application settings: .env for secrets/runtime flags, YAML for preferences."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from .core.enums import ATS


class Seniority(BaseModel):
    min_years_required_ok: int = 0
    max_years_required_ok: int = 4


class Locations(BaseModel):
    accept: list[str]
    remote_ok: bool = True
    remote_must_allow_india: bool = True


class Salary(BaseModel):
    currency: str = "INR"
    min_annual: int = 0


class Matching(BaseModel):
    fit_threshold: int = Field(default=70, ge=0, le=100)


class Caps(BaseModel):
    per_day: int = Field(default=15, ge=1)
    hard_max_per_day: int = Field(default=20, ge=1)
    per_company_per_30_days: int = Field(default=2, ge=1)


class Autonomy(BaseModel):
    require_approval_until: date
    auto_submit_min_confidence: float = Field(default=0.85, ge=0, le=1)


class ApplyWindow(BaseModel):
    start: str = "09:00"  # HH:MM, local time
    end: str = "22:00"


class Schedule(BaseModel):
    discovery_every_hours: int = Field(default=4, ge=1)
    apply_window: ApplyWindow = Field(default_factory=ApplyWindow)
    apply_every_minutes: int = Field(default=30, ge=1)
    max_jobs_per_apply_run: int = Field(default=3, ge=1)


class Preferences(BaseModel):
    """Mirrors config/preferences.yaml. Unknown keys are rejected to catch typos."""

    model_config = {"extra": "forbid"}

    candidate_name: str
    target_roles: list[str]
    title_exclude_keywords: list[str] = Field(default_factory=list)
    seniority: Seniority = Field(default_factory=Seniority)
    locations: Locations
    salary: Salary = Field(default_factory=Salary)
    matching: Matching = Field(default_factory=Matching)
    caps: Caps = Field(default_factory=Caps)
    autonomy: Autonomy
    schedule: Schedule = Field(default_factory=Schedule)
    blocklist_companies: list[str] = Field(default_factory=list)

class CompanyConfig(BaseModel):
    """One entry of config/companies.yaml. Unknown keys are rejected to catch typos."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    ats: ATS
    slug: str = Field(min_length=1)
    careers_url: str = Field(min_length=1)
    priority: int = Field(default=3, ge=1, le=5)  # 1 = dream company

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None

    telegram_bot_token: str = ""
    telegram_chat_id: int = 0

    mail_imap_host: str = "imap.gmail.com"
    mail_address: str = ""
    mail_app_password: str = ""

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    jobpilot_data_dir: Path = Path("./data")
    jobpilot_timezone: str = "Asia/Kolkata"
    jobpilot_dry_run: bool = True  # stays true until the Phase 4 go-live gate
    jobpilot_headless: bool = False

    config_dir: Path = Path("./config")
    prompts_dir: Path = Path("./prompts")

    def load_yaml(self, name: str) -> dict[str, Any]:
        """Load a YAML file from config_dir. Raises FileNotFoundError if missing."""
        data = yaml.safe_load((self.config_dir / name).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = f"{name} must contain a YAML mapping at the top level"
            raise ValueError(msg)
        return data

    @property
    def preferences(self) -> Preferences:
        """Validated preferences. Re-read on each access so edits apply without a restart."""
        return Preferences.model_validate(self.load_yaml("preferences.yaml"))

    def load_companies(self) -> list[CompanyConfig]:
        """Validated entries of companies.yaml. Raises ValueError listing every bad entry."""
        raw = yaml.safe_load((self.config_dir / "companies.yaml").read_text(encoding="utf-8"))
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError("companies.yaml must be a list of companies")
        companies: list[CompanyConfig] = []
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for i, item in enumerate(raw, start=1):
            try:
                company = CompanyConfig.model_validate(item)
            except ValueError as e:
                problems.append(f"entry {i}: {str(e).splitlines()[0]}")
                continue
            pair = (company.ats.value, company.slug)
            if pair in seen:
                problems.append(f"entry {i}: {company.ats.value}/{company.slug} is listed twice")
                continue
            seen.add(pair)
            companies.append(company)
        if problems:
            raise ValueError("companies.yaml has problems:\n  " + "\n  ".join(problems))
        return companies    


@lru_cache
def get_settings() -> Settings:
    """Cached settings; creates the runtime data folders on first call."""
    s = Settings()
    for sub in ("profile", "resumes", "artifacts", "browser_profile", "logs"):
        (s.jobpilot_data_dir / sub).mkdir(parents=True, exist_ok=True)
    return s