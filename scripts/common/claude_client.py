"""Anthropic client wrapper for web-search-backed research (Lead Hunter, Enricher) and
plain-text generation (Copywriter).

The web_search tool name/version below matches the Claude API's server tool as of this
build — check https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool
if Anthropic ships a newer tool version and this needs bumping.
"""

import os

import anthropic

MODEL = "claude-sonnet-4-5"


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def research(prompt: str, max_uses: int = 5, max_tokens: int = 4096) -> str:
    """Run a prompt with web search enabled; returns the concatenated text output."""
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def generate(prompt: str, max_tokens: int = 1024) -> str:
    """Plain generation, no web search (used by the Copywriter)."""
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
