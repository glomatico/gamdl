"""Offline contract tests for the initial asynchronous API implementation."""

import asyncio
import io
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

from gamdl.api import (
    ApiHttpClient,
    AppleMusicApi,
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    ItunesApi,
    RequestPacer,
    WrapperApi,
)
from gamdl.api.auth import AppleMusicCredentials, media_user_token_from_cookies
from gamdl.api.constants import APPLE_MUSIC_API_URL
from gamdl.logging import get_logger


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_errors_are_sanitized_and_classified(self):
        secret = "secret-cookie-and-token"

        def handler(request):
            return httpx.Response(401, text=secret)

        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client)
            with self.assertRaises(GamdlAuthenticationError) as raised:
                await api.request_json("GET", "https://example.test/me", operation="me")
            self.assertEqual(raised.exception.status_code, 401)
            self.assertNotIn(secret, str(raised.exception))
            await api.aclose()
            self.assertFalse(client.is_closed)  # Caller owns injected client.

    async def test_rate_limit_exposes_retry_after_without_retrying_post(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(429, headers={"Retry-After": "7"}, text="private")

        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client)
            with self.assertRaises(GamdlRateLimitError) as raised:
                await api.request(
                    "POST", "https://example.test/license", operation="license"
                )
            self.assertEqual(raised.exception.retry_after, 7)
        self.assertEqual(calls, ["POST"])

    async def test_safe_get_retry(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client)
            self.assertEqual(
                await api.request_json(
                    "GET", "https://example.test/", operation="test"
                ),
                {"ok": True},
            )
        self.assertEqual(attempts, 2)

    async def test_invalid_json_and_array_payload(self):
        responses = iter(
            [httpx.Response(200, text="not-json"), httpx.Response(200, json=[1])]
        )
        async with mock_client(lambda req: next(responses)) as client:
            api = ApiHttpClient("apple", client=client)
            with self.assertRaises(GamdlApiResponseError):
                await api.request_json("GET", "https://example.test", operation="parse")
            with self.assertRaises(GamdlApiResponseError):
                await api.request_json("GET", "https://example.test", operation="parse")

    async def test_pacer_serializes_concurrent_requests_across_clients(self):
        active = 0
        max_active = 0

        async def handler(request):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.015)
            active -= 1
            return httpx.Response(200, json={"ok": True})

        pacer = RequestPacer(interval=0.003)
        async with mock_client(handler) as client:
            a = ApiHttpClient("apple", client=client, pacer=pacer)
            b = ApiHttpClient("itunes", client=client, pacer=pacer)
            await asyncio.gather(
                *(
                    api.request_json("GET", "https://example.test/", operation="test")
                    for api in [a, b, a, b]
                )
            )
        self.assertEqual(max_active, 1)

    async def test_owned_client_is_closed(self):
        async with ApiHttpClient("test") as api:
            self.assertFalse(api.client.is_closed)
        self.assertTrue(api.client.is_closed)


class AuthenticationTests(unittest.TestCase):
    def test_netscape_cookies_and_credential_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".music.apple.com\tTRUE\t/\tTRUE\t2147483647\tmedia-user-token\ttopsecret\n",
                encoding="utf8",
            )
            self.assertEqual(media_user_token_from_cookies(str(path)), "topsecret")
        self.assertNotIn("topsecret", repr(AppleMusicCredentials("foo", "topsecret")))


class AppleMusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_auth_and_search(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/v1/me/account":
                return httpx.Response(
                    200,
                    json={
                        "meta": {"subscription": {"active": True, "storefront": "br"}}
                    },
                )
            return httpx.Response(200, json={"results": {"songs": {"data": []}}})

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client, token="secret-token", media_user_token="private-cookie"
            ) as api:
                self.assertEqual(api.storefront, "br")
                self.assertTrue(api.active_subscription)
                await api.get_search_results("Janet Jackson")

            self.assertFalse(client.is_closed)
        self.assertEqual(requests[1].url.path, "/v1/catalog/br/search")
        self.assertEqual(requests[1].url.params["term"], "Janet Jackson")
        self.assertEqual(requests[1].headers["authorization"], "Bearer secret-token")

    async def test_get_token_scrapes_webplayer_using_single_session(self):
        visited = []

        def handler(request):
            visited.append(request.url.path)
            if request.url.path == "/":
                return httpx.Response(
                    200, text='<script src="/assets/index-test.js"></script>'
                )
            return httpx.Response(200, text='"eyJabc.eyJdef.ghijk"')

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(client=client) as api:
                self.assertEqual(api.credentials.developer_token, "eyJabc.eyJdef.ghijk")
        self.assertEqual(visited, ["/", "/assets/index-test.js"])

    async def test_pagination_preserves_order_and_validates_host(self):
        first = {"data": [{"id": "1"}], "next": "/v1/me/library/playlists?offset=1"}
        second = {"data": [{"id": "2"}], "next": None}

        def handler(request):
            self.assertEqual(request.url.host, "amp-api.music.apple.com")
            return httpx.Response(
                200, json=second if request.url.params.get("offset") == "1" else first
            )

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                items = [item["id"] async for item in api.iter_library_playlists()]
                self.assertEqual(items, ["1", "2"])
                with self.assertRaises(GamdlApiResponseError):
                    await api.get_extended_api_data("https://malicious.example/steal")

    async def test_license_exchange_never_retries_post(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"status": 0, "license": "private-license"})

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(client=client, token="token") as api:
                result = await api.get_license_exchange("123", "uri", "challenge")
                self.assertEqual(result["status"], 0)
        self.assertEqual(len(requests), 1)


class ItunesTests(unittest.IsolatedAsyncioTestCase):
    async def test_brazil_storefront_id_is_discovered(self):
        visits = []

        def handler(request):
            visits.append(request)
            if request.url.path.endswith("musickit.js"):
                return httpx.Response(200, text='BR:"BRA",BRA:"143503"')
            return httpx.Response(200, json={"results": []})

        async with mock_client(handler) as client:
            async with await ItunesApi.create(client=client, storefront="br") as api:
                self.assertEqual(api.storefront_id, 143503)
                await api.get_lookup_result("123")
                self.assertEqual(visits[1].url.params["country"], "br")


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_login_and_two_factor_callback(self):
        state = {"authenticated": False}
        endpoints = []

        def handler(request):
            endpoints.append(request.url.path)
            if request.url.path == "/me":
                return httpx.Response(
                    200,
                    json={
                        "version": "0.0.2",
                        "auth": {
                            "state": "logged_in"
                            if state["authenticated"]
                            else "logged_out"
                        },
                    },
                )
            if request.url.path == "/login":
                return httpx.Response(202)
            if request.url.path == "/login/2fa":
                state["authenticated"] = True
                return httpx.Response(200)
            return httpx.Response(200, json={"playback": "ok"})

        async def get_code():
            return "123456"

        async with mock_client(handler) as client:
            async with await WrapperApi.create(
                base_url="http://wrapper.test",
                client=client,
                get_credentials=lambda: ("username", "password"),
                get_2fa_code=get_code,
            ) as api:
                self.assertEqual((await api.get_playback("42"))["playback"], "ok")
                self.assertEqual(api.decrypt_port, 10020)
        self.assertEqual(endpoints, ["/me", "/login", "/login/2fa", "/me", "/playback"])

    async def test_rejects_wrong_version(self):
        async with mock_client(
            lambda _: httpx.Response(
                200, json={"version": "0.0.1", "auth": {"state": "logged_in"}}
            )
        ) as client:
            with self.assertRaises(GamdlApiResponseError):
                await WrapperApi.create(client=client)


class LoggingTests(unittest.TestCase):
    def test_structlog_respects_host_handlers_and_context(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        host = logging.getLogger("gamdl.test_event")
        host.addHandler(handler)
        host.setLevel(logging.DEBUG)
        try:
            get_logger("gamdl.test_event").bind(operation="search").debug("completed")
            self.assertIn("operation='search'", output.getvalue())
        finally:
            host.removeHandler(handler)
            host.setLevel(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
