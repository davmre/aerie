"""Pytest fixtures for Aerie collector tests."""

import pytest
from pathlib import Path

from database import init_database
from classifier import setup_prompts_and_modes
from server import create_app


@pytest.fixture
def test_db(tmp_path):
    """Create a fresh test database for each test."""
    db_path = tmp_path / "test.db"
    setup_prompts_and_modes(db_path)
    return db_path


@pytest.fixture
def app(test_db):
    """Create Flask app configured with test database."""
    app = create_app({
        "DATABASE": test_db,
        "TESTING": True,
        "CLASSIFICATION_ENABLED": False,
    })
    return app


@pytest.fixture
def client(app):
    """Create Flask test client."""
    return app.test_client()
