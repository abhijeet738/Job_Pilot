"""The `jobpilot` command line (plan Section 11.1).

Commands for later phases (scout, match, apply, bot, daemon, report) are added as their
modules are written. Errors the user can fix show one red line and exit 1, not a traceback.
"""

import asyncio
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .core.enums import JobStatus
from .core.errors import JobPilotError
from .db import repo
from .db.session import init_db
from .llm import client
from .log import configure_logging
from .profile import answer_bank
from .profile.onboarding import RichPrompter, onboard, render_profile_summary
from .profile.resume_parser import ParsedResume, extract_resume_text, suspect_values
from .profile.schema import load_profile, profile_path, save_profile
from .settings import get_settings

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(name="jobpilot", help="Your personal job application assistant.",
                  no_args_is_help=True, add_completion=False)  # fmt: skip
profile_app = typer.Typer(help="Your profile.", no_args_is_help=True)
companies_app = typer.Typer(help="Watched companies.", no_args_is_help=True)
jobs_app = typer.Typer(help="Discovered jobs.", no_args_is_help=True)
answers_app = typer.Typer(help="The answer bank.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
app.add_typer(companies_app, name="companies")
app.add_typer(jobs_app, name="jobs")
app.add_typer(answers_app, name="answers")

CONFIG_FILES = ("preferences.yaml", "companies.yaml", "models.yaml", "field_aliases.yaml")
_JOB_OWNED = frozenset(
    {JobStatus.DISCOVERED, JobStatus.REJECTED_FILTER, JobStatus.REJECTED_FIT, JobStatus.QUEUED}
)


@app.callback()
def _main() -> None:
    configure_logging(to_console=False)  # logs go to data/logs/, not over Rich's output


@contextmanager
def _friendly_errors() -> Iterator[None]:
    """Show fixable problems as one red line and exit 1."""
    try:
        yield
    except (JobPilotError, FileNotFoundError, ValueError) as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from e


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]Error:[/] {message}")
    return typer.Exit(1)


# ------------------------------------------------------------------------------- init


@app.command()
def init() -> None:
    """Create the data folders and database, and check the configuration."""
    with _friendly_errors():
        s = get_settings()  # creates data/ and its subfolders
        init_db()
        console.print(f"Data folder: {s.jobpilot_data_dir.resolve()}")
        console.print("Database: ready")

        env, example = Path(".env"), Path(".env.example")
        if env.exists():
            console.print(".env: found")
        elif example.exists():
            shutil.copy2(example, env)
            console.print("[yellow].env created from .env.example. Fill in your keys.[/]")
        else:
            console.print("[yellow].env: not found and no .env.example to copy[/]")

        missing = [n for n in CONFIG_FILES if not (s.config_dir / n).is_file()]
        if missing:
            raise _fail(f"missing config files in {s.config_dir}: {', '.join(missing)}")
        prefs = s.preferences  # validates preferences.yaml
        s.load_companies()  # validates companies.yaml
        console.print(f"Configuration: valid ({len(prefs.target_roles)} target roles)")

        if s.jobpilot_dry_run:
            console.print("Mode: [green]DRY RUN[/] (nothing is ever submitted)")
        else:
            console.print("Mode: [bold red]LIVE[/] (applications can be submitted)")


# ----------------------------------------------------------------------------- profile


@profile_app.command("init")
def profile_init(
    resume: Annotated[Path, typer.Option("--resume", help="Your resume as a text PDF.")],
) -> None:
    """Parse your resume, confirm every section and save your profile."""
    with _friendly_errors():
        init_db()
        text = extract_resume_text(resume)  # rejects scans and bad files before any model call
        existing = load_profile() if profile_path().exists() else None
        if existing is not None:
            console.print("Existing profile found: questions you already answered are skipped.")

        console.print("Reading your resume with the AI model...")
        parsed = asyncio.run(
            client.ask("resume_parse", "resume_parse", ParsedResume, resume_text=text)
        )
        suspects = suspect_values(parsed, text)
        profile = onboard(
            parsed, suspects, RichPrompter(console), resume_pdf=resume, existing=existing
        )
        if profile is None:
            console.print("Nothing saved.")
            return
        path = profile_path()
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.bak"))  # keep the previous profile
        console.print(f"[green]Saved {save_profile(profile)}[/]")


