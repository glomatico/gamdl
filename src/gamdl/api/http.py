"""HTTP transport and coordinated request pacing for API services.

Media/CDN transfers should use a separate transport and concurrency policy.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import httpx
from httpx_retries import Retry

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
        self.retry = Retry(
            total=retries,
            allowed_methods={"GET", "HEAD"},
            status_forcelist={429, 500, 502, 503, 504},
            backoff_factor=0.5,
            backoff_jitter=0,
            max_backoff_wait=60,
        )

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
        retry = self.retry
        attempt = 0

        while True:
            attempt += 1
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
                if (
                    retry.is_retryable_method(method)
                    and retry.is_retryable_exception(exc)
                    and not retry.is_exhausted()
                ):
                    retry = retry.increment()
                    await retry.asleep(exc)
                    continue
                raise GamdlTransportError(
                    f"{self.service}: network request failed",
                    service=self.service,
                ) from exc

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

            if (
                retry.is_retryable_method(method)
                and retry.is_retryable_status_code(status)
                and not retry.is_exhausted()
            ):
                logger.warning(
                    "request_retry",
                    service=self.service,
                    operation=operation,
                    status_code=status,
                    attempt=attempt,
                )
                retry = retry.increment()
                await response.aclose()
                await retry.asleep(response)
                continue

            if status == 429:
                retry_after = None
                if header := response.headers.get("Retry-After"):
                    try:
                        retry_after = retry.parse_retry_after(header)
                    except ValueError:
                        pass
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
