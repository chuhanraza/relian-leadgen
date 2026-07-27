"""Groq client for text generation — free tier, no billing card required at all (unlike
Claude/Gemini, which both gate their free quotas behind a linked billing account).
Get a free key at https://console.groq.com/keys and set it as GROQ_API_KEY.
"""

import os

from groq import Groq

MODEL_QUALITY = "llama-3.3-70b-versatile"
MODEL_FAST = "llama-3.1-8b-instant"


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


def generate(prompt: str, max_tokens: int = 1024, model: str = MODEL_QUALITY) -> str:
    global _call_count, _total_tokens_used
    _call_count += 1
    client = _client()
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    if completion.usage is not None:
        _total_tokens_used += completion.usage.total_tokens
    return completion.choices[0].message.content or ""
