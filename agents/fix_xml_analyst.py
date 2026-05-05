"""
agents/fix_xml_analyst.py
=========================
XMLAnalyst — pure XML parsing (no LLM) that builds a component map and
summary text from an iFlow BPMN2 XML string.

Exports:
  ComponentInfo  — component metadata
  XMLAnalyst     — analyse(xml_str, focused_component) -> summary_str
"""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

_TAG_TYPE_MAP: Dict[str, str] = {
    "serviceTask":      "adapter_step",
    "exclusiveGateway": "cbr",
    "sequenceFlow":     "flow",
    "startEvent":       "start",
    "endEvent":         "end",
    "callActivity":     "subprocess",
    "subProcess":       "subprocess",
    "parallelGateway":  "join_split",
}


@dataclass
class ComponentInfo:
    comp_id: str
    comp_type: str
    name: str
    connections_in: List[str] = field(default_factory=list)
    connections_out: List[str] = field(default_factory=list)
    properties: Dict[str, str] = field(default_factory=dict)


class XMLAnalyst:
    """
    Parse an iFlow BPMN2 XML and return a summary of the component topology.
    Used to inject structural context into LLM prompts without calling any API.
    """

    def analyse(self, xml_str: str, focused_component: Optional[str] = None) -> str:
        """
        Build a component map from xml_str and return a plain-text summary.
        focused_component is highlighted first if given.
        Returns "" on parse failure.
        """
        if not xml_str:
            return ""
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            logger.warning("[XMLAnalyst] XML parse failed: %s", exc)
            return ""

        component_map = self._build_map(root)
        return self._build_summary(component_map, focused_component)

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_map(root: ET.Element) -> Dict[str, ComponentInfo]:
        component_map: Dict[str, ComponentInfo] = {}

        # Pass 1: collect all elements with an id
        for elem in root.iter():
            comp_id = elem.get("id", "")
            if not comp_id:
                continue
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            component_map[comp_id] = ComponentInfo(
                comp_id=comp_id,
                comp_type=_TAG_TYPE_MAP.get(tag, tag),
                name=elem.get("name", ""),
            )

        # Pass 2: wire sequence flows
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "sequenceFlow":
                src = elem.get("sourceRef", "")
                tgt = elem.get("targetRef", "")
                if src in component_map:
                    component_map[src].connections_out.append(tgt)
                if tgt in component_map:
                    component_map[tgt].connections_in.append(src)

        # Pass 3: extract ifl:property key/value pairs
        for elem in root.iter():
            comp_id = elem.get("id", "")
            if comp_id not in component_map:
                continue
            ext = elem.find(f"{{{_BPMN2}}}extensionElements")
            if ext is None:
                continue
            for prop in ext.findall(f"{{{_IFL}}}property"):
                k_el = prop.find(f"{{{_IFL}}}key") or prop.find("key")
                v_el = prop.find(f"{{{_IFL}}}value") or prop.find("value")
                if k_el is not None and v_el is not None:
                    component_map[comp_id].properties[k_el.text or ""] = (v_el.text or "")[:120]

        return component_map

    @staticmethod
    def _build_summary(
        component_map: Dict[str, ComponentInfo],
        focused_component: Optional[str],
    ) -> str:
        lines: List[str] = ["=== iFlow Component Map (static analysis) ==="]

        if focused_component and focused_component in component_map:
            c = component_map[focused_component]
            lines.append(f"[TARGET] {c.comp_id} ({c.comp_type}) name='{c.name}'")
            if c.connections_in:
                lines.append(f"  <- from: {', '.join(c.connections_in)}")
            if c.connections_out:
                lines.append(f"  -> to:   {', '.join(c.connections_out)}")
            for k, v in list(c.properties.items())[:6]:
                lines.append(f"  {k} = {v}")
            lines.append("")

        for comp_id, c in component_map.items():
            if comp_id == focused_component:
                continue
            name_part = f" '{c.name}'" if c.name else ""
            flow_part = (
                f" -> {', '.join(c.connections_out[:2])}"
                if c.connections_out else ""
            )
            lines.append(f"  {comp_id} [{c.comp_type}]{name_part}{flow_part}")

        return "\n".join(lines)
