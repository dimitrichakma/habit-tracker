"""Phase 6.3 — real (non-mocked) pgvector pipeline test.

``evaluation/test_rag_agent.py`` mocks retrieval entirely, so nothing there
exercises the actual vector store. This is the counterpart: it writes to and
reads from a live Postgres + pgvector through ``src/vector_store.HabitMemoryStore``
and proves the per-user metadata filter really isolates one user's weekly
summaries from another's.

Needs ``OPENAI_API_KEY`` (real ``text-embedding-3-small`` calls) and a throwaway
Postgres (see ``conftest.pgvector_url``). Skips cleanly without either.

Run: ``uv run pytest tests/``  (add ``TEST_DATABASE_URL=...`` to skip the
Docker container and use your own scratch DB instead).
"""

from __future__ import annotations

import os

import pytest

if not os.environ.get("OPENAI_API_KEY"):
    pytest.skip("OPENAI_API_KEY required for real embeddings", allow_module_level=True)

# A dedicated collection name — the fixture drops exactly this on teardown and
# never touches the app's real "habit_summaries" collection or any table.
COLLECTION = "habit_summaries_itest"

USER_A = 101
USER_B = 202


@pytest.fixture
def store(pgvector_url, monkeypatch):
    """A ``HabitMemoryStore`` bound to the throwaway database and an isolated
    collection. ``COLLECTION_NAME`` is read at import time and the store is a
    module singleton, so both are reset here."""
    monkeypatch.setenv("DATABASE_URL", pgvector_url)
    import src.vector_store as vector_store

    monkeypatch.setattr(vector_store, "COLLECTION_NAME", COLLECTION)
    monkeypatch.setattr(vector_store, "_vector_store", None)
    monkeypatch.setattr(vector_store, "_store", None)

    memory_store = vector_store.get_habit_memory_store()
    try:
        yield memory_store
    finally:
        try:
            memory_store.vectorstore.delete_collection()
        except Exception:
            pass


def _seed(store) -> None:
    store.add_summary(
        doc_id=f"user{USER_A}-w1",
        text="Ran 5k every morning before work and held a steady 6am wake time all week.",
        user_id=USER_A,
        metadata={"week_ending": "2026-08-30"},
    )
    store.add_summary(
        doc_id=f"user{USER_A}-w2",
        text="Morning runs held on weekdays but both weekend mornings were skipped.",
        user_id=USER_A,
    )
    store.add_summary(
        doc_id=f"user{USER_B}-w1",
        text="Did not exercise at all this week; every planned gym session was missed.",
        user_id=USER_B,
    )


def test_write_then_retrieve_relevant(store):
    _seed(store)
    docs = store.similarity_search("morning running consistency", user_id=USER_A, k=3)
    assert docs, "expected stored summaries back"
    joined = " ".join(doc.page_content for doc in docs)
    assert "Ran 5k" in joined or "Morning runs" in joined


def test_metadata_filter_isolates_users(store):
    _seed(store)
    # The exact call query_past_behavior makes: the generic VectorStore
    # interface with a user_id filter, never a backend-specific method.
    a_docs = store.vectorstore.similarity_search(
        "missed every workout", k=5, filter={"user_id": USER_A}
    )
    assert a_docs, "USER_A should still match something"
    assert all("gym session was missed" not in doc.page_content for doc in a_docs), (
        "USER_B's summary leaked into USER_A's filtered search"
    )

    b_docs = store.vectorstore.similarity_search(
        "missed every workout", k=5, filter={"user_id": USER_B}
    )
    assert any("gym session was missed" in doc.page_content for doc in b_docs)
    assert all("Ran 5k" not in doc.page_content for doc in b_docs)


def test_add_summary_upserts_on_doc_id(store):
    store.add_summary(doc_id="dupe-1", text="First version: skipped meditation all week.", user_id=USER_A)
    store.add_summary(
        doc_id="dupe-1", text="Second version: meditated daily without a single miss.", user_id=USER_A
    )
    docs = store.vectorstore.similarity_search(
        "meditation habit", k=10, filter={"user_id": USER_A}
    )
    matches = [doc for doc in docs if "meditat" in doc.page_content.lower()]
    assert len(matches) == 1, f"expected 1 row for doc_id 'dupe-1', got {len(matches)}"
    assert "Second version" in matches[0].page_content
