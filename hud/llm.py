#!/usr/bin/env python3
"""Minimal OpenAI-compatible client used by the HUD.

Groq, OpenRouter, OpenAI and Ollama all speak the same
``/chat/completions`` + ``/audio/transcriptions`` schema, so a single client
covers every provider. Deliberately uses only the standard library (``urllib``)
to match the rest of this repo's dependency-light style.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body


@dataclass
class LLMResult:
    text: str
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)


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

    def _request(self, path: str, data: bytes, content_type: str,
                 timeout: Optional[float] = None):
        url = "{}{}".format(self.base_url, path)
        req = urllib.request.Request(
            url, data=data, headers=self._headers(content_type), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")), _lower_headers(resp.headers)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            retry_after = None
            ra = exc.headers.get("Retry-After") if exc.headers else None
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = None
            raise LLMError(
                "{} {} -> HTTP {}".format(req.get_method(), url, exc.code),
                status=exc.code, retry_after=retry_after, body=body) from exc
        except urllib.error.URLError as exc:
            raise LLMError("network error for {}: {}".format(url, exc.reason)) from exc

    # -- models ------------------------------------------------------------
    def _get_json(self, path: str, timeout: Optional[float] = None) -> Any:
        url = "{}{}".format(self.base_url, path)
        req = urllib.request.Request(
            url, headers=self._headers("application/json"), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise LLMError("GET {} -> HTTP {}".format(url, exc.code),
                           status=exc.code, body=body) from exc
        except urllib.error.URLError as exc:
            raise LLMError("network error for {}: {}".format(url, exc.reason)) from exc

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

    # -- speech-to-text ----------------------------------------------------
    def transcribe(self, wav_bytes: bytes, model: str, filename: str = "chunk.wav",
                   language: Optional[str] = None, prompt: Optional[str] = None,
                   timeout: Optional[float] = None) -> LLMResult:
        fields: Dict[str, str] = {"model": model, "response_format": "json"}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt
        body, content_type = _encode_multipart(
            fields,
            [("file", filename, "audio/wav", wav_bytes)],
        )
        obj, headers = self._request("/audio/transcriptions", body, content_type, timeout)
        text = (obj.get("text") or "").strip() if isinstance(obj, dict) else ""
        return LLMResult(text=text, model=model, headers=headers)


def backoff_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Honor Retry-After when the server sends it, else exponential backoff."""
    if retry_after and retry_after > 0:
        return min(retry_after, 60.0)
    return min(2.0 ** attempt, 30.0)


def now() -> float:
    return time.time()
