from src.agent import SYSTEM_PROMPT_TEMPLATE


def test_reflective_coaching_guardrail_avoids_account_troubleshooting_pivot():
    assert "the forward step must be habit-focused" in SYSTEM_PROMPT_TEMPLATE
    assert "not account/debug troubleshooting" in SYSTEM_PROMPT_TEMPLATE
    assert "Don't pivot to \"no active habits\"" in SYSTEM_PROMPT_TEMPLATE
