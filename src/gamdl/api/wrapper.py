"""Wrapper-v2 session, login/2FA and playback client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .auth import invoke_prompt
from .exceptions import GamdlApiResponseError, GamdlAuthenticationError
from .http import ApiHttpClient, RequestPacer

TARGET_WRAPPER_API_VERSION = "0.0.2"
CredentialsPrompt = Callable[[], tuple[str, str] | Awaitable[tuple[str, str]]]
TwoFactorPrompt = Callable[[], str | Awaitable[str]]


class WrapperApi:
    """Thin wrapper API client; prompts are supplied by the host application."""

    def __init__(
        self,
        http: ApiHttpClient,
        base_url: str,
        *,
        me: dict[str, Any],
        decrypt_host: str = "127.0.0.1",
        decrypt_port: int = 10020,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.me = me
        # Decryption socket configuration is distinct from the HTTP API.
        self.decrypt_host = decrypt_host
        self.decrypt_port = decrypt_port

    async def __aenter__(self) -> WrapperApi:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    @staticmethod
    def validate_api_version(me: dict[str, Any]) -> None:
        if me.get("version") != TARGET_WRAPPER_API_VERSION:
            raise GamdlApiResponseError(
                f"Unsupported wrapper API version (expected {TARGET_WRAPPER_API_VERSION})"
            )

    @classmethod
    async def create(
        cls,
        *,
        base_url: str = "http://127.0.0.1",
        decrypt_host: str = "127.0.0.1",
        decrypt_port: int = 10020,
        get_credentials: CredentialsPrompt | None = None,
        get_2fa_code: TwoFactorPrompt | None = None,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> WrapperApi:
        http = ApiHttpClient("wrapper", client=client, pacer=pacer, timeout=600.0)
        base_url = base_url.rstrip("/")
        try:
            me = await cls.get_me(http, base_url)
            cls.validate_api_version(me)
            if me.get("auth", {}).get("state") == "logged_out" and get_credentials:
                username, password = await invoke_prompt(get_credentials)
                await cls.login(http, base_url, username, password, get_2fa_code)
                me = await cls.get_me(http, base_url)
                cls.validate_api_version(me)

            if me.get("auth", {}).get("state") == "logged_out":
                raise GamdlAuthenticationError("Wrapper is not authenticated")

            return cls(
                http,
                base_url,
                me=me,
                decrypt_host=decrypt_host,
                decrypt_port=decrypt_port,
            )
        except BaseException:
            await http.aclose()
            raise

    @staticmethod
    async def get_me(http: ApiHttpClient, base_url: str) -> dict[str, Any]:
        # Response may contain credentials. Never log it.
        return await http.request_json(
            "GET", f"{base_url}/me", operation="wrapper_get_me"
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
            f"{base_url}/login",
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
            raise GamdlAuthenticationError(
                "Wrapper requires a two-factor authentication code"
            )
        code = await invoke_prompt(get_2fa_code)
        reply = await http.request(
            "POST",
            f"{base_url}/login/2fa",
            operation="wrapper_login_2fa",
            json={"code": code},
        )
        if reply.status_code != 200:
            raise GamdlAuthenticationError(
                "Wrapper two-factor authentication failed",
                status_code=reply.status_code,
            )

    async def get_playback(self, media_id: str) -> dict[str, Any]:
        return await self.http.request_json(
            "GET",
            f"{self.base_url}/playback",
            operation="wrapper_get_playback",
            params={"adam_id": media_id},
        )
