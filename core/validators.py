"""
core/validators.py
==================
iFlow XML validator — runs as a pre-call guard before every update-iflow MCP call.

Exports:
  _fix_ctx                    — ContextVar holding per-fix {filepath, xml} snapshot
  _extract_iflow_file()       — parse get-iflow response → (filepath, xml)
  _check_iflow_xml()          — 7-rule structural checker on modified iFlow XML
  validate_before_update_iflow() — called by mcp_manager.execute() before update-iflow

The _fix_ctx ContextVar is set by FixAgent.execute_incident_fix() after capturing
the pre-fix iFlow snapshot, so all async tasks inherit their own copy.
"""

import json
import re
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from typing import Dict, List, Optional

_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

# Per-fix run state: stores original filepath + XML captured from get-iflow snapshot.
# Each asyncio Task inherits a copy so concurrent fixes don't interfere.
_fix_ctx: ContextVar[Optional[Dict[str, str]]] = ContextVar("_fix_ctx", default=None)


def _extract_iflow_file(snapshot_str: str) -> tuple[str, str]:
    """
    Parse a get-iflow response string and return (filepath, xml_content)
    for the .iflw file. Returns ("", "") if not found.
    """
    try:
        data  = json.loads(snapshot_str) if isinstance(snapshot_str, str) else snapshot_str
        files = data.get("files", []) if isinstance(data, dict) else []
        for f in files:
            fp = f.get("filepath", "")
            if fp.endswith(".iflw"):
                return fp, f.get("content", "")
    except Exception:
        pass
    # Fallback: regex scan for filepath key in raw text
    try:
        m = re.search(r'"filepath"\s*:\s*"([^"]+\.iflw)"', snapshot_str or "")
        if m:
            return m.group(1), ""
    except Exception:
        pass
    # Last resort: if the response itself is raw iFlow XML (starts with <), use it directly.
    stripped = (snapshot_str or "").strip()
    if stripped.startswith("<") and "bpmn" in stripped.lower():
        return "", stripped
    return "", ""


