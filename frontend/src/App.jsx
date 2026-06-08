import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  BarChart2, BookMarked, ChevronDown, ChevronUp,
  Clock, LogOut, Mail, MessageSquare, Moon, Send,
  Square, Sun,
} from "lucide-react";

import {
  ApiError,
  createChatMessage,
  createChatSession,
  createSavedAnswer,
  deleteSavedAnswer,
  fetchPublicShare,
  getAdminStatus,
  getWorkspaceStatus,
  listGmailConnections,
  listSavedAnswers,
  listSharesForAnswer,
  revokeShareLink,
  runGmailIngest,
  runSlackIngest,
  shareSavedAnswer,
  streamQuery,
} from "./api.js";
import SlackSettings from "./slack/SlackSettings.jsx";
import GmailSettings from "./gmail/GmailSettings.jsx";
import AnalyticsPanel from "./AnalyticsPanel.jsx";
import { useAuth } from "./auth/AuthContext.jsx";
import { useWorkspace } from "./auth/WorkspaceContext.jsx";

const MODES = [
  { value: "default",      label: "Default — concise answer" },
  { value: "summary",      label: "Summary — bullet briefing" },
  { value: "decisions",    label: "Decisions — extract decisions" },
  { value: "action_items", label: "Action items — extract tasks" },
  { value: "who_said",     label: "Who said — quote attribution" },
  { value: "exact",        label: "Exact — keyword match" },
  { value: "hybrid",       label: "Hybrid — semantic + keyword" },
];

const DOC_TYPES = [
  { value: "",        label: "Any" },
  { value: "message", label: "Message" },
  { value: "thread",  label: "Thread" },
];

// Source filter (Phase 9). Each option carries the literal value the
// `sources` state holds and the array shape the backend wants. We use
// a sentinel UI value ("all") rather than the empty list so the
// <select> rendering is straightforward; sourcesToList() does the
// conversion at request-build time.
const SOURCE_OPTIONS = [
  { value: "all",   label: "All sources" },
  { value: "slack", label: "Slack only"  },
  { value: "gmail", label: "Gmail only"  },
  { value: "both",  label: "Slack + Gmail" },
];

/**
 * Convert the UI's `sources` choice into the `allowed_sources` array
 * the backend expects (or `null` when the request should be sent
 * without the field, preserving the pre-Phase-9 default).
 *
 *   "all"   -> null          (omit the field; default = all sources)
 *   "slack" -> ["slack"]
 *   "gmail" -> ["gmail"]
 *   "both"  -> ["slack","gmail"]   (explicit "allow both" — same
 *                                   observable behavior as "all" but
 *                                   distinguishes user intent in logs.)
 */
function sourcesToList(value) {
  switch (value) {
    case "slack": return ["slack"];
    case "gmail": return ["gmail"];
    case "both":  return ["slack", "gmail"];
    case "all":
    default:      return null;
  }
}

// ----------------------------------------------------------------------
// Query history (localStorage)
// ----------------------------------------------------------------------
// We store the last N user queries — NOT assistant answers — so a returning
// user can re-fire a past question with its filters. All reads/writes go
// through safe wrappers; if localStorage is disabled (private mode, quota
// exceeded, SSR), we silently degrade to an in-memory-only experience.
const HISTORY_STORAGE_KEY = "secondBrain.queryHistory";
const HISTORY_LIMIT = 30;

/**
 * The shape we persist per history item. Kept compact: nothing about the
 * conversation context (multi-turn memory) is stored — that's session-only
 * by design.
 */
function makeHistoryItem(params) {
  return {
    id:            crypto.randomUUID(),
    timestamp:     Date.now(),
    question:      params.question || "",
    mode:          params.mode || "default",
    topK:          params.topK ?? 5,
    channel:       params.channel || "",
    user:          params.user || "",
    documentType:  params.documentType || "",
    sources:       params.sources || "all",
    dateQuery:     params.dateQuery || "",
    startDate:     params.startDate || "",
    endDate:       params.endDate || "",
  };
}

function loadHistory() {
  try {
    const raw = window.localStorage.getItem(HISTORY_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Defensive: filter out anything that doesn't at least have a
    // question — schema drift across versions shouldn't break the panel.
    return parsed.filter(
      (it) => it && typeof it.question === "string" && it.question.trim()
    );
  } catch {
    return [];
  }
}

function saveHistory(items) {
  try {
    window.localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(items));
  } catch {
    // QuotaExceeded, private-mode lockdown, etc. We don't surface this —
    // the in-memory state still works for the rest of the session.
  }
}

/**
 * Push a new entry to the front of `prev`, capped at HISTORY_LIMIT.
 * Deduplicates adjacent identical entries so clicking "Ask" twice in a
 * row doesn't fill the panel with duplicates. We only compare the
 * head — two identical queries with a different one between them are
 * both kept on purpose (they represent real re-runs spaced over time).
 */
function pushHistoryItem(prev, params) {
  const item = makeHistoryItem(params);
  if (prev.length > 0 && isSameQuery(prev[0], item)) {
    // Update the timestamp of the existing head so "Last asked" stays fresh.
    return [{ ...prev[0], timestamp: item.timestamp }, ...prev.slice(1)];
  }
  return [item, ...prev].slice(0, HISTORY_LIMIT);
}

function isSameQuery(a, b) {
  // Treat two items as the same query when every user-visible field
  // matches. Timestamp / id are intentionally not part of the comparison.
  const FIELDS = [
    "question", "mode", "topK", "channel", "user",
    "documentType", "dateQuery", "startDate", "endDate",
  ];
  return FIELDS.every((f) => (a[f] ?? "") === (b[f] ?? ""));
}

// ----------------------------------------------------------------------
// Saved answers (localStorage)
// ----------------------------------------------------------------------
// Like query history but stores full assistant ANSWERS — not just the
// question/filters — so users can revisit useful results without re-running
// the LLM. We cap at 50 to keep the localStorage payload bounded; assistant
// answers + source arrays add up faster than raw query items.
const SAVED_STORAGE_KEY = "secondBrain.savedAnswers";
const SAVED_LIMIT = 50;

/**
 * Build a saved item from a completed assistant entry + the user-bubble
 * that triggered it. We snapshot the user's question + filters + the
 * answer + sources + debug at the moment of save, so the saved view can
 * replay the bubble exactly even if the LLM, the indexed Slack data, or
 * the local query history change later.
 */
function makeSavedItem({ question, params, answer, sources, debug }) {
  return {
    id:        crypto.randomUUID(),
    timestamp: Date.now(),
    question:  question || "",
    answer:    answer || "",
    sources:   Array.isArray(sources) ? sources : [],
    mode:      (params && params.mode) || "default",
    filters: {
      topK:         (params && params.topK) ?? 5,
      channel:      (params && params.channel) || "",
      user:         (params && params.user) || "",
      documentType: (params && params.documentType) || "",
      sources:      (params && params.sources) || "all",
      dateQuery:    (params && params.dateQuery) || "",
      startDate:    (params && params.startDate) || "",
      endDate:      (params && params.endDate) || "",
    },
    // Snapshot debug too so the saved-view can re-show cache/mode/
    // inference badges. Stored on a best-effort basis — if missing,
    // the saved view simply won't render those badges.
    debug: debug || null,
  };
}

function loadSavedAnswers() {
  try {
    const raw = window.localStorage.getItem(SAVED_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Defensive: drop entries missing the bare minimum fields. Schema
    // drift across versions shouldn't crash the panel.
    return parsed.filter(
      (it) => it
        && typeof it.id === "string"
        && typeof it.question === "string"
        && typeof it.answer === "string"
    );
  } catch {
    return [];
  }
}

function saveSavedAnswers(items) {
  try {
    window.localStorage.setItem(SAVED_STORAGE_KEY, JSON.stringify(items));
  } catch {
    // QuotaExceeded / private mode / disabled storage — silently degrade
    // to in-memory only, just like the query-history layer.
  }
}

/**
 * Push to the front of `prev`, capped at SAVED_LIMIT.
 * We DO NOT dedupe by question text — the same question can produce
 * different answers over time (different filters, refreshed Slack
 * data) and the user may legitimately want to save several variants.
 */
function pushSavedItem(prev, item) {
  return [item, ...prev].slice(0, SAVED_LIMIT);
}

// ----------------------------------------------------------------------
// Theme (light / dark) — persisted in localStorage, system pref by default
// ----------------------------------------------------------------------
const THEME_STORAGE_KEY = "secondBrain.theme";

function loadTheme() {
  // 1. Honor an explicit user choice if one was saved.
  try {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Private mode / disabled storage — fall through to system pref.
  }
  // 2. Default to the OS preference.
  try {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
  } catch {
    // matchMedia missing in some test environments.
  }
  return "light";
}

function saveTheme(theme) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Silent fallback — theme is still applied via React state.
  }
}

/**
 * Produce a short plaintext preview of an answer for use in the saved
 * panel. Strips the most common markdown constructs so the preview
 * doesn't render as `**bold** ## heading ` raw tokens.
 *
 * Intentionally light-touch — we're not parsing markdown, just cleaning
 * the visible noise. Worst case a fancy answer renders with a few stray
 * asterisks in the preview, which is acceptable.
 */
function buildAnswerPreview(answer, max = 140) {
  if (typeof answer !== "string" || !answer) return "";
  let s = answer
    // strip fenced code blocks first so their language tags don't survive
    .replace(/```[\s\S]*?```/g, " ")
    // inline code -> bare contents
    .replace(/`([^`]+)`/g, "$1")
    // images -> alt text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    // links -> link text
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    // bold/italic markers
    .replace(/[*_]{1,3}([^*_]+)[*_]{1,3}/g, "$1")
    // leading list markers / headings
    .replace(/^\s*([#>\-*+]|\d+\.)\s+/gm, "")
    // collapse whitespace
    .replace(/\s+/g, " ")
    .trim();
  if (s.length > max) s = s.slice(0, max - 1).trimEnd() + "…";
  return s;
}

// ----------------------------------------------------------------------
// Export helpers (Markdown + TXT)
// ----------------------------------------------------------------------

/**
 * Zero-pad an integer to at least 2 digits. Tiny helper, used only by
 * the timestamp formatter below.
 */
function _pad2(n) {
  return String(n).padStart(2, "0");
}

/**
 * Build the YYYY-MM-DD-HH-mm portion of an export filename, in LOCAL
 * time so the filename matches what the user sees on the clock. Accepts
 * an optional ms-epoch so saved-item exports can use the bookmark's
 * `timestamp` rather than the moment of clicking Export.
 */
function _filenameTimestamp(epochMs) {
  const d = epochMs ? new Date(epochMs) : new Date();
  return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}`
       + `-${_pad2(d.getHours())}-${_pad2(d.getMinutes())}`;
}

