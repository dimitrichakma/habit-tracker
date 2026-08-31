"""Vector store access for semantic habit memory (Phase 4).

`HabitMemoryStore` is the ONE place the rest of the app touches the vector
database — swap ChromaDB for pgvector in Phase 5 by rewriting this file only.
Internally it holds a LangChain `Chroma` VectorStore and exposes it via
`.vectorstore`, so `tools.query_past_behavior` calls the *generic*
`VectorStore.similarity_search` interface, never a Chroma-specific API. That
keeps the tool (and its test mock) independent of the backing store.

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

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_openai import OpenAIEmbeddings

CHROMA_PATH = os.environ.get("CHROMA_PATH", "chroma_db")
COLLECTION_NAME = os.environ.get("HABIT_MEMORY_COLLECTION", "habit_memory")
EMBEDDING_MODEL = "text-embedding-3-small"


class HabitMemoryStore:
    """Wrapper over one LangChain `Chroma` collection of weekly behavioral
    summaries. The swap boundary for Phase 5's move to pgvector."""

    def __init__(
        self,
        *,
        path: str = CHROMA_PATH,
        collection_name: str = COLLECTION_NAME,
    ) -> None:
        self._vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=OpenAIEmbeddings(model=EMBEDDING_MODEL),
            persist_directory=path,
        )

    @property
    def vectorstore(self) -> VectorStore:
        """The backing LangChain VectorStore. Callers use its generic
        `.similarity_search(query, k=..., filter=...)` — never a
        Chroma-specific method."""
        return self._vectorstore

    def add_summary(
        self,
        *,
        doc_id: str,
        text: str,
        user_id: int,
        metadata: dict | None = None,
    ) -> None:
        """Insert or replace one summary. `doc_id` is stable per (user, week),
        so re-running a week's summarization overwrites rather than duplicates."""
        meta: dict = {"user_id": int(user_id)}
        if metadata:
            meta.update(metadata)
        self._vectorstore.add_texts(texts=[text], metadatas=[meta], ids=[doc_id])

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
    """Process-wide singleton. Tests swap this module's `_store` to point
    elsewhere."""
    global _store
    if _store is None:
        _store = HabitMemoryStore()
    return _store
