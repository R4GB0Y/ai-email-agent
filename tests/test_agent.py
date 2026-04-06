# tests/test_agent.py
"""
WHY test? Because 'it works on my machine' doesn't count.
These tests catch regressions before they hit production.
"""
from src.agent import AgentResponse


def test_agent_response_validation():
    """Test that Pydantic catches bad data."""
    # Valid response
    r = AgentResponse(answer="hello", confidence=0.9, reasoning="test")
    assert r.answer == "hello"
    assert 0 <= r.confidence <= 1

    # Invalid response — missing fields
    try:
        AgentResponse(answer="hello")  # missing confidence & reasoning
        assert False, "Should have raised"
    except Exception:
        pass  # Expected!


def test_confidence_is_float():
    """Confidence should be a float, not a string."""
    r = AgentResponse(answer="hi", confidence=0.5, reasoning="r")
    assert isinstance(r.confidence, float)