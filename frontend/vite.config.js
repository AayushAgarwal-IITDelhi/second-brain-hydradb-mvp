import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Default Vite dev server is http://localhost:5173 — which is already in
// the backend's CORS allowlist, so no extra setup is needed.
export default defineConfig({
  plugins: [react()],
});