"""Vector store access for semantic habit memory (Phase 4).

`HabitMemoryStore` is the ONE place the rest of the app touches the vector
database. Internally it now holds a LangChain `PGVector` store (pgvector on the
same Postgres/Neon database as the relational tables — was ChromaDB) and
exposes it via `.vectorstore`, so `tools.query_past_behavior` calls the
*generic* `VectorStore.similarity_search` interface, never a backend-specific
API. That keeps the tool (and its test mock) independent of the backing store.

Embeddings: OpenAI `text-embedding-3-small` via `langchain_openai`
(`OPENAI_API_KEY` required). This is the one deliberate non-Anthropic piece
of the project — OpenAI is used ONLY as the embedding backend (and as
`deepeval`'s dependency). Every model that *generates* or *judges* text —
the coaching agent, the weekly summariser, the eval judge — stays Claude.

Scoping: the collection is shared, but every entry is written with a
`user_id` metadata tag; callers filter reads by `user_id` (a server-resolved
value, never LLM-supplied — same rule as every tool in `tools.py`).
"""

from __future__ import annotations

import os

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector
from langsmith import traceable

COLLECTION_NAME = os.environ.get("HABIT_MEMORY_COLLECTION", "habit_summaries")
EMBEDDING_MODEL = "text-embedding-3-small"


def _scrub_store_inputs(inputs: dict) -> dict:
    """Phase 6.1 — what LangSmith records as this call's inputs. Explicit
    allow-list: only the query/topic, the resolved user id, the doc id, and
    metadata keys. No credentials pass through these methods, but keep the
    recorded payload deliberately narrow rather than dumping the bound args."""
    safe: dict = {}
    if "query" in inputs:
        safe["query"] = inputs["query"]
    if "user_id" in inputs:
        safe["user_id"] = inputs["user_id"]
    if "k" in inputs:
        safe["k"] = inputs["k"]
    if "doc_id" in inputs:
        safe["doc_id"] = inputs["doc_id"]
    if inputs.get("text") is not None:
        safe["text_chars"] = len(inputs["text"])
    if inputs.get("metadata"):
        safe["metadata_keys"] = sorted(inputs["metadata"].keys())
    return safe


def _connection_url() -> str:
    """The Postgres URL for pgvector — same database as the relational tables.
    Forces the psycopg (v3) driver, which is what's installed."""
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


_vector_store: PGVector | None = None


def get_vector_store() -> PGVector:
    """Process-wide `PGVector` over the `habit_summaries` collection. Created
    lazily (first use), then reused so a single engine/pool is shared.

    `create_extension=True` (the default) has PGVector run
    `CREATE EXTENSION IF NOT EXISTS vector` itself; `database.init_db()` also
    does it — both are idempotent. `use_jsonb=True` stores metadata as JSONB,
    which is what the `{"user_id": ...}` read filter queries against.
    """
    global _vector_store
    if _vector_store is None:
        _vector_store = PGVector(
            embeddings=OpenAIEmbeddings(model=EMBEDDING_MODEL),
            collection_name=COLLECTION_NAME,
            connection=_connection_url(),
            use_jsonb=True,
            # Keep psycopg3 prepared statements off so Neon's pooled endpoint
            # ("-pooler" host, PgBouncer) works, matching database.py.
            engine_args={"connect_args": {"prepare_threshold": None}},
        )
    return _vector_store


class HabitMemoryStore:
    """Wrapper over the `habit_summaries` pgvector collection of weekly
    behavioral summaries. The single boundary the rest of the app goes
    through to reach the vector database."""

    def __init__(self) -> None:
        self._vectorstore = get_vector_store()

    @property
    def vectorstore(self) -> VectorStore:
        """The backing LangChain VectorStore. Callers use its generic
        `.similarity_search(query, k=..., filter=...)` — never a
        backend-specific method."""
        return self._vectorstore

    @traceable(
        run_type="chain",
        name="habit_memory.add_summary",
        process_inputs=_scrub_store_inputs,
    )
    def add_summary(
        self,
        *,
        doc_id: str,
        text: str,
        user_id: int,
        metadata: dict | None = None,
    ) -> None:
        """Insert or replace one summary. `doc_id` is stable per (user, week),
        so re-running a week's summarization overwrites rather than duplicates
        (PGVector.add_texts upserts on explicit ids)."""
        meta: dict = {"user_id": int(user_id)}
        if metadata:
            meta.update(metadata)
        self._vectorstore.add_texts(texts=[text], metadatas=[meta], ids=[doc_id])

    @traceable(
        run_type="retriever",
        name="habit_memory.similarity_search",
        process_inputs=_scrub_store_inputs,
    )
    def similarity_search(self, query: str, *, user_id: int, k: int = 3) -> list[Document]:
        """Convenience for non-tool callers: top-`k` summaries for this user,
        scoped by the `user_id` metadata filter. `tools.query_past_behavior`
        does NOT use this — it calls `.vectorstore.similarity_search` directly
        so its test can mock the generic interface."""
        return self._vectorstore.similarity_search(
            query, k=k, filter={"user_id": int(user_id)}
        )


_store: HabitMemoryStore | None = None


def get_habit_memory_store() -> HabitMemoryStore:
    """Process-wide singleton. Tests swap this module's `_store` (or patch
    this accessor) to point elsewhere."""
    global _store
    if _store is None:
        _store = HabitMemoryStore()
    return _store
