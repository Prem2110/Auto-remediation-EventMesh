import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_verifier_build_agent_includes_message_log_tool():
    """After build_agent, the verifier tool list must include a message-log tool."""
    log_tool = MagicMock()
    log_tool.server = "integration_suite"
    log_tool.name = "get_message_logs"
    log_tool.mcp_tool_name = "get_message_logs"
    log_tool.description = "Retrieve message processing logs"

    test_tool = MagicMock()
    test_tool.server = "mcp_testing"
    test_tool.name = "test_iflow_with_payload"
    test_tool.mcp_tool_name = "test_iflow_with_payload"
    test_tool.description = "Test iFlow with payload"

    get_iflow_tool = MagicMock()
    get_iflow_tool.server = "integration_suite"
    get_iflow_tool.name = "get_iflow"
    get_iflow_tool.mcp_tool_name = "get-iflow"
    get_iflow_tool.description = "Get iFlow configuration"

    captured_tools = []

    async def _fake_build_agent(tools, system_prompt, **kwargs):
        captured_tools.extend(tools)
        return MagicMock()

    mock_mcp = MagicMock()
    mock_mcp.tools = [log_tool, test_tool, get_iflow_tool]
    mock_mcp.build_agent = _fake_build_agent

    from agents.verifier_agent import VerifierAgent
    verifier = VerifierAgent(mock_mcp)
    await verifier.build_agent()

    tool_names = [
        getattr(t, "mcp_tool_name", None) or getattr(t, "name", "") or str(t)
        for t in captured_tools
    ]
    assert any(
        "message_log" in n.lower() or "message-log" in n.lower()
        for n in tool_names
    ), f"get_message_logs not found in tool list: {tool_names}"


@pytest.mark.asyncio
async def test_verifier_system_prompt_mentions_message_log():
    """The verifier system prompt must reference get_message_logs or message log check."""
    captured_prompts = []

    async def _fake_build_agent(tools, system_prompt, **kwargs):
        captured_prompts.append(system_prompt)
        return MagicMock()

    mock_mcp = MagicMock()
    mock_mcp.tools = []
    mock_mcp.build_agent = _fake_build_agent

    from agents.verifier_agent import VerifierAgent
    verifier = VerifierAgent(mock_mcp)
    await verifier.build_agent()

    assert captured_prompts, "build_agent was not called"
    prompt = captured_prompts[0]
    assert "message_log" in prompt.lower() or "message log" in prompt.lower(), \
        "system_prompt does not mention message log check"
