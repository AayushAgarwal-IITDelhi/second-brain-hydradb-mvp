// Thin client for the Second Brain backend.
//
// The frontend reads two Vite env vars at build time:
//   VITE_API_BASE_URL   e.g. http://127.0.0.1:8000
//   VITE_APP_API_KEY    must match APP_API_KEY in the backend's .env
//
// Both are visible in the bundled JS — that's fine for a local MVP where
// the API key is just a shared dev secret. Do not ship a production
// build with a real production key embedded.

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
const API_KEY = import.meta.env.VITE_APP_API_KEY || "";

/**
 * Thrown for any non-2xx response or network failure.
 * The component switches on `status` to render different messages.
 */
export class ApiError extends Error {
  constructor(message, { status = 0, errorType = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errorType = errorType;
  }
}

/**
 * Build the request body, omitting optional fields that are blank/null
 * so the backend's Pydantic model uses its defaults instead of receiving
 * "" for a filter the user didn't fill in.
 */
function buildRequestBody({
  question,
  topK,
  mode,
  channel,
  user,
  documentType,
  startDate,
  endDate,
  dateQuery,
  conversationHistory,
}) {
  const body = { question, top_k: topK, mode };
  if (channel && channel.trim()) body.channel = channel.trim();
  if (user && user.trim()) body.user = user.trim();
  if (documentType) body.document_type = documentType;

  // Natural-language date phrase. The backend parses it; explicit start/end
  // timestamps from the date pickers will still override the parsed range.
  if (dateQuery && dateQuery.trim()) body.date_query = dateQuery.trim();

  const startSec = dateInputToUnixSeconds(startDate, "start");
  const endSec = dateInputToUnixSeconds(endDate, "end");
  if (startSec !== null) body.start_timestamp = startSec;
  if (endSec !== null) body.end_timestamp = endSec;

  // Recent chat turns for follow-up reference resolution ("he", "that",
  // "the earlier discussion"). Backend caps at 6 server-side; we cap
  // here too so we don't send bytes the server is going to discard.
  // Sending an empty array would defeat the cache for stateless asks,
  // so we omit the field entirely when there's nothing to send.
  if (Array.isArray(conversationHistory) && conversationHistory.length > 0) {
    const trimmed = conversationHistory
      .filter((m) => m && (m.role === "user" || m.role === "assistant")
                       && typeof m.content === "string"
                       && m.content.trim().length > 0)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.content }));
    if (trimmed.length > 0) body.conversation_history = trimmed;
  }

  return body;
}

function dateInputToUnixSeconds(value, bound) {
  if (!value) return null;
  const parts = value.split("-").map((p) => parseInt(p, 10));
  if (parts.length !== 3 || parts.some(Number.isNaN)) return null;
  const [y, m, d] = parts;
  const date =
    bound === "end"
      ? new Date(y, m - 1, d, 23, 59, 59, 999)
      : new Date(y, m - 1, d, 0, 0, 0, 0);
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime() / 1000;
}

function assertApiKey() {
  if (!API_KEY) {
    throw new ApiError(
      "VITE_APP_API_KEY is not set. Copy .env.example to .env.local and fill it in.",
      { status: 0, errorType: "config_missing" }
    );
  }
}

/**
 * Detect AbortError reliably across browsers.
 *
 * - When you abort a fetch, the browser rejects with a DOMException
 *   whose `name` is "AbortError".
 * - When a reader is cancelled, the same error surfaces.
 * - Some bundlers wrap it; check `name` first, then fall back to checking
 *   the AbortSignal directly.
 */
function isAbortError(err, signal) {
  if (err && err.name === "AbortError") return true;
  if (signal && signal.aborted) return true;
  return false;
}

// ====================================================================
// POST /api/query  (non-streaming)
// ====================================================================
export async function askQuery(params) {
  assertApiKey();

  const url = `${API_BASE_URL}/api/query`;
  const body = buildRequestBody(params);

  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError(
      `Could not reach the backend. Is it running on ${API_BASE_URL}?`,
      { status: 0, errorType: "network_error" }
    );
  }

  let data = null;
  try { data = await response.json(); } catch { /* non-JSON */ }

  if (!response.ok) {
    throw new ApiError(messageForStatus(response.status, data), {
      status: response.status,
      errorType: (data && data.error_type) || "",
    });
  }
  return data;
}

// ====================================================================
// GET /api/admin/status
// ====================================================================
/**
 * Fetch a small ingestion-status snapshot for the admin card.
 * Returns null if the request fails — the admin card is best-effort
 * UX and shouldn't break the rest of the app on failure.
 */
export async function getAdminStatus() {
  if (!API_KEY) return null;

  const url = `${API_BASE_URL}/api/admin/status`;
  let response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { "X-API-Key": API_KEY },
    });
  } catch {
    return null;
  }
  if (!response.ok) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

