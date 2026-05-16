import { useEffect, useRef, useState } from "react";

import { ApiError, streamQuery } from "./api.js";

const MODES = [
  { value: "default",      label: "Default — concise answer" },
  { value: "summary",      label: "Summary — bullet briefing" },
  { value: "decisions",    label: "Decisions — extract decisions" },
  { value: "action_items", label: "Action items — extract tasks" },
  { value: "who_said",     label: "Who said — quote attribution" },
];

const DOC_TYPES = [
  { value: "",        label: "Any" },
  { value: "message", label: "Message" },
  { value: "thread",  label: "Thread" },
];

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

  // Chat history.
  const [entries, setEntries] = useState([]);
  const [submitting, setSubmitting] = useState(false);

  // Latest in-flight stream's AbortController so the user can cancel.
  // useRef so it survives re-renders and so the Stop handler always sees
  // the freshest controller.
  const activeStreamRef = useRef(null);

  // Auto-scroll to bottom on any timeline change.
  const bottomRef = useRef(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

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

    const params = {
      question: trimmed,
      topK,
      mode,
      channel,
      user,
      documentType,
      startDate,
      endDate,
    };

    const userEntry = makeUserEntry(trimmed, params);
    const assistantEntry = makePendingAssistantEntry();

    setEntries((current) => [...current, userEntry, assistantEntry]);
    setQuestion("");
    setSubmitting(true);

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

  return (
    <div className="app">
      <header className="app__header">
        <h1>Second Brain</h1>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={handleClearChat}
          disabled={entries.length === 0 && !submitting}
        >
          Clear chat
        </button>
      </header>

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

  const cacheHit = entry.debug && entry.debug.cache_hit === true;

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
      </div>
      <div className="bubble__body">
        {entry.answer ? (
          <p className="answer">
            {entry.answer}
            {entry.streaming && <span className="cursor">▍</span>}
          </p>
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