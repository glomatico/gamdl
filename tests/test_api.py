"""Offline contract tests for the initial asynchronous API implementation."""

import asyncio
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from gamdl.api import (
    ApiHttpClient,
    AppleMusicApi,
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
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

    async def test_retry_exhaustion_keeps_safe_api_exception(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="sensitive response body")

        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client, retries=1)
            with self.assertRaises(GamdlApiResponseError) as result:
                await api.request("GET", "https://example.test/", operation="test")

        self.assertEqual(calls, 2)
        self.assertEqual(result.exception.status_code, 503)
        self.assertNotIn("sensitive", str(result.exception))

    async def test_get_transport_retry_and_post_no_retry(self):
        calls = {"GET": 0, "POST": 0}

        def handler(request):
            calls[request.method] += 1
            if request.method == "POST" or calls["GET"] == 1:
                raise httpx.ConnectError("do not log remote error bodies")
            return httpx.Response(200, json={"ok": True})

        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client)
            self.assertEqual(
                await api.request_json("GET", "https://example.test", operation="test"),
                {"ok": True},
            )
            with self.assertRaises(GamdlTransportError) as result:
                await api.request("POST", "https://example.test", operation="license")

        self.assertEqual(calls, {"GET": 2, "POST": 1})
        self.assertNotIn("do not log", str(result.exception))

    async def test_pacer_does_not_block_other_requests_while_retry_sleeps(self):
        """A retry must free the shared lock before backoff, unlike RetryTransport
        wrapped *inside* a locked high-level client request.
        """
        retry_is_sleeping = asyncio.Event()
        resume_retry = asyncio.Event()
        slow_attempts = 0

        def handler(request):
            nonlocal slow_attempts
            if request.url.path == "/slow":
                slow_attempts += 1
                if slow_attempts == 1:
                    return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        async def controlled_backoff(_retry, _response):
            retry_is_sleeping.set()
            await resume_retry.wait()

        pacer = RequestPacer()
        async with mock_client(handler) as client:
            api = ApiHttpClient("apple", client=client, pacer=pacer)
            with patch("gamdl.transport.private.Retry.asleep", new=controlled_backoff):
                slow = asyncio.create_task(
                    api.request_json(
                        "GET", "https://example.test/slow", operation="slow"
                    )
                )
                try:
                    await asyncio.wait_for(retry_is_sleeping.wait(), timeout=3)
                    fast = await asyncio.wait_for(
                        api.request_json(
                            "GET", "https://example.test/fast", operation="fast"
                        ),
                        timeout=3,
                    )
                    self.assertEqual(fast, {"ok": True})
                finally:
                    resume_retry.set()
                    await asyncio.wait_for(slow, timeout=3)
        self.assertEqual(slow_attempts, 2)

    async def test_invalid_retry_after_does_not_hide_rate_limit(self):
        async with mock_client(
            lambda request: httpx.Response(429, headers={"Retry-After": "invalid"})
        ) as client:
            api = ApiHttpClient("apple", client=client, retries=0)
            with self.assertRaises(GamdlRateLimitError) as result:
                await api.request("GET", "https://example.test", operation="test")
        self.assertIsNone(result.exception.retry_after)

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
    async def test_isrc_lookup_uses_separate_catalog_endpoints(self):
        requests = []
        code = "USUM71703085"

        def handler(request):
            requests.append(request)
            kind = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "123", "type": kind, "attributes": {"isrc": code}}],
                    "meta": {"filters": {"isrc": {code: [{"id": "123"}]}}},
                },
            )

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client, storefront="br", language="pt-BR", token="test-token"
            ) as api:
                song = await api.get_songs_by_isrc(code)
                video = await api.get_music_videos_by_isrc(code)
                with self.assertRaises(ValueError):
                    await api._get_by_isrc(code, "albums")

        self.assertEqual(
            [r.url.path for r in requests],
            [
                "/v1/catalog/br/songs",
                "/v1/catalog/br/music-videos",
            ],
        )
        self.assertEqual([r.url.params["filter[isrc]"] for r in requests], [code, code])
        self.assertEqual([r.url.params["l"] for r in requests], ["pt-BR", "pt-BR"])
        self.assertTrue(
            all(r.headers["authorization"] == "Bearer test-token" for r in requests)
        )
        self.assertEqual(song["data"][0]["type"], "songs")
        self.assertEqual(video["data"][0]["type"], "music-videos")

    async def test_account_auth_and_search(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/v1/me/account":
                return httpx.Response(
                    200,
                    json={
                        "meta": {"subscription": {"active": True, "storefront": "br"}},
                        "data": [
                            {"attributes": {"restrictions": {"music": "explicit"}}}
                        ],
                    },
                )
            return httpx.Response(200, json={"results": {"songs": {"data": []}}})

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client, token="secret-token", media_user_token="private-cookie"
            ) as api:
                self.assertEqual(api.storefront, "br")
                self.assertTrue(api.active_subscription)
                self.assertEqual(api.account_restrictions, {"music": "explicit"})
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

    async def test_get_lyrics_fetches_standard_ttml(self):
        requests = []
        ttml = '<tt xmlns="http://www.w3.org/ns/ttml"><body /></tt>'

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={"data": [{"id": "123", "attributes": {"ttml": ttml}}]},
            )

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client,
                storefront="br",
                token="test-token",
                media_user_token="test-user-token",
            ) as api:
                lyrics = await api.get_lyrics("123", language="pt-BR")

        # The authenticated constructor also requests account info.
        self.assertEqual(len(requests), 2)
        request = requests[1]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "amp-api.music.apple.com")
        self.assertEqual(request.url.path, "/v1/catalog/br/songs/123/lyrics")
        self.assertEqual(request.url.params["l"], "pt-BR")
        self.assertEqual(request.headers["authorization"], "Bearer test-token")
        self.assertIn("media-user-token=test-user-token", request.headers["cookie"])
        self.assertEqual(lyrics["data"][0]["attributes"]["ttml"], ttml)

    async def test_get_syllable_lyrics_localization_and_raw_response(self):
        requests = []
        ttml = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>'
            '<span begin="00:01.000" end="00:01.500">Hello</span>'
            "</p></div></body></tt>"
        )
        payload = {
            "data": [
                {
                    "id": "456",
                    "attributes": {
                        "ttml": ttml,
                        "ttmlLocalizations": {"pt-BR": "localized TTML"},
                    },
                }
            ],
        }

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=payload)

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client, token="test-token", storefront="us"
            ) as api:
                result = await api.get_syllable_lyrics(
                    "456",
                    language="en-US",
                    lyrics_language="pt-BR",
                    script="Latn",
                    extend="ttmlLocalizations",
                )

        self.assertEqual(result, payload)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.path, "/v1/catalog/us/songs/456/syllable-lyrics")
        self.assertEqual(
            dict(request.url.params),
            {
                "l": "en-US",
                "l[lyrics]": "pt-BR",
                "l[script]": "Latn",
                "extend": "ttmlLocalizations",
            },
        )

    async def test_syllable_lyrics_omits_unspecified_localization(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": []})

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                self.assertEqual(await api.get_syllable_lyrics("123"), {"data": []})
                self.assertEqual(await api.get_lyrics("123"), {"data": []})

        self.assertEqual(len(requests), 2)
        self.assertEqual(str(requests[0].url.query, "ascii"), "")
        self.assertEqual(str(requests[1].url.query, "ascii"), "")

    async def test_lyrics_unavailable_and_api_errors_are_preserved(self):
        responses = iter(
            [
                httpx.Response(404, json={"errors": [{"detail": "private"}]}),
                httpx.Response(200, json={"errors": [{"detail": "private"}]}),
            ]
        )
        async with mock_client(lambda request: next(responses)) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                with self.assertRaises(GamdlApiResponseError) as missing:
                    await api.get_syllable_lyrics("123")
                self.assertEqual(missing.exception.status_code, 404)
                self.assertNotIn("private", str(missing.exception))

                with self.assertRaises(GamdlApiResponseError) as error:
                    await api.get_lyrics("123")
                self.assertNotIn("private", str(error.exception))

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

    async def test_search_iterator_paginates_each_resource_type(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.host, "amp-api.music.apple.com")
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            self.assertEqual(request.url.path, "/v1/catalog/br/search")

            if request.url.params.get("offset") == "2":
                self.assertEqual(request.url.params["types"], "songs")
                return httpx.Response(
                    200,
                    json={
                        "results": {"songs": {"data": [{"id": "s3", "type": "songs"}]}}
                    },
                )
            if request.url.params.get("offset") == "1":
                self.assertEqual(request.url.params["types"], "albums")
                return httpx.Response(
                    200,
                    json={
                        "results": {
                            "albums": {"data": [{"id": "a2", "type": "albums"}]}
                        }
                    },
                )

            self.assertEqual(request.url.params["types"], "songs,albums,artists")
            self.assertEqual(request.url.params["limit"], "2")
            return httpx.Response(
                200,
                json={
                    "results": {
                        "songs": {
                            "data": [
                                {"id": "s1", "type": "songs"},
                                {"id": "s2", "type": "songs"},
                            ],
                            "next": "/v1/catalog/br/search?types=songs&offset=2",
                        },
                        "albums": {
                            "data": [{"id": "a1", "type": "albums"}],
                            "next": "/v1/catalog/br/search?types=albums&offset=1",
                        },
                        # Artists intentionally missing: Apple can omit empty groups.
                    }
                },
            )

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(
                client=client, token="test-token", storefront="br"
            ) as api:
                results = [
                    item["id"]
                    async for item in api.iter_search_results(
                        "test", types="songs,albums,artists", limit=2
                    )
                ]

        self.assertEqual(results, ["s1", "s2", "s3", "a1", "a2"])
        self.assertEqual(len(requests), 3)

    async def test_search_iterator_is_lazy(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": {
                        "songs": {
                            "data": [{"id": "first"}],
                            "next": "/v1/catalog/us/search?types=songs&offset=1",
                        }
                    }
                },
            )

        async with mock_client(handler) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                async for item in api.iter_search_results("test", types="songs"):
                    self.assertEqual(item["id"], "first")
                    break
        self.assertEqual(len(requests), 1)

    async def test_search_iterator_rejects_bad_result_shapes_and_cycles(self):
        responses = iter(
            [
                {"results": []},
                {"results": {"songs": {"data": "not-a-list"}}},
                {"results": {"songs": {"data": [{"id": "first"}], "next": "/loop"}}},
                {"results": {"songs": {"data": [], "next": "/loop"}}},
            ]
        )

        async with mock_client(
            lambda _: httpx.Response(200, json=next(responses))
        ) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                for _ in range(2):
                    with self.assertRaises(GamdlApiResponseError):
                        [
                            item
                            async for item in api.iter_search_results(
                                "test", types="songs"
                            )
                        ]
                with self.assertRaises(GamdlApiResponseError):
                    [
                        item
                        async for item in api.iter_search_results("test", types="songs")
                    ]

    async def test_library_collection_iterators_with_offsets(self):
        for kind in ("songs", "music-videos", "albums", "playlists"):
            with self.subTest(kind=kind):
                requests = []

                def handler(request):
                    requests.append(request)
                    self.assertEqual(request.url.path, f"/v1/me/library/{kind}")
                    if request.url.params.get("offset") == "5":
                        return httpx.Response(
                            200,
                            json={"data": [{"id": f"{kind}-2"}]},
                        )
                    self.assertEqual(request.url.params["offset"], "4")
                    self.assertEqual(request.url.params["limit"], "1")
                    return httpx.Response(
                        200,
                        json={
                            "data": [{"id": f"{kind}-1"}],
                            "next": f"/v1/me/library/{kind}?limit=1&offset=5",
                        },
                    )

                async with mock_client(handler) as client:
                    async with await AppleMusicApi.create(
                        client=client, token="x"
                    ) as api:
                        iterator = getattr(
                            api, f"iter_library_{kind.replace('-', '_')}"
                        )
                        values = [i["id"] async for i in iterator(limit=1, offset=4)]
                self.assertEqual(values, [f"{kind}-1", f"{kind}-2"])
                self.assertEqual(len(requests), 2)

    async def test_playlist_album_and_artist_iterators(self):
        for kind, kwargs, path in (
            (
                "playlist_tracks",
                {"playlist_id": "123"},
                "/v1/catalog/br/playlists/123/tracks",
            ),
            (
                "playlist_tracks",
                {"playlist_id": "123", "is_library": True},
                "/v1/me/library/playlists/123/tracks",
            ),
            ("album_tracks", {"album_id": "456"}, "/v1/catalog/br/albums/456/tracks"),
            (
                "album_tracks",
                {"album_id": "456", "is_library": True},
                "/v1/me/library/albums/456/tracks",
            ),
            (
                "artist_view",
                {"artist_id": "789", "view": "top-songs"},
                "/v1/catalog/br/artists/789/view/top-songs",
            ),
        ):
            with self.subTest(kind=kind, kwargs=kwargs):
                calls = []

                def handler(request):
                    calls.append(request)
                    self.assertEqual(request.url.path, path)
                    if request.url.params.get("offset") == "1":
                        return httpx.Response(200, json={"data": [{"id": "second"}]})
                    self.assertEqual(request.url.params["limit"], "1")
                    return httpx.Response(
                        200,
                        json={"data": [{"id": "first"}], "next": f"{path}?offset=1"},
                    )

                async with mock_client(handler) as client:
                    async with await AppleMusicApi.create(
                        client=client, token="x", storefront="br"
                    ) as api:
                        iterator = getattr(api, f"iter_{kind}")
                        results = [i["id"] async for i in iterator(**kwargs, limit=1)]
                self.assertEqual(results, ["first", "second"])
                self.assertEqual(len(calls), 2)

    async def test_rejects_unsafe_artist_view(self):
        async with mock_client(
            lambda req: self.fail("unexpected API request")
        ) as client:
            async with await AppleMusicApi.create(client=client, token="x") as api:
                with self.assertRaises(ValueError):
                    [i async for i in api.iter_artist_view("123", "../../private")]

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
