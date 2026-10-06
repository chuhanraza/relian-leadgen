"""Groq client for text generation — free tier, no billing card required at all (unlike
Claude/Gemini, which both gate their free quotas behind a linked billing account).
Get a free key at https://console.groq.com/keys and set it as GROQ_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque

from groq import BadRequestError, Groq, NotFoundError, RateLimitError

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


class GroqQuotaExhausted(RuntimeError):
    """The free DAILY limit (tokens or requests per day) is spent. Not a rejection and not
    retryable today: callers must stop making model calls, leave their work pending, and
    NOT mark anything as processed.
    """


class GroqRateLimited(RuntimeError):
    """A per-minute 429 that was still failing after the one wait-and-retry. Transient: the
    item should be left pending, not treated as a model rejection.
    """


MAX_RETRY_AFTER_SECONDS = 90

# The breaker is shared across the separate stage processes of one job (lead_hunter,
# enricher, copywriter all run as their own `python scripts/X.py`) through a small flag file
# in the job workspace, same pattern as token_usage_log. A flag older than the TTL is
# ignored so a stale local file can never block a fresh day.
_QUOTA_FLAG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".groq_quota_exhausted.json")
QUOTA_FLAG_TTL_SECONDS = 3 * 3600
_quota_tripped: dict | None = None


def _classify_429(exc: RateLimitError) -> tuple[str, float]:
    """('daily' | 'minute', seconds_to_wait). Groq's 429 text names the limit that tripped,
    e.g. "... on tokens per day (TPD): Limit 200000, Used 199569 ..." vs "... per minute (TPM)".
    """
    body = exc.body if isinstance(exc.body, dict) else {}
    msg = str((body.get("error") or {}).get("message") or exc)
    kind = "daily" if re.search(r"per day|\(TPD\)|\(RPD\)", msg) else "minute"
    wait = 10.0
    retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
    try:
        if retry_after:
            wait = float(retry_after)
        elif m := re.search(r"try again in (?:(\d+)m)?(?:([\d.]+)s)?", msg):
            wait = int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    except ValueError:
        pass
    return kind, min(wait, MAX_RETRY_AFTER_SECONDS)


def quota_tripped() -> dict | None:
    """The breaker state ({'model','message','at'}) if a daily limit has tripped this run
    (in this process or an earlier stage of the same job), else None.
    """
    global _quota_tripped
    if _quota_tripped:
        return _quota_tripped
    try:
        with open(_QUOTA_FLAG_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("at", 0) < QUOTA_FLAG_TTL_SECONDS:
            _quota_tripped = data
            return data
    except (OSError, ValueError):
        pass
    return None


def _trip_quota(model: str, message: str) -> None:
    global _quota_tripped
    _quota_tripped = {"model": model, "message": message[:300], "at": time.time()}
    print(f"[groq_client] !!! DAILY QUOTA EXHAUSTED on {model}: {message[:200]} — no further model calls this run")
    try:
        with open(_QUOTA_FLAG_PATH, "w") as f:
            json.dump(_quota_tripped, f)
    except OSError:
        pass


def reset_quota_breaker() -> None:
    global _quota_tripped
    _quota_tripped = None
    if os.path.exists(_QUOTA_FLAG_PATH):
        os.remove(_QUOTA_FLAG_PATH)


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


def _complete(client: Groq, model: str, prompt: str, max_tokens: int):
    """One completion, with the dead-model fallback. Raises groq.RateLimitError untouched."""
    try:
        return _create_completion(client, model, prompt, max_tokens)
    except (BadRequestError, NotFoundError) as exc:
        error = (exc.body or {}).get("error", {}) if isinstance(exc.body, dict) else {}
        code = error.get("code", "")
        fallback_model = FALLBACK_MODELS.get(model)
        if code not in _DEAD_MODEL_CODES or fallback_model is None:
            raise
        _log_fallback(model, fallback_model, code, error.get("message", str(exc)))
        return _create_completion(client, fallback_model, prompt, max_tokens)


def generate(prompt: str, max_tokens: int = 1024, model: str = MODEL_QUALITY) -> str:
    global _call_count, _total_tokens_used
    if tripped := quota_tripped():
        raise GroqQuotaExhausted(f"circuit breaker open ({tripped['model']}: {tripped['message']})")
    _call_count += 1
    client = _client()
    # Both default models are OpenAI gpt-oss reasoning models on Groq: they spend some of
    # max_tokens on a hidden reasoning pass before the actual answer, so callers with
    # tight budgets (~<100 tokens) must size for that overhead, not just the answer length.
    _pace_for_tpm(model, len(prompt) // 3 + max_tokens)
    try:
        completion = _complete(client, model, prompt, max_tokens)
    except RateLimitError as exc:
        kind, wait = _classify_429(exc)
        if kind == "daily":
            _trip_quota(model, str(exc))
            raise GroqQuotaExhausted(str(exc)) from exc
        print(f"[groq_client] per-minute 429 on {model}, waiting {wait:.0f}s then retrying once")
        time.sleep(wait)
        try:
            completion = _complete(client, model, prompt, max_tokens)
        except RateLimitError as exc2:
            if _classify_429(exc2)[0] == "daily":
                _trip_quota(model, str(exc2))
                raise GroqQuotaExhausted(str(exc2)) from exc2
            raise GroqRateLimited(str(exc2)) from exc2
    if completion.usage is not None:
        _total_tokens_used += completion.usage.total_tokens
        _tokens_by_model[model] = _tokens_by_model.get(model, 0) + completion.usage.total_tokens
        _recent_calls.setdefault(model, deque()).append((time.monotonic(), completion.usage.total_tokens))
    return completion.choices[0].message.content or ""
