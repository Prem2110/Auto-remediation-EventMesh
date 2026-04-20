"""
core/state.py
=============
Shared mutable state that must be visible to both the agent pipeline (writers)
and the FastAPI endpoint layer (readers).

Anything that changes during a fix run and needs to be polled via an API
endpoint belongs here.  Pure configuration constants go in core/constants.py.

Exports:
  FIX_PROGRESS          — {incident_id: {step, message, pct}} updated live
  get_fix_progress(id)  — safe reader used by /fix-progress endpoint
"""

import time
from typing import Any, Dict, Optional

from core.constants import FIX_PROGRESS_TTL_SECONDS, MAX_FIX_PROGRESS_ENTRIES

# ─────────────────────────────────────────────
# LIVE FIX PROGRESS
# ─────────────────────────────────────────────
# Written by OrchestratorAgent._set_progress() during each fix run.
# Read by GET /fix-progress?incident_id=... in main.py.
# Keys:   incident_id (str)
# Values: {"step": str, "message": str, "pct": int, ...}
FIX_PROGRESS: Dict[str, Dict[str, Any]] = {}


def cleanup_fix_progress(now: Optional[float] = None) -> None:
    """Remove stale fix-progress entries and cap the total in-memory entry count."""
    if not FIX_PROGRESS:
        return

    current = now if now is not None else time.time()
    expired_ids = []
    for incident_id, entry in FIX_PROGRESS.items():
        updated_epoch = entry.get("_updated_epoch")
        if updated_epoch is not None and current - float(updated_epoch) > FIX_PROGRESS_TTL_SECONDS:
            expired_ids.append(incident_id)

    for incident_id in expired_ids:
        FIX_PROGRESS.pop(incident_id, None)

    overflow = len(FIX_PROGRESS) - MAX_FIX_PROGRESS_ENTRIES
    if overflow > 0:
        oldest_ids = sorted(
            FIX_PROGRESS,
            key=lambda incident_id: float(FIX_PROGRESS[incident_id].get("_updated_epoch", 0.0)),
        )[:overflow]
        for incident_id in oldest_ids:
            FIX_PROGRESS.pop(incident_id, None)


def get_fix_progress(incident_id: str) -> Optional[Dict[str, Any]]:
    """Return the current progress snapshot for incident_id, or None."""
    cleanup_fix_progress()
    return FIX_PROGRESS.get(incident_id)
