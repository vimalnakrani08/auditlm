"""Teacher model (Claude Opus 4.8) for demonstration distillation. Uses the proven
no-temperature request body (temperature is deprecated for opus-4-8), surfaces the API
error body, and retries transient failures. Key from ANTHROPIC_API_KEY (never logged).
A --mock teacher (no API) is available for validating the pipeline offline.
"""

from __future__ import annotations

import os
import time

import httpx

URL = "https://api.anthropic.com/v1/messages"
OPUS = "claude-opus-4-8"
TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.PoolTimeout)


def opus(system: str, user: str, max_tokens: int = 1024, retries: int = 4, backoff: float = 3.0) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — required for Opus distillation")
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = httpx.post(URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                         "content-type": "application/json"},
                           json={"model": OPUS, "max_tokens": max_tokens, "system": system,
                                 "messages": [{"role": "user", "content": user}]},
                           timeout=120.0)
        except TRANSIENT as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic API {r.status_code} for {OPUS}: {r.text[:800]}")
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    raise last  # type: ignore[misc]


def mock_opus(system: str, user: str, max_tokens: int = 1024, **kw) -> str:
    """Offline stand-in to validate the pipeline flow (no API)."""
    if "write one realistic" in system.lower() or "exam question" in system.lower():
        return "What are the key requirements of the relevant standard for this engagement?"
    return ("Under the governing standard, the auditor should perform the required procedures. "
            "Per AS 1215, audit documentation must be retained for seven years. Where the retrieved "
            "passages do not specify the detail, the requirement from professional knowledge is applied. "
            "(mock answer for offline pipeline validation.)")
