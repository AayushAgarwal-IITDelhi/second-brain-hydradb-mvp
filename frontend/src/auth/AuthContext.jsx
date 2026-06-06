// AuthContext — hydrates the current Supabase session and keeps it in
// sync with sign-in / sign-out / token-refresh events. Exposes a
// minimal API the rest of the app uses via `useAuth()`.

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { supabase } from "../lib/supabase.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState(null);

  useEffect(() => {
    let mounted = true;

    // Hydrate the cached session on first mount.
    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (!mounted) return;
        setSession(data?.session ?? null);
        setLoading(false);
      })
      .catch((err) => {
        if (!mounted) return;
        console.error("Supabase session hydration failed:", err);
        setAuthError("Could not connect to authentication service. Please reload the page.");
        setLoading(false);
      });

    // Stay in sync. Also fires on token refresh, so `accessToken`
    // downstream stays current without us doing anything.
    const { data: sub } = supabase.auth.onAuthStateChange(
      (_event, newSession) => {
        setSession(newSession);
      }
    );

    return () => {
      mounted = false;
      sub?.subscription?.unsubscribe?.();
    };
  }, []);

  const value = useMemo(
    () => ({
      session,
      user: session?.user ?? null,
      accessToken: session?.access_token ?? "",
      loading,
      authError,
      async signIn(email, password) {
        const { error } = await supabase.auth.signInWithPassword({
          email,
          password,
        });
        if (error) throw error;
      },
      async signUp(email, password) {
        const { error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
      },
      async signOut() {
        await supabase.auth.signOut();
      },
    }),
    [session, loading]
  );

  return (
    <AuthContext.Provider value={value}>
      {authError && !loading && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            background: "#fee2e2",
            color: "#991b1b",
            padding: "12px 16px",
            textAlign: "center",
            zIndex: 9999,
            fontSize: "14px",
          }}
        >
          🔒 {authError}
        </div>
      )}
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within <AuthProvider>");
  }
  return ctx;
}