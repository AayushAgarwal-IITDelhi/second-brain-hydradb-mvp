import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, getAdminStatus, streamQuery } from "./api.js";

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
  // Form state
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("default");
  const [topK, setTopK] = useState(5);
  const [channel, setChannel] = useState("");
  const [user, setUser] = useState("");
  const [documentType, setDocumentType] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [dateQuery, setDateQuery] = useState("");

  // Chat history.
  const [entries, setEntries] = useState([]);
  const [submitting, setSubmitting] = useState(false);

  // Admin status snapshot (refreshed periodically and after each query).
  const [adminStatus, setAdminStatus] = useState(null);

  // Query history — the user's previous questions + filters, persisted
  // to localStorage. Initial value comes from disk via a lazy initializer
  // so we don't read storage on every re-render.
  const [history, setHistory] = useState(() => loadHistory());
  const [historyOpen, setHistoryOpen] = useState(false);

  // Persist history whenever it changes. Cheap because we cap at 30.
  useEffect(() => {
    saveHistory(history);
  }, [history]);

  // Latest in-flight stream's AbortController so the user can cancel.
  const activeStreamRef = useRef(null);

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
      topK, mode, channel, user, documentType,
      startDate, endDate, dateQuery,
    }));

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
    setStartDate(item.startDate || "");
    setEndDate(item.endDate || "");
    setDateQuery(item.dateQuery || "");
  }

  function handleLoadHistory(item) {
    if (submitting) return;
    loadHistoryItemIntoComposer(item);
    setHistoryOpen(false); // collapse so the composer is visible
  }

  function handleRunAgain(item) {
    if (submitting) return;
    loadHistoryItemIntoComposer(item);
    setHistoryOpen(false);
    // Submit on the next tick so the state updates above have flushed
    // — handleSubmit reads `question` / filters from state.
    setTimeout(() => {
      const form = document.querySelector(".composer");
      if (form) form.requestSubmit();
    }, 0);
  }

  function handleDeleteHistoryItem(id) {
    setHistory((prev) => prev.filter((it) => it.id !== id));
  }

  function handleClearHistory() {
    setHistory([]);
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>Second Brain</h1>
        <div className="app__header-actions">
          <button
            type="button"
            className={`btn btn--ghost ${historyOpen ? "btn--active" : ""}`}
            onClick={() => setHistoryOpen((open) => !open)}
            aria-expanded={historyOpen}
            aria-controls="query-history-panel"
            disabled={submitting}
            title={history.length > 0
              ? `${history.length} saved quer${history.length === 1 ? "y" : "ies"}`
              : "No saved queries yet"}
          >
            History {history.length > 0 && (
              <span className="btn__count">{history.length}</span>
            )}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={handleClearChat}
            disabled={entries.length === 0 && !submitting}
          >
            Clear chat
          </button>
        </div>
      </header>

      {historyOpen && (
        <QueryHistoryPanel
          history={history}
          onLoad={handleLoadHistory}
          onRunAgain={handleRunAgain}
          onDelete={handleDeleteHistoryItem}
          onClearAll={handleClearHistory}
          submitting={submitting}
        />
      )}

      <AdminStatus status={adminStatus} />

      <main className="chat">
        {entries.length === 0 && <EmptyState />}
        {entries.map((entry) =>
          entry.role === "user" ? (
            <UserBubble key={entry.id} entry={entry} />
          ) : (
            <AssistantBubble key={entry.id} entry={entry} />
          )
        )}
        <div ref={bottomRef} />
      </main>

      <form className="composer" onSubmit={handleSubmit}>
        <div className="composer__controls">
          <label className="field">
            <span className="field__label">Mode</span>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              disabled={submitting}
            >
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </label>

          <label className="field field--narrow">
            <span className="field__label">top_k</span>
            <select
              value={topK}
              onChange={(e) => setTopK(parseInt(e.target.value, 10))}
              disabled={submitting}
            >
              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="field__label">Channel (optional)</span>
            <input
              type="text"
              value={channel}
              onChange={(e) => setChannel(e.target.value)}
              placeholder="e.g. product"
              disabled={submitting}
            />
          </label>

          <label className="field">
            <span className="field__label">User (optional)</span>
            <input
              type="text"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="e.g. Rahul"
              disabled={submitting}
            />
          </label>

          <label className="field field--narrow">
            <span className="field__label">Type</span>
            <select
              value={documentType}
              onChange={(e) => setDocumentType(e.target.value)}
              disabled={submitting}
            >
              {DOC_TYPES.map((d) => (
                <option key={d.value} value={d.value}>{d.label}</option>
              ))}
            </select>
          </label>

          <label className="field field--wide">
            <span className="field__label">Date phrase (optional)</span>
            <input
              type="text"
              value={dateQuery}
              onChange={(e) => setDateQuery(e.target.value)}
              placeholder="last week, yesterday, after May 10..."
              disabled={submitting}
            />
          </label>

          <label className="field field--narrow">
            <span className="field__label">From</span>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              disabled={submitting}
            />
          </label>

          <label className="field field--narrow">
            <span className="field__label">To</span>
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              disabled={submitting}
            />
          </label>
        </div>

        <div className="composer__input-row">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask your second brain anything from Slack..."
            rows={2}
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
              className="btn btn--ghost"
              onClick={handleStop}
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn--primary"
              disabled={!question.trim()}
            >
              Ask
            </button>
          )}
        </div>
      </form>
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
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={onClearAll}
            disabled={submitting}
          >
            Clear history
          </button>
        )}
      </div>

      {history.length > 0 && (
        <ul className="history__list">
          {history.map((item) => (
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

function EmptyState() {
  return (
    <div className="empty">
      <p className="empty__lead">Ask anything from your Slack workspace.</p>
      <ul className="empty__examples">
        <li>"What did we decide about the memory layer?"</li>
        <li>"Summary of yesterday's design discussion"</li>
        <li>"Who said they would document the API contract?"</li>
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

function AssistantBubble({ entry }) {
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
        <a
          href={permalink}
          target="_blank"
          rel="noreferrer"
          className="source__link"
        >
          Open in Slack →
        </a>
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