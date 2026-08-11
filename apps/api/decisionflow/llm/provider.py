"""Gemini client.

Everything that talks to the model goes through here, so retries, timeouts,
model fallback and error mapping exist in one place rather than being
reinvented by each agent.

Two operational facts drove this design, both established by calling the API
rather than reading docs:

  * The models *list* endpoint is not proof of access — `gemini-2.5-flash` is
    still listed but returns 404 "no longer available to new users".
  * Pro models are quota-restricted on free-tier keys and 429 immediately,
    while Flash models succeed. Hence `complete_with_fallback`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors, types

from decisionflow.core.config import settings
from decisionflow.core.errors import LLMUnavailableError
from decisionflow.core.logging import get_logger

log = get_logger(__name__)

_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if not settings.llm_configured:
        raise LLMUnavailableError(
            "No Gemini API key is configured. Set GEMINI_API_KEY to enable AI features."
        )
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests that swap the key."""
    global _client
    _client = None


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    # Token counts, when the API reports them — worth surfacing because cost
    # per question is a real product concern, not a curiosity.
    input_tokens: int | None = None
    output_tokens: int | None = None


def _usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None, None
    return (
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "candidates_token_count", None),
    )


async def complete(
    prompt: str,
    *,
    model: str | None = None,
    system_instruction: str | None = None,
    temperature: float = 0.0,
    response_schema: Any | None = None,
    max_output_tokens: int | None = None,
) -> Completion:
    """One model call.

    `temperature=0` by default: this system generates SQL and reports figures,
    where the same question should give the same answer. Creativity is a defect
    here, not a feature.
    """
    chosen = model or settings.gemini_model

    config = types.GenerateContentConfig(
        temperature=temperature,
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
    )
    if response_schema is not None:
        # Structured output: the API constrains generation to the schema, which
        # is far more reliable than asking for JSON and parsing hopefully.
        config.response_mime_type = "application/json"
        config.response_schema = response_schema

    try:
        response = await asyncio.wait_for(
            get_client().aio.models.generate_content(
                model=chosen, contents=prompt, config=config
            ),
            timeout=settings.gemini_timeout_seconds,
        )
    except TimeoutError as exc:
        raise LLMUnavailableError(
            f"The model did not respond within {settings.gemini_timeout_seconds} seconds."
        ) from exc
    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        log.warning("llm.api_error", model=chosen, code=code)
        raise LLMUnavailableError(_describe(code, exc)) from exc

    input_tokens, output_tokens = _usage(response)
    return Completion(
        text=(response.text or "").strip(),
        model=chosen,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _describe(code: int | None, exc: Exception) -> str:
    """Turn an API failure into something a user can act on."""
    if code == 429:
        return "The model is rate limited right now. Please try again in a moment."
    if code == 404:
        return (
            "The configured model is unavailable to this API key. "
            "Check GEMINI_MODEL against the models your key can call."
        )
    if code in (401, 403):
        return "The Gemini API key was rejected. Check GEMINI_API_KEY."
    return f"The language model is unavailable: {exc}"


async def complete_with_fallback(prompt: str, **kwargs: Any) -> Completion:
    """Try the reasoning model, fall back to the primary one when throttled.

    Pro models are quota-restricted on free-tier keys. Rather than forcing the
    deployment to choose between "good model that 429s" and "lesser model that
    works", try the better one and degrade quietly. Only rate limiting triggers
    the fallback — a 404 or auth failure would fail identically on the second
    model, so retrying would just double the latency of an error.
    """
    primary = kwargs.pop("model", None) or settings.gemini_reasoning_model
    try:
        return await complete(prompt, model=primary, **kwargs)
    except LLMUnavailableError as exc:
        if "rate limited" not in str(exc) or primary == settings.gemini_model:
            raise
        log.info("llm.falling_back", from_model=primary, to_model=settings.gemini_model)
        return await complete(prompt, model=settings.gemini_model, **kwargs)


def parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON response, tolerating markdown fences.

    Structured output makes fences unlikely, but a single stray fence should
    not cost the user their question.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```")

    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError as exc:
        raise LLMUnavailableError(f"The model returned malformed JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMUnavailableError("The model returned JSON that was not an object.")
    return parsed
