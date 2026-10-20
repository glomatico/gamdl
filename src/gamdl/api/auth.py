"""Credential sources and helpers, without credential persistence or UI prompts."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from typing import TypeVar
from urllib.parse import urljoin

from .constants import APPLE_MUSIC_URL
from .exceptions import GamdlApiResponseError, GamdlAuthenticationError
from .http import ApiHttpClient

_JS_URI = re.compile(r'/((?:assets/)?index[~-][^/"\s]+\.js)')
_TOKEN = re.compile(r'"(eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"')

T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class AppleMusicCredentials:
    """In-memory credentials. Their values are intentionally hidden from repr."""

    developer_token: str
    media_user_token: str | None = None

    def __repr__(self) -> str:
        return "AppleMusicCredentials(<redacted>)"


def media_user_token_from_cookies(path: str) -> str:
    """Load a current Apple Music token from a Netscape-format cookies file."""
    jar = MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as exc:
        raise GamdlAuthenticationError("Could not load Apple Music cookies") from exc

    for cookie in jar:
        if (
            cookie.name == "media-user-token"
            and cookie.domain.lstrip(".").lower() == "music.apple.com"
            and cookie.value
        ):
            return cookie.value
    raise GamdlAuthenticationError("Apple Music media-user-token cookie is missing")


def credentials_from_wrapper(account: dict) -> AppleMusicCredentials:
    auth = account.get("auth", {})
    if not isinstance(auth, dict):
        raise GamdlAuthenticationError("Wrapper did not provide valid authentication")
    developer_token = auth.get("dev_token")
    media_user_token = auth.get("music_user_token")
    if not developer_token or not media_user_token:
        raise GamdlAuthenticationError("Wrapper authentication tokens are missing")
    return AppleMusicCredentials(developer_token, media_user_token)


async def fetch_developer_token(http: ApiHttpClient) -> str:
    """Discover the current web-player developer token (best-effort)."""
    homepage = await http.get_text(
        APPLE_MUSIC_URL,
        operation="discover_developer_token_home",
        follow_redirects=True,
    )
    match = _JS_URI.search(homepage)
    if not match:
        raise GamdlApiResponseError("Apple Music web-player script was not found")

    script_url = urljoin(APPLE_MUSIC_URL + "/", match.group(1))
    script = await http.get_text(
        script_url, operation="discover_developer_token_script"
    )
    match = _TOKEN.search(script)
    if not match:
        raise GamdlApiResponseError("Apple Music developer token was not found")
    return match.group(1)


async def invoke_prompt(
    callback: Callable[[], T | Awaitable[T]],
) -> T:
    """Invoke a host-provided synchronous or asynchronous auth callback."""
    result = callback()
    if inspect.isawaitable(result):
        return await result
    return result
