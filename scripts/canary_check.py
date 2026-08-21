"""Early-warning canary: a fast, minimal check of the three systems that have each broken
this pipeline in the last month (Groq model availability, Supabase, Gmail OAuth) — run
every 2 hours by .github/workflows/canary.yml, far more often than the daily pipelines, so
a break surfaces within hours instead of being noticed days later when a run silently does
nothing.

This is deliberately separate from scripts/healthcheck.py: healthcheck.py gates a single
pipeline run and only needs one working model. This makes one real call to EACH configured
model (MODEL_QUALITY and MODEL_FAST) individually, since a fallback in scripts/common/groq_client.py
can mask one of them going dark — the fallback firing is a fine outcome for a pipeline run,
but a canary that reports "OK" while quietly relying on a fallback every single run defeats
the purpose of a canary.

Usage: python scripts/canary_check.py
Exit code 0 = all clear. Non-zero = something is broken; see printed detail.
"""

import sys

from dotenv import load_dotenv

load_dotenv()


def check_groq_model(model: str) -> None:
    import os

    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        max_tokens=150,
        reasoning_effort="low",
    )
    reply = completion.choices[0].message.content or ""
    if "OK" not in reply.upper():
        raise RuntimeError(f"unexpected response from {model}: {reply!r}")


def check_supabase() -> None:
    from common.db import get_client

    get_client().table("regions_covered").select("vertical").limit(1).execute()


def check_gmail() -> None:
    from google.auth.transport.requests import Request

    from common.gmail_client import _credentials

    creds = _credentials()
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("OAuth refresh did not return an access token")


def main() -> None:
    from common.groq_client import MODEL_FAST, MODEL_QUALITY

    checks = {
        f"Groq MODEL_QUALITY ({MODEL_QUALITY})": lambda: check_groq_model(MODEL_QUALITY),
        f"Groq MODEL_FAST ({MODEL_FAST})": lambda: check_groq_model(MODEL_FAST),
        "Supabase": check_supabase,
        "Gmail OAuth": check_gmail,
    }
    failures = []
    for name, fn in checks.items():
        try:
            fn()
            print(f"[canary] {name}: OK")
        except Exception as exc:  # noqa: BLE001 — want the message, not to classify the error
            print(f"[canary] {name}: FAILED — {exc}")
            failures.append(name)

    if failures:
        sys.exit(
            f"[canary] URGENT: {', '.join(failures)} broken. "
            f"This is the 2-hourly canary — something is down right now, not just stale."
        )
    print("[canary] all clear")


if __name__ == "__main__":
    main()
