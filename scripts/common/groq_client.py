"""Groq client for text generation — free tier, no billing card required at all (unlike
Claude/Gemini, which both gate their free quotas behind a linked billing account).
Get a free key at https://console.groq.com/keys and set it as GROQ_API_KEY.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque

from groq import BadRequestError, Groq, NotFoundError

MODEL_QUALITY = "openai/gpt-oss-120b"
MODEL_FAST = "openai/gpt-oss-20b"

# Groq retires models with little notice (this pipeline has already been broken twice by
# it — see 86db404). When the API reports the current model dead, generate() retries once
# against the mapped fallback instead of the whole pipeline going dark. Picks are other
# currently-active chat models per `client.models.list()` (checked 2026-08-21), from a
# different vendor where possible so a single vendor's deprecation wave can't take out
# both a model and its fallback at once.
FALLBACK_MODELS = {
    MODEL_QUALITY: "qwen/qwen3.6-27b",
    MODEL_FAST: "groq/compound-mini",
}

# Groq error codes (confirmed against the live API, not guessed) that mean "this model is
# gone" rather than some other 400/404 — a bad prompt or bad param must not trigger a
# fallback retry.
_DEAD_MODEL_CODES = {"model_decommissioned", "model_not_found"}

# reasoning_effort is not a uniform param across Groq models: gpt-oss models accept "low",
# qwen only accepts "none"/"default", and groq/compound-mini rejects the param outright.
# Confirmed against the live API per model, not assumed — get this wrong and the fallback
# call fails too, defeating the point. None means "omit the param entirely".
_REASONING_EFFORT = {
    MODEL_QUALITY: "low",
    MODEL_FAST: "low",
    "qwen/qwen3.6-27b": "none",
    "groq/compound-mini": None,
}


def _create_completion(client: Groq, model: str, prompt: str, max_tokens: int):
    kwargs = {"max_tokens": max_tokens}
    effort = _REASONING_EFFORT.get(model, "low")
    if effort is not None:
        kwargs["reasoning_effort"] = effort
    return client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], **kwargs
    )


def _client() -> Groq:
    return Groq(api_key=os.environ["GROQ_API_KEY"])


# Module-level counters, same pattern as web_search.py's ddg failure counter — lets a
# caller (Lead Hunter) read a running total of calls/tokens it has made THIS run without
# threading a counter through every function signature. Reset at the start of each
# run() invocation that wants to police its own usage.
_call_count = 0
_total_tokens_used = 0
_tokens_by_model: dict[str, int] = {}

# Free-tier limits (https://console.groq.com/docs/rate-limits, checked 2026-10-06), per
# ORGANIZATION per model for both gpt-oss models: 8K tokens/min, 200K tokens/day, 1K
# requests/day. The x-ratelimit-limit-tokens header is the PER-MINUTE number; the daily
# token balance is not exposed in any response header.
TPM_LIMIT = 8_000
TPM_HEADROOM = 0.85  # pace to 85% of the per-minute limit
_recent_calls: dict[str, deque] = {}  # model -> deque[(monotonic_ts, tokens)]


def _pace_for_tpm(model: str, estimated_tokens: int) -> None:
    """Sleep just long enough that this call can't push the last 60s of usage on this model
    over the per-minute limit — a 429 on TPM wastes the call and, for verify, silently
    turns a good candidate into a rejection.
    """
    dq = _recent_calls.setdefault(model, deque())
    while True:
        now = time.monotonic()
        while dq and now - dq[0][0] >= 60:
            dq.popleft()
        if not dq or sum(t for _, t in dq) + estimated_tokens <= TPM_LIMIT * TPM_HEADROOM:
            return
        time.sleep(max(0.5, 60 - (now - dq[0][0]) + 0.25))


def get_call_count() -> int:
    return _call_count


def reset_call_count() -> None:
    global _call_count
    _call_count = 0


def get_tokens_used(model: str | None = None) -> int:
    """Total tokens this process has spent, or just those spent on `model`."""
    if model is None:
        return _total_tokens_used
    return _tokens_by_model.get(model, 0)


def reset_tokens_used() -> None:
    global _total_tokens_used
    _total_tokens_used = 0
    _tokens_by_model.clear()


def _log_fallback(original_model: str, fallback_model: str, error_code: str, error_message: str) -> None:
    print(
        f"[groq_client] FALLBACK FIRED: {original_model!r} -> {fallback_model!r} "
        f"(code={error_code!r}): {error_message}",
        file=sys.stderr,
    )
    try:
        from common.db import get_client

        get_client().table("model_fallback_events").insert(
            {
                "original_model": original_model,
                "fallback_model": fallback_model,
                "error_code": error_code,
                "error_message": error_message,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — logging the fallback must never mask it
        print(f"[groq_client] could not record fallback event to Supabase: {exc}", file=sys.stderr)


def generate(prompt: str, max_tokens: int = 1024, model: str = MODEL_QUALITY) -> str:
    global _call_count, _total_tokens_used
    _call_count += 1
    client = _client()
    # Both default models are OpenAI gpt-oss reasoning models on Groq: they spend some of
    # max_tokens on a hidden reasoning pass before the actual answer, so callers with
    # tight budgets (~<100 tokens) must size for that overhead, not just the answer length.
    _pace_for_tpm(model, len(prompt) // 3 + max_tokens)
    try:
        completion = _create_completion(client, model, prompt, max_tokens)
    except (BadRequestError, NotFoundError) as exc:
        error = (exc.body or {}).get("error", {}) if isinstance(exc.body, dict) else {}
        code = error.get("code", "")
        fallback_model = FALLBACK_MODELS.get(model)
        if code not in _DEAD_MODEL_CODES or fallback_model is None:
            raise
        _log_fallback(model, fallback_model, code, error.get("message", str(exc)))
        completion = _create_completion(client, fallback_model, prompt, max_tokens)
    if completion.usage is not None:
        _total_tokens_used += completion.usage.total_tokens
        _tokens_by_model[model] = _tokens_by_model.get(model, 0) + completion.usage.total_tokens
        _recent_calls.setdefault(model, deque()).append((time.monotonic(), completion.usage.total_tokens))
    return completion.choices[0].message.content or ""
