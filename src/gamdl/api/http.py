"""HTTP transport and coordinated request pacing for API services.

Media/CDN transfers should use a separate transport and concurrency policy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

import httpx

from ..logging import get_logger
from .exceptions import (
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
)

logger = get_logger(__name__)


class RequestPacer:
    """Serialize protected API operations and delay between completions.

    Pass the *same* instance to multiple API clients to coordinate them.
    This is deliberately not used for parallel CDN/media downloads.
    """

    def __init__(self, interval: float = 0.0) -> None:
        if interval < 0:
            raise ValueError("Request interval must be nonnegative")
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def __aenter__(self) -> RequestPacer:
        await self._lock.acquire()
        try:
            delay = self._next_start - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            return self
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        self._next_start = asyncio.get_running_loop().time() + self.interval
        self._lock.release()


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class ApiHttpClient:
    """Owns an HTTPX client unless one was injected by the caller.

    GETs are retried for common transient statuses and transport problems.
    POSTs (especially DRM licenses and login) are never retried implicitly.
    """

    def __init__(
        self,
        service: str,
        *,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
        retries: int = 2,
        timeout: float = 30.0,
    ) -> None:
        if retries < 0:
            raise ValueError("Retry count must be nonnegative")
        self.service = service
        self._owns_client = client is None
        self.client = (
            client if client is not None else httpx.AsyncClient(timeout=timeout)
        )
        self.pacer = pacer
        self.retries = retries

    async def __aenter__(self) -> ApiHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        method = method.upper()
        max_attempts = self.retries + 1 if method in {"GET", "HEAD"} else 1

        for attempt in range(1, max_attempts + 1):
            start = monotonic()
            try:
                if self.pacer is None:
                    response = await self.client.request(method, url, **kwargs)
                else:
                    async with self.pacer:
                        response = await self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                logger.warning(
                    "request_transport_error",
                    service=self.service,
                    operation=operation,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt == max_attempts:
                    raise GamdlTransportError(
                        f"{self.service}: network request failed",
                        service=self.service,
                    ) from exc
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                continue

            status = response.status_code
            logger.debug(
                "request_completed",
                service=self.service,
                operation=operation,
                method=method,
                status_code=status,
                attempt=attempt,
                elapsed_ms=round((monotonic() - start) * 1000),
            )

            retry_after = _retry_after(response.headers.get("Retry-After"))
            if status in {429, 500, 502, 503, 504} and attempt < max_attempts:
                delay = (
                    retry_after
                    if retry_after is not None
                    else min(2 ** (attempt - 1), 8)
                )
                logger.warning(
                    "request_retry",
                    service=self.service,
                    operation=operation,
                    status_code=status,
                    attempt=attempt,
                    wait_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue

            if status == 429:
                raise GamdlRateLimitError(service=self.service, retry_after=retry_after)
            if status in {401, 403}:
                raise GamdlAuthenticationError(
                    f"{self.service}: authentication rejected (HTTP {status})",
                    service=self.service,
                    status_code=status,
                )
            if status >= 400:
                raise GamdlApiResponseError(
                    f"{self.service}: request failed (HTTP {status})",
                    service=self.service,
                    status_code=status,
                )
            return response

        raise AssertionError("Unreachable request retry state")

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = await self.request(method, url, operation=operation, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise GamdlApiResponseError(
                f"{self.service}: invalid JSON response",
                service=self.service,
                status_code=response.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise GamdlApiResponseError(
                f"{self.service}: expected a JSON object",
                service=self.service,
                status_code=response.status_code,
            )
        return data

    async def get_text(self, url: str, *, operation: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, operation=operation, **kwargs)
        return response.text
