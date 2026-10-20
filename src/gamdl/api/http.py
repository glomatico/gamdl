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
    """Serialize protected API attempts with a delay between completions.

    Share one instance across API clients to coordinate calls to remote
    services. ``interval`` specifies the minimum delay, in seconds, after
    the end of one attempt before the next starts. A retry releases the lock
    while backing off, allowing other waiting requests to proceed.

    Do not use this pacer for concurrent CDN/media transfers.
    """

    def __init__(self, interval: float = 0.0) -> None:
        """Set the nonnegative delay between protected request attempts."""
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
    """Wrap HTTPX with request pacing, retry policy, and sanitized API errors.

    Automatically retry GET/HEAD for selected transient status codes and
    transport errors, using ``httpx-retries`` for backoff and ``Retry-After``.
    POSTs, including login and DRM license requests, are never retried.
    Optional pacing is applied to each network attempt, not to backoff time.

    An internally created HTTPX client is closed by :meth:`aclose` or the
    async context manager. An injected client remains caller-owned.
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
        """Configure the transport, retry policy, and optional shared pacer.

        Args:
            service: Safe service label for exceptions and diagnostics.
            client: Optional externally managed HTTPX async client. When
                supplied, its transport settings and lifetime are unchanged.
            pacer: Optional shared API request pacer.
            retries: Number of additional GET/HEAD attempts after the first.
            timeout: Default request timeout for an internally created client.

        Raises:
            ValueError: If ``retries`` is negative.
        """
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
        """Close the HTTPX client if this wrapper created it."""
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
        """Execute an HTTP request with safe error translation and retries.

        Args:
            method: HTTP method, such as ``"GET"`` or ``"POST"``.
            url: Target URL. Caller is responsible for validating which host
                can receive authentication headers.
            operation: Non-sensitive label used for structured diagnostics.
            **kwargs: HTTPX request arguments such as ``params``, ``headers``
                or ``json``. Their values are never deliberately logged.

        Returns:
            Successful HTTPX response, without decoding its body.

        Raises:
            GamdlTransportError: Network request failed after allowed retries.
            GamdlAuthenticationError: Service returned HTTP 401 or 403.
            GamdlRateLimitError: Service returned HTTP 429 after retries.
            GamdlApiResponseError: Service returned another HTTP error status.
        """
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
        """Execute a request and require a JSON object response.

        Returns:
            Decoded JSON dictionary (including any application-specific
            fields, whose validation belongs to the service client).

        Raises:
            GamdlApiResponseError: Body is not valid JSON or is not an object.
            GamdlApiError: HTTP or transport errors from :meth:`request`.
        """
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
        """Fetch and decode a text response using the standard request policy."""
        response = await self.request("GET", url, operation=operation, **kwargs)
        return response.text
