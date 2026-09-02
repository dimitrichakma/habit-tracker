"""Phase 4 — LLM-as-a-Judge evaluation of the RAG coaching agent.

For each golden case in ``datasets/golden_habits.json``:

  1. ``mock_vector_search`` (autouse) makes the agent's ``query_past_behavior``
     tool return that case's ``retrieval_context`` as LangChain ``Document``s —
     no real ChromaDB, no embedding call.
  2. The REAL agent (``src.agent.build_agent``) is invoked directly — no
     FastAPI, no JWT — on a **unique per-case thread_id** (so
     ``AsyncSqliteSaver`` can't bleed one case's conversation into the next).
  3. The tool's own ``ToolMessage`` output is pulled from the message history
     as ``actual_retrieved_context`` and passed to ``LLMTestCase`` — *not* the
     golden JSON's ``retrieval_context`` (that only feeds the mock).
  4. ``FaithfulnessMetric``, ``ContextualPrecisionMetric`` and
     ``CoachingEmpathyMetric`` grade the reply — all three on the same
     ``claude-opus-5`` judge (the two built-ins default to OpenAI otherwise).

Isolation: the agent's checkpoint DB and the app's ``habits.db`` are pointed
at throwaway temp files — this suite never reads or writes the real ones.

Run: ``pytest evaluation/`` (needs ``pytest.ini``'s ``asyncio_mode = auto``,
added in Step 9). Makes real Anthropic API calls.

--- Note on the mock target -------------------------------------------------
The plan called for patching ``VectorStore.similarity_search`` (the generic
base). That cannot work: ``langchain_chroma.Chroma`` (and every concrete
vector store) *overrides* ``similarity_search``, so patching the abstract
base has no effect on a real instance (verified). Instead the mock swaps the
whole ``get_habit_memory_store`` accessor for a fake whose
``.vectorstore.similarity_search`` returns the Documents. This is still
provider-agnostic — the tool keeps calling ``.vectorstore.similarity_search``
and the fake answers regardless of what the real backend is — so it survives
the Phase 5 Chroma → pgvector swap unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from langsmith import tracing_context
from langsmith import utils as ls_utils
import sqlalchemy
from sqlalchemy.orm import sessionmaker

import src.agent as agent_module
import src.database as database
from deepeval.metrics import ContextualPrecisionMetric, FaithfulnessMetric
from deepeval.test_case import LLMTestCase
from langchain_core.documents import Document
from langchain_core.messages import ToolMessage

from evaluation.conftest import load_golden_cases
from evaluation.metrics.custom_empathy import CoachingEmpathyMetric, get_judge_model

GOLDEN_CASES = load_golden_cases()
CASE_IDS = [case["name"] for case in GOLDEN_CASES]

FAITHFULNESS_THRESHOLD = 0.7
CONTEXT_PRECISION_THRESHOLD = 0.7
EMPATHY_THRESHOLD = 0.7

QUERY_PAST_BEHAVIOR = "query_past_behavior"


@pytest.fixture(autouse=True)
def no_langsmith_tracing(monkeypatch):
    """Phase 6.1: keep the eval suite out of LangSmith. `conftest.py` loads
    `.env`, which sets `LANGSMITH_TRACING=true` for the real deployment — but
    these runs must not export traces to the real "habit-tracker" project or
    pay per-call tracing latency. Force it off (env + langsmith's cached env
    lookup + a context override) for every case. Does not touch
    `isolated_state`, `mock_vector_search`, the per-case `thread_id`, or the
    `actual_retrieved_context` extraction."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    ls_utils.get_env_var.cache_clear()
    with tracing_context(enabled=False):
        yield
    ls_utils.get_env_var.cache_clear()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point the agent's checkpoint DB and the app's habits.db at throwaway
    temp files — the eval suite must never touch the real ones (CLAUDE.md).
    habits.db is created empty so the agent's DB-backed tools cleanly report
    "no habits" and it leans on the mocked vector memory."""
    monkeypatch.setattr(agent_module, "CHECKPOINT_DB", str(tmp_path / "checkpoints_eval.db"))
    engine = sqlalchemy.create_engine(
        f"sqlite:///{tmp_path / 'habits_eval.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, autocommit=False),
    )
    database.init_db()


@pytest.fixture(autouse=True)
def mock_vector_search(request, monkeypatch):
    """Intercept the agent's vector retrieval: `query_past_behavior` calls
    `get_habit_memory_store().vectorstore.similarity_search(...)`, and this
    swaps that for a fake returning this case's `retrieval_context` as real
    `Document` objects. Provider-agnostic — nothing here names Chroma — so it
    is unchanged by a future Chroma → pgvector swap."""
    case = request.node.callspec.params["case"]
    documents = [Document(page_content=chunk) for chunk in case["retrieval_context"]]

    class _FakeVectorStore:
        def similarity_search(self, query: str, k: int = 4, **kwargs) -> list[Document]:
            return list(documents)[:k]

    class _FakeMemoryStore:
        vectorstore = _FakeVectorStore()

    monkeypatch.setattr("src.tools.get_habit_memory_store", lambda: _FakeMemoryStore())


def _as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _tool_message_content(messages: list, tool_name: str) -> str | None:
    """The string content of the first ToolMessage from `tool_name`, if any."""
    for message in messages:
        if isinstance(message, ToolMessage) and getattr(message, "name", None) == tool_name:
            return message.content if isinstance(message.content, str) else _as_text(message.content)
    return None


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=CASE_IDS)
async def test_rag_agent_golden_case(case):
    # Unique thread_id per case — no AsyncSqliteSaver memory bleed between
    # parametrized runs. Integer form because tools.py does int(thread_id);
    # a raw uuid string would crash the user-id resolution.
    thread_id = str(uuid.uuid4().int % 10**12)

    async with agent_module.build_agent() as agent:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": case["input"]}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    messages = result["messages"]

    actual_output = _as_text(messages[-1].content)
    actual_retrieved_context = _tool_message_content(messages, QUERY_PAST_BEHAVIOR)
    assert actual_retrieved_context, (
        f"agent never called {QUERY_PAST_BEHAVIOR} for input: {case['input']!r}"
    )

    test_case = LLMTestCase(
        input=case["input"],
        actual_output=actual_output,
        expected_output=case["expected_output"],
        retrieval_context=[actual_retrieved_context],
    )

    judge = get_judge_model()
    metrics = [
        FaithfulnessMetric(threshold=FAITHFULNESS_THRESHOLD, model=judge),
        ContextualPrecisionMetric(threshold=CONTEXT_PRECISION_THRESHOLD, model=judge),
        CoachingEmpathyMetric(threshold=EMPATHY_THRESHOLD),
    ]

    failures: list[str] = []
    for metric in metrics:
        await metric.a_measure(test_case)
        if not metric.is_successful():
            failures.append(
                f"{metric.__name__}: score={metric.score:.2f} "
                f"(threshold {metric.threshold}) — {metric.reason}"
            )

    assert not failures, "\n".join(
        ["", f"case: {case['name']}", f"reply: {actual_output}", *failures]
    )
