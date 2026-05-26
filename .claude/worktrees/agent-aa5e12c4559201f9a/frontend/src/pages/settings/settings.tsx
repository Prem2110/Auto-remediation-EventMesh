import { useState, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchSettings,
  patchSettings,
  resetSetting,
  fetchHealthCheck,
  type SettingSchema,
  type HealthCheckResult,
  type HealthStatus,
} from "../../services/api.ts";
import SvgIcon from "../../components/icons/SvgIcon.tsx";
import styles from "./settings.module.css";

// ── helpers ───────────────────────────────────────────────────────────────────

function impactColor(impact: string) {
  if (impact === "high")   return styles.impactHigh;
  if (impact === "medium") return styles.impactMedium;
  return styles.impactLow;
}

function formatDefault(v: unknown, type: string): string {
  if (type === "json") return JSON.stringify(v, null, 2);
  if (type === "bool") return String(v);
  return String(v);
}

// ── per-setting row ───────────────────────────────────────────────────────────

interface SettingRowProps {
  setting: SettingSchema;
  onSave: (key: string, value: unknown) => Promise<void>;
  onReset: (key: string) => Promise<void>;
  saving: string | null;
}

function SettingRow({ setting, onSave, onReset, saving }: SettingRowProps) {
  const [editing, setEditing]   = useState(false);
  const [localVal, setLocalVal] = useState<string>("");
  const [jsonError, setJsonError] = useState<string>("");

  const isBusy = saving === setting.key;

  function startEdit() {
    setLocalVal(
      setting.type === "json"
        ? JSON.stringify(setting.current_value, null, 2)
        : String(setting.current_value)
    );
    setJsonError("");
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setJsonError("");
  }

  async function save() {
    let parsed: unknown = localVal;
    if (setting.type === "int")   { parsed = parseInt(localVal, 10); if (isNaN(parsed as number)) return; }
    if (setting.type === "float") { parsed = parseFloat(localVal);   if (isNaN(parsed as number)) return; }
    if (setting.type === "bool")  { parsed = localVal.toLowerCase() === "true"; }
    if (setting.type === "json")  {
      try { parsed = JSON.parse(localVal); } catch { setJsonError("Invalid JSON"); return; }
    }
    await onSave(setting.key, parsed);
    setEditing(false);
  }

  const displayValue =
    setting.type === "json"
      ? "[object]"
      : setting.type === "bool"
      ? String(setting.current_value)
      : String(setting.current_value);

  return (
    <div className={`${styles.row} ${setting.is_overridden ? styles.rowOverridden : ""}`}>
      {/* left: label + badges + description */}
      <div className={styles.rowLeft}>
        <div className={styles.rowHeader}>
          <span className={styles.rowLabel}>{setting.label}</span>
          <span className={`${styles.impactBadge} ${impactColor(setting.impact)}`}>
            {setting.impact} impact
          </span>
          {setting.is_overridden && (
            <span className={styles.overriddenBadge}>customised</span>
          )}
        </div>
        <span className={styles.rowKey}>{setting.key}</span>
        <p className={styles.rowDesc}>{setting.description}</p>
        <div className={styles.rowMeta}>
          <span className={styles.metaItem}>
            <strong>Default:</strong> {formatDefault(setting.default, setting.type)}
          </span>
          <span className={`${styles.metaItem} ${styles.effectiveNote}`}>
            <SvgIcon name="lightning" size={11} style={{ marginRight: 3 }} />
            <strong>Takes effect:</strong> {setting.when_effective}
          </span>
        </div>
      </div>

      {/* right: value + controls */}
      <div className={styles.rowRight}>
        {editing ? (
          <div className={styles.editBlock}>
            {setting.type === "bool" ? (
              <select
                className={styles.input}
                value={localVal}
                onChange={(e) => setLocalVal(e.target.value)}
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : setting.type === "json" ? (
              <>
                <textarea
                  className={`${styles.textarea} ${jsonError ? styles.inputError : ""}`}
                  value={localVal}
                  onChange={(e) => { setLocalVal(e.target.value); setJsonError(""); }}
                  rows={10}
                  spellCheck={false}
                />
                {jsonError && <span className={styles.errorMsg}>{jsonError}</span>}
              </>
            ) : (
              <input
                className={styles.input}
                type={setting.type === "float" ? "number" : setting.type === "int" ? "number" : "text"}
                step={setting.type === "float" ? "0.01" : undefined}
                value={localVal}
                onChange={(e) => setLocalVal(e.target.value)}
              />
            )}
            <div className={styles.editActions}>
              <button
                className={styles.btnSave}
                onClick={save}
                disabled={isBusy}
              >
                {isBusy ? "Saving…" : "Save"}
              </button>
              <button className={styles.btnCancel} onClick={cancel}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className={styles.valueBlock}>
            <span className={styles.currentValue}>
              {setting.type === "json" ? (
                <code className={styles.jsonPreview}>
                  {JSON.stringify(setting.current_value, null, 2)}
                </code>
              ) : (
                <code>{displayValue}</code>
              )}
            </span>
            <div className={styles.valueActions}>
              <button className={styles.btnEdit} onClick={startEdit} disabled={isBusy}>
                <SvgIcon name="wrench" size={13} /> Edit
              </button>
              {setting.is_overridden && (
                <button
                  className={styles.btnReset}
                  onClick={() => onReset(setting.key)}
                  disabled={isBusy}
                  title="Reset to default"
                >
                  <SvgIcon name="refresh" size={13} /> Reset
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── policy table editor ────────────────────────────────────────────────────────

const POLICY_ACTIONS = ["AUTO_FIX", "APPROVAL", "RETRY", "TICKET_CREATED"] as const;

interface PolicyEditorProps {
  policies: Record<string, { action: string; replay_after_fix: boolean }>;
  onChange: (updated: Record<string, { action: string; replay_after_fix: boolean }>) => void;
}

function PolicyEditor({ policies, onChange }: PolicyEditorProps) {
  return (
    <div className={styles.policyTable}>
      <div className={styles.policyHeader}>
        <span>Error Type</span>
        <span>Action</span>
        <span>Replay After Fix</span>
      </div>
      {Object.entries(policies).map(([errorType, policy]) => (
        <div key={errorType} className={styles.policyRow}>
          <span className={styles.errorTypeLabel}>{errorType}</span>
          <select
            className={styles.policySelect}
            value={policy.action}
            onChange={(e) =>
              onChange({ ...policies, [errorType]: { ...policy, action: e.target.value } })
            }
          >
            {POLICY_ACTIONS.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <label className={styles.toggleLabel}>
            <input
              type="checkbox"
              checked={policy.replay_after_fix}
              onChange={(e) =>
                onChange({ ...policies, [errorType]: { ...policy, replay_after_fix: e.target.checked } })
              }
            />
            <span className={styles.toggleText}>{policy.replay_after_fix ? "Yes" : "No"}</span>
          </label>
        </div>
      ))}
    </div>
  );
}

// ── theme picker ──────────────────────────────────────────────────────────────

const THEMES = [
  { id: "plain",    label: "Plain",            sidebar: "#ffffff",                                        primary: "#2563eb", bg: "#f0eeff" },
  { id: "sap",      label: "SAP Horizon",      sidebar: "#354a5e",                                        primary: "#0070f2", bg: "#f5f6f7" },
  { id: "sap-blue", label: "SAP Blue",         sidebar: "linear-gradient(180deg,#0057c2,#0070f2)",        primary: "#0070f2", bg: "#f5f7fa" },
  { id: "aurora",   label: "Aurora (default)", sidebar: "linear-gradient(175deg,#0f1f5c,#2563eb)",        primary: "#3b82f6", bg: "#eff6ff" },
  { id: "fresh",    label: "Fresh",            sidebar: "#f7fef9",                                        primary: "#16a34a", bg: "#e8f5ee" },
  { id: "prism",    label: "Prism",            sidebar: "#ffffff",                                        primary: "#9b5de5", bg: "linear-gradient(135deg,#ffe4ec,#f3e8ff,#fff3e0)" },
  { id: "mono",     label: "Mono",             sidebar: "#efefef",                                        primary: "#1a1a1a", bg: "#f2f2f2" },
  { id: "brutal",  label: "Brutal",           sidebar: "#ffffff",                                        primary: "#ffe500", bg: "#f5f5f5" },
  { id: "dark",     label: "Dark",             sidebar: "#0f172a",                                        primary: "#818cf8", bg: "#0f172a" },
  { id: "terminal", label: "Terminal",         sidebar: "#0a0a0a",                                        primary: "#00ff41", bg: "#0a0a0a" },
  { id: "nord",     label: "Nord",             sidebar: "#2e3440",                                        primary: "#88c0d0", bg: "#e5e9f0" },
  { id: "copper",   label: "Copper",           sidebar: "#141009",                                        primary: "#b87333", bg: "#1a140e" },
] as const;

type ThemeId = (typeof THEMES)[number]["id"];

function applyTheme(id: ThemeId) {
  if (id === "plain") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", id);
  }
  localStorage.setItem("orbit-theme", id);
}

const FONT_SIZES = [
  { id: "sm",  label: "S", value: "0.8rem"  },
  { id: "md",  label: "M", value: "0.875rem" },
  { id: "lg",  label: "L", value: "0.95rem" },
] as const;
type FontSizeId = (typeof FONT_SIZES)[number]["id"];

function applyFontSize(id: FontSizeId) {
  const size = FONT_SIZES.find((f) => f.id === id)!.value;
  document.documentElement.style.setProperty("--orbit-font-size-base", size);
  localStorage.setItem("orbit-font-size", id);
}

// ── health check panel ────────────────────────────────────────────────────────

const HEALTH_SERVICES = [
  { id: "all",        label: "All Services"  },
  { id: "mcp",        label: "MCP Server"    },
  { id: "db",         label: "Database"      },
  { id: "event_mesh", label: "Event Mesh"    },
] as const;

type HealthServiceId = (typeof HEALTH_SERVICES)[number]["id"];

function statusDot(s: HealthStatus) {
  if (s === "ok")       return styles.dotOk;
  if (s === "degraded") return styles.dotDegraded;
  return styles.dotError;
}

function statusBadge(s: HealthStatus) {
  if (s === "ok")       return styles.badgeOk;
  if (s === "degraded") return styles.badgeDegraded;
  return styles.badgeError;
}

function serviceIcon(name: string) {
  if (name === "MCP Server") return "server-stack";
  if (name === "Database")   return "circle-stack";
  return "event-mesh";
}

function CheckRow({ result }: { result: HealthCheckResult }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = Object.keys(result.detail).length > 0;
  return (
    <div className={styles.checkRow}>
      <div className={styles.checkMain}>
        <span className={`${styles.checkDot} ${statusDot(result.status)}`} />
        <SvgIcon name={serviceIcon(result.service)} size={15} style={{ flexShrink: 0, color: "var(--orbit-text-muted, #6b7280)" }} />
        <span className={styles.checkName}>{result.service}</span>
        <span className={`${styles.checkBadge} ${statusBadge(result.status)}`}>{result.status}</span>
        <span className={styles.checkMsg}>{result.message}</span>
        {hasDetail && (
          <button className={styles.checkExpand} onClick={() => setExpanded((v) => !v)} title="Toggle details">
            <SvgIcon name={expanded ? "chevron-down" : "chevron-right"} size={12} />
          </button>
        )}
      </div>
      {expanded && hasDetail && (
        <pre className={styles.checkDetail}>{JSON.stringify(result.detail, null, 2)}</pre>
      )}
    </div>
  );
}

function HealthCheckPanel() {
  const [open,     setOpen]       = useState(false);
  const [selected, setSelected]   = useState<HealthServiceId>("all");
  const [running,  setRunning]    = useState(false);
  const [results,  setResults]    = useState<HealthCheckResult[] | null>(null);
  const [overall,  setOverall]    = useState<HealthStatus | null>(null);
  const [errMsg,   setErrMsg]     = useState<string | null>(null);

  async function runCheck() {
    setRunning(true);
    setResults(null);
    setOverall(null);
    setErrMsg(null);
    try {
      const data = await fetchHealthCheck(selected);
      setResults(data.checks);
      setOverall(data.overall);
    } catch (e) {
      setErrMsg(String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className={styles.healthCard}>
      {/* ── collapsed / expanded toggle row ── */}
      <button className={styles.healthToggle} onClick={() => setOpen((v) => !v)}>
        <div className={styles.healthToggleLeft}>
          <SvgIcon name="server-stack" size={15} style={{ flexShrink: 0 }} />
          <span className={styles.healthTitle}>System Health</span>
          {overall && !open && (
            <span className={`${styles.checkBadge} ${statusBadge(overall)}`}>
              {overall === "ok" ? "All good" : overall === "degraded" ? "Degraded" : "Issues found"}
            </span>
          )}
        </div>
        <SvgIcon name={open ? "chevron-down" : "chevron-right"} size={14} style={{ flexShrink: 0, color: "var(--orbit-text-muted, #9ca3af)" }} />
      </button>

      {/* ── body — only rendered when open ── */}
      {open && (
        <div className={styles.healthBody}>
          <p className={styles.healthDesc}>
            Run an on-demand liveness probe for MCP, the database, and SAP Event Mesh.
          </p>

          <div className={styles.healthControls}>
            <select
              className={styles.healthSelect}
              value={selected}
              onChange={(e) => { setSelected(e.target.value as HealthServiceId); setResults(null); setOverall(null); setErrMsg(null); }}
              disabled={running}
            >
              {HEALTH_SERVICES.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>

            <button className={styles.healthRunBtn} onClick={runCheck} disabled={running}>
              {running ? (
                <><span className={styles.spinner} />Checking…</>
              ) : (
                <><SvgIcon name="loop" size={13} />Run Check</>
              )}
            </button>

            {overall && !running && (
              <span className={`${styles.checkBadge} ${statusBadge(overall)}`} style={{ alignSelf: "center" }}>
                {overall === "ok" ? "All good" : overall === "degraded" ? "Degraded" : "Issues found"}
              </span>
            )}
          </div>

          {errMsg && (
            <div className={styles.healthError}>
              <SvgIcon name="warning" size={13} />
              {errMsg}
            </div>
          )}

          {results && results.length > 0 && (
            <div className={styles.checkList}>
              {results.map((r) => <CheckRow key={r.service} result={r} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────────

export default function Settings() {
  const qc = useQueryClient();
  const [saving,      setSaving]      = useState<string | null>(null);
  const [toast,       setToast]       = useState<{ msg: string; ok: boolean } | null>(null);
  const [filter,      setFilter]      = useState<string>("All");
  const [activeTheme, setActiveTheme] = useState<ThemeId>(
    () => (localStorage.getItem("orbit-theme") as ThemeId) || "aurora"
  );
  const [activeFontSize, setActiveFontSize] = useState<FontSizeId>(
    () => (localStorage.getItem("orbit-font-size") as FontSizeId) || "md"
  );

  // policy editor local state (separate because it has its own inline editor)
  const [editingPolicies, setEditingPolicies] = useState(false);
  const [localPolicies,   setLocalPolicies]   = useState<Record<string, { action: string; replay_after_fix: boolean }>>({});

  const { data, isLoading, error } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    staleTime: 60_000,
  });

  const allSettings = data?.settings ?? [];

  const groups = useMemo(
    () => ["All", ...Array.from(new Set(allSettings.map((s) => s.group)))],
    [allSettings]
  );

  const visible = filter === "All"
    ? allSettings
    : allSettings.filter((s) => s.group === filter);

  // separate policy setting from the rest
  const nonPolicies = visible.filter((s) => s.key !== "REMEDIATION_POLICIES");
  const policySetting = allSettings.find((s) => s.key === "REMEDIATION_POLICIES");

  function showToast(msg: string, ok: boolean) {
    setToast({ msg, ok });
    setTimeout(() => setToast(null), 3500);
  }

  async function handleSave(key: string, value: unknown) {
    setSaving(key);
    try {
      const result = await patchSettings({ [key]: value });
      if (result.errors.length > 0) {
        showToast(`Error: ${result.errors[0].error}`, false);
      } else {
        await qc.invalidateQueries({ queryKey: ["settings"] });
        showToast(`${key} updated`, true);
      }
    } catch (e) {
      showToast(String(e), false);
    } finally {
      setSaving(null);
    }
  }

  async function handleReset(key: string) {
    setSaving(key);
    try {
      await resetSetting(key);
      await qc.invalidateQueries({ queryKey: ["settings"] });
      showToast(`${key} reset to default`, true);
    } catch (e) {
      showToast(String(e), false);
    } finally {
      setSaving(null);
    }
  }

  function startPolicyEdit() {
    const current = (policySetting?.current_value ?? policySetting?.default) as Record<
      string,
      { action: string; replay_after_fix: boolean }
    >;
    setLocalPolicies(JSON.parse(JSON.stringify(current)));
    setEditingPolicies(true);
  }

  async function savePolicies() {
    await handleSave("REMEDIATION_POLICIES", localPolicies);
    setEditingPolicies(false);
  }

  return (
    <div className={styles.page}>
      {/* ── header ── */}
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>
            <SvgIcon name="settings" size={22} style={{ marginRight: 8 }} />
            Runtime Settings
          </h1>
          <p className={styles.pageSubtitle}>
            Changes apply immediately — no restart required. All overrides are persisted to the database.
          </p>
        </div>
        <div className={styles.impactLegend}>
          <span className={`${styles.impactBadge} ${styles.impactHigh}`}>high impact</span>
          <span className={`${styles.impactBadge} ${styles.impactMedium}`}>medium impact</span>
          <span className={`${styles.impactBadge} ${styles.impactLow}`}>low impact</span>
        </div>
      </div>

      {/* ── disclaimer ── */}
      <div className={styles.disclaimer}>
        <SvgIcon name="warning" size={15} style={{ flexShrink: 0, marginTop: 1 }} />
        <div>
          <strong>When do changes take effect?</strong> Every setting is saved to the database
          immediately and loaded into memory. However, most changes only apply to the{" "}
          <em>next</em> incident, cycle, or tool call — they do not interrupt work already in
          progress. The only exception is <code>Enable Autonomous Fixing</code>, which takes
          effect instantly across all agents. Each setting row shows its specific timing below
          the description.
        </div>
      </div>

      {/* ── appearance ── */}
      <div className={styles.appearanceCard}>
        <h2 className={styles.appearanceTitle}>Appearance</h2>
        <p className={styles.appearanceDesc}>Choose a colour theme. Applied instantly and remembered across sessions.</p>
        <div className={styles.themeGrid}>
          {THEMES.map((t) => (
            <button
              key={t.id}
              className={`${styles.themeBtn} ${activeTheme === t.id ? styles.themeBtnActive : ""}`}
              onClick={() => { setActiveTheme(t.id); applyTheme(t.id); }}
              title={t.label}
            >
              <div className={styles.themePreview} style={{ background: t.bg }}>
                <div className={styles.themeSidebar} style={{ background: t.sidebar }} />
                <div className={styles.themeAccent}  style={{ background: t.primary }} />
              </div>
              <span className={styles.themeLabel}>{t.label}</span>
              {activeTheme === t.id && <span className={styles.themeCheck}>✓</span>}
            </button>
          ))}
        </div>

        <div className={styles.fontSizeRow}>
          <span className={styles.fontSizeLabel}>Font Size</span>
          <div className={styles.fontSizeBtns}>
            {FONT_SIZES.map((f) => (
              <button
                key={f.id}
                className={`${styles.fontSizeBtn} ${activeFontSize === f.id ? styles.fontSizeBtnActive : ""}`}
                onClick={() => { setActiveFontSize(f.id); applyFontSize(f.id); }}
                title={f.value}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ── health check ── */}
      <HealthCheckPanel />

      {/* ── toast ── */}
      {toast && (
        <div className={`${styles.toast} ${toast.ok ? styles.toastOk : styles.toastErr}`}>
          <SvgIcon name={toast.ok ? "check-circle" : "warning"} size={15} />
          {toast.msg}
        </div>
      )}

      {/* ── group filter tabs ── */}
      <div className={styles.tabs}>
        {groups.map((g) => (
          <button
            key={g}
            className={`${styles.tab} ${filter === g ? styles.tabActive : ""}`}
            onClick={() => setFilter(g)}
          >
            {g}
          </button>
        ))}
      </div>

      {/* ── loading / error ── */}
      {isLoading && <div className={styles.state}>Loading settings…</div>}
      {error   && <div className={`${styles.state} ${styles.stateError}`}>Failed to load settings.</div>}

      {/* ── non-policy settings ── */}
      {!isLoading && !error && (
        <div className={styles.settingsList}>
          {nonPolicies.map((s) => (
            <SettingRow
              key={s.key}
              setting={s}
              onSave={handleSave}
              onReset={handleReset}
              saving={saving}
            />
          ))}
        </div>
      )}

      {/* ── remediation policies ── */}
      {!isLoading && !error && policySetting && (filter === "All" || filter === "Remediation Policies") && (
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <div>
              <h2 className={styles.sectionTitle}>Remediation Policies</h2>
              <p className={styles.sectionDesc}>
                {policySetting.description}
              </p>
              <p className={styles.sectionDesc} style={{ marginTop: 6 }}>
                <strong>Action meanings:</strong>{" "}
                <span><code>AUTO_FIX</code> — agent applies fix automatically. </span>
                <span><code>APPROVAL</code> — fix is staged and waits for human approval. </span>
                <span><code>RETRY</code> — the failed message is retried without changing the iFlow. </span>
                <span><code>TICKET_CREATED</code> — a support ticket is raised; no fix is attempted. </span>
                <strong>Replay After Fix</strong> — when enabled, the original failed message is
                re-submitted to the iFlow after a successful fix.
              </p>
            </div>
            <div className={styles.policyControls}>
              <span className={`${styles.impactBadge} ${styles.impactHigh}`}>high impact</span>
              {policySetting.is_overridden && (
                <span className={styles.overriddenBadge}>customised</span>
              )}
              {!editingPolicies ? (
                <div style={{ display: "flex", gap: 8 }}>
                  <button className={styles.btnEdit} onClick={startPolicyEdit}>
                    <SvgIcon name="wrench" size={13} /> Edit policies
                  </button>
                  {policySetting.is_overridden && (
                    <button
                      className={styles.btnReset}
                      onClick={() => handleReset("REMEDIATION_POLICIES")}
                      disabled={saving === "REMEDIATION_POLICIES"}
                    >
                      <SvgIcon name="refresh" size={13} /> Reset
                    </button>
                  )}
                </div>
              ) : (
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className={styles.btnSave}
                    onClick={savePolicies}
                    disabled={saving === "REMEDIATION_POLICIES"}
                  >
                    {saving === "REMEDIATION_POLICIES" ? "Saving…" : "Save policies"}
                  </button>
                  <button className={styles.btnCancel} onClick={() => setEditingPolicies(false)}>
                    Cancel
                  </button>
                </div>
              )}
            </div>
          </div>

          {editingPolicies ? (
            <PolicyEditor policies={localPolicies} onChange={setLocalPolicies} />
          ) : (
            <PolicyEditor
              policies={
                (policySetting.current_value ?? policySetting.default) as Record<
                  string,
                  { action: string; replay_after_fix: boolean }
                >
              }
              onChange={() => {}}
            />
          )}
        </div>
      )}
    </div>
  );
}
