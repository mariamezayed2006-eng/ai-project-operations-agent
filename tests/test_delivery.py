"""Phase 7 delivery checks: packaging, config, and demo-scenario inventory.

These are cheap, offline invariants over the repository's delivery artifacts
(config example, ignore rules, pinned dependencies, README, and the demo
scenario catalog). They guard the "final packaging" and "no secrets
committed" acceptance items without making any network or database calls.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PLACEHOLDER_MARKERS = ("<your-", "your-", "example", "placeholder", "changeme", "xxx")
SECRETISH_PREFIXES = ("sk-", "sk_", "ghp_", "github_pat_", "AKIA", "xoxb-", "xoxp-")


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_env_example_is_secret_free_and_selects_ollama():
    """The committed config example carries no key and defaults to local Ollama."""
    text = _read(".env.example")
    assert "LLM_PROVIDER=ollama" in text
    assert "OLLAMA_BASE_URL=http://localhost:11434/v1" in text
    lowered = text.lower()
    assert not any(prefix.lower() in lowered for prefix in SECRETISH_PREFIXES)
    for key, value in re.findall(r"^\s*([A-Z0-9_]+)\s*=\s*(.+?)\s*$", text, re.MULTILINE):
        if "KEY" in key or "TOKEN" in key or "SECRET" in key or "PASSWORD" in key:
            assert any(marker in value.lower() for marker in PLACEHOLDER_MARKERS), f"{key} looks real"


def test_gitignore_covers_secrets_and_sqlite_artifacts():
    """Secrets and regenerable SQLite files never get committed."""
    lines = {line.strip() for line in _read(".gitignore").splitlines() if line.strip()}
    for expected in (".env", "*.db", "*.db-shm", "*.db-wal", "sessions.db"):
        assert expected in lines, f".gitignore missing {expected}"


def test_requirements_pin_runtime_dependencies():
    """The documented install path pins the verified runtime set."""
    text = _read("requirements.txt")
    for pkg in ("openai-agents", "gradio", "pydantic", "pytest", "python-dotenv"):
        assert re.search(rf"^{re.escape(pkg)}==", text, re.MULTILINE), f"{pkg} not pinned"


def test_demo_scenarios_doc_lists_all_twelve():
    """The demo catalog enumerates scenarios 1-12 and links each to a test."""
    text = _read("docs/DEMO_SCENARIOS.md")
    for n in range(1, 13):
        assert re.search(rf"test_sc{n:02d}_", text), f"scenario {n} missing a test link"
    assert "tests/test_demo_scenarios.py" in text


def test_readme_documents_setup_run_and_test():
    """The README keeps accurate, runnable instructions for the repo."""
    text = _read("README.md")
    assert "pip install -r requirements.txt" in text
    assert "python seed_database.py" in text
    assert "python app.py" in text
    assert "python -m pytest tests/ -q" in text
    assert "docs/DEMO_SCENARIOS.md" in text
    assert "ollama" in text.lower()


def test_demo_scenario_test_file_covers_twelve_cases():
    """All 12 scenario tests exist in the dedicated module."""
    text = _read("tests/test_demo_scenarios.py")
    for n in range(1, 13):
        assert f"def test_sc{n:02d}_" in text, f"missing test_sc{n:02d}"


def test_trace_screenshots_exist_and_are_indexed():
    """Captured UI/trace evidence exists and every file is referenced in the catalog."""
    catalog = _read("docs/DEMO_SCENARIOS.md")
    shots = sorted((REPO_ROOT / "docs" / "screenshots").glob("*.png"))
    assert len(shots) >= 7, "expected the captured walkthrough screenshots"
    for shot in shots:
        assert shot.name in catalog, f"{shot.name} not indexed in DEMO_SCENARIOS.md"