// ====================================================================
// POST /api/query/stream  (Server-Sent Events)
// ====================================================================
/**
 * Stream an answer from the backend.
 *
 * @param {Object} params       Same shape as askQuery().
 * @param {Object} handlers
 * @param {(text: string) => void} handlers.onToken
 *        Called for every streamed token chunk.
 * @param {(final: Object) => void} handlers.onDone
 *        Called once at the end with { answer, sources, debug }.
 * @param {(err: ApiError) => void} handlers.onError
 *        Called if the stream errors. After this fires no more callbacks.
 * @param {() => void} [handlers.onAbort]
 *        Called once if the request was aborted via `signal`. This is
 *        NOT an error — the user intentionally stopped the stream. After
 *        this fires no more callbacks. If omitted, abort is a silent
 *        no-op (back-compat with older callers).
 * @param {AbortSignal} [signal]
 *        Optional AbortSignal so the caller can cancel mid-stream.
 *
 * We can't use EventSource because it doesn't support custom request
 * headers (we need X-API-Key) or POST bodies. So we fetch() with a
 * text/event-stream body and parse SSE manually.
 */
export async function streamQuery(params, handlers, signal) {
  assertApiKey();
  const { onToken, onDone, onError, onAbort } = handlers;

  // If the signal is already aborted before we start, short-circuit.
  if (signal && signal.aborted) {
    onAbort && onAbort();
    return;
  }

  const url = `${API_BASE_URL}/api/query/stream`;
  const body = buildRequestBody(params);

  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (isAbortError(err, signal)) {
      onAbort && onAbort();
      return;
    }
    onError(new ApiError(
      `Could not reach the backend. Is it running on ${API_BASE_URL}?`,
      { status: 0, errorType: "network_error" }
    ));
    return;
  }

  // Errors come back as JSON bodies, not SSE.
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch { /* non-JSON */ }
    onError(new ApiError(messageForStatus(response.status, data), {
      status: response.status,
      errorType: (data && data.error_type) || "",
    }));
    return;
  }

  if (!response.body) {
    onError(new ApiError("Streaming not supported by this browser.", {
      status: 0, errorType: "stream_unsupported",
    }));
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      // Cheap inline check — `reader.read()` will also throw AbortError
      // when the signal aborts mid-read, but checking here means we exit
      // promptly even between two reads.
      if (signal && signal.aborted) {
        onAbort && onAbort();
        return;
      }

      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE messages are separated by a blank line.
      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const rawMessage = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const parsed = parseSseMessage(rawMessage);
        if (!parsed) continue;
        handleSseMessage(parsed, { onToken, onDone, onError });
      }
    }
  } catch (err) {
    if (isAbortError(err, signal)) {
      onAbort && onAbort();
      return;
    }
    onError(new ApiError("Stream interrupted.", {
      status: 0, errorType: "stream_error",
    }));
  } finally {
    // Best-effort cleanup. If the reader is already closed (done==true)
    // these are no-ops. If aborted mid-read they release resources.
    try { reader.releaseLock(); } catch { /* ignore */ }
  }
}

function parseSseMessage(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue; // blank or comment
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const field = line.slice(0, idx);
    const value = line.slice(idx + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  const dataText = dataLines.join("\n");
  let data;
  try { data = JSON.parse(dataText); } catch { data = { text: dataText }; }
  return { event, data };
}

function handleSseMessage({ event, data }, { onToken, onDone, onError }) {
  if (event === "token") {
    if (typeof data.text === "string") onToken(data.text);
  } else if (event === "done") {
    onDone({
      answer:  typeof data.answer === "string" ? data.answer : "",
      sources: Array.isArray(data.sources) ? data.sources : [],
      debug:   data.debug || {},
    });
  } else if (event === "error") {
    onError(new ApiError(data.detail || "Stream error.", {
      status: 0,
      errorType: data.error_type || "stream_error",
    }));
  }
}

// ====================================================================
// Error message mapping
// ====================================================================
function messageForStatus(status, data) {
  if (status === 401) {
    return "Unauthorized — check that VITE_APP_API_KEY matches the backend's APP_API_KEY.";
  }
  if (status === 422 && data && Array.isArray(data.detail)) {
    return formatValidationErrors(data.detail);
  }
  if (status === 429) {
    return (data && data.detail) || "Too many requests. Please slow down.";
  }
  if (status === 504) {
    return "An upstream service timed out. Please try again.";
  }
  if (status >= 500) {
    return (data && data.detail) || "Server error. Please try again.";
  }
  return (data && data.detail) || `Request failed (HTTP ${status}).`;
}

function formatValidationErrors(detailArray) {
  const issues = detailArray
    .map((d) => {
      const field = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : "field";
      return `${field}: ${d.msg}`;
    })
    .slice(0, 3);
  return "Invalid request — " + issues.join("; ");
}