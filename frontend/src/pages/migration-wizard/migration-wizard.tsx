import { useState, useRef } from "react";
import { uploadMigrationFiles } from "../../services/api.ts";
import type { IUploadedFile, FileSource } from "../../types/index.ts";
import SvgIcon from "../../components/icons/SvgIcon.tsx";
import styles from "./migration-wizard.module.css";

type WizardStep = "upload" | "config" | "preview";

const STEPS: { key: WizardStep; label: string }[] = [
  { key: "upload", label: "Upload Files" },
  { key: "config", label: "Configuration" },
  { key: "preview", label: "Preview" },
];

export default function MigrationWizard() {
  const [step, setStep]           = useState<WizardStep>("upload");
  const [files, setFiles]         = useState<IUploadedFile[]>([]);
  const [fileMap, setFileMap]     = useState<Map<string, File>>(new Map());
  const [iflowName, setIflowName] = useState("");
  const [issueType, setIssueType] = useState<"UDF" | "Java">("UDF");
  const [loading, setLoading]     = useState(false);
  const [oldCode, setOldCode]     = useState("");
  const [newCode, setNewCode]     = useState("");
  const [error, setError]         = useState("");

  const codeFileRef  = useRef<HTMLInputElement>(null);
  const errorFileRef = useRef<HTMLInputElement>(null);

  function addFiles(rawFiles: FileList | null, source: FileSource) {
    if (!rawFiles || rawFiles.length === 0) return;
    const newEntries: IUploadedFile[] = [];
    const newMap = new Map(fileMap);
    Array.from(rawFiles).forEach((file) => {
      const fileId = `${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
      newEntries.push({
        fileId,
        fileName:     file.name,
        fileType:     file.name.split(".").pop() || "",
        fileSizeKB:   (file.size / 1024).toFixed(2),
        lastModified: new Date(file.lastModified).toLocaleString(),
        source,
      });
      newMap.set(fileId, file);
    });
    setFiles((prev) => [...prev, ...newEntries]);
    setFileMap(newMap);
  }

  function removeFile(fileId: string) {
    setFiles((prev) => prev.filter((f) => f.fileId !== fileId));
    setFileMap((prev) => { const m = new Map(prev); m.delete(fileId); return m; });
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>, source: FileSource) {
    e.preventDefault();
    addFiles(e.dataTransfer.files, source);
  }

  async function handleConvert() {
    if (files.length === 0) { setError("Please upload at least one file."); return; }
    setError("");
    setLoading(true);
    const formData = new FormData();
    formData.append("name", iflowName);
    formData.append("issue", issueType);
    files.forEach((meta) => {
      const file = fileMap.get(meta.fileId);
      if (!file) return;
      formData.append(meta.source === "CODEFILE" ? "source_code" : "error_screenshot", file);
    });
    try {
      const [res] = await Promise.all([
        uploadMigrationFiles(formData),
        new Promise((r) => setTimeout(r, 2000)),
      ]);
      if (res.newCode) {
        setOldCode(res.oldCode || "");
        setNewCode(res.newCode);
        setStep("preview");
      } else {
        setError("No valid response received from server.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "File upload failed.");
    } finally {
      setLoading(false);
    }
  }

  function handleDownloadResult() {
    if (!newCode.trim()) return;
    const blob = new Blob([newCode], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "result.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function handleReset() {
    setStep("upload"); setFiles([]); setFileMap(new Map());
    setIflowName(""); setIssueType("UDF"); setOldCode(""); setNewCode(""); setError("");
  }

  const currentIdx = STEPS.findIndex((s) => s.key === step);

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Migration Wizard</h1>

      {/* ── Step indicator ── */}
      <div className={styles.stepIndicator}>
        {STEPS.map((s, i) => {
          const isDone   = i < currentIdx;
          const isActive = i === currentIdx;
          return (
            <div key={s.key} className={styles.stepItem}>
              <div className={styles.stepContent}>
                <div
                  className={`${styles.stepNum} ${
                    isDone ? styles.stepNumDone : isActive ? styles.stepNumActive : styles.stepNumPending
                  }`}
                >
                  {isDone ? "✓" : i + 1}
                </div>
                <span
                  className={`${styles.stepLabel} ${
                    isActive ? styles.stepLabelActive : isDone ? styles.stepLabelDone : ""
                  }`}
                >
                  {s.label}
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <div className={`${styles.stepLine} ${isDone ? styles.stepLineDone : ""}`} />
              )}
            </div>
          );
        })}
      </div>

      {/* ── Step card ── */}
      <div className={styles.card}>
        {step === "upload"  && renderUploadStep()}
        {step === "config"  && renderConfigStep()}
        {step === "preview" && renderPreviewStep()}
      </div>
    </div>
  );

  // ── Step 1: Upload ──────────────────────────────────────────────────────────
  function renderUploadStep() {
    return (
      <>
        <h2 className={styles.stepHeading}>Upload Files</h2>
        <p className={styles.stepDesc}>Upload your source code and/or error screenshot files.</p>

        <div className={styles.uploaderGrid}>
          <DropZone
            label="Source Code File"
            source="CODEFILE"
            onDrop={(e) => handleDrop(e, "CODEFILE")}
            onBrowse={() => codeFileRef.current?.click()}
          />
          <input ref={codeFileRef} type="file" multiple hidden
            onChange={(e) => { addFiles(e.target.files, "CODEFILE"); e.target.value = ""; }} />

          <DropZone
            label="Error Screenshot"
            source="ERROR"
            onDrop={(e) => handleDrop(e, "ERROR")}
            onBrowse={() => errorFileRef.current?.click()}
          />
          <input ref={errorFileRef} type="file" multiple hidden
            onChange={(e) => { addFiles(e.target.files, "ERROR"); e.target.value = ""; }} />
        </div>

        {files.length > 0 && (
          <div className={styles.fileList}>
            <div className={styles.fileListHeader}>Uploaded Files ({files.length})</div>
            {files.map((f) => (
              <div key={f.fileId} className={styles.fileRow}>
                <span className={styles.fileEmoji}>
                  <SvgIcon name={f.source === "CODEFILE" ? "document" : "image"} size={18} />
                </span>
                <div className={styles.fileMeta}>
                  <span className={styles.fileName}>{f.fileName}</span>
                  <span className={styles.fileInfo}>{f.fileSizeKB} KB · {f.lastModified}</span>
                </div>
                <span className={`${styles.sourceTag} ${f.source === "CODEFILE" ? styles.tagCode : styles.tagError}`}>
                  {f.source === "CODEFILE" ? "Code" : "Error"}
                </span>
                <button className={styles.removeBtn} onClick={() => removeFile(f.fileId)} title="Remove">✕</button>
              </div>
            ))}
          </div>
        )}

        {error && <div className={styles.errorStrip}>{error}</div>}

        <div className={styles.stepFooter}>
          <span />
          <div className={styles.footerRight}>
            <button className={styles.btnPrimary} disabled={files.length === 0}
              onClick={() => setStep("config")}>
              Next →
            </button>
          </div>
        </div>
      </>
    );
  }

  // ── Step 2: Config ──────────────────────────────────────────────────────────
  function renderConfigStep() {
    return (
      <>
        <h2 className={styles.stepHeading}>Configuration</h2>
        <p className={styles.stepDesc}>Name your iFlow and select the conversion type.</p>

        <div className={styles.formField}>
          <label className={styles.label} htmlFor="iflowNameInput">iFlow Name</label>
          <input
            id="iflowNameInput"
            className={styles.nameInput}
            placeholder="Enter iFlow name"
            value={iflowName}
            onChange={(e) => setIflowName(e.target.value)}
          />
        </div>

        <div className={styles.formField}>
          <label className={styles.label}>Conversion Type</label>
          <div className={styles.radioGroup}>
            <label className={styles.radioOption}>
              <input type="radio" name="issueType" value="UDF" checked={issueType === "UDF"}
                onChange={() => setIssueType("UDF")} />
              UDF
            </label>
            <label className={styles.radioOption}>
              <input type="radio" name="issueType" value="Java" checked={issueType === "Java"}
                onChange={() => setIssueType("Java")} />
              Java
            </label>
          </div>
        </div>

        {error && <div className={styles.errorStrip}>{error}</div>}

        {loading && (
          <div className={styles.loadingBox}>
            <div className={styles.spinner} />
            <span className={styles.loadingText}>Converting your files, please wait…</span>
          </div>
        )}

        <div className={styles.stepFooter}>
          <button className={styles.btnSecondary} disabled={loading}
            onClick={() => { setStep("upload"); setError(""); }}>
            ← Back
          </button>
          <div className={styles.footerRight}>
            <button className={styles.btnPrimary}
              disabled={loading || files.length === 0} onClick={handleConvert}>
              {loading ? "Converting…" : "Convert →"}
            </button>
          </div>
        </div>
      </>
    );
  }

  // ── Step 3: Preview ─────────────────────────────────────────────────────────
  function renderPreviewStep() {
    return (
      <>
        <h2 className={styles.stepHeading}>Review Result</h2>
        <p className={styles.stepDesc}>Compare the original and converted code before downloading.</p>

        <div className={styles.codeGrid}>
          {oldCode && (
            <div className={styles.codePanel}>
              <span className={styles.codePanelLabel}>Original Code</span>
              <pre className={styles.codeBlock}>{oldCode}</pre>
            </div>
          )}
          <div className={styles.codePanel}>
            <span className={styles.codePanelLabel}>Converted Code</span>
            <pre className={styles.codeBlock}>{newCode}</pre>
          </div>
        </div>

        <div className={styles.stepFooter}>
          <button className={styles.btnSecondary} onClick={handleReset}>
            <SvgIcon name="refresh" size={14} style={{ verticalAlign: "middle", marginRight: "0.3rem" }} />Start Over
          </button>
          <div className={styles.footerRight}>
            <button className={styles.btnSuccess} onClick={handleDownloadResult}>
              <SvgIcon name="download" size={14} style={{ verticalAlign: "middle", marginRight: "0.3rem" }} />Download Result
            </button>
          </div>
        </div>
      </>
    );
  }
}

// ── Drop Zone ────────────────────────────────────────────────────────────────
interface DropZoneProps {
  label:    string;
  source:   FileSource;
  onDrop:   (e: React.DragEvent<HTMLDivElement>) => void;
  onBrowse: () => void;
}

function DropZone({ label, source, onDrop, onBrowse }: DropZoneProps) {
  const [hovering, setHovering] = useState(false);
  const isCode = source === "CODEFILE";

  return (
    <div
      className={`${styles.dropZone} ${hovering ? styles.dropZoneHover : ""}`}
      onDragOver={(e) => { e.preventDefault(); setHovering(true); }}
      onDragLeave={() => setHovering(false)}
      onDrop={(e) => { setHovering(false); onDrop(e); }}
    >
      <span className={styles.dropIcon}><SvgIcon name={isCode ? "folder" : "image"} size={28} /></span>
      <span className={styles.dropLabel}>{label}</span>
      <span className={`${styles.dropType} ${isCode ? styles.dropTypeCode : styles.dropTypeError}`}>
        {isCode ? "Code File" : "Error / Screenshot"}
      </span>
      <span className={styles.dropHint}>Drag & drop files here, or</span>
      <button className={styles.browseBtn} onClick={onBrowse} type="button">Browse Files</button>
    </div>
  );
}
