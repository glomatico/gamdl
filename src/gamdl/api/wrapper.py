"""Standalone Wrapper-v2 1.x client: sessions, playback and Temari contexts.

The wrapper exports Apple Music tokens to the trusted local client but never
decrypts media samples.
Contexts are fetched per content key and consumed in Gamdl's native Rust
extension. Do not expose an unauthenticated Wrapper server to the network.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gamdl.transport import ApiHttpClient, RequestPacer

from .auth import invoke_prompt
from .exceptions import GamdlApiResponseError, GamdlAuthenticationError

WRAPPER_VERSION = ">=1.0.0,<2.0.0"
WRAPPER_PROTOCOL = 1
TEMARI_PROFILES = {
    "x86_64": "apple-music-1109-x86_64",
    "arm64-v8a": "apple-music-1109-arm64-v8a",
}


def _reported_value(value: object) -> str:
    """Display only short, harmless server metadata in compatibility errors."""
    if isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", value
    ):
        return value
    if type(value) is int and 0 <= value <= 9999:
        return str(value)
    return "missing or invalid"


CredentialsPrompt = Callable[[], tuple[str, str] | Awaitable[tuple[str, str]]]
TwoFactorPrompt = Callable[[], str | Awaitable[str]]


class WrapperApi:
    """A trusted local Wrapper-v2 HTTP session; no sample socket.

    An optional HTTPX client supplied to :meth:`create` stays caller-owned.
    Context requests are serialized per instance to respect the single native
    worker. The wrapper itself is never logged out when this client closes.
    """

    def __init__(
        self,
        http: ApiHttpClient,
        base_url: str,
        *,
        session: dict[str, Any],
        architecture: str | None = None,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.architecture = architecture
        self._native_lock = asyncio.Lock()

    async def __aenter__(self) -> WrapperApi:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    @staticmethod
    def validate_info(info: dict[str, Any]) -> None:
        if not isinstance(info, dict):
            raise GamdlApiResponseError("Invalid Wrapper v2 /info response")

        version = info.get("version")
        if not isinstance(version, str) or not re.fullmatch(
            r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
        ):
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 version: {_reported_value(version)}. "
                f"Gamdl requires Wrapper v2 {WRAPPER_VERSION}."
            )

        protocol = info.get("protocol_version")
        if type(protocol) is not int or protocol != WRAPPER_PROTOCOL:
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 protocol version: {_reported_value(protocol)}. "
                f"Gamdl requires protocol {WRAPPER_PROTOCOL}."
            )

        architecture = info.get("architecture")
        expected_profile = (
            TEMARI_PROFILES.get(architecture) if isinstance(architecture, str) else None
        )
        if expected_profile is None:
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 architecture: {_reported_value(architecture)}. "
                f"Supported architectures: {', '.join(TEMARI_PROFILES)}."
            )
        if info.get("profile") != expected_profile:
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 Temari profile: "
                f"{_reported_value(info.get('profile'))}. "
                f"Expected {expected_profile} for {architecture}."
            )
        if info.get("context_supported") is not True:
            raise GamdlApiResponseError("Wrapper v2 does not support Temari contexts")
        if info.get("context_format") != "temari":
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 context format: "
                f"{_reported_value(info.get('context_format'))}. Expected temari."
            )
        if info.get("decryption") != "client_side":
            raise GamdlApiResponseError(
                f"Unsupported Wrapper v2 decryption mode: "
                f"{_reported_value(info.get('decryption'))}. Expected client_side."
            )

    @classmethod
    async def create(
        cls,
        *,
        base_url: str = "http://127.0.0.1:8080",
        get_credentials: CredentialsPrompt | None = None,
        get_2fa_code: TwoFactorPrompt | None = None,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> WrapperApi:
        base_url = base_url.rstrip("/")
        http = ApiHttpClient("wrapper", client=client, pacer=pacer, timeout=620.0)
        try:
            info = await http.request_json(
                "GET", f"{base_url}/info", operation="wrapper_info"
            )
            cls.validate_info(info)
            session = await cls.get_session(http, base_url)
            if session.get("state") == "logged_out":
                try:
                    await http.request_json(
                        "POST",
                        f"{base_url}/session/restore",
                        operation="wrapper_restore",
                    )
                except GamdlApiResponseError as error:
                    if error.status_code != 404:
                        raise
                session = await cls.get_session(http, base_url)
            if session.get("state") != "authenticated" and get_credentials is not None:
                username, password = await invoke_prompt(get_credentials)
                await cls.login(http, base_url, username, password, get_2fa_code)
                session = await cls.get_session(http, base_url)
            if session.get("state") != "authenticated":
                raise GamdlAuthenticationError("Wrapper is not authenticated")
            return cls(
                http, base_url, session=session, architecture=info["architecture"]
            )
        except BaseException:
            await http.aclose()
            raise

    @staticmethod
    async def get_session(http: ApiHttpClient, base_url: str) -> dict[str, Any]:
        return await http.request_json(
            "GET", f"{base_url}/session", operation="wrapper_session"
        )

    @staticmethod
    async def login(
        http: ApiHttpClient,
        base_url: str,
        username: str,
        password: str,
        get_2fa_code: TwoFactorPrompt | None = None,
    ) -> None:
        response = await http.request(
            "POST",
            f"{base_url}/session/login",
            operation="wrapper_login",
            json={"username": username, "password": password},
        )
        if response.status_code == 200:
            return
        if response.status_code != 202:
            raise GamdlAuthenticationError(
                "Wrapper login failed", status_code=response.status_code
            )
        if get_2fa_code is None:
            raise GamdlAuthenticationError("Wrapper requires a two-factor code")
        # Never echo or log the challenge ID or code.
        try:
            challenge = response.json()
            challenge_id = challenge["challenge_id"]
            if (
                challenge.get("kind") != "two_factor"
                or not isinstance(challenge_id, str)
                or not challenge_id
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise GamdlAuthenticationError(
                "Wrapper returned an invalid login challenge"
            ) from None
        code = await invoke_prompt(get_2fa_code)
        reply = await http.request(
            "POST",
            f"{base_url}/session/challenge",
            operation="wrapper_2fa",
            json={"challenge_id": challenge_id, "code": code},
        )
        if reply.status_code != 200:
            raise GamdlAuthenticationError(
                "Wrapper two-factor authentication failed",
                status_code=reply.status_code,
            )

    async def get_playback(self, media_id: str) -> dict[str, Any]:
        async with self._native_lock:
            return await self.http.request_json(
                "GET",
                f"{self.base_url}/playback",
                operation="wrapper_playback",
                params={"adam_id": media_id},
            )

    async def get_credentials(self) -> dict[str, str]:
        """Export Apple Music credentials; never cache or log the response."""
        async with self._native_lock:
            result = await self.http.request_json(
                "GET",
                f"{self.base_url}/session/credentials",
                operation="wrapper_credentials",
            )
        if not all(
            isinstance(result.get(name), str) and result[name]
            for name in ("dev_token", "music_user_token")
        ):
            raise GamdlAuthenticationError("Wrapper authentication tokens are missing")
        return result

    async def get_context(self, media_id: str, key_uri: str) -> dict[str, Any]:
        """Retrieve one Temari context; no caching across keys or sessions."""
        if not media_id.isascii() or not media_id.isdecimal():
            raise ValueError("A numeric Apple Music media ID is required")
        if not key_uri.startswith("skd://") or len(key_uri) <= 6:
            raise ValueError("A FairPlay SKD key URI is required")
        async with self._native_lock:
            context = await self.http.request_json(
                "GET",
                f"{self.base_url}/key",
                operation="wrapper_context",
                params={"adamId": media_id, "uri": key_uri},
            )
        expected_profile = TEMARI_PROFILES.get(self.architecture)
        if (
            expected_profile is None
            or context.get("profile") != expected_profile
            or not all(isinstance(context.get(name), str) for name in ("ctx", "state"))
            or (
                self.architecture == "x86_64"
                and not all(
                    type(context.get(name)) is int and 0 <= context[name] <= 255
                    for name in ("bl", "r9b")
                )
            )
        ):
            raise GamdlApiResponseError(
                "Wrapper returned an incompatible Temari context"
            )
        return context
