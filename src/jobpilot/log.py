"""Structured JSON logging with PII and secret redaction (plan Sections 13.1 and 13.2).

Redaction runs in the formatter, as the last step before rendering, so it also covers
log records emitted by third-party libraries (httpx, playwright, telegram), not just ours.
"""

import logging
import logging.handlers
import re
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

from .settings import get_settings

REDACTED = "[REDACTED]"
_HANDLER_TAG = "_jobpilot_handler"

EventDict = MutableMapping[str, Any]

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_PHONE = re.compile(
    r"(?<![\w.])(?:"
    r"\+\d{1,3}[\s-]?\d[\d\s-]{7,12}\d"  # international, e.g. +91 98765 43210
    r"|[6-9]\d{4}[\s-]?\d{5}"  # Indian mobile: 10 digits starting 6-9
    r")(?!\w)"
)

_CTC = re.compile(
    r"(?:(?<!\w)(?:rs\.?|inr|₹)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:lpa|lakhs?|lacs?|k|cr|crores?))?"
    r"|(?<![\w.])\d+(?:\.\d+)?\s?(?:lpa|lakhs?|lacs?)(?!\w))",
    re.IGNORECASE,
)

_TELEGRAM_TOKEN = re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{20,}")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")

# Order matters: emails first (they can contain digits), then secrets, CTC, phones.
_PATTERNS = (_EMAIL, _TELEGRAM_TOKEN, _API_KEY, _CTC, _PHONE)

_SENSITIVE_EXACT = frozenset(
    {
        "email",
        "phone",
        "ctc",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
    }
)
_SENSITIVE_SUFFIXES = (
    "_email",
    "_phone",
    "_ctc",
    "_password",
    "_secret",
    "_token",
    "_api_key",
)

_SCALARS = (str, int, float, bool, type(None))


def redact_text(text: str) -> str:
    """Replace emails, phone numbers, CTC amounts and credentials inside free text."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in _SENSITIVE_EXACT or k.endswith(_SENSITIVE_SUFFIXES)


def redact_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact a log value. A sensitive key hides its whole value.

    Objects that are not plain JSON types are converted with str() and then redacted,
    because the JSON renderer would otherwise fall back to repr() and could leak.
    """
    if key is not None and _is_sensitive_key(key) and value is not None:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v) for v in value]
    if isinstance(value, _SCALARS):
        return value
    return redact_text(str(value))


def redact_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact every key and value of the event."""
    return {k: redact_value(v, k) for k, v in event_dict.items()}


def configure_logging(
    level: int = logging.INFO, *, to_file: bool = True, to_console: bool = True
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # keeps reconfiguration (and tests) predictable
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,  # records from other libraries get the same fields
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_processor,  # last step before rendering: nothing escapes it
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    for old in list(root.handlers):  # drop handlers from an earlier call
        if getattr(old, _HANDLER_TAG, False):
            root.removeHandler(old)
            old.close()

    handlers: list[logging.Handler] = []
    if to_console:
        handlers.append(logging.StreamHandler(sys.stderr))
    if to_file:
        log_dir = get_settings().jobpilot_data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.TimedRotatingFileHandler(
                log_dir / "jobpilot.log", when="midnight", backupCount=14, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(level)

    # httpx logs full request URLs at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A structured logger. Usage: log = get_logger(__name__); log.info("event", key=value)."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def bind_run(**context: Any) -> None:
    """Attach context (application_id, job_id, node) to every log line until clear_run()."""
    structlog.contextvars.bind_contextvars(**context)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()