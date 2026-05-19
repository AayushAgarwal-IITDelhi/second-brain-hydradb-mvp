"""
Minimal HydraDB client for the Second Brain MVP.

Endpoints used:
    POST {base_url}/ingestion/upload_knowledge   (ingestion — multipart)
    POST {base_url}/recall/full_recall           (retrieval — JSON)

Verified ingestion contract:
    - Content-Type: multipart/form-data  (set automatically by `requests`)
    - Form fields:
        tenant_id     = <HYDRADB_TENANT_ID>
        sub_tenant_id = <HYDRADB_SUB_TENANT_ID>
        files         = one or more uploaded .md / .txt files
    - Auth header:
        Authorization: Bearer <HYDRADB_API_KEY>

Recall contract (JSON body): tenant_id, sub_tenant_id, query, top_k.
If the deployment rejects `top_k`, flip RECALL_TOP_K_FIELD below.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import requests

from errors import HydraDBError, UpstreamTimeoutError
from retry import retry, RetryExhausted


# ---------------------------------------------------------------------- #
# If your HydraDB deployment uses "max_results" instead of "top_k" for the
# recall endpoint, change this one constant. The full_recall method will
# print a clear hint pointing here whenever the server returns a 4xx.
# ---------------------------------------------------------------------- #
RECALL_TOP_K_FIELD = "top_k"


# Retry-wrapped POST used by upload_knowledge.  Retries on network-level
# transients (timeout, connection reset) but not on auth or 4xx errors.
@retry(
    service="hydradb",
    max_attempts=3,
    initial_delay=1.0,
    retryable_exceptions=(requests.Timeout, requests.ConnectionError, OSError),
)
def _post_upload(url: str, headers: dict, data: dict, files: list) -> requests.Response:
    return requests.post(url, headers=headers, data=data, files=files, timeout=120)


# ---------------------------------------------------------------------- #
# Counting helper (shared by HydraDBClient.upload_knowledge and by the
# CLI's upload_in_batches, so the per-batch print and the run-wide tally
# never drift apart).
#
# HydraDB returns HTTP 202 with a payload like:
#     {
#       "success":       true,
#       "status":        "queued",
#       "success_count": 2,
#       "failed_count":  0,
#       "results":       [ { "id": "...", "status": "queued", "error": null }, ... ]
#     }
#
# A document is treated as FAILED only when:
#   - the HTTP call itself failed  (payload arrives here as {}),
#   - top-level `success` is explicitly False,
#   - a result's `status` is explicitly "failed" or "error", OR
#   - a result's `error` field is non-null / non-empty.
#
# Everything else — "queued", "success", "ok", "indexed", "uploaded",
# missing status, etc. — counts as a successful upload state.
# ---------------------------------------------------------------------- #
FAILED_RESULT_STATUSES = {"failed", "error"}


def _result_is_failed(result: Dict[str, Any]) -> bool:
    """A single result item counts as failed only if explicitly bad."""
    status = (result.get("status") or "").lower()
    if status in FAILED_RESULT_STATUSES:
        return True
    if result.get("error"):  # non-null and non-empty
        return True
    return False


def summarize_upload_response(
    payload: Dict[str, Any],
    batch_size: int,
) -> Tuple[int, int]:
    """
    Return (success_count, failure_count) for one upload_knowledge response.

    Preference order:
      1. Empty payload -> whole batch failed (HTTP / network error upstream).
      2. Top-level `success: false` -> whole batch failed.
      3. Top-level `success_count` / `failed_count` -> trust the server.
      4. Per-item `results` -> count via _result_is_failed.
      5. Nothing usable -> assume the batch_size docs were accepted.
    """
    if not payload:
        return (0, batch_size)

    if payload.get("success") is False:
        return (0, batch_size)

    if "success_count" in payload or "failed_count" in payload:
        ok = int(payload.get("success_count") or 0)
        bad = int(payload.get("failed_count") or 0)
        return (ok, bad)

    results = payload.get("results")
    if isinstance(results, list):
        bad = sum(1 for r in results if _result_is_failed(r))
        ok = len(results) - bad
        return (ok, bad)

    return (batch_size, 0)


class HydraDBClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        sub_tenant_id: Optional[str] = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("HYDRADB_BASE_URL", "https://api.hydradb.com")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("HYDRADB_API_KEY")
        self.tenant_id = tenant_id or os.getenv("HYDRADB_TENANT_ID")
        self.sub_tenant_id = (
            sub_tenant_id
            or os.getenv("HYDRADB_SUB_TENANT_ID", "slack-second-brain")
        )

        if not self.api_key:
            raise ValueError("HYDRADB_API_KEY is not set.")
        if not self.tenant_id:
            raise ValueError("HYDRADB_TENANT_ID is not set.")

    # ------------------------------------------------------------------ #
    def _auth_headers(self) -> Dict[str, str]:
        """
        Only the Authorization header — do NOT set Content-Type here.

        `requests` builds the multipart Content-Type with the correct
        boundary string when we pass `files=`. Setting it manually would
        break the multipart parsing on the server side.
        """
        return {"Authorization": f"Bearer {self.api_key}"}

    # ------------------------------------------------------------------ #
    def upload_knowledge(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Upload a batch of knowledge files to HydraDB.

        Each item in `files` looks like:
            {"filename": "slack_all-second-brain_1778775842.md",
             "content":  "<markdown text>"}

        `content` may be str or bytes; str is encoded as UTF-8.

        Returns the parsed HydraDB response dict on success, or {} on failure.
        """
        if not files:
            print("[hydradb] No files to upload.")
            return {}

        url = f"{self.base_url}/ingestion/upload_knowledge"

        # Form fields (everything that isn't a file goes in `data`).
        data = {
            "tenant_id": self.tenant_id,
            "sub_tenant_id": self.sub_tenant_id,
        }

        # Build the multipart file list. Reusing the same form key "files"
        # for every entry is what tells the server this is a multi-file
        # upload (FastAPI-style `List[UploadFile]`).
        multipart_files = []
        for item in files:
            filename = item["filename"]
            content = item["content"]
            content_bytes = (
                content.encode("utf-8") if isinstance(content, str) else content
            )
            multipart_files.append(
                ("files", (filename, content_bytes, "text/markdown"))
            )

        # ------------------------------------------------------------------
        try:
            response = _post_upload(
                url,
                headers=self._auth_headers(),
                data=data,
                files=multipart_files,
            )
        except (requests.RequestException, RetryExhausted) as e:
            print(f"[hydradb] Network error talking to HydraDB: {e}")
            return {}

        # Always print the raw response so failures are easy to debug.
        print(f"[hydradb] POST {url} -> HTTP {response.status_code}")
        print(f"[hydradb] Response body: {response.text}")

        if response.status_code >= 400:
            return {}

        try:
            payload = response.json()
        except ValueError:
            print("[hydradb] Response was not JSON.")
            return {}

        # ----- Per-file logging -------------------------------------------
        # Print one line per result so individual failures are easy to spot,
        # regardless of how we end up counting the batch totals.
        results = payload.get("results")
        if isinstance(results, list):
            for i, r in enumerate(results):
                status = (r.get("status") or "").lower() or "unknown"
                ref = (
                    r.get("id")
                    or r.get("doc_id")
                    or r.get("filename")
                    or f"file_{i}"
                )
                err = r.get("error")
                err_suffix = f" error={err!r}" if err else ""
                print(f"[hydradb] result {i}: ref={ref} status={status}{err_suffix}")

        # ----- Batch totals (uses shared helper) --------------------------
        ok, bad = summarize_upload_response(payload, batch_size=len(files))
        source = (
            "server-reported"
            if ("success_count" in payload or "failed_count" in payload)
            else "derived"
        )
        print(f"[hydradb] batch summary ({source}): {ok} ok, {bad} failed")

        return payload

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    @retry(
        service="hydradb",
        max_attempts=3,
        initial_delay=1.0,
        retryable_exceptions=(requests.Timeout, requests.ConnectionError, OSError,
                               UpstreamTimeoutError, HydraDBError),
    )
    def full_recall(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        """
        Call POST /recall/full_recall and return the parsed JSON response.

        Raises:
            HydraDBError          on non-2xx, empty query, or bad JSON.
            UpstreamTimeoutError  when the request times out.

        Operators can still see the full HydraDB error body in stdout
        via the log line we print here; the exception only carries a
        friendly summary back to the caller.
        """
        if not query or not query.strip():
            raise HydraDBError(
                detail="Query was empty.",
                log_context="full_recall called with empty query",
            )

        url = f"{self.base_url}/recall/full_recall"
        payload = {
            "tenant_id": self.tenant_id,
            "sub_tenant_id": self.sub_tenant_id,
            "query": query,
            RECALL_TOP_K_FIELD: top_k,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
        except requests.Timeout as e:
            raise UpstreamTimeoutError(
                log_context=f"HydraDB full_recall timed out: {e}",
            )
        except requests.RequestException as e:
            raise HydraDBError(
                log_context=f"network error during full_recall: {e}",
            )
        # RetryExhausted bubbles up as HydraDBError so callers see a stable type.

        print(f"[hydradb] POST {url} -> HTTP {response.status_code}")

        if response.status_code >= 400:
            # Print operator-facing detail to stdout but don't echo it to
            # the user — that body can include long stack traces.
            print(f"[hydradb] Response body: {response.text}")
            print(
                f"[hydradb] HINT: HydraDB rejected the request. If the error "
                f"mentions '{RECALL_TOP_K_FIELD}' or an unknown field, change "
                f"RECALL_TOP_K_FIELD at the top of hydradb_client.py "
                f"(e.g. to 'max_results')."
            )
            raise HydraDBError(
                detail=f"Knowledge backend returned HTTP {response.status_code}.",
                log_context=f"full_recall HTTP {response.status_code} body={response.text[:400]}",
                upstream_status=response.status_code,
            )

        try:
            return response.json()
        except ValueError:
            raise HydraDBError(
                log_context=f"non-JSON recall response: {response.text[:500]}",
            )