/**
 * Build a safe export filename of the form
 *   second-brain-answer-YYYY-MM-DD-HH-mm.{md,txt}
 *
 * The filename is fixed-shape — no part of the question is interpolated,
 * which means we don't have to defend against path traversal, slashes,
 * Unicode lookalikes, etc. The only variable parts are the timestamp
 * digits and the extension we control.
 */
function safeExportFilename(extension, epochMs) {
  const ext = extension === "md" || extension === "txt" ? extension : "txt";
  return `second-brain-answer-${_filenameTimestamp(epochMs)}.${ext}`;
}

/**
 * Format a Slack ts string ("1778775842.876209") or a unix-seconds
 * number into a human-friendly date string for inclusion in exports.
 * Returns an empty string when the value is unparseable.
 */
function _formatTimestampForExport(value) {
  if (value === null || value === undefined || value === "") return "";
  let n = NaN;
  if (typeof value === "number") n = value;
  else if (typeof value === "string") {
    const parsed = parseFloat(value.trim());
    if (!Number.isNaN(parsed)) n = parsed;
  }
  if (!Number.isFinite(n)) return "";
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return "";
  // ISO-style, local: "2026-05-18 14:32:09"
  return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}`
       + ` ${_pad2(d.getHours())}:${_pad2(d.getMinutes())}:${_pad2(d.getSeconds())}`;
}

/**
 * Render an answer as a Markdown document with the question, the
 * assistant's answer body, the sources, and an exported-at footer.
 *
 * The answer body is included verbatim — the LLM already produces
 * markdown, so re-formatting would risk breaking citations or code
 * blocks. Source snippets are wrapped in blockquotes so any markdown
 * special characters inside them render literally.
 */
function buildMarkdownExport({ question, answer, sources, exportedAt }) {
  const when = exportedAt ? new Date(exportedAt) : new Date();
  const lines = [];

  lines.push("# " + (question || "(no question)").trim());
  lines.push("");
  lines.push("## Answer");
  lines.push("");
  lines.push((answer || "").trim() || "_(empty answer)_");
  lines.push("");

  const arr = Array.isArray(sources) ? sources : [];
  if (arr.length > 0) {
    lines.push("## Sources");
    lines.push("");
    arr.forEach((s, i) => {
      const num = (typeof s.index === "number" && s.index > 0) ? s.index : (i + 1);
      lines.push(`### [${num}] ${s.channel ? "#" + s.channel : "(unknown channel)"}`);
      lines.push("");
      const metaParts = [];
      if (s.user) metaParts.push(`**User:** ${s.user}`);
      const ts = _formatTimestampForExport(s.timestamp);
      if (ts) metaParts.push(`**Timestamp:** ${ts}`);
      if (s.document_type) metaParts.push(`**Type:** ${s.document_type}`);
      if (metaParts.length > 0) {
        lines.push(metaParts.join(" · "));
        lines.push("");
      }
      if (s.permalink) {
        lines.push(`**Link:** [${s.permalink}](${s.permalink})`);
        lines.push("");
      }
      if (s.snippet && s.snippet.trim()) {
        // Blockquote each snippet line so internal markdown chars (asterisks,
        // backticks, leading hashes) render as text, not syntax.
        const quoted = s.snippet.split("\n").map((ln) => `> ${ln}`).join("\n");
        lines.push(quoted);
        lines.push("");
      }
    });
  }

  lines.push("---");
  lines.push("");
  lines.push(`_Exported from Second Brain at ${_formatTimestampForExport(when.getTime() / 1000) || when.toISOString()}._`);
  lines.push("");
  return lines.join("\n");
}

/**
 * Plain-text variant of the markdown export. Same content, no markdown
 * syntax — suitable for pasting into a doc that won't render markdown.
 */
function buildTxtExport({ question, answer, sources, exportedAt }) {
  const when = exportedAt ? new Date(exportedAt) : new Date();
  const lines = [];

  lines.push("QUESTION");
  lines.push("--------");
  lines.push((question || "(no question)").trim());
  lines.push("");

  lines.push("ANSWER");
  lines.push("------");
  lines.push((answer || "").trim() || "(empty answer)");
  lines.push("");

  const arr = Array.isArray(sources) ? sources : [];
  if (arr.length > 0) {
    lines.push("SOURCES");
    lines.push("-------");
    lines.push("");
    arr.forEach((s, i) => {
      const num = (typeof s.index === "number" && s.index > 0) ? s.index : (i + 1);
      lines.push(`[${num}] ${s.channel ? "#" + s.channel : "(unknown channel)"}`);
      const metaParts = [];
      if (s.user) metaParts.push(`User: ${s.user}`);
      const ts = _formatTimestampForExport(s.timestamp);
      if (ts) metaParts.push(`Timestamp: ${ts}`);
      if (s.document_type) metaParts.push(`Type: ${s.document_type}`);
      if (metaParts.length > 0) lines.push("    " + metaParts.join(" | "));
      if (s.permalink) lines.push(`    Link: ${s.permalink}`);
      if (s.snippet && s.snippet.trim()) {
        // Indent snippet lines so the source structure stays readable
        // when pasted into a plain-text editor.
        s.snippet.split("\n").forEach((ln) => lines.push("    " + ln));
      }
      lines.push("");
    });
  }

  lines.push("--");
  lines.push(`Exported from Second Brain at ${_formatTimestampForExport(when.getTime() / 1000) || when.toISOString()}.`);
  lines.push("");
  return lines.join("\n");
}

/**
 * Trigger a browser download of `content` as `filename` with the given
 * MIME type. Uses the standard URL.createObjectURL + click + revoke
 * dance; no library required.
 *
 * Defensive: wrapped in try/catch so that the (very unusual) case of a
 * sandboxed iframe or other blocked-download environment fails silently
 * rather than throwing into a React render path.
 */
function downloadFile(content, filename, mimeType) {
  try {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    // Some browsers require the element to be in the DOM before .click().
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Revoke on the next tick so the click can resolve before we
    // invalidate the URL.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (err) {
    console.error("[export] download failed:", err);
  }
}

/**
 * High-level convenience: export the given turn (question/answer/sources)
 * as either "md" or "txt" with a safe filename and the right MIME type.
 * `epochMs` lets callers anchor the filename to a specific point in time
 * (e.g. saved-item exports use the bookmark's saved-at timestamp).
 */
function exportAnswer({ question, answer, sources, format, epochMs }) {
  const fmt = format === "md" ? "md" : "txt";
  const content = fmt === "md"
    ? buildMarkdownExport({ question, answer, sources, exportedAt: epochMs })
    : buildTxtExport({ question, answer, sources, exportedAt: epochMs });
  const filename = safeExportFilename(fmt, epochMs);
  const mime = fmt === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8";
  downloadFile(content, filename, mime);
}

function makeUserEntry(question, params) {
  return {
    id: crypto.randomUUID(),
    role: "user",
    question,
    params,
  };
}

function makePendingAssistantEntry() {
  return {
    id: crypto.randomUUID(),
    role: "assistant",
    streaming: true,
    aborted: false,
    error: null,
    answer: "",
    sources: [],
    debug: null,
  };
}

/**
 * Walk the chat timeline and return at most the last 6 (user, assistant)
 * messages in chronological order, formatted as the API expects:
 *   [{ role: "user", content: "..." }, { role: "assistant", content: "..." }, ...]
 *
 * Skips:
 *   - assistant bubbles still streaming (no completed turn yet)
 *   - assistant bubbles that errored (no real answer to feed back)
 *   - aborted bubbles whose `answer` is empty (no useful turn)
 * Aborted bubbles that DO have partial text are kept — that text was real
 * model output and is fair game for reference resolution.
 *
 * The backend caps at 6 as well, so this is defense in depth; doing it on
 * the client also keeps the request body small.
 */
function buildHistoryFromEntries(entries) {
  const turns = [];
  for (const entry of entries) {
    if (entry.role === "user") {
      const content = (entry.question || "").trim();
      if (content) turns.push({ role: "user", content });
      continue;
    }
    // assistant
    if (entry.streaming) continue;
    if (entry.error) continue;
    const content = (entry.answer || "").trim();
    if (!content) continue;
    turns.push({ role: "assistant", content });
  }
  // Keep only the last 6 turns. Slicing the tail also keeps user/assistant
  // pairing reasonable in practice (typical flow alternates), though we
  // don't force pairing — the LLM is robust to either.
  return turns.slice(-6);
}

