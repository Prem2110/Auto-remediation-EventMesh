# Message Mappings, XSLT, Value Mappings

For when failure involves mapping steps, schema validation, or transformation logic.

---

## Mapping types in CPI

| Type | When used | Extension | Editor |
|---|---|---|---|
| Message Mapping (MM) | XML-to-XML, JSON-to-XML, etc. | `.mmap` (binary) | Visual mapping editor |
| XSLT Mapping | Complex XML transformations | `.xsl` / `.xslt` | XML editor |
| Operation Mapping | Wraps MM with interface metadata | `.opmap` | Operation mapping editor |
| Value Mapping | Code list lookups | `.vmap` | Value mapping editor |
| JSON-to-XML / XML-to-JSON Converter | Format conversion steps | (config) | Step properties |
| EDI converters | EDIFACT, X12, etc. | (config) | Step properties |
| CSV-to-XML / XML-to-CSV | Tabular conversions | (config) | Step properties |

---

## Message Mapping pitfalls

### Optional fields treated as required

Mappings often built against the "happy path" sample. When source omits an optional field:
- XPath returns empty → target field becomes empty string or `nil`
- Downstream parsing fails ("expected number, got empty string")

**Fix**: explicit handling of empty in mapping (default value, conditional, or `removeContexts`).

### Context handling in array-like mappings

CPI message mapping is context-aware. Common mistake: forgetting to use `removeContexts`, `splitByValue`, or `collapseContexts` when transforming repeating structures.

Symptoms:
- Output has one giant target element instead of N
- Output has nested arrays where flat was expected
- Order of elements scrambled

**Fix**: study the context behavior of your source/target. The default context is often wrong for one-to-many mappings.

### Hardcoded values that look like business logic

A mapping with `setValue("US")` somewhere — works in dev (US-only tenant), fails when EU records arrive in prod. Nearly invisible in mapping diagrams.

**Fix**: externalize via configure-time parameters or value mappings. Code-review mappings specifically for `setValue` constants.

### Namespace prefix mismatches

XML mapping cares about namespace URI, not prefix. But XPath in custom functions cares about both unless the namespace context is configured. Silent failure: the mapping "works" but produces no output for the affected node.

**Fix**: declare namespaces explicitly in custom function context; don't rely on prefix matching.

### Number formatting locale-dependent

`formatNumber()` uses the runtime locale by default — comma vs period as decimal separator. Same iFlow, different output in different tenant regions.

**Fix**: specify locale explicitly in formatting functions.

---

## XSLT pitfalls

### XSLT 1.0 vs 2.0+ engine differences

CPI ships with an XSLT processor (often a Saxon variant). XSLT 2.0+ features (`for-each-group`, regex, sequences) need the right processor version. Don't assume 2.0 features work without checking.

### Identity transform copying namespaces you didn't want

```xml
<xsl:template match="@*|node()">
  <xsl:copy>
    <xsl:apply-templates select="@*|node()"/>
  </xsl:copy>
</xsl:template>
```

Copies everything including source namespaces. Downstream receivers that expect specific namespaces fail validation.

**Fix**: explicit element construction, or strip namespaces with targeted templates.

### Position-dependent logic when input order isn't stable

`position() = 1` assumes the source produces nodes in a stable order. JSON-to-XML conversion often doesn't preserve object key order.

**Fix**: select by key/predicate, not position, whenever possible.

---

## Value Mapping pitfalls

### Missing source entry → null target

Value mapping with no match returns null (or empty). Mapping doesn't fail — silently produces missing data.

**Fix**: configure default value in mapping function call, or validate post-mapping.

### Case sensitivity

Value mappings are case-sensitive by default. `"US"` and `"us"` are different keys.

**Fix**: normalize case in pre-processing, or maintain both variants in mapping.

### Agency / identifier scheme confusion

Value mappings have agency and scheme fields. Wrong scheme = lookup misses even if key exists.

**Fix**: verify scheme matches usage; document scheme conventions per integration scenario.

---

## Schema validation

CPI offers XML Schema Validation and JSON Schema Validation steps. Recommended pattern: validate at the boundary (immediately after sender), not deep in the iFlow.

Reasons:
- Fail fast with a clear error
- Avoid wasted processing of doomed messages
- Easy to route invalid messages to a quarantine queue

Never silently log-and-continue on schema failure. The whole point is to catch bad data.

---

## Error signatures specific to mappings

| Pattern | Meaning |
|---|---|
| `Conversion Exception` | Type mismatch (e.g., letter where number expected) |
| `MappingException: Cannot produce subtree` | Required target structure couldn't be built — source missing data |
| `XPathExpressionException` | XPath references node that doesn't exist |
| `XSLT transformation error` | XSLT logic error or namespace issue |
| `Value mapping not found` | Lookup missed and no default configured |
| `Schema validation failed` | Payload doesn't match XSD/JSON Schema |
| `Cannot find mapping function` | Custom function referenced but not deployed |

---

## Remediation blast radius for mappings

**All mapping changes are `REVIEW_REQUIRED`** at minimum, often `NEVER_AUTO`. Mappings encode business logic; changing them changes semantics. Even adding a default value can change downstream behavior in unexpected ways.

Exception: adding a *missing* default for a field where production data has clearly diverged from the spec, and the default is documented and reviewed — still `REVIEW_REQUIRED`, but lower-risk.

---

## CSV / EDI / flat-file conversions

### CSV pitfalls
- Delimiter detection: comma vs semicolon vs tab. Specify explicitly.
- Quoting: embedded quotes in fields can break parsing if quote escape rule is wrong.
- Header row: sometimes present, sometimes not. Misconfiguration → first data row treated as header.

### EDIFACT / X12
- Segment terminators and element separators vary by partner agreement.
- Acknowledgment patterns (CONTRL, 997) are part of the protocol — not handling them is a partner-relationship issue, not a CPI issue.

### Fixed-width
- Field positions sensitive to whitespace and encoding. UTF-8 vs ASCII matters when field widths assume byte counts.
