import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


GOOD_XML = """<?xml version="1.0"?>
<root id="Collab_1">
  <process id="ReceiverHTTP_1">
    <ifl:property xmlns:ifl="http://sap.com/xi/mapping/ifl">
      <ifl:key>Address</ifl:key>
      <ifl:value>http://old.example.com</ifl:value>
    </ifl:property>
  </process>
</root>"""


def _make_rca_response(component_id, current_value, confidence=0.85):
    return json.dumps({
        "root_cause": "wrong address",
        "proposed_fix": "fix address",
        "confidence": confidence,
        "auto_apply": False,
        "error_type": "HTTP_CALL_FAILED",
        "affected_component": component_id,
        "property_to_change": "Address",
        "current_value": current_value,
        "correct_value": "http://new.example.com",
        "fixes": [{"component_id": component_id, "property_to_change": "Address",
                   "current_value": current_value, "correct_value": "http://new.example.com"}],
    })


def _make_msg(content):
    m = MagicMock()
    m.content = content
    return m


@pytest.mark.asyncio
async def test_rca_retries_on_component_mismatch():
    """When first RCA has a wrong component_id, agent must be called a second time."""
    invoke_calls = []

    async def _fake_invoke(payload, config=None):
        invoke_calls.append(payload)
        if len(invoke_calls) == 1:
            # First call: returns wrong component
            content = _make_rca_response("WRONG_COMPONENT", "http://nonexistent")
        else:
            # Retry: returns correct component
            content = _make_rca_response("ReceiverHTTP_1", "http://old.example.com")
        return {"messages": [_make_msg(content)]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = _fake_invoke
    mock_mcp = MagicMock()
    mock_mcp.agent = mock_agent
    mock_mcp.tools = []

    with patch("agents.rca_agent.get_similar_patterns", return_value=[]), \
         patch("agents.rca_agent.get_vector_store") as mock_vs, \
         patch("db.database.get_patterns_by_error_type", return_value=[]), \
         patch("db.database.log_agent_event"), \
         patch("agents.rca_agent.asyncio.gather", new=AsyncMock(return_value=[
             [], [], [],
             (MagicMock(
                 retrieve_relevant_notes=MagicMock(return_value=[]),
                 format_notes_for_prompt=MagicMock(return_value=""),
             ), []),
             GOOD_XML,
             "",
         ])):
        mock_vs.return_value.retrieve_relevant_notes.return_value = []
        mock_vs.return_value.format_notes_for_prompt.return_value = ""

        from agents.rca_agent import RCAAgent
        agent = RCAAgent(mock_mcp)
        agent._agent = mock_agent
        agent._current_error_type = "HTTP_CALL_FAILED"
        agent._current_iflow_id = "TestIflow"

        result = await agent.run_rca({
            "iflow_id": "TestIflow",
            "error_message": "Connection refused",
            "error_type": "HTTP_CALL_FAILED",
            "message_guid": "",
        })

    assert len(invoke_calls) == 2, f"Expected 2 invocations, got {len(invoke_calls)}"
    assert result["affected_component"] == "ReceiverHTTP_1"


@pytest.mark.asyncio
async def test_rca_no_retry_when_no_mismatch():
    """When first RCA has correct component, no retry should occur."""
    invoke_calls = []

    async def _fake_invoke(payload, config=None):
        invoke_calls.append(payload)
        return {"messages": [_make_msg(
            _make_rca_response("ReceiverHTTP_1", "http://old.example.com")
        )]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = _fake_invoke
    mock_mcp = MagicMock()
    mock_mcp.agent = mock_agent
    mock_mcp.tools = []

    with patch("agents.rca_agent.get_similar_patterns", return_value=[]), \
         patch("agents.rca_agent.get_vector_store") as mock_vs, \
         patch("db.database.get_patterns_by_error_type", return_value=[]), \
         patch("db.database.log_agent_event"), \
         patch("agents.rca_agent.asyncio.gather", new=AsyncMock(return_value=[
             [], [], [],
             (MagicMock(
                 retrieve_relevant_notes=MagicMock(return_value=[]),
                 format_notes_for_prompt=MagicMock(return_value=""),
             ), []),
             GOOD_XML,
             "",
         ])):
        mock_vs.return_value.retrieve_relevant_notes.return_value = []
        mock_vs.return_value.format_notes_for_prompt.return_value = ""

        from agents.rca_agent import RCAAgent
        agent = RCAAgent(mock_mcp)
        agent._agent = mock_agent
        agent._current_error_type = "HTTP_CALL_FAILED"
        agent._current_iflow_id = "TestIflow"

        result = await agent.run_rca({
            "iflow_id": "TestIflow",
            "error_message": "Connection refused",
            "error_type": "HTTP_CALL_FAILED",
            "message_guid": "",
        })

    assert len(invoke_calls) == 1, f"Expected 1 invocation, got {len(invoke_calls)}"
