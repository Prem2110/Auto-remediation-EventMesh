"""
core/xml_patcher.py
===================
Programmatic iFlow XML patcher for structured fix operations.

Instead of letting the LLM rewrite free-form XML, the LLM describes WHAT to change
as a structured operation dict. This module applies that change surgically using the
same stdlib ET and namespace constants as validators.py.

Supported change_type values:
    update_property   — change an existing <ifl:property> value by key name
    update_expression — alias for update_property (XPath / Groovy expressions are properties)
    add_property      — add a new <ifl:property> inside extensionElements
    update_attribute  — change an XML attribute on the target element itself
    structural        — signals the caller to use the free-XML fallback (not applied here)

Exports:
    PatcherError
    apply_fix_operation(iflow_xml, operation)  -> patched XML str
    apply_fix_operations(iflow_xml, operations) -> patched XML str (rolls back on error)
"""

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# SAP CPI iFlow XML namespaces — identical to validators.py
_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

# Register prefixes so ET serialises them as bpmn2:/ifl: not ns0:/ns1:
ET.register_namespace("bpmn2", _BPMN2)
ET.register_namespace("ifl",   _IFL)


class PatcherError(Exception):
    """Raised when a structured fix operation cannot be applied."""


# ── internal helpers ──────────────────────────────────────────────────────────

def _find_by_id(root: ET.Element, component_id: str) -> ET.Element | None:
    for elem in root.iter():
        if elem.get("id") == component_id:
            return elem
    return None


def _find_extension_elements(target: ET.Element) -> ET.Element | None:
    return target.find(f"{{{_BPMN2}}}extensionElements")


def _find_property(ext: ET.Element, field: str):
    """
    Find the <ifl:property> whose <ifl:key> text matches field.
    Returns (prop_elem, value_elem) or (None, None).
    """
    if ext is None:
        return None, None
    for prop in ext.findall(f"{{{_IFL}}}property"):
        key_text = (
            prop.findtext(f"{{{_IFL}}}key")
            or prop.findtext("key")
            or ""
        )
        if key_text == field:
            val_elem = prop.find(f"{{{_IFL}}}value") or prop.find("value")
            return prop, val_elem
    return None, None


# ── public API ────────────────────────────────────────────────────────────────

def apply_fix_operation(iflow_xml: str, operation: Dict[str, Any]) -> str:
    """
    Apply a single structured fix operation to iFlow XML.
    Returns the patched XML string.
    Raises PatcherError on any failure so FixAgent can fall back to free XML.
    """
    change_type      = (operation.get("change_type") or "").strip()
    target_component = (operation.get("target_component") or "").strip()
    field            = (operation.get("field") or "").strip()
    new_value        = str(operation.get("new_value") or "")
    old_value        = str(operation.get("old_value") or "")

    if change_type == "structural":
        raise PatcherError(
            f"change_type=structural — structural changes require the free-XML fallback. "
            f"Reason: {operation.get('reason', 'not specified')}"
        )
    if not change_type:
        raise PatcherError("operation missing change_type.")
    if not target_component:
        raise PatcherError("operation missing target_component.")
    if not field and change_type != "update_attribute":
        raise PatcherError("operation missing field.")

    try:
        root = ET.fromstring(iflow_xml.encode("utf-8"))
    except ET.ParseError as exc:
        raise PatcherError(f"iFlow XML is not parseable: {exc}") from exc

    target = _find_by_id(root, target_component)
    if target is None:
        raise PatcherError(
            f"Component id='{target_component}' not found in iFlow XML. "
            "Verify the RCA returned the correct step ID."
        )

    # ── update_property / update_expression ──────────────────────────────
    if change_type in ("update_property", "update_expression"):
        ext = _find_extension_elements(target)
        if ext is None:
            raise PatcherError(
                f"Component '{target_component}' has no extensionElements — "
                f"cannot update property '{field}'."
            )
        prop, val_elem = _find_property(ext, field)
        if prop is None:
            raise PatcherError(
                f"Property key='{field}' not found in '{target_component}'. "
                "Use change_type='add_property' to create it."
            )
        current = (val_elem.text if val_elem is not None else "") or ""
        if old_value and current != old_value:
            raise PatcherError(
                f"Property '{field}' old_value mismatch: "
                f"expected '{old_value[:120]}', found '{current[:120]}'"
            )
        if val_elem is None:
            val_elem = ET.SubElement(prop, f"{{{_IFL}}}value")
        val_elem.text = new_value
        logger.info(
            "[Patcher] %s '%s' on '%s': '%s' → '%s'",
            change_type, field, target_component,
            (old_value or current)[:80], new_value[:80],
        )

    # ── add_property ──────────────────────────────────────────────────────
    elif change_type == "add_property":
        ext = _find_extension_elements(target)
        if ext is None:
            raise PatcherError(
                f"Component '{target_component}' has no extensionElements."
            )
        existing, _ = _find_property(ext, field)
        if existing is not None:
            raise PatcherError(
                f"Property '{field}' already exists in '{target_component}'. "
                "Use change_type='update_property' instead."
            )
        new_prop    = ET.SubElement(ext, f"{{{_IFL}}}property")
        key_el      = ET.SubElement(new_prop, f"{{{_IFL}}}key")
        val_el      = ET.SubElement(new_prop, f"{{{_IFL}}}value")
        key_el.text = field
        val_el.text = new_value
        logger.info(
            "[Patcher] Added property '%s'='%s' to '%s'",
            field, new_value[:80], target_component,
        )

    # ── update_attribute ─────────────────────────────────────────────────
    elif change_type == "update_attribute":
        current = target.get(field, "")
        if old_value and current != old_value:
            raise PatcherError(
                f"Attribute '{field}' old_value mismatch: "
                f"expected '{old_value}', found '{current}'"
            )
        target.set(field, new_value)
        logger.info(
            "[Patcher] Updated attribute '%s' on '%s': '%s' → '%s'",
            field, target_component, current, new_value,
        )

    else:
        raise PatcherError(
            f"Unknown change_type='{change_type}'. "
            "Valid: update_property, add_property, update_attribute, update_expression, structural."
        )

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def apply_fix_operations(iflow_xml: str, operations: List[Dict[str, Any]]) -> str:
    """
    Apply a list of fix operations sequentially.
    Rolls back to the original XML if any operation fails.
    """
    current = iflow_xml
    for i, op in enumerate(operations):
        try:
            current = apply_fix_operation(current, op)
        except PatcherError as exc:
            raise PatcherError(
                f"Operation {i + 1}/{len(operations)} failed: {exc}. "
                "All changes rolled back to original."
            ) from exc
    return current
