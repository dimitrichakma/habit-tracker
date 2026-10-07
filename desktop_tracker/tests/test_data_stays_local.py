from pathlib import Path

from desktop_tracker import config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_db_path_is_outside_repo():
    assert not config.DB_PATH.resolve().is_relative_to(REPO_ROOT)


def test_db_path_is_under_application_support():
    assert "Application Support" in config.DB_PATH.parts
