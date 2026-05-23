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
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

# Global iFlow properties that live at collaboration level by design — never flag these.
ALLOWED_COLLABORATION_KEYS: frozenset = frozenset({
    "namespaceMapping",
    "httpSessionHandling",
    "accessControlMaxAge",
    "returnExceptionToSender",
    "log",
    "corsEnabled",
    "exposedHeaders",
    "componentVersion",
    "allowedHeaderList",
    "ServerTrace",
    "allowedOrigins",
    "accessControlAllowCredentials",
    "allowedHeaders",
    "allowedMethods",
    "cmdVariantUri",
})

import threading as _threading

# Process-global store: iflow_id → {"filepath": str, "xml": str}
# Replaces ContextVar which does not propagate into LangChain agent sub-tasks.
_fix_ctx_store: Dict[str, Dict[str, str]] = {}
_fix_ctx_lock  = _threading.Lock()

def _fix_ctx_set(iflow_id: str, filepath: str, xml: str) -> None:
    with _fix_ctx_lock:
        _fix_ctx_store[iflow_id] = {"filepath": filepath, "xml": xml}
    logger.debug("[ValidatorCtx] set: iflow=%s filepath=%s xml_len=%d", iflow_id, filepath, len(xml))

def _fix_ctx_get(iflow_id: str) -> Optional[Dict[str, str]]:
    with _fix_ctx_lock:
        return _fix_ctx_store.get(iflow_id)

def _fix_ctx_clear(iflow_id: str) -> None:
    with _fix_ctx_lock:
        _fix_ctx_store.pop(iflow_id, None)
    logger.debug("[ValidatorCtx] cleared: iflow=%s", iflow_id)

# Legacy shim — keeps any remaining .set()/.get() calls working without crashing
class _FixCtxShim:
    _current_iflow: Dict = {}

    def set(self, value: Optional[Dict[str, str]]) -> None:
        if value and value.get("filepath"):
            iflow_id = value["filepath"].split("/")[0] if "/" in value.get("filepath","") else ""
            if iflow_id:
                _fix_ctx_set(iflow_id, value["filepath"], value.get("xml", ""))

    def get(self) -> Optional[Dict[str, str]]:
        return None  # shim only — real lookup is done by iFlow ID in validate_iflow_xml

_fix_ctx = _FixCtxShim()


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
        missing = all_gw_ids - default_source_ids
        logger.debug(
            "[Validator] gateway check: total_gateways=%d missing_default=%d ids=%s",
            len(all_gw_ids), len(missing), sorted(missing),
        )
        return missing
    except Exception as exc:
        logger.warning(
            "[Validator] _find_gateways_without_default failed — returning empty set "
            "(gateway regression checks will be skipped): %s", exc
        )
        return set()


