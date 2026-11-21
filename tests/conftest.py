"""Shared fixtures (plan Section 13.3). Later tasks add: forms_server."""

import ipaddress
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine

from jobpilot.db import session
from jobpilot.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- settings / db


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test runs against a temporary data dir and with dry-run forced on.

    Environment variables override .env, so a developer's real .env cannot leak in.
    """
    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBPILOT_DRY_RUN", "true")
    monkeypatch.setenv("JOBPILOT_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("PROMPTS_DIR", str(REPO_ROOT / "prompts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_db(tmp_path: Path) -> Iterator[Engine]:
    """A fresh SQLite database (WAL, foreign keys on) with every table created."""
    engine = session.make_engine(tmp_path / "test.db")
    session.set_engine(engine)
    session.init_db(engine)
    yield engine
    session.reset_engine()


# -------------------------------------------------------------------------------- network


def _is_local(address: Any) -> bool:
    """True for loopback and unix-socket addresses."""
    if isinstance(address, str | bytes):  # unix socket path
        return True
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname other than localhost


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a non-local socket. Tests marked `live` are exempt.

    This guards Python code only. A Chromium process started by Playwright is not covered,
    so browser tests must navigate to the local forms server and nothing else.
    """
    if request.node.get_closest_marker("live"):
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_local(address):
            raise RuntimeError(f"network access is blocked in tests: {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_local(address):
            raise RuntimeError(f"network access is blocked in tests: {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


# ------------------------------------------------------------------------------- fake LLM


@dataclass
class FakeCall:
    """One recorded call to the fake model."""

    agent: str
    prompt: str
    temperature: float | None
    variables: dict[str, Any]
    system: str
    user: str


@dataclass
class FakeLLM:
    """Stand-in for llm.client.ask. Register responses per prompt name, then inspect calls.

    Responses for one prompt are returned in order; the last one repeats forever.
    An Exception instance is raised instead of returned (to simulate BudgetExceeded etc.).
    """

    calls: list[FakeCall] = field(default_factory=list)
    _queues: dict[str, list[BaseModel | Exception]] = field(default_factory=dict)

    def set(self, prompt: str, *responses: BaseModel | Exception) -> None:
        if not responses:
            raise ValueError("give at least one response")
        self._queues[prompt] = list(responses)

    def calls_for(self, prompt: str) -> list[FakeCall]:
        return [c for c in self.calls if c.prompt == prompt]

    async def ask(
        self,
        agent: str,
        prompt: str,
        response_model: type[BaseModel],
        *,
        temperature: float | None = None,
        **variables: object,
    ) -> BaseModel:
        from jobpilot.llm import client  # lazy: keeps collection light
        from jobpilot.llm.prompts import render

        client.resolve_model(agent)  # a typo in the agent name fails here
        system, user = render(prompt, **variables)  # a bad variable fails here
        self.calls.append(FakeCall(agent, prompt, temperature, dict(variables), system, user))

        queue = self._queues.get(prompt)
        if not queue:
            raise AssertionError(
                f"fake_llm: no response registered for prompt '{prompt}'. "
                f"Call fake_llm.set('{prompt}', <{response_model.__name__} instance>) first."
            )
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, response_model):
            raise AssertionError(
                f"fake_llm: '{prompt}' expects {response_model.__name__}, "
                f"but {type(item).__name__} was registered"
            )
        return item.model_copy(deep=True)  # callers cannot mutate the registered object


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Replace llm.client.ask with a FakeLLM. No real model is ever called.

    Works only for code that calls `client.ask(...)` through the module (see client.py).
    """
    from jobpilot.llm import client

    fake = FakeLLM()
    monkeypatch.setattr(client, "ask", fake.ask)
    return fake