def _check_iflow_xml(original_xml: str, modified_xml: str) -> List[str]:
    """
    Structural checks on the modified iFlow XML.
    Returns a list of error strings (empty = valid).

    Checks:
      1. No ifl:property inside bpmn2:collaboration extensionElements
      2. Version attributes must not change from original
      3. Platform version caps on NEW elements (IFLMAP profile limits)
      4. XPath expressions with namespace prefixes must have 'declare namespace'
      5. Content Modifier header rows must use srcType="Expression" not "Constant"
      6. Every exclusiveGateway (CBR) must have a default route
      7. Groovy script references must use /script/<Name>.groovy format
    """
    errors: List[str] = []
    try:
        mod_root = ET.fromstring(modified_xml)
    except ET.ParseError as e:
        return [f"Modified iFlow XML is not valid XML: {e}. Fix the XML before calling update-iflow."]

    # ── Check 1 — no ifl:property inside bpmn2:collaboration extensionElements ──
    collab = mod_root.find(f"{{{_BPMN2}}}collaboration")
    if collab is not None:
        ext = collab.find(f"{{{_BPMN2}}}extensionElements")
        if ext is not None:
            bad_props = ext.findall(f"{{{_IFL}}}property")
            if bad_props:
                keys = [
                    (p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or "?")
                    for p in bad_props
                ]
                errors.append(
                    f"ifl:property elements found inside <bpmn2:collaboration> extensionElements "
                    f"(keys: {keys}). These MUST be placed inside the specific step that uses them "
                    f"(e.g. inside the <bpmn2:serviceTask> for the XPath or mapping step), "
                    f"NOT at collaboration level. Move them to the correct step."
                )

    # ── Check 2 — version attributes must not change from original ──
    if original_xml:
        try:
            orig_root = ET.fromstring(original_xml)
            orig_versions = {
                el.get("id"): el.get("version")
                for el in orig_root.iter()
                if el.get("id") and el.get("version")
            }
            for el in mod_root.iter():
                el_id  = el.get("id")
                el_ver = el.get("version")
                if el_id and el_ver and el_id in orig_versions:
                    if orig_versions[el_id] != el_ver:
                        errors.append(
                            f"Version changed for element '{el_id}': "
                            f"original='{orig_versions[el_id]}', submitted='{el_ver}'. "
                            f"Do not modify version attributes."
                        )
        except ET.ParseError:
            pass

    # ── Check 3 — platform version caps on NEW elements ──
    _VERSION_CAPS: Dict[str, tuple[str, float]] = {
        "EndEvent":            ("EndEvent", 1.0),
        "ExceptionSubProcess": ("ExceptionSubProcess", 1.1),
        "com.sap.soa.proxy.ws": ("SOAP adapter", 1.11),
        "SOAP":                ("SOAP adapter", 1.11),
    }
    orig_ids: set = set()
    if original_xml:
        try:
            orig_root2 = ET.fromstring(original_xml)
            orig_ids = {el.get("id") for el in orig_root2.iter() if el.get("id")}
        except ET.ParseError:
            pass
    for el in mod_root.iter():
        el_id  = el.get("id", "")
        el_ver = el.get("version", "")
        el_tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if el_id and el_id not in orig_ids and el_ver:
            for cap_key, (cap_label, cap_max) in _VERSION_CAPS.items():
                if cap_key in el_tag or cap_key in (el.get("name") or ""):
                    try:
                        if float(el_ver) > cap_max:
                            errors.append(
                                f"New element '{el_id}' has version '{el_ver}' which exceeds "
                                f"the IFLMAP platform maximum for {cap_label} ({cap_max}). "
                                f"Set version='{cap_max}' or lower."
                            )
                    except ValueError:
                        pass

    # ── Check 4 — XPath expressions with namespace prefixes need 'declare namespace' ──
    for el in mod_root.iter():
        key_el = el.find(f"{{{_IFL}}}key") or el.find("key")
        val_el = el.find(f"{{{_IFL}}}value") or el.find("value")
        if key_el is None or val_el is None:
            continue
        key = (key_el.text or "").lower()
        val = (val_el.text or "")
        if "xpath" in key or val.strip().startswith("//") or "//" in val:
            ns_uses = re.findall(r'\b([a-zA-Z][a-zA-Z0-9_]*):[a-zA-Z]', val)
            ns_uses = [p for p in ns_uses if p.lower() not in ("http", "https", "urn", "xmlns")]
            if ns_uses:
                declared = re.findall(r'declare\s+namespace\s+([a-zA-Z][a-zA-Z0-9_]*)\s*=', val)
                missing  = [p for p in ns_uses if p not in declared]
                if missing:
                    errors.append(
                        f"XPath expression in property '{key_el.text}' uses namespace prefix(es) "
                        f"{missing} but no 'declare namespace' directive found. "
                        f"Add inline declarations before the path, e.g.: "
                        f"declare namespace {missing[0]}='http://...'; //{missing[0]}:element"
                    )

    # ── Check 5 — Content Modifier: dynamic-looking values must NOT use srcType="Constant" ──
    # Note: srcType="Constant" is valid in SAP CPI for literal strings (e.g. "application/json").
    # Only flag when the value contains expression syntax (${...}, XPath //) but is marked Constant.
    for task in mod_root.iter(f"{{{_BPMN2}}}serviceTask"):
        ext = task.find(f"{{{_BPMN2}}}extensionElements")
        if ext is None:
            continue
        props = ext.findall(f"{{{_IFL}}}property")
        kv: Dict[str, str] = {}
        for p in props:
            k = p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or ""
            v = p.findtext(f"{{{_IFL}}}value") or p.findtext("value") or ""
            kv[k] = v
        if "headerName" in kv and kv.get("srcType", "") == "Constant":
            src_val = kv.get("srcValue", kv.get("expression", kv.get("value", "")))
            is_dynamic = (
                "${" in src_val
                or src_val.startswith("//")
                or src_val.startswith("$.")
                or re.search(r'\$\{[^}]+\}', src_val)
            )
            if is_dynamic:
                errors.append(
                    f"Content Modifier step '{task.get('id', '?')}' has a header row with "
                    f"srcType='Constant' but the value '{src_val[:80]}' looks like a dynamic expression. "
                    f"Change srcType to 'Expression' for dynamic values."
                )

    # ── Check 6 — every exclusiveGateway (CBR) must have a default route ──
    for gw in mod_root.iter(f"{{{_BPMN2}}}exclusiveGateway"):
        gw_id = gw.get("id", "")
        outgoing_ids = {sf.text.strip() for sf in gw.findall(f"{{{_BPMN2}}}outgoing") if sf.text}
        if not outgoing_ids:
            continue
        has_default = False
        for sf in mod_root.iter(f"{{{_BPMN2}}}sequenceFlow"):
            if sf.get("sourceRef") == gw_id and sf.get("isDefault", "").lower() == "true":
                has_default = True
                break
        if not has_default and len(outgoing_ids) > 1:
            errors.append(
                f"Content-Based Router (exclusiveGateway) '{gw_id}' has no default route. "
                f"Every router MUST have a default outgoing sequenceFlow with isDefault='true'. "
                f"Add a default route to prevent deployment failure."
            )

    # ── Check 7 — Groovy script references must use /script/<Name>.groovy ──
    for el in mod_root.iter():
        key_el = el.find(f"{{{_IFL}}}key") or el.find("key")
        val_el = el.find(f"{{{_IFL}}}value") or el.find("value")
        if key_el is None or val_el is None:
            continue
        key = (key_el.text or "").lower()
        val = (val_el.text or "")
        if "script" in key and "src/main/resources" in val:
            errors.append(
                f"Groovy script reference '{val}' uses the full archive path. "
                f"The model reference must be '/script/<FileName>.groovy' "
                f"(not 'src/main/resources/script/...'). Fix the value."
            )

    return errors


def validate_before_update_iflow(args: Dict) -> List[str]:
    """
    Validate update-iflow args against the per-fix context captured from get-iflow.
    Returns list of error strings. Empty list = valid, proceed with the real API call.
    Called by MultiMCP.execute() before every update-iflow tool call.
    """
    ctx = _fix_ctx.get()
    if ctx is None:
        return []  # no context set (manual / chat use) — skip validation

    errors: List[str] = []
    original_filepath = ctx.get("filepath", "")
    original_xml      = ctx.get("xml", "")

    # Parse the files argument (LLM may pass it as a JSON string or a list)
    files = args.get("files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            files = []

    submitted_filepath = ""
    submitted_xml      = ""
    if isinstance(files, list) and files:
        submitted_filepath = files[0].get("filepath", "") if isinstance(files[0], dict) else ""
        submitted_xml      = files[0].get("content", "")  if isinstance(files[0], dict) else ""
    elif args.get("content"):
        # update-iflow called with a direct content= field (e.g. rollback path, validate tool)
        submitted_xml = args["content"]

    # Check filepath matches original exactly
    if original_filepath and submitted_filepath and submitted_filepath != original_filepath:
        errors.append(
            f"Wrong filepath: you submitted '{submitted_filepath}' but the iFlow filepath "
            f"from get-iflow is '{original_filepath}'. "
            f"Use the EXACT filepath from the get-iflow response — do not guess or invent it."
        )

    # Run XML structural checks
    if submitted_xml:
        errors.extend(_check_iflow_xml(original_xml, submitted_xml))

    return errors
