from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import structlog

from .exceptions import GamdlApiResponseError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

CredentialsFunc = (
    Callable[[], tuple[str, str]] | Callable[[], Awaitable[tuple[str, str]]]
)
TwoFactorCodeFunc = Callable[[], str] | Callable[[], Awaitable[str]]


async def _invoke(func: Callable[[], T | Awaitable[T]]) -> T:
    result = func()
    if inspect.isawaitable(result):
        return await result
    return result


class WrapperApi:
    def __init__(
        self,
        base_url: str,
        decrypt_host: str,
        decrypt_port: int,
        client: httpx.AsyncClient,
        me: dict,
    ):
        self.base_url = base_url
        self.decrypt_host = decrypt_host
        self.decrypt_port = decrypt_port
        self.client = client
        self.me = me

    @classmethod
    async def create(
        cls,
        base_url: str = "http://127.0.0.1",
        decrypt_host: str = "127.0.0.1",
        decrypt_port: int = 10020,
        get_credentials_func: CredentialsFunc | None = None,
        get_2fa_code: TwoFactorCodeFunc | None = None,
    ) -> WrapperApi:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=30.0),
        )

        base_url = base_url.rstrip("/")

        me = await cls.get_me(client, base_url)
        if get_credentials_func is not None and me["auth"]["state"] == "logged_out":
            username, password = await _invoke(get_credentials_func)
            await cls.login(
                client,
                base_url,
                username,
                password,
                get_2fa_code,
            )
            me = await cls.get_me(client, base_url)

        if me.get("auth", {}).get("state") == "logged_out":
            raise GamdlApiResponseError(
                "Wrapper is not authenticated. "
                "Provide get_credentials_func or log in via the wrapper.",
            )

        return cls(base_url, decrypt_host, decrypt_port, client, me)

    @staticmethod
    async def login(
        client: httpx.AsyncClient,
        base_url: str,
        username: str,
        password: str,
        get_2fa_code: TwoFactorCodeFunc | None = None,
    ) -> None:
        base_url = base_url.rstrip("/")
        response = await client.post(
            f"{base_url}/login",
            json={"username": username, "password": password},
        )
        if response.status_code == 200:
            return

        if response.status_code == 202:
            if get_2fa_code is None:
                raise GamdlApiResponseError(
                    "Wrapper login requires 2FA; provide get_2fa_code",
                    status_code=202,
                )
            code = await _invoke(get_2fa_code)
            tfa_response = await client.post(
                f"{base_url}/login/2fa",
                json={"code": code},
            )
            if tfa_response.is_error:
                raise GamdlApiResponseError(
                    "Wrapper 2FA login failed",
                    content=tfa_response.text,
                    status_code=tfa_response.status_code,
                )
            return

        raise GamdlApiResponseError(
            "Wrapper login failed",
            content=response.text,
            status_code=response.status_code,
        )

    @staticmethod
    async def get_me(client: httpx.AsyncClient, base_url: str) -> dict:
        log = logger.bind(action="wrapper_get_me")

        response = None

        try:
            response = await client.get(f"{base_url}/me")
            response.raise_for_status()
            account_info = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching wrapper account info",
                content=getattr(response, "text", None),
                status_code=getattr(response, "status_code", None),
            )

        log.debug("success", account_info=account_info)

        return account_info

    async def get_playback(self, media_id: str) -> dict:
        log = logger.bind(action="wrapper_get_playback", media_id=media_id)

        response = None

        try:
            response = await self.client.get(
                f"{self.base_url}/playback",
                params={"adam_id": media_id},
            )
            response.raise_for_status()
            playback = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching wrapper playback",
                content=getattr(response, "text", None),
                status_code=getattr(response, "status_code", None),
            )

        log.debug("success", playback=playback)

        return playback
