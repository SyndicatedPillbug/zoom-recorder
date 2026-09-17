#!/usr/bin/env python3
"""Rate/token governor for the answer engine.

Groq's free tier is limited by tokens-per-minute and tokens-per-day, not by
context window. This governor (a) prefers the provider's own
``x-ratelimit-*`` headers when present, (b) keeps a local rolling estimate as a
fallback, (c) honors ``Retry-After`` on HTTP 429, and (d) tells the answer
engine when to stop spending on low-value rolling refreshes.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date
from typing import Any, Deque, Dict, Optional, Tuple

# Defaults are 0 == "don't impose a local limit; trust the provider's own
# x-ratelimit-* headers". Set tpm/tpd in config.json only if you want to cap
# below whatever the provider enforces.
DEFAULT_TPM = 0
DEFAULT_TPD = 0

MINUTE = 60.0


class BudgetGovernor:
    def __init__(self, tpm: int = DEFAULT_TPM, tpd: int = DEFAULT_TPD) -> None:
        self._lock = threading.Lock()
        self.tpm = tpm
        self.tpd = tpd
        self._minute_window: Deque[Tuple[float, int]] = deque()
        self._day = date.today()
        self._day_tokens = 0
        self._day_requests = 0
        self._header_tpm_remaining: Optional[int] = None
        self._header_rpd_remaining: Optional[int] = None
        self._blocked_until = 0.0
        self._last_error = ""
        self._pending_reserve = 0
        self.total_calls = 0
        self.total_tokens = 0

    # -- internal ----------------------------------------------------------
    def _roll(self, now: float) -> None:
        cutoff = now - MINUTE
        while self._minute_window and self._minute_window[0][0] < cutoff:
            self._minute_window.popleft()
        today = date.today()
        if today != self._day:
            self._day = today
            self._day_tokens = 0
            self._day_requests = 0

    def _minute_tokens(self) -> int:
        return sum(t for _, t in self._minute_window)

    # -- policy ------------------------------------------------------------
    def blocked_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._blocked_until - time.time())

    def daily_budget_low(self, threshold: float = 0.15) -> bool:
        """True when the day's token budget is nearly exhausted.

        With no configured daily cap (tpd == 0) this only trips if the provider
        itself reports the request budget as nearly gone.
        """
        with self._lock:
            self._roll(time.time())
            if self._header_rpd_remaining is not None and self._header_rpd_remaining <= 3:
                return True
            if self.tpd and self._day_tokens >= self.tpd * (1.0 - threshold):
                return True
            return False

    def can_afford(self, est_tokens: int) -> bool:
        with self._lock:
            now = time.time()
            self._roll(now)
            if now < self._blocked_until:
                return False
            if self._header_tpm_remaining is not None:
                if self._header_tpm_remaining <= est_tokens:
                    return False
            elif self.tpm and (self._minute_tokens() + est_tokens) > self.tpm:
                return False
            if self.tpd and (self._day_tokens + est_tokens) > self.tpd:
                return False
            return True

    # -- accounting --------------------------------------------------------
    def reserve(self, est_tokens: int) -> None:
        """Optimistically count an in-flight call against the local window."""
        with self._lock:
            now = time.time()
            self._roll(now)
            amount = max(0, est_tokens)
            self._minute_window.append((now, amount))
            self._day_tokens += amount
            self._pending_reserve = amount

    def record(self, headers: Optional[Dict[str, str]],
               usage: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            now = time.time()
            self._roll(now)
            self.total_calls += 1
            tokens = 0
            if usage:
                tokens = int(usage.get("total_tokens") or 0)
                if not tokens:
                    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            self.total_tokens += tokens
            self._day_requests += 1
            # True the optimistic reservation up to the actual usage so the
            # daily cap doesn't drift on repeated estimate/actual differences.
            if tokens and self._pending_reserve:
                delta = tokens - self._pending_reserve
                self._day_tokens = max(0, self._day_tokens + delta)
                if self._minute_window:
                    ts, amount = self._minute_window[-1]
                    self._minute_window[-1] = (ts, max(0, amount + delta))
            self._pending_reserve = 0
            if headers:
                rl_tokens = headers.get("x-ratelimit-remaining-tokens")
                if rl_tokens is not None and str(rl_tokens).strip().isdigit():
                    self._header_tpm_remaining = int(rl_tokens)
                rl_req = headers.get("x-ratelimit-remaining-requests")
                if rl_req is not None and str(rl_req).strip().isdigit():
                    self._header_rpd_remaining = int(rl_req)
            self._last_error = ""

    def pause(self, retry_after: Optional[float] = None, reason: str = "") -> None:
        with self._lock:
            delay = retry_after if (retry_after and retry_after > 0) else 20.0
            self._blocked_until = max(self._blocked_until, time.time() + min(delay, 120.0))
            self._last_error = reason or self._last_error

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            self._roll(now)
            return {
                "tpm_limit": self.tpm,
                "tpm_used": self._minute_tokens(),
                "tpd_limit": self.tpd,
                "day_tokens": self._day_tokens,
                "day_requests": self._day_requests,
                "header_tpm_remaining": self._header_tpm_remaining,
                "header_rpd_remaining": self._header_rpd_remaining,
                "blocked_seconds": max(0.0, self._blocked_until - now),
                "total_calls": self.total_calls,
                "total_tokens": self.total_tokens,
                "last_error": self._last_error,
            }
