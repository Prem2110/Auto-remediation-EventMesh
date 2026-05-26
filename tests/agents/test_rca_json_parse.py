from agents.rca_agent import _extract_rca_json


def test_extracts_plain_json():
    raw = '{"root_cause": "x", "confidence": 0.7}'
    assert _extract_rca_json(raw)["confidence"] == 0.7


def test_extracts_from_markdown_fence():
    raw = '```json\n{"root_cause": "x", "confidence": 0.9}\n```\nSome trailing text.'
    assert _extract_rca_json(raw)["confidence"] == 0.9


def test_extracts_last_json_object_when_multiple():
    raw = 'Reasoning: {"partial": true} and final answer: {"root_cause": "y", "confidence": 0.8}'
    result = _extract_rca_json(raw)
    assert result["confidence"] == 0.8


def test_returns_empty_dict_on_garbage():
    assert _extract_rca_json("No JSON here at all.") == {}


def test_returns_empty_dict_on_empty_string():
    assert _extract_rca_json("") == {}


def test_handles_nested_json():
    raw = '{"root_cause": "x", "fixes": [{"component_id": "A", "correct_value": "v"}], "confidence": 0.85}'
    result = _extract_rca_json(raw)
    assert result["confidence"] == 0.85
    assert result["fixes"][0]["component_id"] == "A"
