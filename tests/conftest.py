"""Fixtures for the non-mocked integration tests (Phase 6.3).

Separate from ``evaluation/`` (the LLM-as-a-Judge suite): these hit a *real*
Postgres + pgvector to prove ``src/vector_store.py``'s write / read / per-user
scoping actually works — the one thing the mocked eval suite can't cover
(``evaluation/test_rag_agent.py`` swaps the whole vector store for a fake).

``src/`` never imports from here (same rule as ``evaluation/``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Ryuk (testcontainers' reaper) bind-mounts the Docker socket, which Colima /
# Podman / rootless setups often reject. The pgvector_url fixture already stops
# the container in its finally block, so the reaper is just a safety net —
# disable it unless the caller has an opinion.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

# pytest does not load .env. The integration test needs OPENAI_API_KEY for the
# real ``text-embedding-3-small`` calls (the one non-Claude call in the app).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# .env sets LANGSMITH_TRACING=true for the deployment. Keep this suite out of
# the real "habit-tracker" project — the @traceable calls in vector_store.py
# would otherwise export here (Phase 6.1 / 6.3).
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
try:
    from langsmith import utils as _ls_utils

    _ls_utils.get_env_var.cache_clear()
except Exception:
    pass


@pytest.fixture(scope="session")
def pgvector_url():
    """A throwaway Postgres+pgvector connection URL, in priority order:

    1. ``TEST_DATABASE_URL`` if set — a scratch database or Neon branch *you*
       point at. It must not equal ``DATABASE_URL`` (asserted): CLAUDE.md
       forbids running tests against the real database.
    2. A disposable ``pgvector/pgvector`` Docker container (``testcontainers``),
       started for the session and torn down after.
    3. Skip — there is nothing safe to test against.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        if explicit == os.environ.get("DATABASE_URL"):
            pytest.fail(
                "TEST_DATABASE_URL must not equal DATABASE_URL — refusing to run "
                "integration tests against the real database (CLAUDE.md)."
            )
        yield explicit
        return

    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:  # older testcontainers layout
            from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("neither TEST_DATABASE_URL nor testcontainers is available")

    container = PostgresContainer(
        "pgvector/pgvector:pg16",
        username="itest",
        password="itest",
        dbname="itest",
        driver="psycopg",  # psycopg v3 — matches src/vector_store.py
    )
    try:
        container.start()
    except Exception as exc:  # Docker not running, image pull blocked, ...
        pytest.skip(f"could not start a pgvector container: {exc}")

    try:
        yield container.get_connection_url()
    finally:
        container.stop()
