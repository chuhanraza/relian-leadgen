"""Pre-flight credential/connectivity check, run before the real pipeline stages.

Every pipeline stage intentionally catches and logs per-lead failures rather than
crashing (one bad lead shouldn't kill a whole day's run) — but that means a fully
invalid or expired credential would produce five green checkmarks while silently
doing nothing useful all day. This script makes that failure mode loud instead:
if Groq, Supabase, or the Gmail OAuth refresh don't actually work, it exits
non-zero immediately, before any pipeline stage runs.

Usage: python scripts/healthcheck.py
"""

import sys

from dotenv import load_dotenv

load_dotenv()


def check_groq() -> None:
    import os

    from common.groq_client import generate

    key = os.environ.get("GROQ_API_KEY", "")
    print(f"[healthcheck] Groq key fingerprint: ...{key[-4:] if len(key) >= 4 else '????'}")

    reply = generate("Reply with exactly: OK", max_tokens=5)
    if "OK" not in reply.upper():
        raise RuntimeError(f"unexpected response: {reply!r}")


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
    checks = {"Groq": check_groq, "Supabase": check_supabase, "Gmail OAuth": check_gmail}
    failures = []
    for name, fn in checks.items():
        try:
            fn()
            print(f"[healthcheck] {name}: OK")
        except Exception as exc:  # noqa: BLE001 — want the message, not to classify the error
            print(f"[healthcheck] {name}: FAILED — {exc}")
            failures.append(name)

    if failures:
        sys.exit(
            f"[healthcheck] Pre-flight failed for: {', '.join(failures)}. "
            f"Aborting before running the pipeline — check the corresponding GitHub secret."
        )


if __name__ == "__main__":
    main()
