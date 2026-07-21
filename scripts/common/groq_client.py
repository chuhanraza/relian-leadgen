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


def generate(prompt: str, max_tokens: int = 1024, model: str = MODEL_QUALITY) -> str:
    client = _client()
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return completion.choices[0].message.content or ""
