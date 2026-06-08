// AuthForm — minimal email/password sign-in + sign-up.
//
// Phase 1 is intentionally bare-bones: no magic links, no OAuth, no
// password reset. Adding those is a Phase 2 concern; they slot in here
// as additional buttons calling supabase.auth.* methods.

import { useState } from "react";

import { useAuth } from "./AuthContext.jsx";

function LogoMark() {
  return (
    <div className="auth-shell__logo">
      <svg viewBox="0 0 40 40" width={40} height={40} fill="none" aria-hidden="true">
        <polygon points="20,2 36,11 36,29 20,38 4,29 4,11" stroke="var(--primary)" strokeWidth="1.8" fill="none" strokeLinejoin="round" />
        <circle cx="20" cy="20" r="2.5" fill="var(--primary)" />
        <line x1="20" y1="17.5" x2="20" y2="2"    stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
        <line x1="22.2" y1="21.3" x2="36" y2="29" stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
        <line x1="17.8" y1="21.3" x2="4"  y2="29" stroke="var(--primary)" strokeWidth="1.2" opacity="0.5" />
        <circle cx="20" cy="2"  r="1.8" fill="var(--primary)" opacity="0.72" />
        <circle cx="36" cy="29" r="1.8" fill="var(--primary)" opacity="0.72" />
        <circle cx="4"  cy="29" r="1.8" fill="var(--primary)" opacity="0.72" />
      </svg>
      <span className="auth-shell__wordmark">HYDRA<strong>DB</strong></span>
    </div>
  );
}

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
        <LogoMark />
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