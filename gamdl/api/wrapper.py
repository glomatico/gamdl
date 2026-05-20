from __future__ import annotations

import inspect
import struct
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
        client: httpx.AsyncClient,
        me: dict,
    ):
        self.base_url = base_url
        self.client = client
        self.me = me

    @staticmethod
    def build_decrypt_sample_frame(
        adam_id: str,
        skd_uri: str,
        ciphertexts: list[bytes],
    ) -> bytes:
        """Build wrapper-v2 /decrypt binary request frame."""
        adam_id_bytes = adam_id.encode("utf-8")
        skd_uri_bytes = skd_uri.encode("utf-8")
        if not adam_id_bytes:
            raise ValueError("wrapper-v2: adam_id must not be empty")
        if not skd_uri_bytes:
            raise ValueError("wrapper-v2: skd_uri must not be empty")
        if not ciphertexts:
            raise ValueError("wrapper-v2: ciphertext batch must not be empty")

        frame = bytearray()
        frame += struct.pack(
            ">III",
            len(adam_id_bytes),
            len(skd_uri_bytes),
            len(ciphertexts),
        )
        for ciphertext in ciphertexts:
            frame += struct.pack(">I", len(ciphertext))
        frame += adam_id_bytes
        frame += skd_uri_bytes
        for ciphertext in ciphertexts:
            frame += ciphertext
        return bytes(frame)

    @staticmethod
    def parse_decrypt_sample_frame(data: bytes, expected_count: int) -> list[bytes]:
        """Parse wrapper-v2 /decrypt binary response frame."""
        if len(data) < 4:
            raise IOError("wrapper-v2: POST /decrypt returned a truncated response")
        (sample_count,) = struct.unpack_from(">I", data, 0)
        if sample_count != expected_count:
            raise IOError(
                f"wrapper-v2: expected {expected_count} samples in response, "
                f"got {sample_count}"
            )

        table_end = 4 + sample_count * 4
        if len(data) < table_end:
            raise IOError("wrapper-v2: POST /decrypt returned a truncated length table")

        lengths = [
            struct.unpack_from(">I", data, 4 + i * 4)[0] for i in range(sample_count)
        ]
        offset = table_end
        out: list[bytes] = []
        for i, length in enumerate(lengths):
            end = offset + length
            if end > len(data):
                raise IOError(
                    f"wrapper-v2: POST /decrypt returned truncated sample {i}"
                )
            out.append(data[offset:end])
            offset = end

        if offset != len(data):
            raise IOError("wrapper-v2: POST /decrypt returned trailing bytes")
        return out

    @classmethod
    async def create(
        cls,
        base_url: str = "http://127.0.0.1",
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

        return cls(base_url, client, me)

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

    async def decrypt(
        self,
        adam_id: str,
        skd_uri: str,
        ciphertexts: list[bytes],
    ) -> list[bytes]:
        """Decrypt one POST /decrypt batch; plaintexts match ciphertext order."""
        log = logger.bind(
            action="wrapper_decrypt",
            adam_id=adam_id,
            sample_count=len(ciphertexts),
        )

        frame = self.build_decrypt_sample_frame(adam_id, skd_uri, ciphertexts)
        response = await self.client.post(
            f"{self.base_url}/decrypt",
            content=frame,
            headers={
                "content-type": "application/octet-stream",
                "accept": "application/octet-stream",
            },
        )
        if response.status_code == 401:
            raise IOError(
                "wrapper-v2: POST /decrypt returned 401 — log in with POST /login "
                "or restore a session on the daemon first"
            )
        if response.status_code == 503:
            raise IOError(
                "wrapper-v2: decrypt unavailable (503) — check daemon logs /health "
                "for playback_ready and Apple lib init"
            )
        if response.status_code != 200:
            detail = ""
            try:
                j = response.json()
                detail = (j.get("detail") or j.get("error") or str(j)) or ""
            except Exception:
                detail = (response.text or "")[:500]
            raise IOError(
                f"wrapper-v2: POST /decrypt failed HTTP {response.status_code}: {detail}"
            )

        plaintexts = self.parse_decrypt_sample_frame(
            response.content,
            len(ciphertexts),
        )
        log.debug("success")
        return plaintexts
