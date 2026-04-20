# SAP CPI iFlow XML Patterns

Structural rules and known-failure patterns for the FixAgent. **Read before making any change to iFlow XML.**

---

## 1. Minimal Edit Principle

Only change what the proposed fix requires. Do not:

- Rebuild or restructure the iFlow XML
- Rename IDs, collaborations, or steps
- Change version attributes
- Add steps, channels, or adapters not explicitly required by the fix

If the original iFlow had `id="Integration_Process_1"`, the updated XML must preserve that exact value.

---

## 2. Property Placement — Step Level vs. Flow Level

Configuration properties MUST be placed inside the `<bpmn2:extensionElements>` of the **specific step** that uses them.

**Wrong** — property at collaboration root (causes "Unable to process Definition Checks : null"):

```xml
<bpmn2:collaboration id="Collaboration_1">
  <bpmn2:extensionElements>
    <ifl:property>
      <key>namespaceMapping</key>   <!-- INVALID at this level -->
      <value>d=http://...</value>
    </ifl:property>
  </bpmn2:extensionElements>
</bpmn2:collaboration>
```

**Correct** — property inside the step that uses it:

```xml
<bpmn2:serviceTask id="ContentModifier_1" ...>
  <bpmn2:extensionElements>
    <ifl:property>
      <key>xpathExpression</key>
      <value>declare namespace d='http://schemas.microsoft.com/ado/2007/08/dataservices'; //d:results</value>
    </ifl:property>
  </bpmn2:extensionElements>
</bpmn2:serviceTask>
```

---

## 3. XPath Namespace Declarations

When an XPath expression uses a namespace prefix, declare the namespace **inline in the XPath expression value**:

```
declare namespace d='http://schemas.microsoft.com/ado/2007/08/dataservices';
declare namespace m='http://schemas.microsoft.com/ado/2007/08/dataservices/metadata';
//d:feed/d:entry/m:properties/d:InvoiceID
```

Do NOT add a `namespaceMapping` property at the collaboration or flow root level — SAP CPI ignores it.

Common OData v2 namespace URIs:

| Prefix | URI |
|---|---|
| `d` | `http://schemas.microsoft.com/ado/2007/08/dataservices` |
| `m` | `http://schemas.microsoft.com/ado/2007/08/dataservices/metadata` |
| `atom` | `http://www.w3.org/2005/Atom` |

---

## 4. Content Modifier — Header Row `srcType`

In Content Modifier Header rows, `srcType` must always be `"Expression"`. The value `"Constant"` is rejected by SAP CPI at both upload and deploy time.

```xml
<!-- Header row — correct -->
<ifl:property>
  <key>srcType</key>
  <value>Expression</value>   <!-- NEVER "Constant" for Header rows -->
</ifl:property>
```

For **property rows**, `srcType` may be `"Constant"`, `"Expression"`, or `"Header"` depending on use case.

---

## 5. Component Version Limits (IFLMAP Profile)

Never write a version higher than the platform maximum:

| Component | Maximum Version |
|---|---|
| EndEvent | `1.0` |
| ExceptionSubprocess | `1.1` |
| SOAP adapter | `1.11` |
| Content-Based Router | `1.1` |

If copying XML from a reference iFlow, check every `version="..."` attribute and cap it at the limits above.

---

## 6. Router — Default Route Requirement

Every `<bpmn2:exclusiveGateway>` (Content-Based Router) MUST have a default outgoing route. Without a default route, deployment fails.

The default route condition must be a sequence flow with `isDefault="true"` or a condition that always evaluates to `true`.

---

## 7. Adapter Channels — No Empty Configuration

Never create a sender or receiver channel with empty or placeholder values. If the fix does not require a new channel, do not add one.

If a new channel IS required:

- Set all mandatory adapter fields (host, port, path, credential alias, etc.)
- Set a valid adapter type (`HTTP`, `SOAP`, `SFTP`, `OData`, etc.) — never leave type blank
- Verify the channel connects to actual endpoints in the scenario

---

## 8. Groovy Script File Paths

| Location | Path format |
|---|---|
| Physical file in archive | `src/main/resources/script/<FileName>.groovy` |
| Reference inside iFlow model property | `/script/<FileName>.groovy` |

Do NOT use `/src/main/resources/script/...` or any absolute path as the model reference.

---

## 9. `update-iflow` — Filepath Must Match the Original

The `filepath` in the `update-iflow` files array **must be the exact path** of the `.iflw` file as returned by `get-iflow`.

**Wrong** — invented or reused filepath:
```json
{"filepath": "src/main/resources/scenarioflows/integrationflow/Xlsx.iflw"}
```

**Correct** — filepath extracted from the `get-iflow` response:
```json
{"filepath": "src/main/resources/scenarioflows/integrationflow/Sending files to SFTP server_copy.iflw"}
```

!!! danger
    Using the wrong filepath causes SAP CPI to add a second `.iflw` file to the archive. The original file remains unchanged and the fix is never applied — even though `update-iflow` returns HTTP 200.

---

## 10. Self-Check Before `update-iflow`

Before calling `update-iflow`, verify:

1. The iFlow XML parses as valid XML (no unclosed tags, no duplicate IDs)
2. All changed elements have their versions preserved from the original
3. All `ifl:property` elements are inside the correct parent step element
4. No new unconfigured channels or steps were accidentally added
5. XPath expressions with namespace prefixes include inline `declare namespace` declarations

These checks are also enforced by `core/validators.py` before the MCP call is made.
