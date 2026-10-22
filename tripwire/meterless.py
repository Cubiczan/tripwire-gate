"""Meterless Runtime (Nightshift) provider: the primary AI provider for Tripwire.

Contract (from the Meterless Power Prompt / setup guide):
  * OpenAI-compatible: GET /v1/models for discovery, POST /v1/chat/completions
  * Authorization: Bearer <METERLESS_RUNTIME_TOKEN>  (env only, never hardcoded)
  * meterless-app: tripwire on every request; meterless-scope: user on chat
  * model ids are routing modes (meterless/auto, meterless/fast, ...), not model ids
  * 401/403 on /v1/models = "unauthorized"; 200 with meterless ids = "online"
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

APP_ID = "tripwire"


def normalise_base(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "http://" + url
    url = url.rstrip("/")
    for suffix in ("/v1/chat/completions", "/v1/models", "/healthz", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url + "/v1"


class MeterlessError(RuntimeError):
    def __init__(self, msg: str, state: str = "offline") -> None:
        super().__init__(msg)
        self.state = state


@dataclass
class Receipt:
    """What Nightshift says actually served a request (secrets never included)."""
    requested_model: str
    routed_model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    meterless: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_nemotron(self) -> bool:
        return "nemotron" in self.routed_model.lower()

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["is_nemotron"] = self.is_nemotron
        return d


def _find_routed(obj: Any) -> str | None:
    """Look for an explicit routed/served model in a Meterless response block."""
    if isinstance(obj, dict):
        for k in ("routed_model", "served_model", "winning_model", "resolved_model", "upstream_model"):
            if isinstance(obj.get(k), str):
                return obj[k]
        for v in obj.values():
            r = _find_routed(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_routed(v)
            if r:
                return r
    return None


class MeterlessClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 model: str | None = None, timeout: float = 180.0) -> None:
        self.base_url = normalise_base(base_url or os.environ.get("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1"))
        self._token = token if token is not None else os.environ.get("METERLESS_RUNTIME_TOKEN", "")
        self.model = model or os.environ.get("METERLESS_MODEL", "meterless/auto")
        self.timeout = timeout
        if self._token in ("", "rt-local"):
            raise MeterlessError("METERLESS_RUNTIME_TOKEN is not set (copy it from Nightshift's Console)", "unauthorized")

    def _req(self, path: str, body: dict | None = None, chat: bool = False):
        headers = {"Authorization": "Bearer " + self._token, "meterless-app": APP_ID}
        if chat:
            headers["meterless-scope"] = "user"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}"), dict(r.headers.items())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:400]
            state = "unauthorized" if e.code in (401, 403) else "offline"
            raise MeterlessError(f"Meterless HTTP {e.code}: {msg}", state) from None
        except urllib.error.URLError as e:
            raise MeterlessError(f"Meterless runtime not reachable at {self.base_url}: {e.reason}") from None

    def models(self) -> list[str]:
        data, _ = self._req("/models")
        return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]

    def status(self) -> str:
        try:
            ids = self.models()
        except MeterlessError as e:
            return e.state
        return "online" if any(i.startswith("meterless/") for i in ids) else "offline"

    def chat(self, messages: list[dict[str, str]], model: str | None = None,
             max_tokens: int | None = None, temperature: float = 0.2) -> tuple[str, Receipt, dict]:
        model = model or self.model
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        t0 = time.time()
        data, headers = self._req("/chat/completions", body, chat=True)
        latency = int((time.time() - t0) * 1000)
        try:
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        except (KeyError, IndexError, TypeError):
            raise MeterlessError(f"unexpected chat response: {json.dumps(data)[:300]}") from None
        meta = data.get("meterless") if isinstance(data.get("meterless"), dict) else {}
        routed = (_find_routed(meta) or _find_routed(data)
                  or headers.get("meterless-model") or headers.get("x-meterless-model") or data.get("model", ""))
        keep = {k: v for k, v in headers.items() if k.lower().startswith(("meterless", "x-meterless"))}
        receipt = Receipt(model, str(routed), latency, data.get("usage") or {}, meta, keep)
        return text, receipt, data
