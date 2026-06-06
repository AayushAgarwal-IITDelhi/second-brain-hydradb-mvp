// Supabase client singleton.
//
// VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY are read at build time and
// baked into the bundle. The anon key is safe to ship — Postgres RLS
// enforces per-user access on the database side.

import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL;
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  // Don't throw — Vite still needs to render *something*. The AuthGate
  // surfaces the failure to sign in with a clearer message than a crash.
  // eslint-disable-next-line no-console
  console.error(
    "Supabase env not configured. Set VITE_SUPABASE_URL and " +
      "VITE_SUPABASE_ANON_KEY in frontend/.env.local"
  );
}

export const supabase = createClient(
  SUPABASE_URL || "https://invalid.supabase.co",
  SUPABASE_ANON_KEY || "invalid-anon-key"
);