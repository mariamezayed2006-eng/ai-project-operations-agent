"""Provider configuration tests (offline; no network, no keys, no model calls)."""

import os

import pytest

import agents as project_agents  # noqa: F401  (registers agents_sdk alias)
import agents_sdk
import provider
from provider import (
    MISTRAL_BASE_URL,
    OLLAMA_BASE_URL,
    OPENROUTER_BASE_URL,
    DEFAULT_MODEL,
    get_model_name,
    get_provider_name,
    get_run_config,
)


def clean_env(monkeypatch):
    """Remove every provider/model variable for deterministic defaults."""
    for var in ("LLM_PROVIDER", "LLM_MODEL", "OPENAI_MODEL", "MISTRAL_API_KEY", "MISTRAL_BASE_URL",
                "OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
                "OPENROUTER_REFERER", "OPENROUTER_TITLE", "OLLAMA_BASE_URL", "OLLAMA_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_default_model_is_openrouter_free(monkeypatch):
    """Out of the box the project targets OpenRouter's free router."""
    clean_env(monkeypatch)
    assert get_model_name() == DEFAULT_MODEL == "openrouter/free"


def test_agent_model_matches_live_configuration():
    """Agents use whatever the environment selects (no cleaning: robust to a local .env)."""
    assert project_agents.MODEL_NAME == get_model_name()


def test_model_precedence_llm_over_openai(monkeypatch):
    """Explicit LLM_MODEL wins; legacy OPENAI_MODEL still honored underneath."""
    clean_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    assert get_model_name() == "gpt-4o-mini"
    monkeypatch.setenv("LLM_MODEL", "mistral-small-latest")
    assert get_model_name() == "mistral-small-latest"


def test_auto_prefers_openrouter_then_mistral(monkeypatch):
    """Key presence drives auto selection; auto order is unchanged by Ollama."""
    clean_env(monkeypatch)
    assert get_provider_name() == "openai"
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    assert get_provider_name() == "mistral"
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert get_provider_name() == "openrouter"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert get_provider_name() == "openai"
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        get_provider_name()


def test_offline_returns_no_run_config(monkeypatch):
    """Without keys the SDK defaults apply (offline behavior preserved)."""
    clean_env(monkeypatch)
    assert get_run_config() is None
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert get_run_config() is None  # SDK default client handles OpenAI keys
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    assert get_run_config() is None  # explicit Mistral but no key: nothing faked
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert get_run_config() is None  # explicit OpenRouter but no key: nothing faked


def test_mistral_run_config_uses_chat_completions(monkeypatch):
    """With a key, resolution goes through Chat Completions, not Responses."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    config = get_run_config()
    assert isinstance(config, agents_sdk.RunConfig)
    assert isinstance(config.model_provider, agents_sdk.OpenAIProvider)
    model = config.model_provider.get_model("mistral-small-latest")
    assert isinstance(model, agents_sdk.OpenAIChatCompletionsModel)
    assert MISTRAL_BASE_URL == "https://api.mistral.ai/v1"


def test_openrouter_run_config_uses_chat_completions(monkeypatch):
    """With a key, OpenRouter resolves through Chat Completions, not Responses."""
    clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = get_run_config()
    assert isinstance(config, agents_sdk.RunConfig)
    assert isinstance(config.model_provider, agents_sdk.OpenAIProvider)
    model = config.model_provider.get_model("openrouter/free")
    assert isinstance(model, agents_sdk.OpenAIChatCompletionsModel)
    assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"


def test_openrouter_ranking_headers_forwarded_only_when_set(monkeypatch):
    """Optional Referer/Title headers reach the client; nothing invented otherwise."""
    clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_REFERER", "https://example.com/demo")
    monkeypatch.setenv("OPENROUTER_TITLE", "Project 4 demo")
    config = get_run_config()
    client = config.model_provider._get_client()
    sent = {k: v for k, v in client._custom_headers.items() if "eferer" in k or "itle" in k}
    assert sent == {"HTTP-Referer": "https://example.com/demo", "X-Title": "Project 4 demo"}


def test_load_env_reads_file_without_overriding_explicit_vars(tmp_path, monkeypatch):
    """Repo .env loading picks up configured keys; explicit exports always win."""
    from provider import load_env

    clean_env(monkeypatch)
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("OPENROUTER_API_KEY=file-key\nLLM_MODEL=file-model\n", encoding="utf-8")
    assert load_env(dotenv_file) is True
    assert os.environ.get("OPENROUTER_API_KEY") == "file-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", "explicit-key")
    assert load_env(dotenv_file) is True
    assert os.environ.get("OPENROUTER_API_KEY") == "explicit-key"
    assert load_env(tmp_path / "missing.env") is False
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("LLM_MODEL", None)


def test_ollama_selected_explicitly_without_cloud_key(monkeypatch):
    """Ollama needs no cloud key; explicit selection works with a clean env."""
    clean_env(monkeypatch)
    assert get_provider_name() == "openai"  # auto default unchanged
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert get_provider_name() == "ollama"
    monkeypatch.setenv("LLM_MODEL", "llama3.2:latest")
    assert get_model_name() == "llama3.2:latest"


def test_ollama_run_config_uses_chat_completions(monkeypatch):
    """Local Ollama resolves through Chat Completions against its base URL."""
    clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2:latest")
    config = get_run_config()
    assert isinstance(config, agents_sdk.RunConfig)
    assert isinstance(config.model_provider, agents_sdk.OpenAIProvider)
    model = config.model_provider.get_model("llama3.2:latest")
    assert isinstance(model, agents_sdk.OpenAIChatCompletionsModel)
    assert str(config.model_provider._get_client().base_url).rstrip("/") == "http://localhost:11434/v1"


def test_ollama_base_url_override(monkeypatch):
    """OLLAMA_BASE_URL redirect works (e.g. remote Ollama host)."""
    clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/v1")
    config = get_run_config()
    assert str(config.model_provider._get_client().base_url).rstrip("/") == "http://gpu-box:11434/v1"


def test_ollama_client_wiring_uses_placeholder_key(monkeypatch):
    """The network client is built with the placeholder key and never touches the network."""
    import openai

    clean_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    seen = {}
    real_client = openai.AsyncOpenAI

    def recording_client(*args, **kwargs):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", recording_client)
    config = get_run_config()
    assert isinstance(config, agents_sdk.RunConfig)
    assert seen.get("api_key") == provider.OLLAMA_API_KEY_PLACEHOLDER
    assert seen.get("base_url") == provider.OLLAMA_BASE_URL
