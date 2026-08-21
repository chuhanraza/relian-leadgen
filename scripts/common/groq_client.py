"""Groq client for text generation — free tier, no billing card required at all (unlike
Claude/Gemini, which both gate their free quotas behind a linked billing account).
Get a free key at https://console.groq.com/keys and set it as GROQ_API_KEY.
"""

import os
import sys

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


def get_call_count() -> int:
    return _call_count


def reset_call_count() -> None:
    global _call_count
    _call_count = 0


def get_tokens_used() -> int:
    return _total_tokens_used


def reset_tokens_used() -> None:
    global _total_tokens_used
    _total_tokens_used = 0


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
    return completion.choices[0].message.content or ""
