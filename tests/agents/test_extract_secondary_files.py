from agents.fix_planner import FixPlanner

_MULTI_FILE = (
    "src/main/resources/scenarioflows/integrationflow/MyIflow.iflw\n"
    "---begin-of-file---\n"
    "<?xml version=\"1.0\"?><root/>\n"
    "---end-of-file---\n"
    "\n"
    "src/main/resources/script/TransformOrder.groovy\n"
    "---begin-of-file---\n"
    "import com.sap.gateway.ip.core.customdev.util.Message\n"
    "def Message processData(Message message) { return message }\n"
    "---end-of-file---\n"
    "\n"
    "src/main/resources/mapping/OrderMapping.xsl\n"
    "---begin-of-file---\n"
    "<?xml version=\"1.0\"?><xsl:stylesheet version=\"1.0\" "
    "xmlns:xsl=\"http://www.w3.org/1999/XSL/Transform\"/>\n"
    "---end-of-file---\n"
)


def test_extracts_groovy_file():
    result = FixPlanner._extract_secondary_files(_MULTI_FILE)
    groovy_keys = [k for k in result if k.endswith(".groovy")]
    assert groovy_keys, f"No .groovy key found in {list(result.keys())}"
    assert "processData" in result[groovy_keys[0]]


def test_extracts_xsl_file():
    result = FixPlanner._extract_secondary_files(_MULTI_FILE)
    xsl_keys = [k for k in result if k.endswith(".xsl")]
    assert xsl_keys, f"No .xsl key found in {list(result.keys())}"
    assert "xsl:stylesheet" in result[xsl_keys[0]]


def test_excludes_iflw():
    result = FixPlanner._extract_secondary_files(_MULTI_FILE)
    assert not any(".iflw" in k for k in result), f"Found .iflw in result keys: {list(result.keys())}"


def test_returns_empty_for_plain_xml():
    result = FixPlanner._extract_secondary_files("<?xml version='1.0'?><root/>")
    assert result == {}


def test_returns_empty_for_empty_string():
    result = FixPlanner._extract_secondary_files("")
    assert result == {}