def _iter_concatenated_package_files(snapshot_str: str) -> Iterator[Tuple[str, str]]:
    """
    Yield (filepath, content) tuples from a concatenated integration package response.

    The integration_suite `get-iflow` tool may return the entire integration package
    as a single concatenated string with blocks delimited by:
      ---begin-of-file--- ... ---end-of-file---

    The *main* iFlow XML lives under:
      src/main/resources/scenarioflows/integrationflow/<iflow_id>.iflw
    """

    def _safe_str(value) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value.lstrip("\ufeff")
        return str(value) if value is not None else ""

    text = _safe_str(snapshot_str)
    if not text:
        return

    # Unwrap {"type":"success","iflowContent":"..."} envelope returned by get-iflow MCP tool.
    # The iflowContent value holds the actual concatenated file listing with ---begin/end-of-file--- markers.
    if text.lstrip().startswith("{"):
        try:
            _parsed = json.loads(text)
            if isinstance(_parsed, dict) and "iflowContent" in _parsed:
                text = _safe_str(_parsed["iflowContent"])
        except (json.JSONDecodeError, ValueError):
            pass

    begin = "---begin-of-file---"
    end = "---end-of-file---"

    # Common tool format: a filepath line immediately precedes the begin marker:
    #   /home/vcap/app/temp/<...>/src/main/resources/.../Foo.iflw
    #   ---begin-of-file---
    #   <file content...>
    #   ---end-of-file---
    # Use a regex so paths with spaces/special chars are handled correctly.
    try:
        pattern = re.compile(
            r"(?ms)(?P<path>[^\r\n]+)\r?\n"
            + re.escape(begin)
            + r"\r?\n(?P<content>.*?)\r?\n"
            + re.escape(end),
        )
        matched_any = False
        for m in pattern.finditer(text):
            matched_any = True
            fp = (m.group("path") or "").strip().replace("\\", "/")
            content = (m.group("content") or "").strip("\r\n")
            if fp:
                yield fp, content
        if matched_any:
            return
    except Exception:
        # Fall through to the legacy scanner below.
        pass
    idx = 0
    while True:
        b = text.find(begin, idx)
        if b == -1:
            break
        block_start = b + len(begin)
        e = text.find(end, block_start)
        if e == -1:
            break

        raw_block = text[block_start:e]

        # Try to detect the filepath from either:
        #  1) the first non-empty line (common format; may include prefixes like /home/... or paths with spaces), or
        #  2) a regex scan over the block header.
        lines = raw_block.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)

        filepath = ""
        if lines:
            first = lines[0].strip().replace("\\", "/")
            # Treat the first line as a path if it looks like one (most tool outputs use this).
            # Do NOT split on spaces: paths may contain spaces/special characters.
            looks_like_path = (
                first.startswith(("src/", "/")) or
                re.match(r"^[A-Za-z]:/", first) or
                (".iflw" in first.lower()) or
                ("scenarioflows/" in first.lower()) or
                ("src/main/resources/" in first.lower())
            )
            if looks_like_path:
                filepath = first
                lines = lines[1:]

        if not filepath:
            header_probe = raw_block[:800]
            # Fallback: any single-line path containing scenarioflows/... or src/main/resources/...
            m = re.search(
                r"([^\r\n]*?(?:scenarioflows/integrationflow|src/main/resources)[^\r\n]*)",
                header_probe,
                flags=re.IGNORECASE,
            )
            filepath = (m.group(1).strip() if m else "").replace("\\", "/")

        content = "\n".join(lines).strip("\r\n")
        if filepath:
            yield filepath, content

        idx = e + len(end)


