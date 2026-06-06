// AuthGate — renders the sign-in form when the user isn't authenticated,
// otherwise passes through to children. This is the only thing wrapped
// around <App />; it keeps App.jsx untouched.

import { AuthForm } from "./AuthForm.jsx";
import { useAuth } from "./AuthContext.jsx";

export function AuthGate({ children }) {
  const { session, loading } = useAuth();

  if (loading) {
    return (
      <div className="auth-shell">
        <div className="auth-shell__card">
          <p className="auth-shell__muted">Loading…</p>
        </div>
      </div>
    );
  }

  if (!session) {
    return <AuthForm />;
  }

  return children;
}