"""Fixtures for the Phase 4 evaluation suite.

Deliberately minimal. Retrieval is mocked inside `test_rag_agent.py` (the
`mock_vector_search` autouse fixture patches the vector store's
`similarity_search`), so there is NO real ChromaDB collection to seed or tear
down here — that would be dead code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# pytest does not load .env. The suite calls the real coaching agent
# (claude-sonnet-5) and the real judge (claude-opus-5); `OPENAI_API_KEY` is
# also read when a HabitMemoryStore is constructed.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
# Keep the run local — no DeepEval / ChromaDB analytics phone-home.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "golden_habits.json"


def load_golden_cases() -> list[dict]:
    """The golden dataset as a list of case dicts. `test_rag_agent.py` calls
    this at collection time to build its `@pytest.mark.parametrize` loop."""
    cases = json.loads(DATASET_PATH.read_text())
    assert cases, "golden_habits.json is empty"
    return cases


@pytest.fixture
def golden_cases() -> list[dict]:
    """The golden dataset, as a fixture."""
    return load_golden_cases()
