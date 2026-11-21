"""The single entry point for model calls (plan Section 8).

No other module imports litellm. Callers must use `from ..llm import client` and then
`client.ask(...)`, NOT `from ..llm.client import ask`: tests replace `client.ask`
(the fake_llm fixture), and a name imported directly would keep pointing at the real one.
"""

import os
from functools import cache
from typing import Any, TypeVar

import instructor
import litellm
from pydantic import BaseModel

from ..core.errors import BudgetExceeded, LLMError
from ..db import repo
from ..log import get_logger
from ..settings import get_settings
from .prompts import render

T = TypeVar("T", bound=BaseModel)

log = get_logger(__name__)

DEFAULT_TEMPERATURE = 0.0
AGENT_TEMPERATURE: dict[str, float] = {"answer_draft": 0.3}


def _export_keys() -> None:
    """Put keys from .env into the process environment: LiteLLM only reads os.environ.

    A variable that is already set in the real environment is never overwritten.
    """
    s = get_settings()
    pairs = {
        "ANTHROPIC_API_KEY": s.anthropic_api_key,
        "OPENAI_API_KEY": s.openai_api_key,
        "GEMINI_API_KEY": s.gemini_api_key,
    }
    if s.langfuse_public_key and s.langfuse_secret_key:
        pairs.update(
            {
                "LANGFUSE_PUBLIC_KEY": s.langfuse_public_key,
                "LANGFUSE_SECRET_KEY": s.langfuse_secret_key,
                "LANGFUSE_HOST": s.langfuse_host,
            }
        )
    for name, value in pairs.items():
        if value and not os.environ.get(name):
            os.environ[name] = value


@cache
def _client() -> Any:
    """Build the Instructor client on first use (importing this module needs no keys)."""
    _export_keys()
    s = get_settings()
    if s.langfuse_public_key and s.langfuse_secret_key:
        litellm.success_callback = ["langfuse"]
        litellm.failure_callback = ["langfuse"]
    return instructor.from_litellm(litellm.acompletion)


def resolve_model(agent: str, cfg: dict[str, Any] | None = None) -> str:
    """The LiteLLM model string for an agent: agents[agent] -> tier -> tiers[tier]."""
    cfg = cfg or get_settings().load_yaml("models.yaml")
    tier = cfg.get("agents", {}).get(agent)
    if tier is None:
        raise LLMError(f"agent '{agent}' is not listed under 'agents' in models.yaml")
    model = cfg.get("tiers", {}).get(tier)
    if model is None:
        raise LLMError(f"tier '{tier}' (used by agent '{agent}') is not defined in models.yaml")
    return str(model)


def _cost_of(raw: Any, agent: str, model: str) -> float:
    """Dollar cost of a completion; 0.0 (with a warning) when LiteLLM cannot price the model."""
    try:
        cost = litellm.completion_cost(completion_response=raw)
    except Exception:
        cost = None
    if cost is None:
        log.warning("cost_unknown", agent=agent, model=model)
        return 0.0
    return float(cost)


async def ask(
    agent: str,
    prompt: str,
    response_model: type[T],
    *,
    temperature: float | None = None,
    **variables: object,
) -> T:
    """Render prompts/<prompt>.md, call the agent's model and return a validated object.

    Raises:
        BudgetExceeded: this month's logged cost has reached monthly_budget_usd.
        LLMError: the agent or its tier is missing from models.yaml.
        PromptError: the prompt file or a template variable is wrong.
    Provider errors and validation failures (after max_retries) propagate unchanged.
    """
    cfg = get_settings().load_yaml("models.yaml")
    limits = cfg["limits"]

    spent = repo.month_llm_cost()
    budget = float(limits["monthly_budget_usd"])
    if spent >= budget:
        raise BudgetExceeded(f"monthly LLM budget reached (${spent:.2f} of ${budget:.2f})")

    model = resolve_model(agent, cfg)
    system, user = render(prompt, **variables)
    if temperature is None:
        temperature = AGENT_TEMPERATURE.get(agent, DEFAULT_TEMPERATURE)

    result, raw = await _client().chat.completions.create_with_completion(
        model=model,
        response_model=response_model,
        max_retries=int(limits["max_retries"]),
        temperature=temperature,
        timeout=float(limits["timeout_seconds"]),
        metadata={"trace_name": agent},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    usage = getattr(raw, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = _cost_of(raw, agent, model)
    repo.log_llm_call(
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )
    log.info(
        "llm_call",
        agent=agent,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6),
    )
    return result