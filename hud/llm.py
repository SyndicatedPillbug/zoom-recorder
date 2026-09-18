#!/usr/bin/env python3
"""Minimal OpenAI-compatible client used by the HUD.

Groq, OpenRouter, OpenAI and Ollama all speak the same
``/chat/completions`` + ``/audio/transcriptions`` schema, so a single client
covers every provider. Deliberately uses only the standard library.

Connections are reused per thread (``http.client`` keep-alive) so the many
small STT/answer calls during a call don't each pay a fresh TCP+TLS handshake.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit


class LLMError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body


# Global kill-switch for egress (`--offline` / privacy.offline). Loopback
# addresses stay allowed so a local Ollama server keeps working offline.
_OFFLINE = False
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def set_offline(value: bool) -> None:
    global _OFFLINE
    _OFFLINE = bool(value)


def is_offline() -> bool:
    return _OFFLINE


@dataclass
class LLMResult:
    text: str
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)


def _lower_headers(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if headers is None:
        return out
    for key in headers.keys():
        try:
            out[key.lower()] = headers.get(key, "")
        except Exception:  # noqa: BLE001
            continue
    return out


def _encode_multipart(fields: Dict[str, str],
                      files: List[Any]) -> "tuple[bytes, str]":
    """Build a multipart/form-data body without pulling in `requests`."""
    boundary = "----zoomrec{}".format(uuid.uuid4().hex)
    body = bytearray()
    for name, value in fields.items():
        body += ("--{}\r\n".format(boundary)).encode("utf-8")
        body += ('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)).encode("utf-8")
        body += str(value).encode("utf-8") + b"\r\n"
    for name, filename, content_type, data in files:
        body += ("--{}\r\n".format(boundary)).encode("utf-8")
        body += ('Content-Disposition: form-data; name="{}"; filename="{}"\r\n'.format(
            name, filename)).encode("utf-8")
        body += ("Content-Type: {}\r\n\r\n".format(content_type)).encode("utf-8")
        body += data + b"\r\n"
    body += ("--{}--\r\n".format(boundary)).encode("utf-8")
    return bytes(body), "multipart/form-data; boundary={}".format(boundary)


class LLMClient:
    def __init__(self, base_url: str, api_key: Optional[str],
                 timeout: float = 60.0, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        # OpenRouter likes these but they are harmless elsewhere.
        self.extra_headers.setdefault(
            "X-OpenRouter-Title", "zoom-recorder-hud")
        parts = urlsplit(self.base_url)
        self._scheme = parts.scheme or "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._local = threading.local()

    # -- internals ---------------------------------------------------------
    def _headers(self, content_type: str) -> Dict[str, str]:
        headers = {
            "Content-Type": content_type,
            "Accept": "application/json",
            # Groq sits behind Cloudflare, which rejects the default
            # "Python-urllib/x.y" signature with HTTP 403 (error 1010).
            "User-Agent": "zoom-recorder-hud/0.1 (+https://github.com)",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer {}".format(self.api_key)
        headers.update(self.extra_headers)
        return headers

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        self._local.conn = conn
        return conn

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.conn = None

    def close(self) -> None:
        """Close this thread's pooled connection (best effort)."""
        self._drop_connection()

    def _raw_request(self, method: str, path: str, body: Optional[bytes],
                     headers: Dict[str, str],
                     timeout: Optional[float]) -> Tuple[int, bytes, Dict[str, str]]:
        """POST/GET over a reusable connection, retrying once on a dropped socket."""
        if _OFFLINE and self._host not in _LOCAL_HOSTS:
            raise LLMError(
                "offline mode: network access is disabled (blocked {})".format(
                    self.base_url))
        url = "{}{}".format(self._prefix, path)
        last_exc: Optional[BaseException] = None
        for attempt in range(2):
            conn = self._connection()
            try:
                if conn.sock is not None and timeout:
                    conn.sock.settimeout(timeout)
                conn.request(method, url, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                resp_headers = dict(resp.getheaders())
                if resp.will_close:
                    self._drop_connection()
                return status, raw, resp_headers
            except (http.client.HTTPException, OSError) as exc:
                last_exc = exc
                self._drop_connection()
                if attempt == 1:
                    break
        raise last_exc if last_exc is not None else OSError("request failed")

    def _decode(self, raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8"))

    def _request(self, path: str, data: bytes, content_type: str,
                 timeout: Optional[float] = None):
        headers = self._headers(content_type)
        url = "{}{}".format(self.base_url, path)
        try:
            status, raw, resp_headers = self._raw_request(
                "POST", path, data, headers, timeout or self.timeout)
        except (http.client.HTTPException, OSError) as exc:
            raise LLMError("network error for {}: {}".format(url, exc)) from exc
        if status >= 400:
            body = raw.decode("utf-8", errors="replace")
            retry_after = None
            ra = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = None
            raise LLMError(
                "POST {} -> HTTP {}".format(url, status),
                status=status, retry_after=retry_after, body=body)
        try:
            return self._decode(raw), _lower_headers(resp_headers)
        except ValueError as exc:
            raise LLMError("invalid JSON from {}".format(url)) from exc

    # -- models ------------------------------------------------------------
    def _get_json(self, path: str, timeout: Optional[float] = None) -> Any:
        headers = self._headers("application/json")
        url = "{}{}".format(self.base_url, path)
        try:
            status, raw, _resp_headers = self._raw_request(
                "GET", path, None, headers, timeout or self.timeout)
        except (http.client.HTTPException, OSError) as exc:
            raise LLMError("network error for {}: {}".format(url, exc)) from exc
        if status >= 400:
            raise LLMError("GET {} -> HTTP {}".format(url, status),
                           status=status, body=raw.decode("utf-8", errors="replace"))
        try:
            return self._decode(raw)
        except ValueError as exc:
            raise LLMError("invalid JSON from {}".format(url)) from exc

    def models(self, timeout: Optional[float] = None) -> List[str]:
        """Return the provider's model ids (used by the settings GUI)."""
        obj = self._get_json("/models", timeout)
        data = obj.get("data") if isinstance(obj, dict) else obj
        ids: List[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
        return ids

    # -- chat --------------------------------------------------------------
    def chat(self, messages: List[Dict[str, str]], model: str,
             max_tokens: int = 400, temperature: float = 0.2,
             response_format: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> LLMResult:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        data = json.dumps(payload).encode("utf-8")
        obj, headers = self._request("/chat/completions", data, "application/json", timeout)
        choices = obj.get("choices") or []
        text = ""
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
        return LLMResult(text=text, model=obj.get("model", model),
                         usage=obj.get("usage") or {}, headers=headers)

    # -- embeddings --------------------------------------------------------
    def embed(self, inputs: List[str], model: str,
              timeout: Optional[float] = None) -> List[List[float]]:
        """Call an OpenAI-compatible ``/embeddings`` endpoint."""
        payload = {"model": model, "input": list(inputs)}
        data = json.dumps(payload).encode("utf-8")
        obj, _headers = self._request("/embeddings", data, "application/json", timeout)
        rows = obj.get("data") if isinstance(obj, dict) else obj
        vectors: List[Optional[List[float]]] = [None] * len(inputs)
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index", 0))
                except (TypeError, ValueError):
                    index = 0
                embedding = item.get("embedding")
                if 0 <= index < len(vectors) and isinstance(embedding, list):
                    vectors[index] = [float(x) for x in embedding]
        if any(v is None for v in vectors):
            raise LLMError("embeddings response incomplete (model={})".format(model))
        return [v for v in vectors if v is not None]

    # -- speech-to-text ----------------------------------------------------
    def transcribe(self, wav_bytes: bytes, model: str, filename: str = "chunk.wav",
                   language: Optional[str] = None, prompt: Optional[str] = None,
                   response_format: str = "json",
                   timeout: Optional[float] = None) -> LLMResult:
        fields: Dict[str, str] = {"model": model, "response_format": response_format}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt
        body, content_type = _encode_multipart(
            fields,
            [("file", filename, "audio/wav", wav_bytes)],
        )
        obj, headers = self._request("/audio/transcriptions", body, content_type, timeout)
        data = obj if isinstance(obj, dict) else {}
        text = (data.get("text") or "").strip()
        return LLMResult(text=text, model=model, headers=headers, data=data)


def backoff_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Honor Retry-After when the server sends it, else exponential backoff."""
    if retry_after and retry_after > 0:
        return min(retry_after, 60.0)
    return min(2.0 ** attempt, 30.0)


def now() -> float:
    return time.time()
