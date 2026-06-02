"""
calm/models.py
==============
Pydantic v2 models for SAP Cloud ALM REST API payloads.
No Any types — all fields are explicitly typed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


class CALMExceptionStatus(StrEnum):
    FAILED     = "FAILED"
    RETRYING   = "RETRYING"
    COMPLETED  = "COMPLETED"
    ESCALATED  = "ESCALATED"
    ERROR      = "ERROR"


class CALMAlertStatus(StrEnum):
    OPEN        = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED    = "RESOLVED"
    CLOSED      = "CLOSED"


class CALMException(BaseModel):
    """
    One failed message exception from CALM Integration & Exception Monitoring.
    Field names match GET /api/calm-intm/v1/exceptions response.

    NOTE: Run scripts/discover_calm_managed_objects.py first to verify
    field names against your actual CALM tenant. Field names may vary
    by CALM release.
    """
    id:              str
    status:          str                          # not enforced — CALM may return other values
    technicalName:   str           = ""           # iFlow technical name
    managedObjectId: str           = ""           # CALM managed object ID (CPI_SBX_01 internal ID)
    errorMessage:    str           = ""
    errorCategory:   Optional[str] = None         # CONNECTIVITY, MAPPING, SECURITY, CONFIGURATION, PROCESSING
    messageId:       Optional[str] = None         # CPI MessageGuid (verify field name with discovery script)
    correlationId:   Optional[str] = None
    startTime:       Optional[datetime] = None
    endTime:         Optional[datetime] = None
    sender:          Optional[str] = None
    receiver:        Optional[str] = None
    alertId:         Optional[str] = None         # parent alert in calm-alert

    model_config = {"extra": "allow"}             # accept extra fields from API


class CALMAlert(BaseModel):
    """Alert from CALM Alert Management — GET /api/calm-alert/v1/alerts/{id}."""
    id:           str
    status:       str
    category:     Optional[str]   = None
    title:        Optional[str]   = None
    description:  Optional[str]   = None
    createdAt:    Optional[datetime] = None
    updatedAt:    Optional[datetime] = None
    assignedTo:   Optional[str]   = None
    exceptionIds: list[str]        = Field(default_factory=list)

    model_config = {"extra": "allow"}


class CALMWebhookPayload(BaseModel):
    """
    Inbound payload from CALM Action Plan HTTP notification.
    Shape depends on how the Action Plan is configured in CALM UI.
    Run scripts/discover_calm_managed_objects.py and inspect a test delivery
    to confirm the exact field names your CALM tenant sends.
    """
    alertId:      str             = ""
    alertStatus:  str             = ""
    eventType:    str             = ""            # "ALERT_CREATED", "ALERT_UPDATED", etc.
    tenantId:     str             = ""
    timestamp:    Optional[datetime] = None
    exceptionIds: list[str]        = Field(default_factory=list)

    model_config = {"extra": "allow"}             # accept any extra fields CALM sends


class CALMAlertPatch(BaseModel):
    """PATCH body for /api/calm-alert/v1/alerts/{id}."""
    status:     Optional[CALMAlertStatus] = None
    assignedTo: Optional[str]             = None
    comment:    Optional[str]             = None