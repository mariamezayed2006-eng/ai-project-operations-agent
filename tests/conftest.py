"""Shared fixtures for the Phase 5+ test suite (older modules keep their local ones)."""

import pytest

import database
import seed_database


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded database per test."""
    db = tmp_path / "shared.db"
    seed_database.seed(db)
    c = database.get_connection(db)
    yield c
    c.close()
