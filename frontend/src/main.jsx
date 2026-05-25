import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App.jsx";
import { AuthProvider } from "./auth/AuthContext.jsx";
import { AuthGate } from "./auth/AuthGate.jsx";
import { WorkspaceProvider } from "./auth/WorkspaceContext.jsx";
import { WorkspaceSwitcher } from "./auth/WorkspaceSwitcher.jsx";
import "./styles.css";

// AuthProvider           -> hydrates Supabase session
//   AuthGate             -> renders <AuthForm /> until signed in
//     WorkspaceProvider  -> loads /api/me/workspaces, pushes creds into api.js
//       <WorkspaceSwitcher />  (floats top-right, outside App)
//       <App />          (untouched 2109-line monolith)
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AuthProvider>
      <AuthGate>
        <WorkspaceProvider>
          <WorkspaceSwitcher />
          <App />
        </WorkspaceProvider>
      </AuthGate>
    </AuthProvider>
  </React.StrictMode>
);