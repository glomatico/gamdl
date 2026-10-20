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
    """Hold Apple Music credentials in memory without exposing them in repr.

    These fields are not persisted or automatically refreshed. Avoid logging
    the attributes individually, even at debug level.
    """

    developer_token: str
    media_user_token: str | None = None

    def __repr__(self) -> str:
        return "AppleMusicCredentials(<redacted>)"


def media_user_token_from_cookies(path: str) -> str:
    """Read a music-user token from an exported Netscape-format cookies file.

    The file must contain a ``media-user-token`` cookie for
    ``music.apple.com``. Cookie expiry is not validated at load time;
    authentication is checked when the API requests account metadata.

    Raises:
        GamdlAuthenticationError: Cookie file cannot be loaded or the token
            cannot be found.
    """
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
    """Extract both Apple Music tokens from a wrapper ``/me`` response.

    Raises:
        GamdlAuthenticationError: Wrapper authentication data is incomplete.
    """
    auth = account.get("auth", {})
    if not isinstance(auth, dict):
        raise GamdlAuthenticationError("Wrapper did not provide valid authentication")
    developer_token = auth.get("dev_token")
    media_user_token = auth.get("music_user_token")
    if not developer_token or not media_user_token:
        raise GamdlAuthenticationError("Wrapper authentication tokens are missing")
    return AppleMusicCredentials(developer_token, media_user_token)


async def fetch_developer_token(http: ApiHttpClient) -> str:
    """Discover an Apple Music web-player developer token (best-effort).

    Fetches the public homepage, locates the bundled player JavaScript, and
    extracts a JWT-shaped token. This depends on Apple's current web-player
    layout and may require updates when their frontend changes.

    Args:
        http: Shared HTTP client used for both discovery requests.

    Returns:
        Discovered developer-token string; no session is saved to disk.

    Raises:
        GamdlApiResponseError: Script URL or token cannot be located.
    """
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
    """Run a host-supplied credential/2FA callback and await it if necessary.

    This helper is UI-agnostic: the callback may use a CLI prompt, a bot, or
    an external authentication interface. It does not create prompts itself.
    """
    result = callback()
    if inspect.isawaitable(result):
        return await result
    return result
