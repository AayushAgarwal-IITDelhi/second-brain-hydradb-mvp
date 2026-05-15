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
 * so the backend's Pydantic model uses its defaults instead of
 * receiving "" for a filter the user didn't fill in.
 */
function buildRequestBody({ question, topK, mode, channel, user, documentType }) {
  const body = {
    question,
    top_k: topK,
    mode,
  };
  if (channel && channel.trim()) body.channel = channel.trim();
  if (user && user.trim()) body.user = user.trim();
  if (documentType) body.document_type = documentType;
  return body;
}

/**
 * POST /api/query and return the parsed JSON.
 * Throws ApiError with a friendly message on failure.
 */
export async function askQuery(params) {
  if (!API_KEY) {
    throw new ApiError(
      "VITE_APP_API_KEY is not set. Copy .env.example to .env.local and fill it in.",
      { status: 0, errorType: "config_missing" }
    );
  }

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
    // Network-level failure: server not running, DNS, CORS blocked entirely.
    throw new ApiError(
      "Could not reach the backend. Is it running on " + API_BASE_URL + "?",
      { status: 0, errorType: "network_error" }
    );
  }

  // Try to parse the body as JSON whether it's a 2xx or 4xx/5xx.
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Non-JSON response (rare for FastAPI). Fall through with `data = null`.
  }

  if (!response.ok) {
    throw new ApiError(messageForStatus(response.status, data), {
      status: response.status,
      errorType: (data && data.error_type) || "",
    });
  }

  return data;
}

/**
 * Translate a backend error into a short user-facing message.
 */
function messageForStatus(status, data) {
  // FastAPI's standard `detail` field is the source of truth when present.
  // For 422 it's an array of validation errors; for everything else it's a
  // string set by our typed errors layer.
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