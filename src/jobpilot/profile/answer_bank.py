"""Answer bank: never ask the same question twice (plan Section 9.1.3).

Lookup order: exact question -> canonical key -> fuzzy (>= 92) -> nothing.
For choice fields a stored answer is only used if it matches one of the CURRENT options,
and the option's own text is what comes back, so a wrong option can never be chosen.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz, process

from ..core.enums import FieldType
from ..core.models import FormField
from ..core.text import normalize
from ..db import repo
from ..db.tables import AnswerBankEntry
from ..settings import get_settings

FUZZY_QUESTION_MIN = 92
FUZZY_OPTION_MIN = 90

# Answers to these depend on the company, so they are never stored or reused.
JOB_SPECIFIC_KEYS = frozenset({"why_this_role"})

_CHOICE_TYPES = frozenset({FieldType.SELECT, FieldType.COMBOBOX, FieldType.RADIO})

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z']+")
_NEGATIONS = frozenset(
    {"not", "no", "never", "without", "cannot", "can't", "don't", "doesn't", "won't", "non", "nor"}
)


@dataclass(frozen=True)
class AnswerHit:
    """A usable stored answer. `value` is exactly what to fill into the field."""

    entry: AnswerBankEntry
    value: str | list[str]
    matched_by: str  # "exact" | "key" | "fuzzy"


# ------------------------------------------------------------------------------- aliases


@lru_cache(maxsize=4)
def _compiled_aliases(
    path: str, mtime_ns: int
) -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    """Parse field_aliases.yaml. mtime_ns is part of the cache key, so edits apply at once."""
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("field_aliases.yaml must be a mapping of key -> list of patterns")
    out: list[tuple[str, tuple[re.Pattern[str], ...]]] = []
    for key, patterns in raw.items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"field_aliases.yaml: '{key}' must be a list of regex strings")
        try:
            out.append((str(key), tuple(re.compile(p, re.IGNORECASE) for p in patterns)))
        except re.error as e:
            raise ValueError(f"field_aliases.yaml: bad regex under '{key}': {e}") from e
    return tuple(out)


def _aliases() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    path = get_settings().config_dir / "field_aliases.yaml"
    return _compiled_aliases(str(path.resolve()), path.stat().st_mtime_ns)


def canonical_key_for(label: str) -> str | None:
    """The canonical key for a form label, or None. First matching key in the file wins."""
    normalized = normalize(label)
    if not normalized:
        return None
    for key, patterns in _aliases():
        if any(p.search(normalized) for p in patterns):
            return key
    return None


def canonical_keys() -> list[str]:
    """Every canonical key in field_aliases.yaml, in file order."""
    return [key for key, _ in _aliases()]


# ------------------------------------------------------------------- fuzzy-match guards


def _numbers(text: str) -> list[str]:
    return sorted(_NUMBER.findall(text))


def _negations(text: str) -> list[str]:
    return sorted(set(_WORD.findall(text)) & _NEGATIONS)


def _equivalent(a: str, b: str) -> bool:
    """Fuzzy scores ignore meaning. Two texts only match if their numbers and negations agree."""
    return _numbers(a) == _numbers(b) and _negations(a) == _negations(b)


def match_option(answer: str, options: list[str]) -> str | None:
    """The option that equals `answer` (exact after normalizing, else >= 90 similar), or None.

    Returns the option's own text. Numbers and negations must agree, so "30 days" can never
    match "60 days" and "Yes" can never match "No".
    """
    target = normalize(answer)
    if not target:
        return None
    by_norm: dict[str, str] = {}
    for option in options:
        n = normalize(option)
        if n:
            by_norm.setdefault(n, option)
    if target in by_norm:
        return by_norm[target]
    best = process.extractOne(
        target, list(by_norm), scorer=fuzz.ratio, score_cutoff=FUZZY_OPTION_MIN
    )
    if best is None or not _equivalent(target, best[0]):
        return None
    return by_norm[best[0]]


# --------------------------------------------------------------------------- resolution


def _decode_list(text: str) -> list[str]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return [text]
    return [str(x) for x in loaded] if isinstance(loaded, list) else [text]


def _resolve(entry: AnswerBankEntry | None, field: FormField, matched_by: str) -> AnswerHit | None:
    """Turn a stored entry into a value that is safe for THIS field, or None."""
    if entry is None:
        return None
    if field.type in _CHOICE_TYPES:
        option = match_option(entry.answer, field.options)
        return AnswerHit(entry, option, matched_by) if option else None
    if field.type == FieldType.CHECKBOX_GROUP:
        chosen = [match_option(v, field.options) for v in _decode_list(entry.answer)]
        if not chosen or any(c is None for c in chosen):
            return None  # every stored choice must exist among the current options
        return AnswerHit(entry, [c for c in chosen if c], matched_by)
    if field.max_length is not None and len(entry.answer) > field.max_length:
        return None  # never truncate an answer the user wrote
    return AnswerHit(entry, entry.answer, matched_by)


def _finish(hit: AnswerHit, bump: bool) -> AnswerHit:
    if bump and hit.entry.id is not None:
        repo.bump_answer_use(hit.entry.id)
    return hit


# ------------------------------------------------------------------------------ lookup


def lookup(question: str, field: FormField, *, bump: bool = True) -> AnswerHit | None:
    """Find a stored answer for this question on this field. None means: ask the human."""
    if field.type == FieldType.FILE:
        return None
    qn = normalize(question)
    if not qn:
        return None

    # Step 1 (plan step 2): exact normalized question.
    hit = _resolve(repo.find_answer_exact(qn), field, "exact")
    if hit:
        return _finish(hit, bump)

    # Step 2: canonical key, newest answer for it.
    key = canonical_key_for(question)
    if key and key not in JOB_SPECIFIC_KEYS:
        hit = _resolve(repo.find_answer_by_key(key), field, "key")
        if hit:
            return _finish(hit, bump)

    # Step 3: fuzzy, with the number and negation guards.
    norms = {entry_id: norm for entry_id, norm in repo.answer_norms()}
    if norms:
        candidates = process.extract(
            qn, norms, scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_QUESTION_MIN, limit=5
        )
        for norm, _score, entry_id in candidates:
            if not _equivalent(qn, norm):
                continue
            hit = _resolve(repo.get_answer(entry_id), field, "fuzzy")
            if hit:
                return _finish(hit, bump)
    return None


# -------------------------------------------------------------------------------- save


def save(
    question: str,
    answer: str | list[str],
    field_type: FieldType | str,
    canonical_key: str | None = None,
) -> AnswerBankEntry | None:
    """Store an answer the user gave. Returns None for job-specific keys (never stored).

    The same normalized question updates its entry in place, so re-answering replaces it.
    A checkbox group's list of choices is stored as JSON.
    """
    if not normalize(question):
        raise ValueError("cannot store an answer for an empty question")
    key = canonical_key or canonical_key_for(question)
    if key in JOB_SPECIFIC_KEYS:
        return None
    text = json.dumps(answer) if isinstance(answer, list) else answer.strip()
    if not text or text == "[]":
        raise ValueError("cannot store an empty answer")
    return repo.save_answer(
        question_raw=question,
        answer=text,
        field_type=FieldType(field_type).value,
        canonical_key=key,
    )