def _extract_iflow_file(snapshot_str: str, iflow_id: str = "") -> tuple[str, str]:
    """
    Parse a get-iflow response string and return (filepath, xml_content)
    for the .iflw file. Returns ("", "") if not found.

    Always returns the XML content as a clean UTF-8 str (never bytes).
    """
    def _safe_str(value) -> str:
        """Decode bytes → UTF-8 and strip BOM; pass str through unchanged."""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value.lstrip("\ufeff")
        return str(value) if value is not None else ""

    # Preferred: concatenated integration package response (BEGIN/END markers)
    try:
        found_paths: List[str] = []
        candidates: List[Tuple[str, str]] = []
        want_suffix = f"{iflow_id}.iflw".lower() if iflow_id else ""
        want_tail = f"scenarioflows/integrationflow/{iflow_id}.iflw".lower() if iflow_id else ""

        for fp, content in _iter_concatenated_package_files(snapshot_str):
            fp_norm = (fp or "").strip().replace("\\", "/")
            if fp_norm:
                found_paths.append(fp_norm)

            fp_low = fp_norm.lower()
            if fp_low.endswith(".iflw") and ("scenarioflows/integrationflow/" in fp_low):
                candidates.append((fp_norm, content))

        if candidates:
            if iflow_id:
                for fp, content in candidates:
                    # Don't require an exact root prefix; just match the filename suffix.
                    fp_l = fp.lower()
                    if want_tail and fp_l.endswith(want_tail):
                        extracted = _safe_str(content).lstrip("\ufeff").strip()
                        if not extracted.startswith("<?xml"):
                            logger.warning(
                                "[ValidatorCtx] _extract_iflow_file: matched path but extracted content does not start "
                                "with '<?xml' (filepath=%s, preview=%r)",
                                fp,
                                extracted[:120],
                            )
                            return fp, ""
                        return fp, extracted
                    if fp_l.endswith("/" + want_suffix) or fp_l.endswith("\\" + want_suffix) or fp_l.endswith(want_suffix):
                        logger.debug(
                            "[ValidatorCtx] _extract_iflow_file: extracted .iflw from concatenated package, filepath=%s",
                            fp,
                        )
                        extracted = _safe_str(content).lstrip("\ufeff").strip()
                        if not extracted.startswith("<?xml"):
                            logger.warning(
                                "[ValidatorCtx] _extract_iflow_file: extracted .iflw does not start with '<?xml' "
                                "(filepath=%s, preview=%r)",
                                fp,
                                extracted[:120],
                            )
                            return fp, ""
                        return fp, extracted

            if len(candidates) == 1:
                fp, content = candidates[0]
                logger.debug(
                    "[ValidatorCtx] _extract_iflow_file: extracted sole integrationflow .iflw from concatenated package, filepath=%s",
                    fp,
                )
                extracted = _safe_str(content).lstrip("\ufeff").strip()
                if not extracted.startswith("<?xml"):
                    logger.warning(
                        "[ValidatorCtx] _extract_iflow_file: extracted sole integrationflow .iflw does not start with '<?xml' "
                        "(filepath=%s, preview=%r)",
                        fp,
                        extracted[:120],
                    )
                    return fp, ""
                return fp, extracted

            for fp, content in candidates:
                if (content or "").strip():
                    logger.debug(
                        "[ValidatorCtx] _extract_iflow_file: extracted first non-empty integrationflow .iflw from concatenated package, filepath=%s",
                        fp,
                    )
                    extracted = _safe_str(content).lstrip("\ufeff").strip()
                    if not extracted.startswith("<?xml"):
                        logger.warning(
                            "[ValidatorCtx] _extract_iflow_file: extracted integrationflow .iflw does not start with '<?xml' "
                            "(filepath=%s, preview=%r)",
                            fp,
                            extracted[:120],
                        )
                        return fp, ""
                    return fp, extracted

        # If markers exist but we couldn't match, print the discovered filepaths to aid debugging.
        if ("---begin-of-file---" in (snapshot_str or "")) and found_paths:
            logger.debug(
                "[ValidatorCtx] _extract_iflow_file: concatenated package paths discovered (%d): %s",
                len(found_paths),
                found_paths,
            )
    except Exception as exc:
        logger.debug("[ValidatorCtx] _extract_iflow_file concatenated parse failed: %s", exc)

    try:
        data  = json.loads(snapshot_str) if isinstance(snapshot_str, str) else snapshot_str
        files = data.get("files", []) if isinstance(data, dict) else []
        any_iflw: List[Tuple[str, str]] = []
        for f in files:
            fp = f.get("filepath", "")
            fp_norm = (fp or "").replace("\\", "/")
            if fp_norm.endswith(".iflw"):
                # Prefer canonical iFlow location and specific iFlow ID when known.
                fp_low = fp_norm.lower()
                if "scenarioflows/integrationflow/" in fp_low:
                    if (not iflow_id) or fp_low.endswith(want_tail or f"/{iflow_id}.iflw".lower()):
                        logger.debug(
                            "[ValidatorCtx] _extract_iflow_file: found integrationflow .iflw via JSON parse, filepath=%s",
                            fp_norm,
                        )
                        extracted = _safe_str(f.get("content", "")).lstrip("\ufeff").strip()
                        if not extracted.startswith("<?xml"):
                            logger.warning(
                                "[ValidatorCtx] _extract_iflow_file: JSON .iflw content does not start with '<?xml' "
                                "(filepath=%s, preview=%r)",
                                fp_norm,
                                extracted[:120],
                            )
                            return fp_norm, ""
                        return fp_norm, extracted
                any_iflw.append((fp_norm, _safe_str(f.get("content", ""))))

        if any_iflw:
            if iflow_id:
                for fp_norm, content in any_iflw:
                    if fp_norm.endswith(f"/{iflow_id}.iflw") or fp_norm.endswith(f"{iflow_id}.iflw"):
                        logger.debug(
                            "[ValidatorCtx] _extract_iflow_file: found .iflw via JSON parse (non-canonical path), filepath=%s",
                            fp_norm,
                        )
                        return fp_norm, content
            fp_norm, content = any_iflw[0]
            logger.debug(
                "[ValidatorCtx] _extract_iflow_file: found first .iflw via JSON parse (non-canonical path), filepath=%s",
                fp_norm,
            )
            return fp_norm, content
    except Exception as exc:
        logger.debug("[ValidatorCtx] _extract_iflow_file JSON parse failed: %s", exc)
    # Fallback: regex scan for filepath key in raw text
    try:
        m = re.search(
            r'"filepath"\s*:\s*"(src/main/resources/scenarioflows/integrationflow/[^"]+\.iflw)"',
            snapshot_str or "",
        )
        if m and ((not iflow_id) or m.group(1).replace("\\", "/").endswith(f"/{iflow_id}.iflw")):
            logger.debug(
                "[ValidatorCtx] _extract_iflow_file: found .iflw via regex fallback, filepath=%s",
                m.group(1),
            )
            return m.group(1).replace("\\", "/"), ""

        # Broader regex fallback (legacy tool outputs) if canonical path was not present.
        m2 = re.search(r'"filepath"\s*:\s*"([^"]+\.iflw)"', snapshot_str or "")
        if m2 and ((not iflow_id) or m2.group(1).replace("\\", "/").endswith(f"{iflow_id}.iflw")):
            logger.debug(
                "[ValidatorCtx] _extract_iflow_file: found .iflw via broad regex fallback, filepath=%s",
                m2.group(1),
            )
            return m2.group(1).replace("\\", "/"), ""
    except Exception as exc:
        logger.debug("[ValidatorCtx] _extract_iflow_file regex fallback failed: %s", exc)
    # Last resort: if the response itself is raw iFlow XML (starts with <), use it directly.
    stripped = _safe_str(snapshot_str).strip()
    if stripped.startswith("<") and "bpmn" in stripped.lower():
        logger.debug("[ValidatorCtx] _extract_iflow_file: treating raw XML response as iFlow content")
        return "", stripped
    logger.warning("[ValidatorCtx] _extract_iflow_file: could not locate .iflw file in response")
    return "", ""


