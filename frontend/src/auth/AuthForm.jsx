// AuthForm — minimal email/password sign-in + sign-up.
//
// Phase 1 is intentionally bare-bones: no magic links, no OAuth, no
// password reset. Adding those is a Phase 2 concern; they slot in here
// as additional buttons calling supabase.auth.* methods.

import { useState } from "react";

import { useAuth } from "./AuthContext.jsx";

export function AuthForm() {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState("signin"); // "signin" | "signup"
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setInfo("");

    const trimmedEmail = email.trim();
    if (!trimmedEmail || !password) {
      setError("Email and password are required.");
      return;
    }

    setSubmitting(true);
    try {
      if (mode === "signin") {
        await signIn(trimmedEmail, password);
      } else {
        await signUp(trimmedEmail, password);
        setInfo(
          "Account created. If your Supabase project requires email " +
            "confirmation, check your inbox. Otherwise you can sign in below."
        );
        setMode("signin");
        setPassword("");
      }
    } catch (err) {
      setError(err?.message || "Authentication failed.");
    } finally {
      setSubmitting(false);
    }
  }

  function toggleMode() {
    setMode(mode === "signin" ? "signup" : "signin");
    setError("");
    setInfo("");
  }

  return (
    <div className="auth-shell">
      <form className="auth-shell__card" onSubmit={handleSubmit}>
        <h1 className="auth-shell__title">Second Brain</h1>
        <p className="auth-shell__muted">
          {mode === "signin" ? "Sign in to continue." : "Create an account."}
        </p>

        <label className="auth-shell__label">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
            disabled={submitting}
          />
        </label>

        <label className="auth-shell__label">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={
              mode === "signin" ? "current-password" : "new-password"
            }
            minLength={6}
            required
            disabled={submitting}
          />
        </label>

        {error && (
          <p className="auth-shell__error" role="alert">
            {error}
          </p>
        )}
        {info && <p className="auth-shell__info">{info}</p>}

        <button
          type="submit"
          disabled={submitting}
          className="btn btn--primary auth-shell__submit"
        >
          {submitting ? "…" : mode === "signin" ? "Sign in" : "Sign up"}
        </button>

        <button
          type="button"
          className="auth-shell__link"
          onClick={toggleMode}
          disabled={submitting}
        >
          {mode === "signin"
            ? "No account? Sign up."
            : "Have an account? Sign in."}
        </button>
      </form>
    </div>
  );
}