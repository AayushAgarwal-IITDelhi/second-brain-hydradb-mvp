// WorkspaceContext — fetches the user's workspaces from the backend,
// remembers which one is currently active (in localStorage), and
// pushes credentials into api.js's module-level auth context so the
// rest of the app's helpers don't need to thread tokens through.
//
// Bootstrap order (the part that previously raced):
//
//   1. AuthProvider hydrates the Supabase session and signals
//      `loading: false`. accessToken may be a fresh access_token or
//      empty (no session).
//
//   2. We install a fresh-token GETTER into api.js via setAuthContext.
//      The getter calls supabase.auth.getSession() at request time so
//      Supabase's auto-refresh produces a current token even if the
//      one in our React state has gone stale (e.g. the tab sat idle
//      past the 1-hour Supabase default expiry).
//
//   3. Once auth has resolved, we GET /api/me/workspaces via the
//      shared api.js helper (which sends ONLY the bearer token —
//      that route is user-only, NOT workspace-scoped, which is the
//      whole point of this fix).
//
//   4. We resolve activeWorkspaceId:
//        - keep the localStorage value if it's still a member of the
//          returned list,
//        - otherwise default to the first workspace,
//        - otherwise empty.
//
//   5. Whenever accessToken (presence/absence) or activeWorkspaceId
//      changes, re-install creds into api.js. The token GETTER itself
//      is stable across renders so workspace-id flips don't disturb
//      live requests.

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { listUserWorkspaces, setAuthContext } from "../api.js";
import { supabase } from "../lib/supabase.js";
import { useAuth } from "./AuthContext.jsx";

const STORAGE_KEY = "secondBrain.activeWorkspaceId";

const WorkspaceContext = createContext(null);

function loadActiveId() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function saveActiveId(id) {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // localStorage disabled (private mode, quota, etc.) — fall back to
    // in-memory only for this session.
  }
}

/**
 * Get the freshest possible Supabase access token. supabase-js caches
 * the session in memory + localStorage and auto-refreshes it before it
 * expires, so calling getSession() per request is cheap (no network
 * unless a refresh is actually due).
 *
 * Returns an empty string when nobody is signed in, which api.js maps
 * to an `auth_missing` ApiError.
 */
async function readCurrentAccessToken() {
  try {
    const { data } = await supabase.auth.getSession();
    return data?.session?.access_token || "";
  } catch {
    return "";
  }
}

export function WorkspaceProvider({ children }) {
  const { accessToken, loading: authLoading } = useAuth();
  const [workspaces, setWorkspaces] = useState([]);
  const [activeWorkspaceId, setActiveIdState] = useState(() => loadActiveId());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Token getter installed in api.js. We hold it in a ref so its
  // identity stays stable across re-renders — that way swapping the
  // active workspace doesn't churn api.js's token source. The getter
  // always reads from supabase-js, which is the only safe source of
  // truth for "the current valid token right now".
  const tokenGetterRef = useRef(readCurrentAccessToken);

  // Keep api.js's auth context in sync.
  //
  // We push BOTH the token getter AND the active workspace id every
  // time either accessToken (as a presence signal — empty vs not) or
  // activeWorkspaceId changes. Pushing the GETTER (not the static
  // token) means subsequent api.js calls re-resolve via supabase-js
  // and pick up refreshes automatically.
  //
  // When `accessToken` is empty (signed out) we install an empty
  // string instead of the getter, so api.js immediately surfaces an
  // auth_missing error rather than asking supabase for a session it
  // has just lost.
  useEffect(() => {
    if (accessToken) {
      setAuthContext({
        accessToken: tokenGetterRef.current,
        activeWorkspaceId,
      });
    } else {
      setAuthContext({ accessToken: "", activeWorkspaceId: "" });
    }
  }, [accessToken, activeWorkspaceId]);

  // Fetch the user's workspaces.
  //
  // Effect runs whenever auth becomes ready with a token. We deliberately
  // wait for authLoading=false AND accessToken to be truthy before
  // firing so we never send a request with no/partial credentials.
  //
  // `cancelled` guards against a stale fetch resolving after the user
  // signs out (or switches accounts) by the time the JSON parses.
  useEffect(() => {
    if (authLoading) {
      // Auth is still resolving — stay in the loading state, do nothing.
      return undefined;
    }
    if (!accessToken) {
      // Signed out (or session never loaded). Clean slate.
      setWorkspaces([]);
      setActiveIdState((current) => {
        // Don't clobber the saved id from localStorage — the user may
        // sign back in and we want to remember their preference.
        return current;
      });
      setLoading(false);
      setError("");
      return undefined;
    }

    let cancelled = false;
    setLoading(true);
    setError("");

    (async () => {
      try {
        // Push creds BEFORE the call so api.js has them when listUserWorkspaces
        // resolves the token. Using the getter form is what fixes the
        // stale-token symptom: even if `accessToken` in our React state
        // is briefly out of date, the getter asks supabase-js for the
        // current one and the SDK refreshes if needed.
        setAuthContext({
          accessToken: tokenGetterRef.current,
          activeWorkspaceId,
        });

        const data = await listUserWorkspaces();
        if (cancelled) return;

        const list = Array.isArray(data) ? data : [];
        setWorkspaces(list);

        // Auto-select first workspace if there's no valid stored choice.
        setActiveIdState((current) => {
          if (current && list.some((w) => w.id === current)) {
            return current;
          }
          const next = list[0]?.id || "";
          // Persist so a reload doesn't bounce back to the first one if
          // the user just chose another.
          if (next) saveActiveId(next);
          return next;
        });
      } catch (e) {
        if (cancelled) return;
        setError(e?.message || "Could not load workspaces.");
        setWorkspaces([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [accessToken, authLoading]);  // intentionally not depending on activeWorkspaceId

  function setActiveWorkspaceId(id) {
    setActiveIdState(id);
    saveActiveId(id);
  }

  const value = useMemo(
    () => ({
      workspaces,
      activeWorkspaceId,
      setActiveWorkspaceId,
      loading,
      error,
    }),
    [workspaces, activeWorkspaceId, loading, error]
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) {
    throw new Error("useWorkspace must be used within <WorkspaceProvider>");
  }
  return ctx;
}