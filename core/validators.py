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
import logging
import re
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

# Per-fix run state: stores original filepath + XML captured from get-iflow snapshot.
# Each asyncio Task inherits a copy so concurrent fixes don't interfere.
_fix_ctx: ContextVar[Optional[Dict[str, str]]] = ContextVar("_fix_ctx", default=None)


def _find_gateways_without_default(xml_str: str) -> set:
    """Return set of exclusiveGateway IDs that have no default route."""
    try:
        root = ET.fromstring(xml_str)
        all_gw_ids: set = set()
        default_source_ids: set = set()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "exclusiveGateway":
                all_gw_ids.add(elem.get("id", ""))
            if tag == "sequenceFlow":
                if elem.get("isDefault", "").lower() == "true":
                    default_source_ids.add(elem.get("sourceRef", ""))
        return all_gw_ids - default_source_ids
    except Exception:
        return set()


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
        return [f"NEW: Modified iFlow XML is not valid XML: {e}. Fix the XML before calling update-iflow."]

    # Parse original XML once — reused by all pre-existing issue checks below.
    orig_root: Optional[ET.Element] = None
    if original_xml:
        try:
            orig_root = ET.fromstring(original_xml)
        except ET.ParseError:
            pass

    # ── Check 1 — no ifl:property inside bpmn2:collaboration extensionElements ──
    # Pre-existing: if the same property keys were already at collaboration level
    # in the original iFlow, do not block — it is a design issue that pre-dates this fix.
    orig_collab_prop_keys: set = set()
    if orig_root is not None:
        orig_collab = orig_root.find(f"{{{_BPMN2}}}collaboration")
        if orig_collab is not None:
            orig_ext = orig_collab.find(f"{{{_BPMN2}}}extensionElements")
            if orig_ext is not None:
                for p in orig_ext.findall(f"{{{_IFL}}}property"):
                    orig_collab_prop_keys.add(
                        p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or "?"
                    )

    collab = mod_root.find(f"{{{_BPMN2}}}collaboration")
    if collab is not None:
        ext = collab.find(f"{{{_BPMN2}}}extensionElements")
        if ext is not None:
            bad_props = ext.findall(f"{{{_IFL}}}property")
            if bad_props:
                all_keys = [
                    p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or "?"
                    for p in bad_props
                ]
                new_keys = [k for k in all_keys if k not in orig_collab_prop_keys]
                if new_keys:
                    errors.append(
                        f"NEW: ifl:property elements found inside <bpmn2:collaboration> extensionElements "
                        f"(keys: {new_keys}). These MUST be placed inside the specific step that uses them "
                        f"(e.g. inside the <bpmn2:serviceTask> for the XPath or mapping step), "
                        f"NOT at collaboration level. Move them to the correct step."
                    )
                else:
                    logger.info(
                        "[Validator] Pre-existing issue ignored (existed in original): "
                        "ifl:property inside collaboration extensionElements (keys: %s)", all_keys
                    )

    # ── Check 2 — version attributes must not change from original ──
    # By definition a diff-based check: only fires when new_xml changed a version.
    if orig_root is not None:
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
                        f"NEW: Version changed for element '{el_id}': "
                        f"original='{orig_versions[el_id]}', submitted='{el_ver}'. "
                        f"Do not modify version attributes."
                    )

    # ── Check 3 — platform version caps on NEW elements (already diff-based) ──
    orig_ids: set = {el.get("id") for el in orig_root.iter() if el.get("id")} if orig_root is not None else set()
    _VERSION_CAPS: Dict[str, tuple[str, float]] = {
        "EndEvent":             ("EndEvent", 1.0),
        "ExceptionSubProcess":  ("ExceptionSubProcess", 1.1),
        "com.sap.soa.proxy.ws": ("SOAP adapter", 1.11),
        "SOAP":                 ("SOAP adapter", 1.11),
    }
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
                                f"NEW: New element '{el_id}' has version '{el_ver}' which exceeds "
                                f"the IFLMAP platform maximum for {cap_label} ({cap_max}). "
                                f"Set version='{cap_max}' or lower."
                            )
                    except ValueError:
                        pass

    # ── Check 4 — XPath expressions with namespace prefixes need 'declare namespace' ──
    # Pre-existing: skip if the same (key, value) pair already had this issue in original.
    orig_xpath_issues: set = set()
    if orig_root is not None:
        for el in orig_root.iter():
            k_el = el.find(f"{{{_IFL}}}key") or el.find("key")
            v_el = el.find(f"{{{_IFL}}}value") or el.find("value")
            if k_el is None or v_el is None:
                continue
            k = (k_el.text or "").lower()
            v = (v_el.text or "")
            if "xpath" in k or v.strip().startswith("//") or "//" in v:
                ns_uses = [
                    p for p in re.findall(r'\b([a-zA-Z][a-zA-Z0-9_]*):[a-zA-Z]', v)
                    if p.lower() not in ("http", "https", "urn", "xmlns")
                ]
                if ns_uses:
                    declared = re.findall(r'declare\s+namespace\s+([a-zA-Z][a-zA-Z0-9_]*)\s*=', v)
                    if [p for p in ns_uses if p not in declared]:
                        orig_xpath_issues.add((k_el.text or "", v))

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
                    fingerprint = (key_el.text or "", val)
                    if fingerprint in orig_xpath_issues:
                        logger.info(
                            "[Validator] Pre-existing issue ignored (existed in original): "
                            "XPath missing namespace declaration for property '%s'", key_el.text
                        )
                        continue
                    errors.append(
                        f"NEW: XPath expression in property '{key_el.text}' uses namespace prefix(es) "
                        f"{missing} but no 'declare namespace' directive found. "
                        f"Add inline declarations before the path, e.g.: "
                        f"declare namespace {missing[0]}='http://...'; //{missing[0]}:element"
                    )

    # ── Check 5 — Content Modifier: dynamic-looking values must NOT use srcType="Constant" ──
    # Pre-existing: skip if the same serviceTask had this issue in original.
    orig_dynamic_constant_tasks: set = set()
    if orig_root is not None:
        for task in orig_root.iter(f"{{{_BPMN2}}}serviceTask"):
            ext = task.find(f"{{{_BPMN2}}}extensionElements")
            if ext is None:
                continue
            kv: Dict[str, str] = {}
            for p in ext.findall(f"{{{_IFL}}}property"):
                k = p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or ""
                v = p.findtext(f"{{{_IFL}}}value") or p.findtext("value") or ""
                kv[k] = v
            if "headerName" in kv and kv.get("srcType", "") == "Constant":
                src_val = kv.get("srcValue", kv.get("expression", kv.get("value", "")))
                if "${" in src_val or src_val.startswith("//") or src_val.startswith("$.") or re.search(r'\$\{[^}]+\}', src_val):
                    orig_dynamic_constant_tasks.add(task.get("id", ""))

    for task in mod_root.iter(f"{{{_BPMN2}}}serviceTask"):
        ext = task.find(f"{{{_BPMN2}}}extensionElements")
        if ext is None:
            continue
        kv = {}
        for p in ext.findall(f"{{{_IFL}}}property"):
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
                task_id = task.get("id", "?")
                if task_id in orig_dynamic_constant_tasks:
                    logger.info(
                        "[Validator] Pre-existing issue ignored (existed in original): "
                        "Content Modifier step '%s' uses srcType=Constant with dynamic value", task_id
                    )
                    continue
                errors.append(
                    f"NEW: Content Modifier step '{task_id}' has a header row with "
                    f"srcType='Constant' but the value '{src_val[:80]}' looks like a dynamic expression. "
                    f"Change srcType to 'Expression' for dynamic values."
                )

    # ── Check 6 — every exclusiveGateway (CBR) must have a default route ──
    orig_bad_gateways = _find_gateways_without_default(original_xml or "")
    new_bad_gateways  = _find_gateways_without_default(modified_xml)
    truly_new_gw = new_bad_gateways - orig_bad_gateways
    for gw_id in truly_new_gw:
        errors.append(
            f"NEW: Content-Based Router (exclusiveGateway) '{gw_id}' has no default route. "
            f"Every router MUST have a default outgoing sequenceFlow with isDefault='true'. "
            f"Add a default route to prevent deployment failure."
        )
    for gw_id in new_bad_gateways & orig_bad_gateways:
        logger.info("[Validator] Pre-existing gateway issue ignored: %s", gw_id)

    # ── Check 7 — Groovy script references must use /script/<Name>.groovy ──
    # Pre-existing: skip if the same (key, value) pair was already wrong in original.
    orig_groovy_issues: set = set()
    if orig_root is not None:
        for el in orig_root.iter():
            k_el = el.find(f"{{{_IFL}}}key") or el.find("key")
            v_el = el.find(f"{{{_IFL}}}value") or el.find("value")
            if k_el is None or v_el is None:
                continue
            if "script" in (k_el.text or "").lower() and "src/main/resources" in (v_el.text or ""):
                orig_groovy_issues.add((k_el.text or "", v_el.text or ""))

    for el in mod_root.iter():
        key_el = el.find(f"{{{_IFL}}}key") or el.find("key")
        val_el = el.find(f"{{{_IFL}}}value") or el.find("value")
        if key_el is None or val_el is None:
            continue
        key = (key_el.text or "").lower()
        val = (val_el.text or "")
        if "script" in key and "src/main/resources" in val:
            fingerprint = (key_el.text or "", val)
            if fingerprint in orig_groovy_issues:
                logger.info(
                    "[Validator] Pre-existing issue ignored (existed in original): "
                    "Groovy script reference '%s' uses full archive path", val
                )
                continue
            errors.append(
                f"NEW: Groovy script reference '{val}' uses the full archive path. "
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
