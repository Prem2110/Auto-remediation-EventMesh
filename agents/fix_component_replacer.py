"""
agents/fix_component_replacer.py
=================================
ComponentReplacer — fetches a reference component from S3 via MCP,
merges valid property values from the broken component, applies the
specific fix from RCA, and returns a ready-to-use patched XML.
"""

import copy
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


REPLACEMENT_ELIGIBLE_ERROR_TYPES = {
    "BACKEND_ERROR",
    "CONNECTIVITY_ERROR",
    "AUTH_ERROR",
    "AUTH_CONFIG_ERROR",
    "SCRIPT_ERROR",
}

ALWAYS_PRESERVE_PROPERTIES = {
    "timeout", "connectiontimeout", "responsetimeout",
    "credentialname", "credential_name",
    "address", "url", "endpoint",
    "httpmethod", "method",
    "contenttype", "content_type",
    "headername", "headervalue",
    "proxytype", "proxy",
    "authenticationType", "authtype",
}


class ComponentReplacer:
    """
    Handles the component replacement fix strategy.
    Uses list-iflow-examples and get-iflow-example MCP tools.
    """

    def __init__(self, mcp) -> None:
        self._mcp = mcp

    def is_eligible(self, error_type: str, affected_component: str) -> bool:
        if not affected_component or affected_component.lower() in ("unknown", ""):
            return False
        return error_type in REPLACEMENT_ELIGIBLE_ERROR_TYPES

    async def find_and_fetch_reference(
        self, error_type: str, affected_component: str
    ) -> Tuple[str, str]:
        """
        Step 1: Call list-iflow-examples to get available examples.
        Step 2: Find best matching example for this error_type + component.
        Step 3: Call get-iflow-example to fetch the reference XML.

        Returns (example_name, reference_xml) or ("", "") if not found.
        """
        list_result = await self._mcp.execute_integration_tool(
            "list-iflow-examples", {}
        )
        if not list_result.get("success"):
            logger.info("[ComponentReplacer] list-iflow-examples unavailable")
            return "", ""

        examples_output = list_result.get("output", "")
        examples_str = (
            examples_output if isinstance(examples_output, str)
            else json.dumps(examples_output)
        )

        best_name = self._find_best_match(examples_str, error_type, affected_component)
        if not best_name:
            logger.info(
                "[ComponentReplacer] No matching example for error_type=%s component=%s",
                error_type, affected_component,
            )
            return "", ""

        logger.info(
            "[ComponentReplacer] Selected reference: %s for error_type=%s component=%s",
            best_name, error_type, affected_component,
        )

        get_result = await self._mcp.execute_integration_tool(
            "get-iflow-example", {"name": best_name}
        )
        if not get_result.get("success"):
            logger.warning(
                "[ComponentReplacer] get-iflow-example failed for %s", best_name
            )
            return "", ""

        ref_output = get_result.get("output", "")
        ref_xml = (
            ref_output if isinstance(ref_output, str)
            else json.dumps(ref_output)
        )
        return best_name, ref_xml

    def merge_component(
        self,
        original_full_xml: str,
        reference_xml: str,
        affected_component: str,
        proposed_fix: str,
        error_type: str,
    ) -> Optional[str]:
        """
        3-layer merge:
        Layer 1: Start with reference component structure (known-good)
        Layer 2: Copy ALL valid property values from broken component
        Layer 3: Apply ONLY the specific fix from proposed_fix

        Returns patched full XML or None if merge fails.
        """
        try:
            orig_root = ET.fromstring(original_full_xml)

            broken_elem = None
            broken_parent = None
            for parent in orig_root.iter():
                for child in list(parent):
                    if child.get("id") == affected_component:
                        broken_elem = child
                        broken_parent = parent
                        break
                if broken_elem is not None:
                    break

            if broken_elem is None:
                logger.warning(
                    "[ComponentReplacer] Component id=%s not found in original XML",
                    affected_component,
                )
                return None

            try:
                ref_root = ET.fromstring(reference_xml)
            except ET.ParseError:
                logger.warning("[ComponentReplacer] Reference XML parse failed")
                return None

            broken_tag = broken_elem.tag
            ref_elem = None
            for elem in ref_root.iter():
                if elem.tag == broken_tag:
                    ref_elem = elem
                    break

            if ref_elem is None:
                logger.info(
                    "[ComponentReplacer] No matching element tag=%s in reference",
                    broken_tag,
                )
                return None

            # Layer 1: clone reference element structure
            merged = copy.deepcopy(ref_elem)
            merged.set("id", affected_component)

            # Layer 2: copy valid property values from broken component
            broken_props: dict = {}
            for prop in broken_elem.iter():
                key_elem = prop.find("key") or prop.find("{*}key")
                val_elem = prop.find("value") or prop.find("{*}value")
                if key_elem is not None and val_elem is not None:
                    key = (key_elem.text or "").strip().lower()
                    val = (val_elem.text or "").strip()
                    broken_props[key] = val

            for prop in merged.iter():
                key_elem = prop.find("key") or prop.find("{*}key")
                val_elem = prop.find("value") or prop.find("{*}value")
                if key_elem is not None and val_elem is not None:
                    key = (key_elem.text or "").strip().lower()
                    if key in broken_props:
                        val_elem.text = broken_props[key]

            # Layer 3: apply the specific fix from proposed_fix
            fix_applied = self._apply_fix_hint(merged, proposed_fix, error_type)
            if fix_applied:
                logger.info("[ComponentReplacer] Applied fix hint to merged component")

            siblings = list(broken_parent)
            idx = siblings.index(broken_elem)
            broken_parent.remove(broken_elem)
            broken_parent.insert(idx, merged)

            ET.register_namespace("", "http://www.omg.org/spec/BPMN/20100524/MODEL")
            result = ET.tostring(orig_root, encoding="unicode")
            logger.info(
                "[ComponentReplacer] Merge successful for component=%s",
                affected_component,
            )
            return result

        except Exception as exc:
            logger.warning("[ComponentReplacer] merge_component failed: %s", exc)
            return None

    def _apply_fix_hint(
        self, elem: ET.Element, proposed_fix: str, error_type: str
    ) -> bool:
        """
        Extract a specific property fix from proposed_fix text and apply it.
        Returns True if a fix was applied.
        """
        if not proposed_fix:
            return False

        fix_lower = proposed_fix.lower()

        patterns = [
            r'(?:change|update|set|fix)\s+["\']?(\w+)["\']?\s+to\s+["\']?([^\s,\.]+)',
            r'["\']?(\w+)["\']?\s+should be\s+["\']?([^\s,\.]+)',
            r'replace\s+["\']?(\w+)["\']?\s+with\s+["\']?([^\s,\.]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, fix_lower)
            if match:
                prop_name = match.group(1).lower()
                new_value = match.group(2).strip("'\"")

                for prop in elem.iter():
                    key_elem = prop.find("key") or prop.find("{*}key")
                    val_elem = prop.find("value") or prop.find("{*}value")
                    if key_elem is not None and val_elem is not None:
                        if key_elem.text and key_elem.text.lower() == prop_name:
                            val_elem.text = new_value
                            return True

        return False

    @staticmethod
    def _find_best_match(
        examples_output: str, error_type: str, affected_component: str
    ) -> Optional[str]:
        names: list = []
        try:
            parsed = json.loads(examples_output)
            if isinstance(parsed, list):
                names = [str(n) for n in parsed]
            elif isinstance(parsed, dict):
                names = list(parsed.keys())
        except Exception:
            names = re.findall(
                r'[\w][\w\-_.]*(?:iflow|adapter|receiver|sender|http|sftp|soap|rest|script)',
                examples_output, re.IGNORECASE,
            )

        if not names:
            names = [
                line.strip()
                for line in examples_output.splitlines()
                if line.strip() and not line.startswith("{")
            ]

        if not names:
            return None

        adapter_keywords: dict = {
            "BACKEND_ERROR":      ["http", "rest", "odata", "receiver"],
            "CONNECTIVITY_ERROR": ["http", "sftp", "soap", "receiver"],
            "AUTH_ERROR":         ["oauth", "basic", "cert", "auth", "security"],
            "AUTH_CONFIG_ERROR":  ["credential", "auth", "security"],
            "SCRIPT_ERROR":       ["groovy", "script"],
        }
        type_kws = adapter_keywords.get(error_type, [])
        comp_kws = [
            w.lower() for w in re.split(r'[_\-]', affected_component) if len(w) > 2
        ]

        scores: dict = {}
        for name in names:
            nl = name.lower()
            score = 0
            for kw in comp_kws:
                if kw in nl:
                    score += 3
            for kw in type_kws:
                if kw in nl:
                    score += 2
            if score > 0:
                scores[name] = score

        return max(scores, key=scores.get) if scores else None
