"""Prompt rendering: prompts/<name>.md -> (system, user) strings.

File layout: a '## system' heading and a '## user' heading, each on its own line.
The source is split BEFORE rendering, so no variable value (for example a job description
containing '## user') can change where the sections start.
"""

import re
from functools import cache, lru_cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined, Template, TemplateSyntaxError, UndefinedError

from ..core.errors import PromptError
from ..settings import get_settings

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SYSTEM_HEADING = re.compile(r"^##[ \t]+system[ \t]*$", re.MULTILINE)
_USER_HEADING = re.compile(r"^##[ \t]+user[ \t]*$", re.MULTILINE)


@cache
def _env() -> Environment:
    # Prompts are plain text, not HTML, so autoescape must stay off.
    return Environment(
        undefined=StrictUndefined,  # an undefined variable raises instead of rendering ""
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701
    )


def split_sections(source: str, label: str = "prompt") -> tuple[str, str]:
    """Split a prompt file into (system, user) template sources. Raises PromptError."""
    system = _SYSTEM_HEADING.search(source)
    user = _USER_HEADING.search(source)
    if system is None or user is None:
        raise PromptError(f"{label}: needs a '## system' and a '## user' heading")
    if user.start() < system.start():
        raise PromptError(f"{label}: '## system' must come before '## user'")
    if source[: system.start()].strip():
        raise PromptError(f"{label}: text before the '## system' heading")
    if user.start() < system.end():
        raise PromptError(f"{label}: '## system' must come before '## user'")
    system_src = source[system.end() : user.start()].strip()
    user_src = source[user.end() :].strip()
    if not system_src or not user_src:
        raise PromptError(f"{label}: the system and user sections must not be empty")
    return system_src, user_src


@lru_cache(maxsize=64)
def _compile(path: str, mtime_ns: int) -> tuple[Template, Template]:
    """Compile both sections. mtime_ns is part of the cache key, so edits are picked up."""
    file = Path(path)
    system_src, user_src = split_sections(file.read_text(encoding="utf-8"), file.name)
    env = _env()
    try:
        return env.from_string(system_src), env.from_string(user_src)
    except TemplateSyntaxError as e:
        raise PromptError(
            f"{file.name}: template syntax error on line {e.lineno}: {e.message}"
        ) from e


def render(name: str, **variables: object) -> tuple[str, str]:
    """Render prompts/<name>.md and return (system, user).

    Raises PromptError for a bad name, a missing file, a malformed file or an undefined
    variable. Variables used with 'or' defaults (e.g. max_length) must still be passed,
    as None: StrictUndefined refuses to evaluate a missing variable even in a condition.
    """
    if not _NAME.match(name):
        raise PromptError(f"invalid prompt name: {name!r}")
    path = get_settings().prompts_dir / f"{name}.md"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError as e:
        raise PromptError(f"prompt file not found: {path}") from e
    system_template, user_template = _compile(str(path.resolve()), mtime_ns)
    try:
        return (
            system_template.render(**variables).strip(),
            user_template.render(**variables).strip(),
        )
    except UndefinedError as e:
        raise PromptError(f"prompt '{name}': {e.message}") from e


def prompt_names() -> list[str]:
    """Names of every prompt file, used by the 'render every prompt' test."""
    return sorted(p.stem for p in get_settings().prompts_dir.glob("*.md"))