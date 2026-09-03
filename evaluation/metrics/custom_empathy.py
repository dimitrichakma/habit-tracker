"""CoachingEmpathyMetric — Phase 4 LLM-as-a-Judge metric (DeepEval).

Grades one coach reply on two things at once: does it acknowledge the user's
missed / skipped / struggling habit *supportively and without shaming*, AND
does it offer a *concrete, actionable* alternative or next step? Produces a
score in [0.0, 1.0] plus a written reason.

Judge model: `claude-opus-5` (verified via the Models API to be the strongest
model that still accepts an explicit `temperature` — Claude Fable 5, the only
tier above it, rejects the sampling params the spec asks for and requires
30-day retention). It is a distinct, stronger tier than the coaching agent
being graded (`claude-sonnet-5`), which reduces self-grading bias.

`temperature=0.0` per the eval spec. Note: `claude-opus-5` has deprecated the
sampling params, so DeepEval's `AnthropicModel` drops it — opus-5 is already
near-deterministic, and Anthropic has no `seed` equivalent, so temperature=0
is the available (and only) determinism lever, stated for intent.

`test_rag_agent.py` imports `get_judge_model()` and hands the SAME model to
`FaithfulnessMetric` (which would otherwise default to OpenAI internally) — so
both metrics judge on one Claude provider. (`ContextualPrecisionMetric` was
dropped from that suite — with mocked retrieval it had no signal; see
`test_rag_agent.py`'s module docstring.)
"""

from __future__ import annotations

import json

from deepeval.metrics import BaseMetric
from deepeval.models import AnthropicModel
from deepeval.test_case import LLMTestCase

JUDGE_MODEL_NAME = "claude-opus-5"

_judge_model: AnthropicModel | None = None


def get_judge_model() -> AnthropicModel:
    """Process-wide judge model, shared by CoachingEmpathyMetric and (in
    test_rag_agent.py) the two built-in RAG metrics — so every metric grades
    with the same Anthropic judge."""
    global _judge_model
    if _judge_model is None:
        _judge_model = AnthropicModel(model=JUDGE_MODEL_NAME, temperature=0.0)
    return _judge_model


_GRADING_PROMPT = """You are grading ONE reply from an AI habit coach.

The user said:
\"\"\"{input}\"\"\"

The coach replied:
\"\"\"{actual_output}\"\"\"

Grade the reply on TWO things together:

1. ACKNOWLEDGEMENT — Does it acknowledge the user's missed / skipped /
   struggling habit directly and supportively: naming what happened, without
   shaming, blaming, lecturing, guilt-tripping, or expressing disappointment
   in the user?

2. ACTIONABILITY — Does it offer at least one concrete, actionable next step
   or a smaller alternative version of the habit — not merely vague
   encouragement like "you've got this"?

Scoring, 0.0 to 1.0:
- 1.0  both fully met: supportive acknowledgement AND a concrete actionable path.
- ~0.5 only one of the two is met, or the acknowledgement is present but flat
       and generic.
- 0.0  no acknowledgement at all, OR no actionable path at all.
- Any shaming, blaming, or judgemental language caps the score at 0.2,
  regardless of other merits.

Respond with ONLY a JSON object and nothing else, in exactly this form:
{{"score": <number between 0 and 1>, "reason": "<2-4 sentences citing specific words or phrases from the reply>"}}
"""


def _parse_verdict(text: str) -> tuple[float, str]:
    """Pull {"score", "reason"} out of the judge's reply, tolerating a stray
    markdown fence or surrounding prose."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start : end + 1]
    data = json.loads(raw)
    score = float(data["score"])
    return max(0.0, min(1.0, score)), str(data.get("reason", "")).strip()


class CoachingEmpathyMetric(BaseMetric):
    """LLM-as-a-Judge: does the coach's reply acknowledge a failure
    supportively (no shaming) AND offer an actionable alternative?"""

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold
        self.model = get_judge_model()
        self.evaluation_model = self.model.get_model_name()
        self.include_reason = True
        self.async_mode = True
        self.strict_mode = False
        self.verbose_mode = False
        self.score: float | None = None
        self.reason: str | None = None
        self.success: bool | None = None
        self.error: str | None = None

    def _prompt(self, test_case: LLMTestCase) -> str:
        if not test_case.input or not test_case.actual_output:
            raise ValueError(
                "CoachingEmpathyMetric needs both `input` and `actual_output` on the test case."
            )
        return _GRADING_PROMPT.format(
            input=test_case.input, actual_output=test_case.actual_output
        )

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        text, _cost = self.model.generate(self._prompt(test_case))
        self.score, self.reason = _parse_verdict(text)
        self.success = self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        text, _cost = await self.model.a_generate(self._prompt(test_case))
        self.score, self.reason = _parse_verdict(text)
        self.success = self.score >= self.threshold
        return self.score

    def is_successful(self) -> bool:
        if self.error is not None:
            return False
        return bool(self.success)

    @property
    def __name__(self) -> str:
        return "Coaching Empathy"
