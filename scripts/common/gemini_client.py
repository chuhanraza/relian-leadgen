"""Google Gemini client wrapper for web-search-backed research (Lead Hunter, Enricher) and
plain-text generation (Copywriter). Uses Gemini's free API tier — no billing required.

Get a free key at https://aistudio.google.com/apikey and set it as GEMINI_API_KEY.
"""

import os

from google import genai
from google.genai import types

MODEL = "gemini-2.0-flash"


def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def research(prompt: str, max_tokens: int = 4096) -> str:
    """Run a prompt with Google Search grounding enabled; returns the text output."""
    client = _client()
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[grounding_tool],
            max_output_tokens=max_tokens,
        ),
    )
    return response.text or ""


def generate(prompt: str, max_tokens: int = 1024) -> str:
    """Plain generation, no search grounding (used by the Copywriter)."""
    client = _client()
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=max_tokens),
    )
    return response.text or ""
