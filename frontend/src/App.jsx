import { useEffect, useRef, useState } from "react";

import { askQuery, ApiError } from "./api.js";

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

/**
 * One entry in the chat timeline.
 * role = 'user'      -> question only
 * role = 'assistant' -> answer + sources + (optional) error
 */
function makeUserEntry(question, params) {
  return {
    id: crypto.randomUUID(),
    role: "user",
    question,
    params,            // snapshot of the params used (helpful for display)
  };
}

function makePendingAssistantEntry() {
  return {
    id: crypto.randomUUID(),
    role: "assistant",
    loading: true,
    error: null,
    answer: "",
    sources: [],
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

  // Chat history for the session.
  const [entries, setEntries] = useState([]);
  const [submitting, setSubmitting] = useState(false);

  // Auto-scroll to the bottom whenever an entry is added or updated.
  const bottomRef = useRef(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries]);

  function updateAssistantEntry(id, patch) {
    setEntries((current) =>
      current.map((e) => (e.id === id ? { ...e, ...patch } : e))
    );
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || submitting) return;

    // Build the params for both the request and the chat entry snapshot.
    const params = {
      question: trimmed,
      topK,
      mode,
      channel,
      user,
      documentType,
    };

    const userEntry = makeUserEntry(trimmed, params);
    const assistantEntry = makePendingAssistantEntry();

    setEntries((current) => [...current, userEntry, assistantEntry]);
    setQuestion("");
    setSubmitting(true);

    try {
      const data = await askQuery(params);
      updateAssistantEntry(assistantEntry.id, {
        loading: false,
        answer: data.answer || "",
        sources: Array.isArray(data.sources) ? data.sources : [],
        debug: data.debug || null,
      });
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Unexpected error.";
      const status = err instanceof ApiError ? err.status : 0;
      updateAssistantEntry(assistantEntry.id, {
        loading: false,
        error: { message, status },
      });
    } finally {
      setSubmitting(false);
    }
  }

  function handleClearChat() {
    setEntries([]);
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>Second Brain</h1>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={handleClearChat}
          disabled={entries.length === 0}
        >
          Clear chat
        </button>
      </header>

      <main className="chat">
        {entries.length === 0 && (
          <EmptyState />
        )}
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
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
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
                <option key={n} value={n}>
                  {n}
                </option>
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
                <option key={d.value} value={d.value}>
                  {d.label}
                </option>
              ))}
            </select>
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
              // Enter to submit, Shift+Enter for newline.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSubmit(e);
              }
            }}
          />
          <button
            type="submit"
            className="btn btn--primary"
            disabled={submitting || !question.trim()}
          >
            {submitting ? "Searching..." : "Ask"}
          </button>
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
      <p className="empty__lead">
        Ask anything from your Slack workspace.
      </p>
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
  // Show only the filters that were actually set, so the bubble stays tidy.
  const usedFilters = [];
  if (params.mode && params.mode !== "default") usedFilters.push(`mode=${params.mode}`);
  if (params.channel) usedFilters.push(`channel=${params.channel}`);
  if (params.user) usedFilters.push(`user=${params.user}`);
  if (params.documentType) usedFilters.push(`type=${params.documentType}`);

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
  if (entry.loading) {
    return (
      <div className="bubble bubble--assistant">
        <div className="bubble__role">Second Brain</div>
        <div className="bubble__body bubble__body--loading">
          <span className="spinner" aria-hidden="true" />
          Searching memory...
        </div>
      </div>
    );
  }

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

  return (
    <div className="bubble bubble--assistant">
      <div className="bubble__role">Second Brain</div>
      <div className="bubble__body">
        {entry.answer ? (
          <p className="answer">{entry.answer}</p>
        ) : (
          <p className="answer answer--empty">(No answer returned.)</p>
        )}

        {entry.sources && entry.sources.length > 0 && (
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

// ----------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------

/**
 * Slack timestamps look like "1778819911.909159" — seconds since epoch
 * with a microsecond fractional part. Show them as a local date/time.
 * Returns the raw string if parsing fails.
 */
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