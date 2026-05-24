"""
core/runtime_config.py
======================
Runtime-mutable configuration singleton.

Constants in core/constants.py are module-level values fixed at import time.
This singleton sits on top: at startup it loads DB overrides; agents/main.py
call cfg.get("KEY") instead of the bare constant, so operator changes via
PATCH /settings take effect immediately without a restart.

Usage:
    from core.runtime_config import cfg
    value = cfg.get("AUTO_FIX_CONFIDENCE")   # float, live
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS SCHEMA
# Each entry defines one tunable value.  "default" mirrors constants.py so
# this file is the single source of truth for the settings UI.
# ─────────────────────────────────────────────────────────────────────────────

SETTINGS_SCHEMA: list[dict] = [
    # ── Fix Behaviour ────────────────────────────────────────────────────────
    {
        "key":            "AUTO_FIX_CONFIDENCE",
        "type":           "float",
        "default":        0.90,
        "label":          "Auto-Fix Confidence Threshold",
        "description":    "Minimum RCA confidence score (0–1) required to apply a fix automatically without human approval.",
        "impact":         "high",
        "group":          "Fix Behaviour",
        "when_effective": "Next incident that reaches the fix-gate decision. Already-in-progress incidents are not affected.",
    },
    {
        "key":            "SUGGEST_FIX_CONFIDENCE",
        "type":           "float",
        "default":        0.70,
        "label":          "Suggest-Fix Confidence Threshold",
        "description":    "Minimum confidence score to queue a fix for human approval instead of discarding it.",
        "impact":         "high",
        "group":          "Fix Behaviour",
        "when_effective": "Next incident that reaches the fix-gate decision.",
    },
    {
        "key":            "AUTO_FIX_ALL_CPI_ERRORS",
        "type":           "bool",
        "default":        True,
        "label":          "Enable Autonomous Fixing",
        "description":    "Master switch. When off, the agent detects and diagnoses errors but never applies a fix automatically.",
        "impact":         "high",
        "group":          "Fix Behaviour",
        "when_effective": "Immediate — the in-memory RUNTIME_FLAGS flag is updated the moment you save.",
    },
    {
        "key":            "AUTO_DEPLOY_AFTER_FIX",
        "type":           "bool",
        "default":        True,
        "label":          "Auto-Deploy After Fix",
        "description":    "Automatically deploy the iFlow immediately after a successful update. Disable to stage fixes without deploying.",
        "impact":         "high",
        "group":          "Fix Behaviour",
        "when_effective": "Next fix that reaches the deploy step. Fixes already in the deploy stage are not affected.",
    },
    {
        "key":            "MAX_CONSECUTIVE_FAILURES",
        "type":           "int",
        "default":        5,
        "label":          "Circuit Breaker Threshold",
        "description":    "Number of consecutive fix failures on the same iFlow before the circuit breaker fires and escalates to a ticket.",
        "impact":         "high",
        "group":          "Fix Behaviour",
        "when_effective": "Next time a new error is received for an iFlow that has existing failures.",
    },
    {
        "key":            "MAX_RETRIES",
        "type":           "int",
        "default":        3,
        "label":          "MCP Tool Retry Attempts",
        "description":    "How many times the MCP client retries a failed tool call (update-iflow, deploy-iflow) before giving up.",
        "impact":         "medium",
        "group":          "Fix Behaviour",
        "when_effective": "Next MCP tool call that encounters an error.",
    },
    {
        "key":            "WEB_SEARCH_ENABLED",
        "type":           "bool",
        "default":        False,
        "label":          "Enable Web Search (DuckDuckGo)",
        "description":    "Allow the RCA and fix-generator agents to search the web via DuckDuckGo when SAP Notes and internal patterns are insufficient. Adds latency to each agent run.",
        "impact":         "medium",
        "group":          "Fix Behaviour",
        "when_effective": "Next agent invocation. In-progress RCA or fix runs are not affected.",
    },
    # ── Throughput ───────────────────────────────────────────────────────────
    {
        "key":            "FAILED_MESSAGE_FETCH_LIMIT",
        "type":           "int",
        "default":        100,
        "label":          "Failed Message Fetch Limit",
        "description":    "Maximum number of failed CPI messages fetched per poll cycle.",
        "impact":         "medium",
        "group":          "Throughput",
        "when_effective": "Next CPI poll cycle (default every 10 minutes).",
    },
    {
        "key":            "MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE",
        "type":           "int",
        "default":        25,
        "label":          "Max Unique Errors Per Cycle",
        "description":    "After deduplication, how many unique error groups enter the fix pipeline per cycle.",
        "impact":         "medium",
        "group":          "Throughput",
        "when_effective": "Next CPI poll cycle.",
    },
    {
        "key":            "RUNTIME_ERROR_DETAIL_CONCURRENCY",
        "type":           "int",
        "default":        8,
        "label":          "Detail Fetch Concurrency",
        "description":    "Number of parallel SAP OData calls made to enrich error details per cycle.",
        "impact":         "low",
        "group":          "Throughput",
        "when_effective": "Next CPI poll cycle.",
    },
    # ── Timing ───────────────────────────────────────────────────────────────
    {
        "key":            "BURST_DEDUP_WINDOW_SECONDS",
        "type":           "int",
        "default":        60,
        "label":          "Burst Dedup Window (seconds)",
        "description":    "Errors with the same iFlow + error type arriving within this window are collapsed into one incident.",
        "impact":         "medium",
        "group":          "Timing",
        "when_effective": "Next incoming error event.",
    },
    {
        "key":            "PENDING_APPROVAL_TIMEOUT_HRS",
        "type":           "int",
        "default":        24,
        "label":          "Approval Timeout (hours)",
        "description":    "How long an incident can sit in AWAITING_APPROVAL before being auto-escalated to a ticket.",
        "impact":         "medium",
        "group":          "Timing",
        "when_effective": "Next approval-timeout sweep (runs every 5 minutes). Already-timed-out incidents are checked on the next sweep.",
    },
    # ── Remediation Policies ─────────────────────────────────────────────────
    {
        "key":     "REMEDIATION_POLICIES",
        "type":    "json",
        "default": {
            "MAPPING_ERROR":        {"action": "AUTO_FIX",       "replay_after_fix": True},
            "DATA_VALIDATION":      {"action": "AUTO_FIX",       "replay_after_fix": True},
            "SSL_ERROR":            {"action": "TICKET_CREATED", "replay_after_fix": False},
            "AUTH_CONFIG_ERROR":    {"action": "AUTO_FIX",       "replay_after_fix": True},
            "AUTH_ERROR":           {"action": "APPROVAL",       "replay_after_fix": False},
            "CONNECTIVITY_ERROR":   {"action": "RETRY",          "replay_after_fix": True},
            "ADAPTER_CONFIG_ERROR": {"action": "AUTO_FIX",       "replay_after_fix": True},
            "BACKEND_ERROR":        {"action": "TICKET_CREATED", "replay_after_fix": False},
            "SFTP_ERROR":           {"action": "TICKET_CREATED", "replay_after_fix": False},
            "DUPLICATE_ERROR":      {"action": "TICKET_CREATED", "replay_after_fix": False},
            "PAYLOAD_SIZE_ERROR":   {"action": "TICKET_CREATED", "replay_after_fix": False},
            "UNKNOWN_ERROR":        {"action": "APPROVAL",       "replay_after_fix": False},
            "ODATA_ERROR":          {"action": "AUTO_FIX",       "replay_after_fix": True},
            "GROOVY_ERROR":         {"action": "AUTO_FIX",       "replay_after_fix": True},
            "SCRIPT_ERROR":         {"action": "AUTO_FIX",       "replay_after_fix": True},
            "SOAP_ERROR":           {"action": "AUTO_FIX",       "replay_after_fix": True},
            "ROUTING_ERROR":        {"action": "AUTO_FIX",       "replay_after_fix": True},
            "PROPERTY_ERROR":       {"action": "AUTO_FIX",       "replay_after_fix": True},
            "IDOC_ERROR":           {"action": "TICKET_CREATED", "replay_after_fix": False},
            "RESOURCE_ERROR":       {"action": "TICKET_CREATED", "replay_after_fix": False},
        },
        "label":          "Remediation Policies",
        "description":    "Per-error-type action: AUTO_FIX, APPROVAL, RETRY, or TICKET_CREATED. Also controls whether to replay the message after a fix.",
        "impact":         "high",
        "group":          "Remediation Policies",
        "when_effective": "Next fix-gate decision for any new or re-queued incident.",
    },
]

_SCHEMA_INDEX: dict[str, dict] = {s["key"]: s for s in SETTINGS_SCHEMA}


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

class _RuntimeConfig:
    """Thin wrapper around a dict of live overrides.

    Falls back to the schema default when no override exists.
    Thread-safe for reads; writes are protected by the GIL (single-process app).
    """

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    # ── read ─────────────────────────────────────────────────────────────────

    def get(self, key: str, fallback: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        schema = _SCHEMA_INDEX.get(key)
        if schema is not None:
            return schema["default"]
        return fallback

    # ── write (in-memory only) ────────────────────────────────────────────────

    def set(self, key: str, value: Any) -> None:
        self._overrides[key] = value

    # ── bulk load from DB ─────────────────────────────────────────────────────

    def load_from_db(self) -> None:
        try:
            from db.database import get_all_settings  # late import — avoids circular
            rows = get_all_settings()
            loaded = 0
            for row in rows:
                k    = row["setting_key"]
                raw  = row["setting_value"]
                dtype = row.get("data_type", "string")
                schema = _SCHEMA_INDEX.get(k)
                if schema is None:
                    continue
                try:
                    coerced = _coerce(raw, dtype)
                    # For REMEDIATION_POLICIES, merge stored value on top of the schema
                    # default so new error types added to the default are visible without
                    # requiring a manual Settings reset.
                    if k == "REMEDIATION_POLICIES" and isinstance(coerced, dict):
                        merged = dict(schema.get("default") or {})
                        merged.update(coerced)
                        coerced = merged
                    self._overrides[k] = coerced
                    loaded += 1
                except Exception as exc:
                    logger.warning("[RuntimeConfig] Could not coerce %s=%r as %s: %s", k, raw, dtype, exc)
            logger.info("[RuntimeConfig] Loaded %d settings from DB", loaded)
        except Exception as exc:
            logger.warning("[RuntimeConfig] load_from_db failed (using defaults): %s", exc)

    # ── snapshot for the API ──────────────────────────────────────────────────

    def all_with_schema(self) -> list[dict]:
        result = []
        for schema in SETTINGS_SCHEMA:
            k = schema["key"]
            result.append({
                **schema,
                "current_value": self.get(k),
                "is_overridden": k in self._overrides,
            })
        return result

    # ── reset a single key to its default ────────────────────────────────────

    def reset(self, key: str) -> None:
        self._overrides.pop(key, None)


cfg = _RuntimeConfig()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _coerce(raw: str, dtype: str) -> Any:
    if dtype == "int":
        return int(raw)
    if dtype == "float":
        return float(raw)
    if dtype == "bool":
        return raw.lower() in ("true", "1", "yes")
    if dtype == "json":
        return json.loads(raw)
    return raw
