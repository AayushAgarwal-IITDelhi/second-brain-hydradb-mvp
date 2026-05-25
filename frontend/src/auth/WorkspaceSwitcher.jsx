// WorkspaceSwitcher — small floating control rendered outside App.jsx
// (in main.jsx) so we don't have to touch the 2109-line monolith for
// Phase 1. The switcher dropdown only renders when the user belongs to
// more than one workspace; the Sign out button is always present.

import { useAuth } from "./AuthContext.jsx";
import { useWorkspace } from "./WorkspaceContext.jsx";

export function WorkspaceSwitcher() {
  const { signOut, user } = useAuth();
  const { workspaces, activeWorkspaceId, setActiveWorkspaceId } =
    useWorkspace();

  if (!user) return null;

  return (
    <div className="ws-switcher" aria-label="Account">
      {workspaces.length > 1 && (
        <select
          className="ws-switcher__select"
          value={activeWorkspaceId}
          onChange={(e) => setActiveWorkspaceId(e.target.value)}
          aria-label="Active workspace"
        >
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name} ({w.role})
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        className="ws-switcher__signout"
        onClick={() => signOut()}
        title={user.email || "Sign out"}
      >
        Sign out
      </button>
    </div>
  );
}