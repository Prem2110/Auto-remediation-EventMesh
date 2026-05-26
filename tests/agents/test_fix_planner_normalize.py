import pytest
from agents.fix_planner import FixPlanner

_XML_ENTITY = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:ifl="http://sap.com/xi/mapping/ifl">
  <ifl:property id="ReceiverHTTP_1">
    <ifl:key>Address</ifl:key>
    <ifl:value>https://api.example.com/v1/orders&amp;format=json</ifl:value>
  </ifl:property>
</root>"""

_XML_CDATA = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:ifl="http://sap.com/xi/mapping/ifl">
  <ifl:property id="ReceiverHTTP_1">
    <ifl:key>Address</ifl:key>
    <ifl:value><![CDATA[https://api.example.com/v1/orders]]></ifl:value>
  </ifl:property>
</root>"""

def test_normalize_value_unescapes_entities():
    assert FixPlanner._normalize_value("a&amp;b") == "a&b"

def test_normalize_value_strips_cdata():
    assert FixPlanner._normalize_value("<![CDATA[hello]]>") == "hello"

def test_normalize_value_strips_whitespace():
    assert FixPlanner._normalize_value("  foo  ") == "foo"

def test_normalize_value_empty():
    assert FixPlanner._normalize_value("") == ""
    assert FixPlanner._normalize_value(None) == ""

def test_value_search_matches_html_entity():
    fixes = [{
        "component_id":       "WRONG_ID",
        "property_to_change": "Address",
        "current_value":      "https://api.example.com/v1/orders&format=json",
        "correct_value":      "https://api.example.com/v2/orders&format=json",
    }]
    result = FixPlanner._apply_direct_patch(_XML_ENTITY, fixes)
    assert result is not None
    assert "v2/orders" in result

def test_value_search_matches_cdata():
    fixes = [{
        "component_id":       "WRONG_ID",
        "property_to_change": "Address",
        "current_value":      "https://api.example.com/v1/orders",
        "correct_value":      "https://api.example.com/v2/orders",
    }]
    result = FixPlanner._apply_direct_patch(_XML_CDATA, fixes)
    assert result is not None
    assert "v2/orders" in result