def _normalize_iflow_filepath(filepath: str) -> str:
    """
    Convert an absolute CF container path to the relative path expected by update-iflow.

    get-iflow returns absolute paths (parseFolder outputs full filesystem paths):
      /home/vcap/app/temp/<uuid>/src/main/resources/scenarioflows/integrationflow/X.iflw

    update-iflow's patchFile() does path.join(newTempFolder, filepath).
    In Node.js, when filepath is absolute, path.join ignores the base and returns the
    absolute path unchanged — which points to the OLD (now-deleted) temp folder.

    We strip the prefix so update-iflow receives:
      src/main/resources/scenarioflows/integrationflow/X.iflw
    """
    if not filepath:
        return filepath
    fp = filepath.replace("\\", "/")
    for anchor in ("src/main/resources/", "src/", "resources/"):
        idx = fp.find(anchor)
        if idx >= 0:
            return fp[idx:]
    return fp


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
        err_line = e.position[0] if (hasattr(e, "position") and e.position) else 0
        if err_line:
            xml_lines = modified_xml.splitlines()
            start = max(0, err_line - 4)
            end   = min(len(xml_lines), err_line + 2)
            ctx   = "\n".join(f"  {start + i + 1}: {xml_lines[start + i]}" for i in range(end - start))
            return [
                f"NEW: Modified iFlow XML is not valid XML: {e}.\n"
                f"XML lines {start + 1}–{end} around the error:\n{ctx}\n"
                "Identify the mismatched or unclosed tag in the snippet above and fix it, "
                "then retry update-iflow."
            ]
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
                new_keys = [
                    k for k in all_keys
                    if k not in orig_collab_prop_keys and k not in ALLOWED_COLLABORATION_KEYS
                ]
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

    # Namespace prefixes declared at collaboration level via namespaceMapping.
    # SAP CPI supports this as an alternative to inline 'declare namespace' in XPath values.
    # e.g. namespaceMapping value = "d=http://schemas.microsoft.com/ado/...,ns1=http://..."
    _collab_ns_prefixes: set = set()
    _mod_collab = mod_root.find(f"{{{_BPMN2}}}collaboration")
    if _mod_collab is not None:
        _mod_collab_ext = _mod_collab.find(f"{{{_BPMN2}}}extensionElements")
        if _mod_collab_ext is not None:
            for _p in _mod_collab_ext.iter():
                _k = _p.find(f"{{{_IFL}}}key") or _p.find("key")
                _v = _p.find(f"{{{_IFL}}}value") or _p.find("value")
                if _k is None or _v is None:
                    continue
                if (_k.text or "").lower() == "namespacemapping":
                    for _entry in (_v.text or "").split(","):
                        _entry = _entry.strip()
                        if "=" in _entry:
                            _prefix = _entry.split("=")[0].strip()
                            # Handle both "d=http://..." and "xmlns:d=http://..." formats
                            if ":" in _prefix:
                                _prefix = _prefix.split(":")[-1]
                            _collab_ns_prefixes.add(_prefix)

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
                    # Prefixes covered by collaboration-level namespaceMapping are valid —
                    # SAP CPI resolves them at runtime without inline declarations.
                    truly_missing = [p for p in missing if p not in _collab_ns_prefixes]
                    if not truly_missing:
                        logger.info(
                            "[Validator] XPath prefix(es) %s covered by collaboration "
                            "namespaceMapping — not an error for property '%s'",
                            missing, key_el.text,
                        )
                        continue
                    fingerprint = (key_el.text or "", val)
                    if fingerprint in orig_xpath_issues:
                        logger.info(
                            "[Validator] Pre-existing issue ignored (existed in original): "
                            "XPath missing namespace declaration for property '%s'", key_el.text
                        )
                        continue
                    errors.append(
                        f"NEW: XPath expression in property '{key_el.text}' uses namespace prefix(es) "
                        f"{truly_missing} but no 'declare namespace' directive found and no "
                        f"matching namespaceMapping entry at collaboration level. "
                        f"Either add inline declarations: "
                        f"declare namespace {truly_missing[0]}='http://...'; //{truly_missing[0]}:element "
                        f"or add the prefix to the iFlow's namespaceMapping property."
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
    errors: List[str] = []

    # Resolve iFlow ID from the submitted filepath or from the args directly.
    # This replaces the ContextVar lookup which did not propagate into LangChain sub-tasks.
    files = args.get("files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            files = []
    _submitted_fp = ""
    if isinstance(files, list) and files:
        _submitted_fp = files[0].get("filepath", "") if isinstance(files[0], dict) else ""

    # Extract iflow_id from filepath: "RuntimeHeaderError/src/main/resources/RuntimeHeaderError.iflw"
    _iflow_id_from_fp = _submitted_fp.split("/")[0] if "/" in _submitted_fp else ""

    ctx = _fix_ctx_get(_iflow_id_from_fp) if _iflow_id_from_fp else None

    if ctx is None:
        # Fallback: search the store for any entry whose filepath matches the submitted one
        with _fix_ctx_lock:
            for _stored in _fix_ctx_store.values():
                if _stored.get("filepath") == _submitted_fp:
                    ctx = _stored
                    break
        if ctx is not None:
            logger.debug(
                "[Validator] ctx resolved via filepath-scan fallback: fp=%s", _submitted_fp
            )

    if ctx is None:
        logger.debug(
            "[Validator] no fix context for iflow=%s — skipping pre-call validation",
            _iflow_id_from_fp or _submitted_fp or "(unknown)",
        )
        return []  # no context set (manual / chat use) — skip validation

    original_filepath = ctx.get("filepath", "")
    original_xml      = ctx.get("xml", "")
    logger.info(
        "[Validator] validate_before_update_iflow: iflow=%s original_fp=%s original_xml_len=%d",
        _iflow_id_from_fp, original_filepath, len(original_xml),
    )

    submitted_filepath = _submitted_fp
    submitted_xml      = ""
    if isinstance(files, list) and files:
        submitted_xml = files[0].get("content", "") if isinstance(files[0], dict) else ""
    elif args.get("content"):
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
