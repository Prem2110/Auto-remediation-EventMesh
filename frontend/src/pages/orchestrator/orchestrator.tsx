import {
  useEffect,
  useRef,
  useState,
  useCallback,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "../../store/app-store.ts";
import { sendChatMessage, fetchPipelineStatus } from "../../services/api.ts";
import type { IHistoryEntry } from "../../types/index.ts";
import styles from "./orchestrator.module.css";

const QUICK_PROMPTS = [
  "Create a new iFlow for ORDERS05",
  "Show me all failed messages",
  "Generate CPI adapter documentation",
  "Test iFlow 'PO_to_ECC'",
];

function formatTs(input?: string): string {
  const d = input ? (() => { const c = input.trim().replace(" ", "T").split(".")[0]; const x = new Date(c); return isNaN(x.getTime()) ? new Date() : x; })() : new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function mdToHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>[\s\S]+?<\/li>)/g, "<ul>$1</ul>");
}

export default function Orchestrator() {
  const { id } = useParams<{ id?: string }>();
  const { history, user, chatBubbles, addChatBubble, clearChatBubbles } = useAppStore();

  const { data: pipelineData } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn:  fetchPipelineStatus,
    refetchInterval: 10_000,
  });
  const pipelineOn = pipelineData?.pipeline_running ?? false;

  const [prompt, setPrompt] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<{ name: string; file: File; id: string }[]>([]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Load history when route ID changes
  useEffect(() => {
    clearChatBubbles();
    if (!id || id === "0") return;
    const record: IHistoryEntry | undefined = history.find((h) => h.id === id);
    if (!record) return;
    record.history.forEach((entry) => {
      addChatBubble({ id: `u-${entry.created_at}`, role: "user",  text: entry.question, timestamp: entry.created_at });
      addChatBubble({ id: `b-${entry.created_at}`, role: "bot",   text: entry.result,   timestamp: entry.created_at });
    });
  }, [id, history]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatBubbles, sending]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 128) + "px";
  }, [prompt]);

  const handleSend = useCallback(async (text?: string) => {
    const msg = (text ?? prompt).trim();
    if (!msg && attachments.length === 0) return;

    addChatBubble({ id: `u-${Date.now()}`, role: "user", text: msg, timestamp: new Date().toISOString() });
    setPrompt("");
    setAttachments([]);

    const fd = new FormData();
    fd.append("query", msg);
    fd.append("user_id", user.email);
    attachments.forEach(({ file }) => fd.append("files", file, file.name));

    setSending(true);
    try {
      const res = await sendChatMessage(fd);
      addChatBubble({ id: `b-${Date.now()}`, role: "bot",   text: res.response || "No response.", timestamp: new Date().toISOString() });
    } catch (err) {
      addChatBubble({ id: `e-${Date.now()}`, role: "error", text: err instanceof Error ? err.message : "Request failed.", timestamp: new Date().toISOString() });
    } finally {
      setSending(false);
    }
  }, [prompt, attachments, user.email, addChatBubble]);

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  }

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files || []);
    setAttachments((p) => [...p, ...files.map((f) => ({ name: f.name, file: f, id: `${Date.now()}-${f.name}` }))]);
    e.target.value = "";
  }

  const isNewChat = !id || id === "0";

  return (
    <div className={styles.page}>

      {/* ── Pipeline Status Bar ── */}
      <div className={styles.aemBar} data-connected={String(pipelineOn)}>
        <span className={styles.aemBarDot} />
        <span className={styles.aemBarLabel}>
          Event Mesh Pipeline
        </span>
        <span className={styles.aemBarSep}>·</span>
        <span className={styles.aemBarItem}>
          Pipeline: <strong>{pipelineOn ? "Running" : "Stopped"}</strong>
        </span>
      </div>

      {/* ── Welcome ── */}
      {isNewChat && chatBubbles.length === 0 && (
        <div className={styles.welcome}>
          <div className={styles.welcomeOrb}>🤖</div>
          <h2 className={styles.welcomeTitle}>
            Hi {user.firstname}, how can I help?
          </h2>
          <p className={styles.welcomeHint}>
            Ask me anything about SAP Integration Suite — creating iFlows,
            mapping messages, running tests, or monitoring errors.
          </p>
          <div className={styles.quickPrompts}>
            {QUICK_PROMPTS.map((qp) => (
              <button
                key={qp}
                className={styles.quickBtn}
                onClick={() => handleSend(qp)}
              >
                {qp}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Chat messages ── */}
      {chatBubbles.length > 0 && (
        <div className={styles.chatArea}>
          {chatBubbles.map((b) => {
            if (b.role === "user") {
              return (
                <div key={b.id} className={styles.userRow}>
                  <div className={styles.userBubble}>
                    <span className={styles.userText}>{b.text}</span>
                    <span className={styles.ts}>{formatTs(b.timestamp)}</span>
                  </div>
                </div>
              );
            }
            if (b.role === "error") {
              return (
                <div key={b.id} className={styles.errorRow}>
                  <span className={styles.errorIcon}>⚠️</span>
                  <div className={styles.errorBubble}>
                    <span className={styles.errorText}>{b.text}</span>
                  </div>
                </div>
              );
            }
            return (
              <div key={b.id} className={styles.botRow}>
                <div className={styles.botAvatar}>🤖</div>
                <div className={styles.botBubble}>
                  <div
                    className={styles.botText}
                    dangerouslySetInnerHTML={{ __html: mdToHtml(b.text) }}
                  />
                  <span className={styles.tsBot}>{formatTs(b.timestamp)}</span>
                </div>
              </div>
            );
          })}

          {/* Typing indicator */}
          {sending && (
            <div className={styles.botRow}>
              <div className={styles.botAvatar}>🤖</div>
              <div className={styles.botBubble}>
                <div className={styles.thinking}>
                  <span /><span /><span />
                </div>
              </div>
            </div>
          )}
          <div ref={chatEndRef} />
        </div>
      )}

      {/* ── Input area ── */}
      <div className={styles.inputArea}>
        {/* Attachment chips */}
        {attachments.length > 0 && (
          <div className={styles.attachRow}>
            {attachments.map((a) => (
              <div key={a.id} className={styles.fileChip}>
                📎 <span className={styles.chipText}>{a.name}</span>
                <button className={styles.chipX} onClick={() => setAttachments((p) => p.filter((x) => x.id !== a.id))}>✕</button>
              </div>
            ))}
          </div>
        )}

        <div className={styles.inputRow}>
          <textarea
            ref={textareaRef}
            className={styles.textarea}
            rows={1}
            placeholder="Ask anything about SAP Integration Suite… (Enter to send, Shift+Enter for newline)"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={sending}
          />

          <button
            className={styles.iconBtn}
            title="Attach file"
            onClick={() => fileInputRef.current?.click()}
            disabled={sending}
          >
            📎
          </button>
          <input ref={fileInputRef} type="file" multiple hidden onChange={handleFileChange} />

          <button
            className={styles.sendBtn}
            disabled={sending || (!prompt.trim() && attachments.length === 0)}
            onClick={() => handleSend()}
            title="Send"
          >
            ➤
          </button>
        </div>
      </div>
    </div>
  );
}
