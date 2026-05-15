"""
Tracks which Slack messages/threads have already been uploaded to HydraDB,
so re-running the ingestion script does not re-upload the same files.

State lives in a plain JSON file (no database) at:
    backend/data/ingestion_state.json

Schema:
    {
      "version": 1,
      "entries": {
        "<stable_key>": {
          "stable_key":   "slack:C123:1778775842.876209",
          "filename":     "slack_all-second-brain_1778775842.md",
          "source_id":    "aa54a1b1-...",       # from HydraDB results, may be null
          "channel_id":   "C123",
          "channel_name": "all-second-brain",
          "ts":           "1778775842.876209",  # for messages
          "thread_ts":    null,                 # for threads
          "uploaded_at":  "2026-05-15T12:34:56.789012+00:00"
        },
        ...
      }
    }
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


STATE_VERSION = 1


# ---------------------------------------------------------------------- #
# Stable keys
# ---------------------------------------------------------------------- #
def stable_key_for_message(channel_id: str, ts: str) -> str:
    """`slack:{channel_id}:{ts}` — never re-derived elsewhere; use this."""
    return f"slack:{channel_id}:{ts}"


def stable_key_for_thread(channel_id: str, thread_ts: str) -> str:
    """`slack_thread:{channel_id}:{thread_ts}` — never re-derived elsewhere; use this."""
    return f"slack_thread:{channel_id}:{thread_ts}"


# ---------------------------------------------------------------------- #
# State container
# ---------------------------------------------------------------------- #
class IngestionState:
    def __init__(self, path: Path):
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ----- disk I/O ---------------------------------------------------- #
    def _load(self) -> None:
        """Load the state file if it exists; otherwise start empty."""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[state] Could not read state file at {self.path}: {e}. "
                f"Starting with empty state."
            )
            return

        entries = raw.get("entries") if isinstance(raw, dict) else None
        if isinstance(entries, dict):
            self.entries = entries

    def save(self) -> None:
        """Atomically write the state file (write to .tmp, then rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": STATE_VERSION, "entries": self.entries}
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)

    # ----- queries ----------------------------------------------------- #
    def has(self, stable_key: str) -> bool:
        return stable_key in self.entries

    # ----- mutations --------------------------------------------------- #
    def mark_uploaded(
        self,
        stable_key: str,
        filename: str,
        channel_id: str,
        channel_name: str,
        ts: Optional[str] = None,
        thread_ts: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> None:
        """Record a successful upload. Caller still needs to call save()."""
        self.entries[stable_key] = {
            "stable_key":   stable_key,
            "filename":     filename,
            "source_id":    source_id,
            "channel_id":   channel_id,
            "channel_name": channel_name,
            "ts":           ts,
            "thread_ts":    thread_ts,
            "uploaded_at":  datetime.now(timezone.utc).isoformat(),
        }