"""Test-only Apple Music authentication selection for live integration tests.

This module is not part of the published ``gamdl`` library. Environment
variables override values from the locally ignored ``.env`` file.
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv

from gamdl.api import AppleMusicApi, RequestPacer, WrapperApi
from gamdl.exceptions import GamdlError

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, repr=False)
class LiveSettings:
    """Local test configuration; repr intentionally excludes credentials."""

    wrapper_url: str = "http://127.0.0.1"
    storefront: str = "br"
    developer_token: str | None = None
    media_user_token: str | None = None
    cookies_path: str | None = None

    def __repr__(self) -> str:
        return "LiveSettings(<redacted>)"

    @classmethod
    def from_env(cls) -> LiveSettings:
        """Load the local .env only for explicitly requested live tests."""
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            wrapper_url=os.getenv("GAMDL_WRAPPER_URL", "http://127.0.0.1"),
            storefront=os.getenv("GAMDL_APPLE_STOREFRONT", "br"),
            developer_token=os.getenv("GAMDL_APPLE_DEVELOPER_TOKEN") or None,
            media_user_token=os.getenv("GAMDL_APPLE_MEDIA_USER_TOKEN") or None,
            cookies_path=os.getenv("GAMDL_APPLE_COOKIES_PATH") or None,
        )


@dataclass(repr=False)
class LiveSession:
    """Authenticated API and selected source, excluding secrets from repr."""

    source: str
    api: AppleMusicApi
    wrapper: WrapperApi | None = None

    def __repr__(self) -> str:
        return (
            f"LiveSession(source={self.source!r}, api=<redacted>, "
            f"wrapper={'available' if self.wrapper is not None else 'unavailable'})"
        )


@asynccontextmanager
async def open_live_session(
    settings: LiveSettings,
    *,
    interval: float = 0.25,
) -> AsyncIterator[LiveSession]:
    """Select the first working auth source: wrapper, token, then cookies.

    A reachable but unauthenticated wrapper is not accepted. Failed choices
    are closed before trying the next source. All HTTP resources are closed at
    context exit, and neither tokens nor wrapper `/me` responses are logged.
    """
    pacer = RequestPacer(interval=interval)
    async with AsyncExitStack() as stack:
        if settings.wrapper_url:
            async with AsyncExitStack() as wrapper_stack:
                try:
                    # Keep HTTP available for the entire session, including
                    # playback requests after the initial /me call.
                    wrapper_http = await wrapper_stack.enter_async_context(
                        httpx.AsyncClient(timeout=3.0)
                    )
                    wrapper = await WrapperApi.create(
                        base_url=settings.wrapper_url,
                        client=wrapper_http,
                        pacer=pacer,
                    )
                    if wrapper.me.get("auth", {}).get("state") not in {
                        "authenticated",
                        "logged_in",
                    }:
                        raise ValueError("Wrapper has no authenticated session")
                    api = await AppleMusicApi.from_wrapper(
                        wrapper,
                        storefront=settings.storefront,
                        pacer=pacer,
                    )
                except (GamdlError, httpx.HTTPError, ValueError, OSError):
                    # Failed wrapper resources close before credential fallback.
                    pass
                else:
                    await wrapper_stack.enter_async_context(api)
                    yield LiveSession(source="wrapper", api=api, wrapper=wrapper)
                    return

        if settings.media_user_token:
            try:
                api = await AppleMusicApi.create(
                    storefront=settings.storefront,
                    token=settings.developer_token,
                    media_user_token=settings.media_user_token,
                    pacer=pacer,
                )
            except (GamdlError, httpx.HTTPError, OSError):
                pass
            else:
                await stack.enter_async_context(api)
                yield LiveSession(source="media_user_token", api=api)
                return

        if settings.cookies_path:
            path = Path(settings.cookies_path).expanduser()
            if not path.is_absolute():
                path = ROOT / path
            try:
                api = await AppleMusicApi.from_cookies(
                    str(path),
                    storefront=settings.storefront,
                    token=settings.developer_token,
                    pacer=pacer,
                )
            except (GamdlError, httpx.HTTPError, OSError):
                pass
            else:
                await stack.enter_async_context(api)
                yield LiveSession(source="cookies", api=api)
                return

        raise RuntimeError(
            "No usable Apple Music authentication source. Check the local "
            "wrapper or the fallback credentials configured in .env."
        )
