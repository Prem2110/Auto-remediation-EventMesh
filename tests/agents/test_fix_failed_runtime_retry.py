import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_new_error_after_fix_creates_new_incident():
    """If runtime error type differs from original, a new incident must be created."""
    created_incidents = []

    def _fake_create(data):
        created_incidents.append(data)
        return "new-incident-id"

    with patch("agents.orchestrator_agent.create_incident", side_effect=_fake_create):
        from agents.orchestrator_agent import _maybe_requeue_runtime_failure

        result = await _maybe_requeue_runtime_failure(
            original_incident={
                "incident_id": "orig-id",
                "iflow_id":    "iflow_A",
                "error_type":  "HTTP_CALL_FAILED",
                "message_guid": "guid1",
            },
            runtime_error_type="AUTH_CONFIG_ERROR",
            runtime_error_msg="401 Unauthorized — credential alias not found",
        )

    assert result is True
    assert len(created_incidents) == 1
    assert created_incidents[0]["error_type"] == "AUTH_CONFIG_ERROR"
    assert created_incidents[0]["iflow_id"] == "iflow_A"
    assert "incident_id" in created_incidents[0]
    assert "created_at" in created_incidents[0]
    assert "parent_incident_id" not in created_incidents[0]


@pytest.mark.asyncio
async def test_same_error_type_does_not_requeue():
    """If runtime error type matches original, no new incident is created."""
    created_incidents = []

    with patch("agents.orchestrator_agent.create_incident", side_effect=lambda d: created_incidents.append(d)):
        from agents.orchestrator_agent import _maybe_requeue_runtime_failure

        result = await _maybe_requeue_runtime_failure(
            original_incident={
                "incident_id": "orig-id",
                "iflow_id":    "iflow_A",
                "error_type":  "HTTP_CALL_FAILED",
            },
            runtime_error_type="HTTP_CALL_FAILED",
            runtime_error_msg="Connection refused",
        )

    assert result is False
    assert len(created_incidents) == 0


@pytest.mark.asyncio
async def test_empty_runtime_error_does_not_requeue():
    """If runtime error msg is empty, no new incident is created."""
    created_incidents = []

    with patch("agents.orchestrator_agent.create_incident", side_effect=lambda d: created_incidents.append(d)):
        from agents.orchestrator_agent import _maybe_requeue_runtime_failure

        result = await _maybe_requeue_runtime_failure(
            original_incident={"incident_id": "x", "iflow_id": "y", "error_type": "HTTP_CALL_FAILED"},
            runtime_error_type="AUTH_CONFIG_ERROR",
            runtime_error_msg="",
        )

    assert result is False
    assert len(created_incidents) == 0
