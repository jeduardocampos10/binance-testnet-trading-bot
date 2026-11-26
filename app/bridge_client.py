"""Server-side, read-only client for the private Binance Testnet Bridge."""

from __future__ import annotations

import hmac
import json
import socket
import ssl
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, OpenerDirector, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 128 * 1024
DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_CACHE_TTL_SECONDS = 15
DEFAULT_MAX_STALE_SECONDS = 300
_ALLOWED_INTERNAL_HOSTS = {"binance-testnet-bridge", "127.0.0.1", "localhost", "::1"}


class BridgeUnavailable(Exception):
    """A safe, non-sensitive Bridge availability failure."""


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise BridgeUnavailable(f"Bridge redirect rejected (status {code})")


def read_bridge_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bridge token must be a regular non-symlink file")
    if path.stat().st_size > 4096:
        raise ValueError("Bridge token file is too large")
    token = path.read_text(encoding="utf-8").rstrip("\r\n")
    if len(token) < 32 or "\x00" in token:
        raise ValueError("Bridge token must contain at least 32 characters")
    return token


def validate_bridge_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _ALLOWED_INTERNAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Bridge URL must use the approved private service or loopback host")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Bridge URL has an invalid port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Bridge URL has an invalid port")
    if parsed.scheme == "https" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("loopback Bridge tests must use HTTP")
    return value.rstrip("/")


def _direct_opener() -> OpenerDirector:
    context = ssl.create_default_context()
    return build_opener(ProxyHandler({}), HTTPHandler(), HTTPSHandler(context=context), RejectRedirects())


class BridgeBalanceClient:
    """Fetch one fixed Bridge capability with bounded in-memory caching."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        opener: OpenerDirector | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self._base_url = validate_bridge_base_url(base_url)
        if len(token) < 32:
            raise ValueError("Bridge token must contain at least 32 characters")
        if not 1 <= timeout_seconds <= 15:
            raise ValueError("Bridge timeout must be between 1 and 15 seconds")
        if cache_ttl_seconds < 0 or max_stale_seconds < cache_ttl_seconds:
            raise ValueError("invalid Bridge cache policy")
        self._token = token
        self._opener = opener if opener is not None else _direct_opener()
        self._clock = clock if clock is not None else time.time
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds
        self._max_stale = max_stale_seconds
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at: float | None = None

    def _cached_result(self, now: float, *, allow_stale: bool) -> dict[str, Any] | None:
        if self._cached is None or self._cached_at is None:
            return None
        age = max(0.0, now - self._cached_at)
        limit = self._max_stale if allow_stale else self._cache_ttl
        if age > limit:
            return None
        result = dict(self._cached)
        result["status"] = "stale" if age > self._cache_ttl else "fresh"
        result["stale"] = age > self._cache_ttl
        result["age_ms"] = int(age * 1000)
        return result

    def balance(self) -> dict[str, Any]:
        now = self._clock()
        cached = self._cached_result(now, allow_stale=False)
        if cached is not None:
            return cached
        if not self._lock.acquire(blocking=False):
            cached = self._cached_result(now, allow_stale=True)
            if cached is not None:
                return cached
            raise BridgeUnavailable("Bridge balance request is already in progress")
        try:
            now = self._clock()
            cached = self._cached_result(now, allow_stale=False)
            if cached is not None:
                return cached
            try:
                normalized = self._fetch()
            except BridgeUnavailable:
                stale = self._cached_result(self._clock(), allow_stale=True)
                if stale is not None:
                    stale["status"] = "stale"
                    stale["stale"] = True
                    return stale
                raise
            fetched_at_ms = int(self._clock() * 1000)
            result = {
                "schema_version": "dashboard.testnet-balance.v1",
                "source": "binance-testnet-bridge",
                "environment": "binance-spot-testnet",
                "observed_at_ms": normalized.get("observed_at_ms") or fetched_at_ms,
                "fetched_at_ms": fetched_at_ms,
                "status": "fresh",
                "stale": False,
                "age_ms": 0,
                "read_only": True,
                "orders_enabled": False,
                "balances": normalized["balances"],
            }
            self._cached = result
            self._cached_at = self._clock()
            return dict(result)
        finally:
            self._lock.release()

    def _fetch(self) -> dict[str, Any]:
        expected_url = f"{self._base_url}/balance"
        request = Request(
            expected_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "cashmaxx-paper-dashboard/1.0",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise BridgeUnavailable(f"Bridge returned HTTP {response.status}")
                if response.geturl() != expected_url:
                    raise BridgeUnavailable("Bridge response URL was unexpected")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except BridgeUnavailable:
            raise
        except HTTPError as exc:
            if exc.code == 429:
                raise BridgeUnavailable("Bridge balance request was rate limited") from None
            raise BridgeUnavailable(f"Bridge returned HTTP {exc.code}") from None
        except (URLError, socket.timeout, TimeoutError):
            raise BridgeUnavailable("Bridge is temporarily unavailable") from None
        except OSError:
            raise BridgeUnavailable("Bridge connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BridgeUnavailable("Bridge response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeUnavailable("Bridge returned invalid JSON") from None
        return self._validate_payload(payload)

    @staticmethod
    def _validate_payload(payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("environment") != "binance-spot-testnet":
            raise BridgeUnavailable("Bridge response had an unexpected contract")
        balances = payload.get("balances")
        if not isinstance(balances, list) or len(balances) > 1000:
            raise BridgeUnavailable("Bridge response had an unexpected contract")
        normalized: list[dict[str, str]] = []
        for item in balances:
            if not isinstance(item, dict) or set(item) != {"asset", "free", "locked"}:
                raise BridgeUnavailable("Bridge response had an unexpected balance")
            asset, free, locked = item.get("asset"), item.get("free"), item.get("locked")
            if (
                not isinstance(asset, str)
                or not asset
                or len(asset) > 32
                or not asset.replace("-", "").isalnum()
                or not isinstance(free, str)
                or not isinstance(locked, str)
            ):
                raise BridgeUnavailable("Bridge response had an unexpected balance")
            try:
                free_value, locked_value = Decimal(free), Decimal(locked)
            except InvalidOperation:
                raise BridgeUnavailable("Bridge response had an unexpected balance") from None
            if any(not value.is_finite() or value < 0 for value in (free_value, locked_value)):
                raise BridgeUnavailable("Bridge response had an unexpected balance")
            normalized.append({"asset": asset, "free": free, "locked": locked})
        observed = payload.get("observed_at_ms")
        if observed is not None and (not isinstance(observed, int) or observed < 0):
            raise BridgeUnavailable("Bridge response had an unexpected contract")
        return {"balances": normalized, "observed_at_ms": observed}

    def token_matches(self, candidate: str) -> bool:
        """Test-only-safe comparison helper without exposing the configured token."""
        return hmac.compare_digest(self._token, candidate)