@profile_app.command("show")
def profile_show(
    facts: Annotated[bool, typer.Option("--facts", help="List every citable fact id.")] = False,
) -> None:
    """Print your profile and standard answers."""
    with _friendly_errors():
        profile = load_profile()
        console.print(render_profile_summary(profile))

        answers = Table(title="Standard answers", show_lines=True)
        answers.add_column("Key")
        answers.add_column("Answer")
        for key, value in profile.standard_answers.items():
            answers.add_row(key, value)
        console.print(answers)

        if facts:
            table = Table(title="Facts (the ids answers can cite)")
            table.add_column("Id")
            table.add_column("Text")
            for fact in profile.facts():
                table.add_row(fact.id, fact.text)
            console.print(table)


# --------------------------------------------------------------------------- companies


@companies_app.command("sync")
def companies_sync() -> None:
    """Load config/companies.yaml into the database."""
    with _friendly_errors():
        init_db()
        configs = get_settings().load_companies()
        known = {(c.ats, c.slug) for c in repo.list_companies(active_only=False)}
        added = updated = 0
        for c in configs:
            if (c.ats.value, c.slug) in known:
                updated += 1
            else:
                added += 1
            repo.upsert_company(
                name=c.name, ats=c.ats, slug=c.slug, careers_url=c.careers_url,
                priority=c.priority,
            )  # fmt: skip
        console.print(f"{len(configs)} companies in the file: {added} added, {updated} updated.")
        inactive = [c.name for c in repo.list_companies(active_only=False) if not c.active]
        if inactive:
            console.print(f"[yellow]Inactive (feed was missing twice): {', '.join(inactive)}[/]")


# -------------------------------------------------------------------------------- jobs


@jobs_app.command("list")
def jobs_list(
    status: Annotated[JobStatus | None, typer.Option("--status", help="Filter by status.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
) -> None:
    """List jobs. Application statuses (SUBMITTED, FAILED...) list applications' jobs."""
    with _friendly_errors():
        init_db()
        if status is None or status in _JOB_OWNED:
            rows = [(job, job.status) for job in repo.list_jobs(status, limit)]
        else:
            rows = []
            for app_row in repo.list_applications(status, limit):
                job = repo.get_job(app_row.job_id)
                if job is not None:
                    rows.append((job, app_row.status))

        names: dict[int, str] = {}  # one lookup per company, not one per job
        table = Table(title=f"Jobs ({len(rows)})")
        for column in ("Id", "Company", "Title", "Location", "Score", "Status"):
            table.add_column(column)
        for job, job_status in rows:
            if job.company_id not in names:
                company = repo.get_company(job.company_id)
                names[job.company_id] = company.name if company else "?"
            score = "-" if job.match_score is None else str(job.match_score)
            table.add_row(str(job.id), names[job.company_id], job.title, job.location or "-",
                          score, job_status)  # fmt: skip
        console.print(table)


# ----------------------------------------------------------------------------- answers


@answers_app.command("list")
def answers_list(
    search: Annotated[str | None, typer.Option("--search", help="Filter by question.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=2000)] = 200,
) -> None:
    """Show the stored answers."""
    with _friendly_errors():
        init_db()
        table = Table(title="Answer bank", show_lines=True)
        for column in ("Id", "Question", "Answer", "Key", "Used"):
            table.add_column(column)
        for entry in repo.list_answers(search, limit):
            table.add_row(str(entry.id), entry.question_raw, entry.answer,
                          entry.canonical_key or "-", str(entry.times_used))  # fmt: skip
        console.print(table)


@answers_app.command("edit")
def answers_edit(
    key: Annotated[str, typer.Argument(help="Canonical key, e.g. notice_period")],
) -> None:
    """Change the newest stored answer for a canonical key."""
    with _friendly_errors():
        init_db()
        entry = repo.find_answer_by_key(key)
        if entry is None:
            known = ", ".join(answer_bank.canonical_keys())
            raise _fail(f"no stored answer for '{key}'. Keys: {known}")
        console.print(f"Question: {entry.question_raw}")
        new = typer.prompt("New answer", default=entry.answer).strip()
        if not new:
            raise _fail("an answer cannot be empty")
        answer_bank.save(entry.question_raw, new, entry.field_type, canonical_key=key)
        console.print(f"[green]Updated:[/] {entry.answer!r} -> {new!r}")


@answers_app.command("delete")
def answers_delete(
    answer_id: Annotated[int, typer.Argument(help="Id from `answers list`")],
) -> None:
    """Delete one stored answer."""
    with _friendly_errors():
        init_db()
        entry = repo.get_answer(answer_id)
        if entry is None:
            raise _fail(f"no answer with id {answer_id}")
        if typer.confirm(f"Delete the answer to {entry.question_raw!r}?", default=False):
            repo.delete_answer(answer_id)
            console.print("Deleted.")