export default function App() {
  // Phase 13: pathname-based public-share routing.
  // The `/shared/<token>` route renders a standalone read-only view
  // with NO auth + NO workspace chrome. We do this BEFORE any auth
  // hooks fire so the public view works for unauthenticated visitors.
  // Hooks order: keep this `useState` always called, even when the
  // value isn't used, so React's hook-order invariant holds.
  const [publicSharePath] = useState(() => {
    if (typeof window === "undefined") return null;
    const m = window.location.pathname.match(/^\/shared\/([^/?#]+)$/);
    return m ? m[1] : null;
  });
  if (publicSharePath) {
    return <PublicShareView token={publicSharePath} />;
  }

  // Form state
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("default");
  const [topK, setTopK] = useState(5);
  const [channel, setChannel] = useState("");
  const [user, setUser] = useState("");
  const [documentType, setDocumentType] = useState("");
  // Phase 9: source filter (Slack / Gmail). Default "all" preserves
  // the pre-Phase-9 behavior; sourcesToList() converts to the backend
  // API shape (null vs ["slack"] / ["gmail"] / ["slack","gmail"]).
  const [sources, setSources] = useState("all");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [dateQuery, setDateQuery] = useState("");

  // Chat history.
  const [entries, setEntries] = useState([]);
  const [submitting, setSubmitting] = useState(false);

  // Admin status snapshot (refreshed periodically and after each query).
  const [adminStatus, setAdminStatus] = useState(null);

  // Phase 13: workspace-level connector + sync snapshot for the
  // status bar at the top of the chat surface. We refresh after
  // each query so the "last synced X ago" hint stays roughly fresh.
  const [workspaceStatus, setWorkspaceStatus] = useState(null);

  // Phase 13: which saved-answer id is currently in the share modal,
  // if any. The modal owns its own loading/error state.
  const [sharingSavedId, setSharingSavedId] = useState(null);

  // Query history — the user's previous questions + filters, persisted
  // to localStorage. Initial value comes from disk via a lazy initializer
  // so we don't read storage on every re-render.
  const [history, setHistory] = useState(() => loadHistory());

  // Persist history whenever it changes. Cheap because we cap at 30.
  useEffect(() => {
    saveHistory(history);
  }, [history]);

  // Saved answers — full assistant answers the user has bookmarked.
  // Distinct from `history` (which only stores user-side params).
  //
  // Phase 2: backend is the source of truth. localStorage stays as a
  // fallback for offline / backend-down so the panel still works.
  const [savedAnswers, setSavedAnswers] = useState(() => loadSavedAnswers());
  // When non-null, the saved-view overlay renders this item full-screen.
  const [viewingSaved, setViewingSaved] = useState(null);

  // Active sidebar panel: 'history' | 'saved' | 'slack' | 'gmail' | 'analytics' | null
  const [activePanel, setActivePanel] = useState(null);
  // Control console collapsed state — persisted.
  const [consoleCollapsed, setConsoleCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem("secondBrain.consoleCollapsed") || "false"); }
    catch { return false; }
  });
  // Status pill dropdown open
  const [statusDropdownOpen, setStatusDropdownOpen] = useState(false);

  useEffect(() => {
    // Always mirror to localStorage so the next session has something
    // to render before the backend round-trip completes.
    saveSavedAnswers(savedAnswers);
  }, [savedAnswers]);

  // Fetch saved answers from the backend on mount and whenever the
  // chat is cleared (cheap signal that the user just landed). Silent
  // on failure — the localStorage fallback is already in state.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await listSavedAnswers();
        if (cancelled || !Array.isArray(rows)) return;
        // Backend rows have a different shape (filters is jsonb, debug
        // is jsonb, timestamps are ISO strings). Reconcile to the
        // existing local shape so the panel renderer doesn't need
        // changes. created_at -> timestamp(ms).
        const reshaped = rows.map((r) => ({
          id:        r.id,
          timestamp: r.created_at ? Date.parse(r.created_at) : Date.now(),
          question:  r.question || "",
          answer:    r.answer   || "",
          sources:   Array.isArray(r.sources) ? r.sources : [],
          mode:      r.mode || "default",
          filters:   r.filters || {},
          debug:     r.debug || null,
        }));
        setSavedAnswers(reshaped);
      } catch {
        // Backend unreachable / 401 / etc. — keep the local fallback.
      }
    })();
    return () => { cancelled = true; };
    // We deliberately do NOT depend on savedAnswers — that would loop.
  }, []);

  // Theme — "light" or "dark". On first load we read the saved
  // preference if any; otherwise we honor prefers-color-scheme.
  const [theme, setTheme] = useState(() => loadTheme());
  useEffect(() => {
    // The data-theme attribute lives on <html> (documentElement) so the
    // override cascades to <body> and everything below it. CSS variables
    // do the rest.
    document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);

  function toggleTheme() {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }

  // Latest in-flight stream's AbortController so the user can cancel.
  const activeStreamRef = useRef(null);

  // Phase 2: backend chat session id for the current conversation. We
  // lazily create a session on the FIRST submit and reuse the same id
  // for subsequent turns until the user clears the chat. Holds null
  // while no session has been created yet (or after a clear). On
  // backend failures we leave it null and silently skip the mirror —
  // the localStorage-backed `history` panel keeps working either way.
  const currentSessionIdRef = useRef(null);

  /**
   * Make sure we have a chat session id for the current conversation.
   * Creates one on the backend on first call, then reuses it for the
   * rest of the conversation. Returns null on backend failure — the
   * caller treats that as "skip the mirror".
   */
  async function ensureChatSession(firstQuestion) {
    if (currentSessionIdRef.current) return currentSessionIdRef.current;
    // Title = the first question, trimmed to fit. The backend caps it
    // again at 200 chars defensively.
    const titleSource = (firstQuestion || "").trim();
    const title = titleSource ? titleSource.slice(0, 200) : "New chat";
    try {
      const row = await createChatSession(title);
      if (row && row.id) {
        currentSessionIdRef.current = row.id;
        return row.id;
      }
    } catch {
      // Fall through — caller skips the mirror.
    }
    return null;
  }

  /**
   * Mirror a chat turn into chat_messages. Best-effort: failures are
   * swallowed so they don't disturb the streaming UX. `sessionId` is
   * passed in (rather than read from the ref) so we can capture it
   * once and chain user+assistant writes through the same value even
   * if the user clears the chat mid-flight.
   */
  function mirrorChatMessage(sessionId, role, content, sources) {
    if (!sessionId) return;
    const trimmed = (content || "").trim();
    if (!trimmed) return;
    createChatMessage(sessionId, {
      role,
      content: trimmed,
      sources: Array.isArray(sources) ? sources : undefined,
    }).catch(() => { /* ignore */ });
  }

  // Auto-scroll to bottom on any timeline change.
  const bottomRef = useRef(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

  // Refresh admin status on mount, every 30s, and after each completed
  // submission (so a freshly ingested doc bumps the counters live).
  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      const data = await getAdminStatus();
      if (!cancelled) setAdminStatus(data);
    }
    refresh();
    const id = setInterval(refresh, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Phase 13: workspace-level status. Refreshes on mount, after
  // queries, and on the same 30s cadence as adminStatus.
  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const data = await getWorkspaceStatus();
        if (!cancelled && data) setWorkspaceStatus(data);
      } catch {
        // Silent: a 401/network blip just keeps the bar hidden until
        // the next refresh succeeds.
      }
    }
    refresh();
    const id = setInterval(refresh, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  function patchAssistantEntry(id, patch) {
    setEntries((current) =>
      current.map((e) => (e.id === id ? { ...e, ...patch } : e))
    );
  }

  function appendAssistantToken(id, text) {
    setEntries((current) =>
      current.map((e) =>
        e.id === id ? { ...e, answer: e.answer + text } : e
      )
    );
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || submitting) return;

    // Defensive: if any leftover controller is somehow still around,
    // abort it before starting a new request so its callbacks can't
    // fire against the new bubble.
    if (activeStreamRef.current) {
      activeStreamRef.current.abort();
      activeStreamRef.current = null;
    }

    // Build conversation_history from completed entries in the timeline.
    // Last 6 messages = up to 3 (user, assistant) pairs. We:
    //   - skip the streaming/loading bubbles (not yet a real turn)
    //   - skip error bubbles and aborted bubbles with no answer
    //   - send turns in chronological order (oldest first)
    // The backend caps at 6 server-side too, so this is defense in depth.
    const conversationHistory = buildHistoryFromEntries(entries);

    const params = {
      question: trimmed,
      topK,
      mode,
      channel,
      user,
      documentType,
      sources,
      startDate,
      endDate,
      dateQuery,
      conversationHistory,
    };

    const userEntry = makeUserEntry(trimmed, params);
    const assistantEntry = makePendingAssistantEntry();

    setEntries((current) => [...current, userEntry, assistantEntry]);
    setQuestion("");
    setSubmitting(true);

    // Record this query in the persistent history panel. We do NOT store
    // the conversation_history field — that's session-only by design (and
    // would balloon the localStorage payload). The remaining fields are
    // enough to fully reconstruct the request when the user clicks a row.
    setHistory((prev) => pushHistoryItem(prev, {
      question: trimmed,
      topK, mode, channel, user, documentType, sources,
      startDate, endDate, dateQuery,
    }));

    // Phase 2: mirror this turn into chat_sessions / chat_messages.
    // Best-effort — failures don't block the actual query. We capture
    // the session id locally so the assistant callback can chain into
    // the SAME session even if the user clears the chat mid-stream.
    const sessionPromise = ensureChatSession(trimmed);
    sessionPromise.then((sid) => {
      mirrorChatMessage(sid, "user", trimmed, null);
    });

    const controller = new AbortController();
    activeStreamRef.current = controller;

    try {
      await streamQuery(
        params,
        {
          onToken: (text) => appendAssistantToken(assistantEntry.id, text),
          onDone: ({ answer, sources, debug }) => {
            // The done callback fires once per stream when the server
            // sends `event: done`. If the user aborted before this, the
            // browser short-circuits and we never get here — that's fine.
            patchAssistantEntry(assistantEntry.id, {
              streaming: false,
              answer: answer || "",
              sources: Array.isArray(sources) ? sources : [],
              debug: debug || null,
            });
            // Mirror the assistant turn. We re-await sessionPromise so
            // if the session create raced the stream, we still chain
            // into the right session.
            sessionPromise.then((sid) => {
              mirrorChatMessage(sid, "assistant", answer || "", sources);
            });
          },
          onError: (err) => {
            const message =
              err instanceof ApiError ? err.message : "Unexpected error.";
            const status = err instanceof ApiError ? err.status : 0;
            patchAssistantEntry(assistantEntry.id, {
              streaming: false,
              error: { message, status },
            });
          },
          // The crucial bit: when the user clicks Stop, the api layer
          // fires this callback instead of treating the abort as an
          // error. We:
          //   - clear the streaming flag (hides spinner + cursor)
          //   - set `aborted: true` so the bubble renders a small
          //     "Stopped by user." note
          //   - keep whatever `answer` text already streamed in
          //   - keep `sources` empty since we never received `done`
          onAbort: () => {
            patchAssistantEntry(assistantEntry.id, {
              streaming: false,
              aborted: true,
            });
          },
        },
        controller.signal
      );
    } finally {
      // Always clear these — no matter how the request ended (success,
      // error, abort, or even an unexpected throw from streamQuery
      // itself). This is what guarantees the UI returns to idle and the
      // next question works normally.
      //
      // We only clear the ref if it still points at OUR controller. If
      // a newer submit has already replaced it (extremely unlikely
      // because `submitting` blocks re-entry, but cheap insurance) we
      // leave that newer one alone.
      if (activeStreamRef.current === controller) {
        activeStreamRef.current = null;
      }
      setSubmitting(false);

      // Refresh admin status so any docs ingested while we were waiting
      // (or as a result of activity unrelated to this query) show up
      // immediately in the admin card.
      getAdminStatus().then((data) => {
        if (data) setAdminStatus(data);
      });
      // Phase 13: refresh workspace status too so the status bar
      // shows fresh "last synced X ago" right after a query that
      // followed a sync.
      getWorkspaceStatus()
        .then((data) => { if (data) setWorkspaceStatus(data); })
        .catch(() => { /* silent: status bar degrades to hidden */ });
    }
  }

  function handleStop() {
    const controller = activeStreamRef.current;
    if (!controller) return;
    // Calling abort() makes the fetch reject inside streamQuery, which
    // triggers our onAbort callback. We don't patch UI state here — let
    // onAbort do it so the source of truth is one place.
    controller.abort();
  }

  function handleClearChat() {
    // If a request is in flight, cancel it first so its callbacks don't
    // try to patch entries that no longer exist.
    if (activeStreamRef.current) {
      activeStreamRef.current.abort();
      activeStreamRef.current = null;
    }
    setEntries([]);
    setSubmitting(false);
    // Clearing the chat starts a fresh conversation server-side too.
    currentSessionIdRef.current = null;
  }

  // -------- Query history handlers --------
  // All three are no-ops while a stream is in flight, since changing the
  // composer state mid-stream would be confusing (and submitting again
  // is blocked by the `submitting` guard in handleSubmit anyway).
  function loadHistoryItemIntoComposer(item) {
    setQuestion(item.question || "");
    setMode(item.mode || "default");
    setTopK(item.topK ?? 5);
    setChannel(item.channel || "");
    setUser(item.user || "");
    setDocumentType(item.documentType || "");
    setSources(item.sources || "all");
    setStartDate(item.startDate || "");
    setEndDate(item.endDate || "");
    setDateQuery(item.dateQuery || "");
  }

  function handleLoadHistory(item) {
    if (submitting) return;
    loadHistoryItemIntoComposer(item);
    setActivePanel(null);
  }

  function handleRunAgain(item) {
    if (submitting) return;
    loadHistoryItemIntoComposer(item);
    setActivePanel(null);
    // Submit on the next tick so the state updates above have flushed
    // — handleSubmit reads `question` / filters from state.
    setTimeout(() => {
      const form = document.querySelector(".control-console");
      if (form) form.requestSubmit();
    }, 0);
  }

  function handleDeleteHistoryItem(id) {
    setHistory((prev) => prev.filter((it) => it.id !== id));
  }

  function handleClearHistory() {
    setHistory([]);
  }

  // -------- Saved-answers handlers --------

  /**
   * Save the assistant bubble at `entryIndex` (which must be a completed
   * assistant entry) along with the user bubble that preceded it. We
   * mark the entry with the new saved item's id so the Save button can
   * toggle to "Saved" without re-querying storage.
   *
   * Phase 2: we ALSO POST to /api/saved-answers. Optimistic UI — the
   * item appears immediately with a temporary client-side id; if the
   * backend accepts it, we swap the temp id for the server-assigned
   * one (and update the assistant entry's savedId so the Save button
   * stays in sync). If the backend fails, the item stays in the panel
   * (graceful degradation) and the next page-load reconcile will
   * remove it when the server-side list returns without it.
   */
  function handleSaveAnswer(entryIndex) {
    const entry = entries[entryIndex];
    if (!entry || entry.role !== "assistant") return;
    if (entry.streaming || entry.error) return;
    if (!entry.answer || !entry.answer.trim()) return;

    // Find the user bubble that immediately preceded this assistant one
    // — that's where the question + filters live.
    const user = entries[entryIndex - 1];
    if (!user || user.role !== "user") return;

    const item = makeSavedItem({
      question: user.question,
      params:   user.params,
      answer:   entry.answer,
      sources:  entry.sources,
      debug:    entry.debug,
    });
    setSavedAnswers((prev) => pushSavedItem(prev, item));
    // Attach the id to the entry so the Save button switches to "Saved".
    setEntries((current) => current.map((e, i) =>
      i === entryIndex ? { ...e, savedId: item.id } : e
    ));

    // Mirror to backend (best-effort).
    createSavedAnswer({
      question: item.question,
      answer:   item.answer,
      sources:  item.sources,
      mode:     item.mode,
      filters:  item.filters,
      debug:    item.debug,
    }).then((row) => {
      if (!row || !row.id || row.id === item.id) return;
      // Swap the optimistic id for the real one returned by the server.
      setSavedAnswers((prev) =>
        prev.map((it) => (it.id === item.id ? { ...it, id: row.id } : it))
      );
      setEntries((current) => current.map((e, i) =>
        i === entryIndex ? { ...e, savedId: row.id } : e
      ));
    }).catch(() => {
      // Stay quiet — the item is already in the panel and will reconcile
      // on the next backend fetch.
    });
  }

  function handleUnsaveAnswer(entryIndex) {
    const entry = entries[entryIndex];
    if (!entry || !entry.savedId) return;
    const sid = entry.savedId;
    setSavedAnswers((prev) => prev.filter((it) => it.id !== sid));
    setEntries((current) => current.map((e, i) =>
      i === entryIndex ? { ...e, savedId: null } : e
    ));
    // Best-effort backend delete. 404 just means it was never persisted
    // (e.g. saved while offline) — we don't surface it.
    deleteSavedAnswer(sid).catch(() => { /* ignore */ });
  }

  function handleDeleteSavedItem(id) {
    setSavedAnswers((prev) => prev.filter((it) => it.id !== id));
    // If the user deleted the item that's currently open in the overlay,
    // close the overlay so we don't render a stale snapshot.
    if (viewingSaved && viewingSaved.id === id) {
      setViewingSaved(null);
    }
    // Also clear `savedId` on any entry that referenced this saved item
    // so the inline Save button returns to "Save".
    setEntries((current) => current.map((e) =>
      e.savedId === id ? { ...e, savedId: null } : e
    ));
    deleteSavedAnswer(id).catch(() => { /* ignore */ });
  }

  function handleClearSavedAnswers() {
    // Capture the ids BEFORE we wipe state so we know what to delete
    // on the backend. We deliberately fire and forget — even if some
    // deletes fail the panel is empty and the next backend reconcile
    // will surface anything that's left.
    const idsToDelete = savedAnswers.map((it) => it.id);
    setSavedAnswers([]);
    setViewingSaved(null);
    setEntries((current) => current.map((e) =>
      e.savedId ? { ...e, savedId: null } : e
    ));
    idsToDelete.forEach((id) => {
      deleteSavedAnswer(id).catch(() => { /* ignore */ });
    });
  }

  function handleOpenSavedItem(item) {
    setViewingSaved(item);
    setActivePanel(null);
  }

  function handlePanelToggle(panel) {
    setActivePanel((current) => (current === panel ? null : panel));
  }

  return (
    <div className="app">
      <Sidebar
        theme={theme}
        toggleTheme={toggleTheme}
        activePanel={activePanel}
        onPanelToggle={handlePanelToggle}
        onClearChat={handleClearChat}
        hasEntries={entries.length > 0 || submitting}
        historyCount={history.length}
        savedCount={savedAnswers.length}
        submitting={submitting}
      />

      {/* Slide-in drawer for all panels */}
      {activePanel && (
        <>
          <div className="panel-backdrop" onClick={() => setActivePanel(null)} />
          <div className="panel-drawer panel-drawer--open">
            <div className="panel-drawer__header">
              <span className="panel-drawer__title">
                {{ history: "Query History", saved: "Saved Answers", slack: "Slack", gmail: "Gmail", analytics: "Analytics" }[activePanel]}
              </span>
              <button
                type="button"
                className="panel-drawer__close"
                onClick={() => setActivePanel(null)}
                aria-label="Close panel"
              >
                ✕
              </button>
            </div>
            <div className="panel-drawer__body">
              {activePanel === "history" && (
                <QueryHistoryPanel
                  history={history}
                  onLoad={handleLoadHistory}
                  onRunAgain={handleRunAgain}
                  onDelete={handleDeleteHistoryItem}
                  onClearAll={handleClearHistory}
                  submitting={submitting}
                />
              )}
              {activePanel === "saved" && (
                <SavedAnswersPanel
                  items={savedAnswers}
                  onOpen={handleOpenSavedItem}
                  onDelete={handleDeleteSavedItem}
                  onClearAll={handleClearSavedAnswers}
                />
              )}
              {activePanel === "slack" && <SlackSettings />}
              {activePanel === "gmail" && <GmailSettings />}
              {activePanel === "analytics" && (
                <AnalyticsPanel onClose={() => setActivePanel(null)} />
              )}
            </div>
          </div>
        </>
      )}

      {viewingSaved && (
        <SavedAnswerOverlay
          item={viewingSaved}
          onClose={() => setViewingSaved(null)}
          onDelete={() => handleDeleteSavedItem(viewingSaved.id)}
          onShare={(id) => setSharingSavedId(id)}
        />
      )}

      {sharingSavedId && (
        <ShareModal
          savedId={sharingSavedId}
          onClose={() => setSharingSavedId(null)}
        />
      )}

      <div className="main-area">
        <header className="main-header">
          <div className="main-header__brand">
            <Logo compact />
            <span className="main-header__wordmark">HYDRA<strong>DB</strong></span>
          </div>
          <div className="main-header__right">
            <AdminStatus status={adminStatus} />
            <StatusPill
              status={workspaceStatus}
              open={statusDropdownOpen}
              onToggle={() => setStatusDropdownOpen((o) => !o)}
            />
          </div>
        </header>

        <main className="chat">
          {entries.length === 0 && <EmptyState workspaceStatus={workspaceStatus} />}
          {entries.map((entry, idx) => {
            if (entry.role === "user") {
              return <UserBubble key={entry.id} entry={entry} />;
            }
            const prior = entries[idx - 1];
            const questionText = (prior && prior.role === "user")
              ? (prior.question || "")
              : "";
            return (
              <AssistantBubble
                key={entry.id}
                entry={entry}
                question={questionText}
                onSave={() => handleSaveAnswer(idx)}
                onUnsave={() => handleUnsaveAnswer(idx)}
              />
            );
          })}
          <div ref={bottomRef} />
        </main>

        <form className="control-console" onSubmit={handleSubmit}>
          <div className={`console-clusters${consoleCollapsed ? " console-clusters--hidden" : ""}`}>
            <div className="instrument-group">
              <span className="instrument-group__label">Mode</span>
              {MODES.map((m) => (
                <button
                  key={m.value}
                  type="button"
                  className={`console-pill${mode === m.value ? " console-pill--active" : ""}`}
                  onClick={() => setMode(m.value)}
                  disabled={submitting}
                  title={m.label}
                >
                  {m.label.split(" — ")[0].split(" ")[0]}
                </button>
              ))}
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">Source</span>
              {SOURCE_OPTIONS.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  className={`console-pill${sources === s.value ? " console-pill--active" : ""}`}
                  onClick={() => setSources(s.value)}
                  disabled={submitting}
                  title={s.label}
                >
                  {s.label.replace(" sources", "").replace(" only", "")}
                </button>
              ))}
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">Type</span>
              {DOC_TYPES.map((d) => (
                <button
                  key={d.value}
                  type="button"
                  className={`console-pill${documentType === d.value ? " console-pill--active" : ""}`}
                  onClick={() => setDocumentType(d.value)}
                  disabled={submitting}
                >
                  {d.label || "Any"}
                </button>
              ))}
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">Top-K</span>
              <div className="console-stepper">
                <button
                  type="button"
                  className="console-stepper__btn"
                  onClick={() => setTopK((k) => Math.max(1, k - 1))}
                  disabled={submitting || topK <= 1}
                  aria-label="Decrease top-k"
                >−</button>
                <span className="console-value">{topK}</span>
                <button
                  type="button"
                  className="console-stepper__btn"
                  onClick={() => setTopK((k) => Math.min(10, k + 1))}
                  disabled={submitting || topK >= 10}
                  aria-label="Increase top-k"
                >+</button>
              </div>
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">Channel</span>
              <input
                type="text"
                className="console-input"
                value={channel}
                onChange={(e) => setChannel(e.target.value)}
                placeholder="e.g. product"
                disabled={submitting}
              />
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">User</span>
              <input
                type="text"
                className="console-input"
                value={user}
                onChange={(e) => setUser(e.target.value)}
                placeholder="e.g. Rahul"
                disabled={submitting}
              />
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">Date</span>
              <input
                type="text"
                className="console-input"
                style={{ width: "148px" }}
                value={dateQuery}
                onChange={(e) => setDateQuery(e.target.value)}
                placeholder="last week, yesterday…"
                disabled={submitting}
              />
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">From</span>
              <input
                type="date"
                className="console-input"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="instrument-group">
              <span className="instrument-group__label">To</span>
              <input
                type="date"
                className="console-input"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                disabled={submitting}
              />
            </div>
          </div>

          <div className="console-row">
            <button
              type="button"
              className="console-toggle"
              onClick={() => {
                const next = !consoleCollapsed;
                setConsoleCollapsed(next);
                try { localStorage.setItem("secondBrain.consoleCollapsed", JSON.stringify(next)); } catch {}
              }}
              aria-label={consoleCollapsed ? "Expand filters" : "Collapse filters"}
              title={consoleCollapsed ? "Expand filters" : "Collapse filters"}
            >
              {consoleCollapsed ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
            <textarea
              className="console-textarea"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask your second brain…"
              rows={1}
              disabled={submitting}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSubmit(e);
                }
              }}
            />
            {submitting ? (
              <button
                type="button"
                className="console-stop"
                onClick={handleStop}
                aria-label="Stop"
                title="Stop streaming"
              >
                <Square size={13} />
              </button>
            ) : (
              <button
                type="submit"
                className="console-send"
                disabled={!question.trim()}
                aria-label="Send"
                title="Send (Enter)"
              >
                <Send size={13} />
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Sub-components
// ----------------------------------------------------------------------

function QueryHistoryPanel({
  history,
  onLoad,
  onRunAgain,
  onDelete,
  onClearAll,
  submitting,
}) {
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const filtered = needle
    ? history.filter((item) => itemMatchesFilter(item, needle))
    : history;

  return (
    <section
      id="query-history-panel"
      className="history"
      aria-label="Recent queries"
    >
      <div className="history__header">
        <div>
          <strong className="history__title">Query History</strong>
          <span className="history__hint">
            {history.length === 0
              ? "Past questions you ask will appear here."
              : "Click a row to load it into the composer."}
          </span>
        </div>
        {history.length > 0 && (
          <ArmedButton
            onConfirm={onClearAll}
            label="Clear history"
            confirmLabel="Confirm clear?"
            title="Remove all entries from query history"
            disabled={submitting}
            size="small"
          />
        )}
      </div>

      <FilterBox
        value={filter}
        onChange={setFilter}
        placeholder="Filter history…"
        matchCount={filtered.length}
        totalCount={history.length}
        ariaLabel="Filter query history"
      />

      {history.length === 0 ? (
        <div className="panel-empty">
          No history yet. Ask a question and it'll show up here.
        </div>
      ) : filtered.length === 0 ? (
        <div className="panel-empty">
          No matches for “{filter.trim()}”.
        </div>
      ) : (
        <ul className="history__list">
          {filtered.map((item) => (
            <li key={item.id} className="history-item">
              <button
                type="button"
                className="history-item__main"
                onClick={() => onLoad(item)}
                disabled={submitting}
                title="Load this query into the composer"
              >
                <div className="history-item__question">
                  {item.question}
                </div>
                <div className="history-item__meta">
                  <span className="history-item__time">
                    {formatIsoRelative(new Date(item.timestamp).toISOString())}
                  </span>
                  {summarizeFilters(item).map((chip) => (
                    <span key={chip} className="history-item__chip">{chip}</span>
                  ))}
                </div>
              </button>
              <div className="history-item__actions">
                <button
                  type="button"
                  className="btn btn--primary btn--small"
                  onClick={() => onRunAgain(item)}
                  disabled={submitting}
                  title="Load this query and run it immediately"
                >
                  Run again
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--small"
                  onClick={() => onDelete(item.id)}
                  disabled={submitting}
                  aria-label="Remove this entry from history"
                  title="Remove from history"
                >
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Lowercase substring match across all visible fields of a history
 * item. Used by the panel filter input. Case-insensitive; needle is
 * already trimmed/lowercased by the caller.
 */
function itemMatchesFilter(item, needle) {
  const hay = [
    item.question, item.mode, item.channel, item.user,
    item.documentType, item.dateQuery,
  ].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(needle);
}

/**
 * Build the small chips shown next to a history row — only the non-default
 * filters so a plain question with default settings looks tidy.
 */
function summarizeFilters(item) {
  const chips = [];
  if (item.mode && item.mode !== "default") chips.push(`mode: ${item.mode}`);
  if (item.topK && item.topK !== 5) chips.push(`top_k: ${item.topK}`);
  if (item.channel) chips.push(`#${item.channel}`);
  if (item.user) chips.push(`@${item.user}`);
  if (item.documentType) chips.push(item.documentType);
  if (item.dateQuery) chips.push(`date: ${item.dateQuery}`);
  if (item.startDate) chips.push(`from ${item.startDate}`);
  if (item.endDate) chips.push(`to ${item.endDate}`);
  return chips;
}

/**
 * Same chip-summary helper for saved items. Saved items keep filters
 * under `item.filters` + `item.mode` (instead of flat top-level fields
 * the way history items do), so we adapt to that shape here rather than
 * forcing one schema on both.
 */
function summarizeSavedFilters(item) {
  const f = item.filters || {};
  const flattened = {
    mode:         item.mode,
    topK:         f.topK,
    channel:      f.channel,
    user:         f.user,
    documentType: f.documentType,
    dateQuery:    f.dateQuery,
    startDate:    f.startDate,
    endDate:      f.endDate,
  };
  return summarizeFilters(flattened);
}

function SavedAnswersPanel({ items, onOpen, onDelete, onClearAll }) {
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const filtered = needle
    ? items.filter((it) => savedItemMatchesFilter(it, needle))
    : items;

  return (
    <section
      id="saved-answers-panel"
      className="saved"
      aria-label="Saved answers"
    >
      <div className="saved__header">
        <div>
          <strong className="saved__title">Saved Answers</strong>
          <span className="saved__hint">
            {items.length === 0
              ? "Answers you bookmark will appear here."
              : "Click a row to open the full answer."}
          </span>
        </div>
        {items.length > 0 && (
          <ArmedButton
            onConfirm={onClearAll}
            label="Clear all"
            confirmLabel="Confirm clear?"
            title="Remove all saved answers"
            size="small"
          />
        )}
      </div>

      <FilterBox
        value={filter}
        onChange={setFilter}
        placeholder="Filter saved answers…"
        matchCount={filtered.length}
        totalCount={items.length}
        ariaLabel="Filter saved answers"
      />

      {items.length === 0 ? (
        <div className="panel-empty">
          No saved answers yet. Use the ★ Save button on an answer to bookmark it.
        </div>
      ) : filtered.length === 0 ? (
        <div className="panel-empty">
          No matches for “{filter.trim()}”.
        </div>
      ) : (
        <ul className="saved__list">
          {filtered.map((item) => (
            <li key={item.id} className="saved-item">
              <button
                type="button"
                className="saved-item__main"
                onClick={() => onOpen(item)}
                title="Open the full saved answer"
              >
                <div className="saved-item__question">{item.question}</div>
                <div className="saved-item__preview">
                  {buildAnswerPreview(item.answer)}
                </div>
                <div className="saved-item__meta">
                  <span className="saved-item__time">
                    {formatIsoRelative(new Date(item.timestamp).toISOString())}
                  </span>
                  <span className="saved-item__sources">
                    {item.sources?.length || 0} source{item.sources?.length === 1 ? "" : "s"}
                  </span>
                  {summarizeSavedFilters(item).slice(0, 3).map((chip) => (
                    <span key={chip} className="saved-item__chip">{chip}</span>
                  ))}
                </div>
              </button>
              <button
                type="button"
                className="btn btn--ghost btn--small"
                onClick={() => onDelete(item.id)}
                aria-label="Remove this saved answer"
                title="Remove this saved answer"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Filter predicate for saved items. We match against the question, the
 * answer body, and every visible filter chip — so typing "engineering"
 * or "exact" narrows the list the way the user would expect.
 */
function savedItemMatchesFilter(item, needle) {
  const filters = item.filters || {};
  const hay = [
    item.question, item.answer, item.mode,
    filters.channel, filters.user, filters.documentType, filters.dateQuery,
  ].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(needle);
}

/**
 * Full-screen-ish overlay that shows the full saved answer + sources,
 * rendered with the same markdown component the live chat uses.
 *
 * We render this as a sibling of the main chat (not a portal) because
 * the existing layout is already a vertical column with a backdrop
 * behavior achievable via fixed positioning + z-index. Keyboard:
 * Escape closes the overlay.
 */
function SavedAnswerOverlay({ item, onClose, onDelete, onShare }) {
  useEffect(() => {
    function onKeydown(e) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeydown);
    return () => window.removeEventListener("keydown", onKeydown);
  }, [onClose]);

  const chips = summarizeSavedFilters(item);

  return (
    <div
      className="saved-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Saved answer"
      onClick={onClose}
    >
      {/* Stop clicks inside the card from bubbling up to the backdrop
          (which would close the overlay). */}
      <div className="saved-overlay__card" onClick={(e) => e.stopPropagation()}>
        <header className="saved-overlay__header">
          <div className="saved-overlay__meta">
            <span className="saved-overlay__time">
              Saved {formatIsoRelative(new Date(item.timestamp).toISOString())}
            </span>
            {chips.map((chip) => (
              <span key={chip} className="saved-overlay__chip">{chip}</span>
            ))}
          </div>
          <div className="saved-overlay__actions">
            <button
              type="button"
              className="btn btn--ghost btn--small"
              onClick={() => exportAnswer({
                question: item.question,
                answer:   item.answer,
                sources:  item.sources,
                format:   "md",
                // Use the bookmark's saved-at time so the exported
                // filename matches when this answer was captured, not
                // when the user clicked Export.
                epochMs:  item.timestamp,
              })}
              title="Download this saved answer as a Markdown file"
            >
              ⬇ MD
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--small"
              onClick={() => exportAnswer({
                question: item.question,
                answer:   item.answer,
                sources:  item.sources,
                format:   "txt",
                epochMs:  item.timestamp,
              })}
              title="Download this saved answer as a plain text file"
            >
              ⬇ TXT
            </button>
            <CopyButton
              text={item.answer}
              label="⧉ Copy"
              copiedLabel="✓ Copied!"
              title="Copy the answer text to clipboard"
              className="btn btn--ghost btn--small"
              copiedClassName="bubble__copy--copied"
            />
            {onShare && item.id && (
              <button
                type="button"
                className="btn btn--ghost btn--small"
                onClick={() => onShare(item.id)}
                title="Create a public share link"
              >
                Share
              </button>
            )}
            <ArmedButton
              onConfirm={onDelete}
              label="Delete"
              confirmLabel="Confirm delete?"
              title="Delete this saved answer"
              size="small"
            />
            <button
              type="button"
              className="btn btn--ghost btn--small"
              onClick={onClose}
              aria-label="Close"
            >
              Close ✕
            </button>
          </div>
        </header>

        <h2 className="saved-overlay__question">{item.question}</h2>

        <div className="saved-overlay__answer answer">
          <AnswerMarkdown text={item.answer} />
        </div>

        {item.sources && item.sources.length > 0 && (
          <div className="sources">
            <div className="sources__heading">
              Sources ({item.sources.length})
            </div>
            <div className="sources__list">
              {item.sources.map((s) => (
                <SourceCard key={`${item.id}-${s.index}`} source={s} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function AdminStatus({ status }) {
  if (!status) {
    // Pre-load placeholder. Doesn't take up much room; avoids layout shift
    // when the first fetch completes.
    return (
      <section className="admin admin--loading" aria-live="polite">
        <span className="admin__label">Status</span>
        <span className="admin__placeholder">loading…</span>
      </section>
    );
  }

  const lastIngested = status.last_ingested_at
    ? formatIsoRelative(status.last_ingested_at)
    : "never";

  return (
    <section className="admin" aria-live="polite">
      <span className="admin__label">Status</span>

      <span
        className={`admin__pill admin__pill--${
          status.realtime_ingest_enabled ? "on" : "off"
        }`}
        title="Slack Events webhook ingestion"
      >
        realtime {status.realtime_ingest_enabled ? "on" : "off"}
      </span>

      <span
        className={`admin__pill admin__pill--${
          status.scheduler_enabled ? "on" : "off"
        }`}
        title="Polling scheduler fallback"
      >
        scheduler {status.scheduler_enabled ? "on" : "off"}
      </span>

      <span className="admin__metric" title={status.last_ingested_at || "no ingestion yet"}>
        last ingest: <strong>{lastIngested}</strong>
      </span>

      <span className="admin__metric">
        docs: <strong>{status.total_docs ?? 0}</strong>
      </span>

      {typeof status.channels_tracked === "number" && (
        <span className="admin__metric">
          channels: <strong>{status.channels_tracked}</strong>
        </span>
      )}
    </section>
  );
}

function EmptyState({ workspaceStatus }) {
  // Phase 13: smarter empty state. If no connectors are linked yet,
  // surface "connect Slack / Gmail first" guidance instead of example
  // questions the user can't possibly run.
  const slackConnected = workspaceStatus?.slack?.connected;
  const gmailConnected = (workspaceStatus?.gmail?.connection_count || 0) > 0;
  const anyConnected = slackConnected || gmailConnected;

  if (workspaceStatus && !anyConnected) {
    return (
      <div className="empty">
        <p className="empty__lead">Welcome to Second Brain.</p>
        <p className="empty__sub">
          Connect a data source to get started. Your messages stay in
          your workspace — nothing is shared across workspaces.
        </p>
        <ul className="empty__examples">
          <li>
            <strong>Connect Slack</strong> — index public-channel
            history and ask "what did we decide?"
          </li>
          <li>
            <strong>Connect Gmail</strong> — surface action items and
            decisions across selected labels.
          </li>
        </ul>
      </div>
    );
  }

  // Sources connected — show example questions tuned to what they
  // actually have.
  const examples = [];
  if (slackConnected) {
    examples.push("\"What did we decide about the memory layer?\"");
    examples.push("\"Summary of yesterday's design discussion\"");
  }
  if (gmailConnected) {
    examples.push("\"Latest email from Rahul about deployment\"");
    examples.push("\"Pending action items from this week\"");
  }
  if (examples.length === 0) {
    examples.push("\"Who said they would document the API contract?\"");
  }
  return (
    <div className="empty">
      <p className="empty__lead">Ask anything from your workspace.</p>
      <ul className="empty__examples">
        {examples.map((ex) => <li key={ex}>{ex}</li>)}
      </ul>
    </div>
  );
}

function UserBubble({ entry }) {
  const { params } = entry;
  const usedFilters = [];
  if (params.mode && params.mode !== "default") usedFilters.push(`mode=${params.mode}`);
  if (params.channel) usedFilters.push(`channel=${params.channel}`);
  if (params.user) usedFilters.push(`user=${params.user}`);
  if (params.documentType) usedFilters.push(`type=${params.documentType}`);
  if (params.dateQuery) usedFilters.push(`date="${params.dateQuery}"`);
  if (params.startDate) usedFilters.push(`from=${params.startDate}`);
  if (params.endDate) usedFilters.push(`to=${params.endDate}`);

  return (
    <div className="bubble bubble--user">
      <div className="bubble__role">You</div>
      <div className="bubble__body">{entry.question}</div>
      {usedFilters.length > 0 && (
        <div className="bubble__meta">{usedFilters.join(" · ")}</div>
      )}
    </div>
  );
}

function AssistantBubble({ entry, question, onSave, onUnsave }) {
  if (entry.error) {
    return (
      <div className="bubble bubble--assistant bubble--error">
        <div className="bubble__role">Second Brain</div>
        <div className="bubble__body">
          <strong>
            {entry.error.status ? `Error ${entry.error.status}` : "Error"}
          </strong>
          <p>{entry.error.message}</p>
        </div>
      </div>
    );
  }

  const debug = entry.debug || {};
  const cacheHit = debug.cache_hit === true;
  const retrievalMode = debug.retrieval_mode;
  const exactMatches = debug.exact_matches_found;
  const dateQueryDebug = debug.date_query;
  const queryRewrite = debug.query_rewrite;
  const inferredPerson = queryRewrite ? queryRewrite.inferred_person : null;
  const inferredChannel = queryRewrite ? queryRewrite.inferred_channel : null;
  const personConfidence = queryRewrite ? queryRewrite.person_confidence : null;
  const channelConfidence = queryRewrite ? queryRewrite.channel_confidence : null;

  // The Save button is only meaningful for COMPLETED answers with real
  // content. We deliberately hide it during streaming and for empty
  // bubbles so users can't bookmark loading/error states.
  const canSave = !entry.streaming
    && !entry.error
    && typeof entry.answer === "string"
    && entry.answer.trim().length > 0;
  const isSaved = Boolean(entry.savedId);

  return (
    <div className="bubble bubble--assistant">
      <div className="bubble__role">
        Second Brain
        {entry.streaming && (
          <span className="bubble__status">
            <span className="spinner" aria-hidden="true" />
            <span>streaming…</span>
          </span>
        )}
        {!entry.streaming && cacheHit && (
          <span className="badge badge--cache" title="Returned from cache">
            cached
          </span>
        )}
        {!entry.streaming && retrievalMode && retrievalMode !== "default" && (
          <span className="badge badge--mode" title="Retrieval strategy">
            mode: {retrievalMode}
          </span>
        )}
        {!entry.streaming && typeof exactMatches === "number"
          && (retrievalMode === "exact" || retrievalMode === "hybrid") && (
          <span
            className={`badge badge--matches ${
              exactMatches === 0 ? "badge--matches-none" : ""
            }`}
            title="Chunks containing at least one query keyword"
          >
            exact matches: {exactMatches}
          </span>
        )}
        {!entry.streaming && dateQueryDebug && (
          <span
            className={`badge badge--date ${
              dateQueryDebug.matched ? "" : "badge--date-failed"
            }`}
            title={dateQueryDebug.note || ""}
          >
            date: {dateQueryDebug.matched ? "✓" : "?"}{" "}
            {dateQueryDebug.phrase}
          </span>
        )}
        {/* Subtle inference chips. Only render when the rewriter actually
            inferred something — never clutter the UI on plain queries. */}
        {!entry.streaming && inferredPerson && (
          <span
            className={`badge badge--person badge--inference-${personConfidence || "weak"}`}
            title={`Detected person filter (${personConfidence || "weak"})`}
          >
            Person: {inferredPerson}
            {personConfidence === "weak" && " (bias)"}
          </span>
        )}
        {!entry.streaming && inferredChannel && (
          <span
            className={`badge badge--channel badge--inference-${channelConfidence || "weak"}`}
            title={`Detected channel filter (${channelConfidence || "weak"})`}
          >
            Channel: {inferredChannel}
            {channelConfidence === "weak" && " (bias)"}
          </span>
        )}
        {canSave && (
          <button
            type="button"
            className={`bubble__save ${isSaved ? "bubble__save--saved" : ""}`}
            onClick={isSaved ? onUnsave : onSave}
            title={isSaved ? "Remove from saved" : "Save this answer"}
            aria-pressed={isSaved}
          >
            {isSaved ? "★ Saved" : "☆ Save"}
          </button>
        )}
        {canSave && (
          <button
            type="button"
            className="bubble__export"
            onClick={() => exportAnswer({
              question, answer: entry.answer, sources: entry.sources,
              format: "md",
            })}
            title="Download this answer as a Markdown file"
          >
            ⬇ MD
          </button>
        )}
        {canSave && (
          <button
            type="button"
            className="bubble__export"
            onClick={() => exportAnswer({
              question, answer: entry.answer, sources: entry.sources,
              format: "txt",
            })}
            title="Download this answer as a plain text file"
          >
            ⬇ TXT
          </button>
        )}
        {canSave && (
          <CopyButton
            text={entry.answer}
            label="⧉ Copy"
            copiedLabel="✓ Copied!"
            title="Copy the answer text to clipboard"
          />
        )}
      </div>
      <div className="bubble__body">
        {entry.answer ? (
          <div className="answer">
            <AnswerMarkdown text={entry.answer} />
            {/* Cursor lives outside the markdown tree so the parser
                never sees the '▍' character. It's a small inline-block
                sibling that visually appears right after the rendered
                output. */}
            {entry.streaming && <span className="cursor">▍</span>}
          </div>
        ) : entry.streaming ? (
          <p className="answer answer--placeholder">Searching memory...</p>
        ) : (
          <p className="answer answer--empty">(No answer returned.)</p>
        )}

        {/* Aborted-by-user note. Sits between the answer and the sources
            so the partial answer is the most visible thing in the bubble. */}
        {!entry.streaming && entry.aborted && (
          <p className="aborted-note">Stopped by user.</p>
        )}

        {!entry.streaming && entry.sources && entry.sources.length > 0 && (
          <div className="sources">
            <div className="sources__heading">
              Sources ({entry.sources.length})
            </div>
            <div className="sources__list">
              {entry.sources.map((s) => (
                <SourceCard key={`${entry.id}-${s.index}`} source={s} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Render an assistant answer as Markdown.
 *
 * Why a thin wrapper:
 *   - Centralizes the remark plugins + component overrides so the
 *     AssistantBubble stays readable.
 *   - Lets the bubble keep the blinking cursor as a sibling (NOT a
 *     child of the markdown tree), which means partial-token states
 *     like an unfinished code fence don't try to render the cursor
 *     character.
 *   - Disables raw HTML by default (react-markdown's default behavior)
 *     so an LLM that emits "<script>...</script>" or "<img onerror=...>"
 *     can't execute anything in the browser. We render markdown only.
 *
 * Citations like "[1]" survive untouched because react-markdown only
 * converts "[text](url)" or reference-style "[text][label]" patterns
 * into links — a bare "[1]" with no matching definition stays as-is.
 * The CJK citation form "【N†source: ...】" has no Markdown meaning so
 * it passes through verbatim.
 */
function AnswerMarkdown({ text }) {
  return (
    <div className="answer-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={MARKDOWN_COMPONENTS}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

// Component overrides applied to the rendered tree. Keeping them at
// module scope avoids re-creating object identities on every render.
const MARKDOWN_COMPONENTS = {
  // External links open in a new tab; internal links (rare in answers)
  // keep default behavior. We destructure `node` out of props so the
  // ast metadata doesn't leak into the DOM as an attribute.
  a({ node, children, href, ...props }) {
    const isExternal = typeof href === "string"
      && /^https?:\/\//i.test(href);
    return (
      <a
        href={href}
        target={isExternal ? "_blank" : undefined}
        rel={isExternal ? "noopener noreferrer" : undefined}
        {...props}
      >
        {children}
      </a>
    );
  },
  // We deliberately don't override `code`. react-markdown v9 dropped the
  // `inline` prop, and the cleanest cross-version way to style inline vs
  // fenced code is via CSS: inline `<code>` lives directly in flow text,
  // while fenced code blocks are always wrapped in `<pre><code>`. The
  // CSS rules `.answer-md code` and `.answer-md pre code` target each
  // case without needing a JS-side discriminator.

  // Tables get a wrapping div so wide tables scroll horizontally
  // instead of bursting the bubble width.
  table({ node, children, ...props }) {
    return (
      <div className="md-table-wrap">
        <table {...props}>{children}</table>
      </div>
    );
  },
};

function SourceCard({ source }) {
  const {
    index,
    channel,
    user,
    timestamp,
    snippet,
    document_type: docType,
    permalink,
  } = source;

  return (
    <article className="source">
      <header className="source__header">
        <span className="source__index">[{index}]</span>
        {channel && <span className="source__channel">#{channel}</span>}
        {docType && <span className="tag">{docType}</span>}
      </header>
      <div className="source__meta">
        {user && <span>{user}</span>}
        {timestamp && <span>· {formatTimestamp(timestamp)}</span>}
      </div>
      {snippet && <p className="source__snippet">{snippet}</p>}
      {permalink && (
        <div className="source__link-row">
          <a
            href={permalink}
            target="_blank"
            rel="noreferrer"
            className="source__link"
          >
            Open in Slack →
          </a>
          <CopyButton
            text={permalink}
            label="⧉ Copy link"
            copiedLabel="✓ Copied!"
            title="Copy this Slack permalink to clipboard"
            className="source-card__copy"
            copiedClassName="source-card__copy--copied"
          />
        </div>
      )}
    </article>
  );
}

function formatTimestamp(slackTs) {
  if (!slackTs) return "";
  const seconds = parseFloat(slackTs);
  if (Number.isNaN(seconds)) return slackTs;
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return slackTs;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Format an ISO timestamp as a short, friendly relative string.
 *  - "12s ago" / "5m ago" / "2h ago" up to a day
 *  - older than 1 day -> "Mar 12, 14:05" local
 */
function formatIsoRelative(iso) {
  if (!iso) return "never";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  const diffMs = Date.now() - then.getTime();
  if (diffMs < 0) return then.toLocaleString();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return then.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ----------------------------------------------------------------------
// Reusable polish components (copy buttons, armed destructive buttons,
// filter inputs). Kept at module scope so they don't recreate identities.
// ----------------------------------------------------------------------

/**
 * Copy `text` to the clipboard using the modern Async Clipboard API.
 *
 * navigator.clipboard requires a secure context (https or localhost).
 * On http://0.0.0.0 or older browsers it returns undefined / throws —
 * we fall back to a hidden textarea + document.execCommand("copy")
 * which is deprecated but still works as a last resort. If both fail
 * we return false and the caller can show an error state.
 */
async function copyToClipboard(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path.
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return Boolean(ok);
  } catch {
    return false;
  }
}

/**
 * Pill-style copy button. Shows "Copied!" for ~1.5s after a successful
 * copy. `className` lets the caller pick which CSS variant to use
 * (bubble__copy vs source-card__copy) so we don't proliferate
 * component variants for tiny visual differences.
 */
function CopyButton({
  text,
  label = "Copy",
  copiedLabel = "Copied!",
  errorLabel = "Copy failed",
  className = "bubble__copy",
  copiedClassName = "bubble__copy--copied",
  title,
}) {
  const [state, setState] = useState("idle"); // idle | copied | error
  const timerRef = useRef(null);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  async function onClick() {
    const ok = await copyToClipboard(text);
    setState(ok ? "copied" : "error");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setState("idle"), 1500);
  }

  const shownLabel =
    state === "copied" ? copiedLabel :
    state === "error" ? errorLabel : label;

  return (
    <button
      type="button"
      className={`${className} ${state === "copied" ? copiedClassName : ""}`}
      onClick={onClick}
      title={title || label}
      disabled={!text}
      aria-live="polite"
    >
      {shownLabel}
    </button>
  );
}

/**
 * Destructive-action button with a two-click "tap to confirm" pattern.
 * First click arms the button — label changes to confirmLabel and the
 * arm style applies. Second click within `armWindowMs` (default 3s)
 * fires `onConfirm`. After the window the button reverts to idle.
 *
 * This is intentionally cheaper than a modal: zero focus management,
 * zero portal, no keyboard traps. The visual switch is enough to make
 * an accidental click into a no-op.
 */
function ArmedButton({
  onConfirm,
  label,
  confirmLabel,
  title,
  className = "btn btn--ghost",
  armedClassName = "btn--armed",
  armWindowMs = 3000,
  disabled = false,
  size = "default", // "default" or "small"
}) {
  const [armed, setArmed] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  function disarm() {
    setArmed(false);
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  function handleClick() {
    if (disabled) return;
    if (!armed) {
      setArmed(true);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setArmed(false), armWindowMs);
      return;
    }
    disarm();
    onConfirm();
  }

  const sizeClass = size === "small" ? " btn--small" : "";

  return (
    <button
      type="button"
      className={`${className}${sizeClass} ${armed ? armedClassName : ""}`}
      onClick={handleClick}
      onBlur={disarm}
      title={title}
      disabled={disabled}
      aria-pressed={armed}
    >
      {armed ? confirmLabel : label}
    </button>
  );
}

/**
 * Compact filter input for the saved + history panels. Shows a
 * "matched / total" count to the right so the user always sees how
 * narrowed the list is.
 */
function FilterBox({
  value,
  onChange,
  placeholder,
  matchCount,
  totalCount,
  ariaLabel,
}) {
  if (totalCount === 0) return null;
  return (
    <div className="panel-filter">
      <input
        type="search"
        className="panel-filter__input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel || placeholder}
      />
      <span className="panel-filter__count">
        {value.trim()
          ? `${matchCount} / ${totalCount}`
          : `${totalCount}`}
      </span>
    </div>
  );
}
// ====================================================================== //
// Phase 13: workspace status bar + share modal + public share view
// ====================================================================== //

function StatusPill({ status, open, onToggle }) {
  const ref = useRef(null);
  const [reingestingSlack, setReingestingSlack] = useState(false);
  const [reingestingGmail, setReingestingGmail] = useState(false);

  useEffect(() => {
    if (!open) return;
    function onOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) onToggle();
    }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [open, onToggle]);

  if (!status) return null;
  const { slack, gmail } = status;
  const slackOk = slack?.connected;
  const gmailOk = (gmail?.connection_count || 0) > 0;

  async function handleSlackIngest() {
    setReingestingSlack(true);
    try { await runSlackIngest(); } catch {}
    setReingestingSlack(false);
  }

  async function handleGmailIngest() {
    setReingestingGmail(true);
    try {
      const data = await listGmailConnections();
      const connections = (data && data.connections) || [];
      await Promise.all(
        connections.map((c) => runGmailIngest(c.id).catch(() => {}))
      );
    } catch {}
    setReingestingGmail(false);
  }

  return (
    <div className="status-pill" ref={ref}>
      <button
        type="button"
        className="status-pill__trigger"
        onClick={onToggle}
        aria-label="Sync status"
      >
        <span
          className={`status-dot status-dot--${slackOk ? "green" : "muted"}`}
          title={slackOk ? "Slack connected" : "Slack disconnected"}
        />
        <span
          className={`status-dot status-dot--${gmailOk ? "cyan" : "muted"}`}
          title={gmailOk ? "Gmail connected" : "Gmail disconnected"}
        />
        <span className="status-pill__label">SYNC</span>
        {open ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
      </button>

      {open && (
        <div className="status-dropdown">
          {slack && (
            <div className="status-row">
              <div className="status-row__info">
                <span className={`status-dot status-dot--${slackOk ? "green" : "muted"}`} />
                <span className="status-row__source">Slack</span>
                <span className="status-row__detail">
                  {slackOk
                    ? `${slack.channels_selected} channel${slack.channels_selected === 1 ? "" : "s"}`
                    : "disconnected"}
                </span>
              </div>
              {slackOk && (
                <button
                  type="button"
                  className="status-row__reingest"
                  onClick={handleSlackIngest}
                  disabled={reingestingSlack}
                >
                  {reingestingSlack ? "…" : "↺ Ingest"}
                </button>
              )}
            </div>
          )}
          {gmail && gmailOk && (
            <div className="status-row">
              <div className="status-row__info">
                <span className="status-dot status-dot--cyan" />
                <span className="status-row__source">Gmail</span>
                <span className="status-row__detail">
                  {gmail.labels_selected} label{gmail.labels_selected === 1 ? "" : "s"}
                  {gmail.last_synced_at && ` · ${formatIsoRelative(gmail.last_synced_at)}`}
                </span>
              </div>
              <button
                type="button"
                className="status-row__reingest"
                onClick={handleGmailIngest}
                disabled={reingestingGmail}
              >
                {reingestingGmail ? "…" : "↺ Ingest"}
              </button>
            </div>
          )}
          {gmail && !gmailOk && (
            <div className="status-row">
              <div className="status-row__info">
                <span className="status-dot status-dot--muted" />
                <span className="status-row__source">Gmail</span>
                <span className="status-row__detail">disconnected</span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


/**
 * Modal that renders the share URL for one saved answer, with a
 * Copy button and a Revoke button. Re-fetches the active shares
 * on open so it always shows the live state.
 *
 * Props:
 *   savedId: the saved-answer id to share (required)
 *   onClose: () => void
 */
function ShareModal({ savedId, onClose }) {
  const [shares, setShares] = useState(null);   // null = loading
  const [error, setError]   = useState("");
  const [busy, setBusy]     = useState(false);

  useEffect(() => {
    let cancelled = false;
    function load() {
      setShares(null); setError("");
      listSharesForAnswer(savedId)
        .then((data) => {
          if (cancelled) return;
          setShares((data && data.shares) || []);
        })
        .catch((e) => {
          if (cancelled) return;
          setError(e?.message || "Could not load shares.");
          setShares([]);
        });
    }
    load();
    return () => { cancelled = true; };
  }, [savedId]);

  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function handleCreate() {
    setBusy(true); setError("");
    try {
      const out = await shareSavedAnswer(savedId);
      setShares((prev) => [
        {
          id:          out.id,
          share_token: out.share_token,
          url:         out.url,
          created_at:  out.created_at,
        },
        ...(prev || []),
      ]);
    } catch (e) {
      setError(e?.message || "Could not create share link.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(token) {
    setBusy(true); setError("");
    try {
      await revokeShareLink(token);
      setShares((prev) => (prev || []).filter((s) => s.share_token !== token));
    } catch (e) {
      setError(e?.message || "Could not revoke share link.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="saved-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Share saved answer"
      onClick={onClose}
    >
      <div
        className="saved-overlay__card share-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="saved-overlay__header">
          <div className="saved-overlay__meta">
            <strong>Share this saved answer</strong>
          </div>
          <div className="saved-overlay__actions">
            <button
              type="button"
              className="btn btn--ghost btn--small"
              onClick={onClose}
              aria-label="Close"
            >
              Close ✕
            </button>
          </div>
        </header>

        <p className="share-modal__hint">
          Anyone with the link can read this answer. No login required.
          Tokens stay valid until you revoke them.
        </p>

        {error && (
          <div className="share-modal__error" role="alert">{error}</div>
        )}

        {shares === null && (
          <div className="share-modal__loading">Loading…</div>
        )}

        {shares !== null && shares.length === 0 && (
          <button
            type="button"
            className="btn btn--primary"
            onClick={handleCreate}
            disabled={busy}
          >
            {busy ? "Creating…" : "Create share link"}
          </button>
        )}

        {shares !== null && shares.length > 0 && (
          <>
            <ul className="share-modal__list">
              {shares.map((s) => (
                <li key={s.share_token} className="share-modal__row">
                  <input
                    type="text"
                    readOnly
                    value={s.url}
                    className="share-modal__url"
                    onFocus={(e) => e.target.select()}
                    aria-label="Public share URL"
                  />
                  <CopyButton
                    text={s.url}
                    label="Copy"
                    copiedLabel="Copied!"
                    className="btn btn--ghost btn--small"
                  />
                  <ArmedButton
                    onConfirm={() => handleRevoke(s.share_token)}
                    label="Revoke"
                    confirmLabel="Confirm revoke?"
                    size="small"
                  />
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="btn btn--ghost btn--small share-modal__add"
              onClick={handleCreate}
              disabled={busy}
            >
              + Create another link
            </button>
          </>
        )}
      </div>
    </div>
  );
}


/**
 * Public read-only view rendered at /shared/{token}. Self-contained:
 * fetches the public payload (no auth, no workspace header), renders
 * the question + answer + sources without any of the app's controls.
 * If the token is bad / revoked / expired the backend 404s and we
 * surface a clean error instead of crashing.
 */
function PublicShareView({ token }) {
  const [data, setData]   = useState(null);    // null = loading
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchPublicShare(token)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => {
        if (cancelled) return;
        if (e?.status === 404) {
          setError("This shared link is no longer available.");
        } else {
          setError(e?.message || "Could not load shared answer.");
        }
      });
    return () => { cancelled = true; };
  }, [token]);

  return (
    <div className="public-share">
      <header className="public-share__header">
        <strong>Second Brain</strong>
        <span className="public-share__hint">Shared answer · read only</span>
      </header>

      {error && (
        <div className="public-share__error" role="alert">
          {error}
        </div>
      )}

      {!error && data === null && (
        <div className="public-share__loading">Loading…</div>
      )}

      {!error && data && (
        <article className="public-share__card">
          <h2 className="public-share__question">{data.question}</h2>
          {data.created_at && (
            <div className="public-share__meta">
              Shared from {formatIsoRelative(data.created_at)}
            </div>
          )}
          <div className="public-share__answer answer">
            <AnswerMarkdown text={data.answer || ""} />
          </div>
          {Array.isArray(data.sources) && data.sources.length > 0 && (
            <div className="sources">
              <div className="sources__heading">
                Sources ({data.sources.length})
              </div>
              <div className="sources__list">
                {data.sources.map((s, idx) => (
                  <SourceCard key={`share-${idx}`} source={s} />
                ))}
              </div>
            </div>
          )}
        </article>
      )}
    </div>
  );
}

// ======================================================================
// Logo — hex-node SVG wordmark
// ======================================================================
function Logo({ compact = false }) {
  const size = compact ? 24 : 34;
  return (
    <svg
      viewBox="0 0 40 40"
      width={size}
      height={size}
      fill="none"
      aria-label="HydraDB logo"
    >
      <polygon
        points="20,2 36,11 36,29 20,38 4,29 4,11"
        stroke="var(--primary)"
        strokeWidth="1.8"
        fill="none"
        strokeLinejoin="round"
      />
      <circle cx="20" cy="20" r="2.5" fill="var(--primary)" />
      <line x1="20" y1="17.5" x2="20" y2="2"    stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
      <line x1="22.2" y1="21.3" x2="36" y2="29" stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
      <line x1="17.8" y1="21.3" x2="4"  y2="29" stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
      <circle cx="20" cy="2"  r="1.8" fill="var(--primary)" opacity="0.72" />
      <circle cx="36" cy="29" r="1.8" fill="var(--primary)" opacity="0.72" />
      <circle cx="4"  cy="29" r="1.8" fill="var(--primary)" opacity="0.72" />
    </svg>
  );
}

// ======================================================================
// SidebarBtn — icon button with optional badge and active indicator
// ======================================================================
function SidebarBtn({ icon, label, active, onClick, disabled, badge, title }) {
  return (
    <button
      type="button"
      className={`sidebar-btn${active ? " sidebar-btn--active" : ""}`}
      onClick={onClick}
      disabled={disabled}
      title={title || label}
      aria-label={label}
      aria-pressed={active || undefined}
    >
      <span className="sidebar-btn__icon">{icon}</span>
      {badge > 0 && (
        <span className="sidebar-btn__badge">{badge > 99 ? "99+" : badge}</span>
      )}
    </button>
  );
}

// ======================================================================
// Sidebar — 64-px icon navigation column
// ======================================================================
function Sidebar({
  theme,
  toggleTheme,
  activePanel,
  onPanelToggle,
  onClearChat,
  hasEntries,
  historyCount,
  savedCount,
  submitting,
}) {
  const { signOut, user } = useAuth();

  return (
    <nav className="sidebar" aria-label="Main navigation">
      <div className="sidebar__logo">
        <Logo />
      </div>

      <div className="sidebar__nav">
        <SidebarBtn
          icon={<MessageSquare size={17} />}
          label="New chat"
          onClick={onClearChat}
          disabled={!hasEntries}
          title="Clear chat"
        />
        <SidebarBtn
          icon={<Clock size={17} />}
          label="Query history"
          active={activePanel === "history"}
          onClick={() => onPanelToggle("history")}
          badge={historyCount}
          title="Query history"
        />
        <SidebarBtn
          icon={<BookMarked size={17} />}
          label="Saved answers"
          active={activePanel === "saved"}
          onClick={() => onPanelToggle("saved")}
          badge={savedCount}
          title="Saved answers"
        />
        <SidebarBtn
          icon={<BarChart2 size={17} />}
          label="Analytics"
          active={activePanel === "analytics"}
          onClick={() => onPanelToggle("analytics")}
          title="Analytics"
        />
      </div>

      <div className="sidebar__divider" />

      <div className="sidebar__connections">
        {/* Slack icon — inline SVG for the brand shape */}
        <SidebarBtn
          icon={
            <svg viewBox="0 0 24 24" width={17} height={17} fill="currentColor" aria-hidden="true">
              <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zm10.122 2.521a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.268 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zm-2.523 10.122a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
            </svg>
          }
          label="Slack settings"
          active={activePanel === "slack"}
          onClick={() => onPanelToggle("slack")}
          title="Slack settings"
        />
        <SidebarBtn
          icon={<Mail size={17} />}
          label="Gmail settings"
          active={activePanel === "gmail"}
          onClick={() => onPanelToggle("gmail")}
          title="Gmail settings"
        />
      </div>

      <div className="sidebar__bottom">
        <SidebarBtn
          icon={theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          label={theme === "dark" ? "Light mode" : "Dark mode"}
          onClick={toggleTheme}
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        />
        {user && (
          <SidebarBtn
            icon={<LogOut size={17} />}
            label="Sign out"
            onClick={() => signOut()}
            title={user.email ? `Sign out (${user.email})` : "Sign out"}
          />
        )}
      </div>
    </nav>
  );
}