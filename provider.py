"""LLM provider configuration for the AI Project Operations Agent (Project 4).

Default backend is OpenRouter's free router through its OpenAI-compatible
Chat Completions endpoint; Mistral and OpenAI remain available as explicit
fallbacks. Selection is environment-driven; no keys in code.

Resolution (read live on every call, so tests and demos need no reloads):

- ``LLM_PROVIDER``: ``auto`` (default) | ``openrouter`` | ``mistral`` |
  ``openai`` | ``ollama``. ``auto`` keeps its long-standing order: OpenRouter
  when ``OPENROUTER_API_KEY`` is set, else Mistral when ``MISTRAL_API_KEY``
  is set, else OpenAI SDK defaults. Ollama is explicit-only (no key exists
  to auto-detect) and needs no cloud key.
- ``LLM_MODEL`` > ``OPENAI_MODEL`` (legacy override) > built-in default.
- OpenRouter and Mistral both use the official SDK path: ``OpenAIProvider``
  with the vendor base URL and ``use_responses=False``, which the installed
  SDK resolves to ``OpenAIChatCompletionsModel`` (verified against the
  installed package, not examples).
- No key for the selected provider returns ``None``: offline behavior is
  preserved and live calls fail honestly inside the SDK (never faked).

Default model ``openrouter/free``: OpenRouter's Free Models Router, which
filters the free pool for the request's required capabilities (tool
calling, structured outputs) and routes each call to a matching free
model. Trade-off is inherent: the backing model varies per request, free
quotas apply (20 req/min, 50/day under $10 lifetime credits — failed
attempts count), and availability fluctuates. Pin ``LLM_MODEL`` to a
specific ``:free`` id for determinism; override freely via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# Ollama needs no real key; the local server ignores auth, but the SDK
# client requires a non-empty value — this placeholder is never a secret.
OLLAMA_API_KEY_PLACEHOLDER = "ollama"
DEFAULT_MODEL = "openrouter/free"

REPO_ROOT = Path(__file__).resolve().parent


def load_env(dotenv_path: str | Path | None = None) -> bool:
    """Load ``.env`` into the process without overriding real environment vars.

    Returns True when a file was loaded. Missing python-dotenv or a missing
    file is not an error (explicit exports keep working). Never logs values.
    """
    path = Path(dotenv_path) if dotenv_path is not None else REPO_ROOT / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    if not path.is_file():
        return False
    return bool(load_dotenv(dotenv_path=path, override=False))


load_env()

PROVIDER_AUTO = "auto"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_MISTRAL = "mistral"
PROVIDER_OPENAI = "openai"
PROVIDER_OLLAMA = "ollama"
_KNOWN_PROVIDERS = (PROVIDER_AUTO, PROVIDER_OPENROUTER, PROVIDER_MISTRAL, PROVIDER_OPENAI, PROVIDER_OLLAMA)


def get_model_name() -> str:
    """Return the configured model name (never a key, never empty)."""
    name = (os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL).strip()
    return name or DEFAULT_MODEL


def get_provider_name() -> str:
    """Return the effective provider: ``openrouter``, ``mistral``, ``ollama``, or ``openai``.

    Raises ValueError on an unknown ``LLM_PROVIDER`` value (fail fast).
    """
    requested = (os.environ.get("LLM_PROVIDER") or PROVIDER_AUTO).strip().lower()
    if requested not in _KNOWN_PROVIDERS:
        raise ValueError(
            "LLM_PROVIDER must be auto, openrouter, mistral, openai, or ollama "
            f"(got {requested!r})."
        )
    if requested in (PROVIDER_OPENROUTER, PROVIDER_MISTRAL, PROVIDER_OPENAI, PROVIDER_OLLAMA):
        return requested
    if os.environ.get("OPENROUTER_API_KEY"):
        return PROVIDER_OPENROUTER
    if os.environ.get("MISTRAL_API_KEY"):
        return PROVIDER_MISTRAL
    return PROVIDER_OPENAI


def _optional_headers() -> dict[str, str]:
    """OpenRouter ranking headers; only sent when configured (both optional)."""
    headers: dict[str, str] = {}
    referer = (os.environ.get("OPENROUTER_REFERER") or "").strip()
    title = (os.environ.get("OPENROUTER_TITLE") or "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def get_run_config() -> Any | None:
    """Build the SDK RunConfig for the effective provider, or None.

    - OpenRouter/Mistral with a key, or Ollama explicitly (no key needed):
      RunConfig with a Chat Completions provider (custom ``AsyncOpenAI``
      client only to carry the base URL and OpenRouter's optional ranking
      headers; no custom HTTP logic).
    - Anything without a key / OpenAI path: None, so SDK defaults apply
      (``OPENAI_API_KEY`` when present, honest offline failures otherwise).
    """
    provider_name = get_provider_name()
    if provider_name == PROVIDER_OPENAI:
        return None
    try:
        import agents_sdk  # normal path: alias registered by agents.py
    except ModuleNotFoundError:
        import agents  # noqa: F401  (registers the alias as a fallback)
        import agents_sdk

    if provider_name == PROVIDER_OLLAMA:
        from openai import AsyncOpenAI

        base_url = os.environ.get("OLLAMA_BASE_URL") or OLLAMA_BASE_URL
        client = AsyncOpenAI(api_key=OLLAMA_API_KEY_PLACEHOLDER, base_url=base_url)
        provider = agents_sdk.OpenAIProvider(openai_client=client, use_responses=False)
        return agents_sdk.RunConfig(model_provider=provider)

    if provider_name == PROVIDER_OPENROUTER:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None
        from openai import AsyncOpenAI

        base_url = os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=_optional_headers() or None)
        provider = agents_sdk.OpenAIProvider(openai_client=client, use_responses=False)
        return agents_sdk.RunConfig(model_provider=provider)

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("MISTRAL_BASE_URL") or MISTRAL_BASE_URL
    provider = agents_sdk.OpenAIProvider(api_key=api_key, base_url=base_url, use_responses=False)
    return agents_sdk.RunConfig(model_provider=provider)
