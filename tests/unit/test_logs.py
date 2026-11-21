import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from jobpilot.log import (
    REDACTED,
    bind_run,
    clear_run,
    configure_logging,
    get_logger,
    redact_processor,
    redact_text,
    redact_value,
)
from jobpilot.settings import get_settings

TELEGRAM_URL = "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe"


# -------------------------------------------------------------------------- redact_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Reach me at asha.k@example.com", f"Reach me at {REDACTED}"),
        ("Call +91 98765 43210 now", f"Call {REDACTED} now"),
        ("Call +919876543210 now", f"Call {REDACTED} now"),
        ("Mobile: 9876543210", f"Mobile: {REDACTED}"),
        ("Mobile: 98765-43210", f"Mobile: {REDACTED}"),
        ("Expected 18 LPA", f"Expected {REDACTED}"),
        ("Expected 12.5 lacs", f"Expected {REDACTED}"),
        ("₹18,00,000 per year", f"{REDACTED} per year"),
        ("Rs. 15 lakh", REDACTED),
        ("INR 1800000", REDACTED),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", REDACTED),
    ],
)
def test_redact_text_replaces_sensitive_values(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected


def test_redact_text_handles_several_values_in_one_line() -> None:
    out = redact_text("asha@example.com | +91 98765 43210 | expects 18 LPA")
    assert out == f"{REDACTED} | {REDACTED} | expects {REDACTED}"


def test_telegram_token_is_removed_from_urls() -> None:
    out = redact_text(TELEGRAM_URL)
    assert "123456789:" not in out
    assert "AAHdq" not in out
    assert out.endswith("/getMe")


@pytest.mark.parametrize(
    "safe",
    [
        "Filled 12 fields for application 42 in 3.5 seconds",
        "Applied to Acme for Backend Engineer",
        "2026-11-19T10:00:00Z",
        "3 workers 15 retries",
        "job_id=1234567 status=SUBMITTED",
    ],
)
def test_redact_text_leaves_ordinary_log_text_alone(safe: str) -> None:
    assert redact_text(safe) == safe


# ------------------------------------------------------------------------- redact_value


@pytest.mark.parametrize(
    "key",
    ["email", "phone", "expected_ctc", "current_ctc", "password", "api_key", "Authorization"],
)
def test_sensitive_keys_hide_the_whole_value(key: str) -> None:
    assert redact_value("nothing a regex would catch", key) == REDACTED


@pytest.mark.parametrize("key", ["telegram_bot_token", "anthropic_api_key", "mail_password"])
def test_sensitive_key_suffixes_are_hidden(key: str) -> None:
    assert redact_value("abc", key) == REDACTED


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "notice_period", "job_id"])
def test_non_sensitive_keys_stay_visible(key: str) -> None:
    assert redact_value(100, key) == 100
    assert redact_value("30 days", key) == "30 days"


def test_none_stays_none_even_for_sensitive_keys() -> None:
    assert redact_value(None, "email") is None


def test_nested_structures_are_redacted_recursively() -> None:
    value = {"profile": {"email": "a@b.co", "city": "Pune"}, "notes": ["x@y.com", "ok"]}
    assert redact_value(value) == {
        "profile": {"email": REDACTED, "city": "Pune"},
        "notes": [REDACTED, "ok"],
    }


def test_non_json_objects_are_stringified_then_redacted() -> None:
    class Holder:
        def __str__(self) -> str:
            return "owner asha@example.com"

    assert redact_value(Holder()) == f"owner {REDACTED}"


def test_redact_processor_covers_every_key_of_the_event() -> None:
    event: dict[str, Any] = {"event": "sent to a@b.co", "phone": "123", "attempt": 1}
    assert redact_processor(None, "info", event) == {
        "event": f"sent to {REDACTED}",
        "phone": REDACTED,
        "attempt": 1,
    }


# ----------------------------------------------------------------------- end to end file


@pytest.fixture
def log_file() -> Iterator[Path]:
    """Configure file-only logging into the (temporary) data dir; undo it afterwards."""
    configure_logging(to_console=False)
    yield get_settings().jobpilot_data_dir / "logs" / "jobpilot.log"
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_jobpilot_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.WARNING)
    structlog.reset_defaults()
    clear_run()


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_email_and_phone_are_redacted_in_the_log_file(log_file: Path) -> None:
    get_logger("jobpilot.test").info(
        "sent to asha@example.com", phone="+919876543210", application_id=7
    )
    raw = log_file.read_text(encoding="utf-8")
    assert "asha@example.com" not in raw
    assert "9876543210" not in raw
    (record,) = _records(log_file)
    assert record["event"] == f"sent to {REDACTED}"
    assert record["phone"] == REDACTED
    assert record["application_id"] == 7
    assert record["level"] == "info"
    assert record["logger"] == "jobpilot.test"
    assert record["timestamp"].endswith("Z")


def test_ctc_is_redacted_in_the_log_file(log_file: Path) -> None:
    get_logger("jobpilot.test").info("answered", expected_ctc="18 LPA", note="asked for 20 LPA")
    raw = log_file.read_text(encoding="utf-8")
    assert "LPA" not in raw


def test_third_party_library_logs_are_redacted_too(log_file: Path) -> None:
    logging.getLogger("thirdparty").warning("GET %s", TELEGRAM_URL)
    raw = log_file.read_text(encoding="utf-8")
    assert "123456789:" not in raw
    assert _records(log_file)[0]["logger"] == "thirdparty"


def test_http_clients_are_quiet_at_info_level(log_file: Path) -> None:
    assert logging.getLogger("httpx").level == logging.WARNING
    logging.getLogger("httpx").info("GET %s", TELEGRAM_URL)
    assert not log_file.exists() or log_file.read_text(encoding="utf-8") == ""


def test_tracebacks_are_redacted(log_file: Path) -> None:
    log = get_logger("jobpilot.test")
    try:
        raise ValueError("bad value for asha@example.com")
    except ValueError:
        log.exception("boom")
    raw = log_file.read_text(encoding="utf-8")
    assert "asha@example.com" not in raw
    (record,) = _records(log_file)
    assert "ValueError" in record["exception"]


def test_bind_run_adds_context_until_cleared(log_file: Path) -> None:
    log = get_logger("jobpilot.test")
    bind_run(application_id=5, job_id=9, node="fill")
    log.info("inside")
    clear_run()
    log.info("outside")
    inside, outside = _records(log_file)
    assert (inside["application_id"], inside["job_id"], inside["node"]) == (5, 9, "fill")
    assert "node" not in outside


def test_configuring_twice_does_not_duplicate_lines(log_file: Path) -> None:
    configure_logging(to_console=False)
    get_logger("jobpilot.test").info("once")
    assert len(_records(log_file)) == 1
    tagged = [h for h in logging.getLogger().handlers if getattr(h, "_jobpilot_handler", False)]
    assert len(tagged) == 1