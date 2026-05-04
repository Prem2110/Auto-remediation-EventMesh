"""
agents/fix_context.py
=====================
FixContext — the single shared contract threaded through FixPlanner,
FixGenerator, FixValidator, and FixApplier.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class FixContext:
    iflow_id: str
    error_type: str
    error_message: str
    proposed_fix: str
    root_cause: str
    affected_component: str
    original_xml: str
    original_filepath: str
    pattern_history: str
    sap_notes: str
    error_type_guidance: str
    sliced_xml: str = ""
    cross_pattern_text: str = ""
    message_guid: str = ""
    user_id: str = ""
    session_id: str = ""
    timestamp: str = ""
    reference_component_name: str = ""
    reference_component_xml: str = ""
    deploy_error_hint: str = ""
