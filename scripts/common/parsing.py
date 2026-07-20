"""Shared helper for pulling a JSON object/array out of a Claude text response.

Models asked to "reply with a fenced JSON block" occasionally wrap it in prose anyway;
this pulls the first ```json ... ``` fence, falling back to the first {...}/[...] span.
"""

import json
import re


def extract_json(text: str):
    fence = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    span = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if span:
        return json.loads(span.group(1))

    raise ValueError(f"No JSON found in model response: {text[:200]!r}")
