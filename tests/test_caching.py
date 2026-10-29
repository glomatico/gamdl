"""Async cache contracts for shared artwork and instance-local metadata."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gamdl.media import (
    Cover,
    CoverCache,
    CoverFormat,
    GamdlMediaError,
    MediaResolver,
    Song,
)
from gamdl.media.cache import InstanceCache

TEMPLATE = "https://artwork.example.test/{w}x{h}.{f}?image=album-one"
ARTWORK = {"url": TEMPLATE, "width": 2000, "height": 2000}


def metadata(media_id: str, kind: str = "songs") -> dict:
    return {"id": media_id, "type": kind, "attributes": {"artwork": ARTWORK}}


class InstanceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_none_failures_and_clear(self):
        cache = InstanceCache()
        cache.seed("metadata", None)
        calls = 0

        async def work():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("temporary")
            return 42

        self.assertIsNone(await cache.get("metadata", work))
        with self.assertRaises(ValueError):
            await cache.get("value", work)
        self.assertEqual(await cache.get("value", work), 42)
        self.assertEqual(await cache.get("value", work), 42)
        self.assertEqual(calls, 2)
        cache.clear()
        self.assertEqual(cache.values, {})

    async def test_cancelled_waiter_does_not_cancel_shared_request(self):
        cache = InstanceCache()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(cache.get("key", slow))
        await started.wait()
        second = asyncio.create_task(cache.get("key", slow))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, "done")
        self.assertEqual(await cache.get("key", slow), "done")
        self.assertEqual(calls, 1)

    async def test_invalidation_detaches_old_inflight_task(self):
        cache = InstanceCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def stale():
            started.set()
            await release.wait()
            return "stale"

        previous = asyncio.create_task(cache.get("key", stale))
        await started.wait()
        cache.clear()
        self.assertEqual(
            await cache.get("key", lambda: asyncio.sleep(0, result="fresh")), "fresh"
        )
        release.set()
        self.assertEqual(await previous, "stale")
        await asyncio.sleep(0)
        self.assertEqual(cache.values["key"], "fresh")


class CoverCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_apple_fixed_extension_url_respects_format_and_cache_key(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(200, content=request.url.path.encode())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(client=client)
            cover = Cover.from_metadata(
                {"url": "https://artwork.example.test/{w}x{h}bb.jpg"},
                cache=cache,
            )
            jpeg = await cover.get_cover_bytes()
            png = await cover.get_cover_bytes(format=CoverFormat.PNG)
            self.assertNotEqual(jpeg, png)
            self.assertEqual(
                requests,
                [
                    "https://artwork.example.test/1200x1200bb.jpg",
                    "https://artwork.example.test/1200x1200bb.png",
                ],
            )
            self.assertEqual(await cover.get_cover_bytes(format="png"), png)
            self.assertEqual(len(requests), 2)
            await cache.aclose()

    async def test_cover_without_cache_still_fetches_lazily(self):
        cover = Cover.from_metadata(ARTWORK)
        self.assertIsNone(cover.cache)
        with patch(
            "gamdl.media.cover.fetch_cover_bytes",
            new_callable=AsyncMock,
            return_value=b"uncached-image",
        ) as fetch:
            self.assertEqual(
                await cover.get_cover_bytes(width=300, height=300), b"uncached-image"
            )
            self.assertEqual(
                await cover.get_cover_bytes(width=300, height=300), b"uncached-image"
            )
            self.assertEqual(fetch.await_count, 2)
            fetch.assert_awaited_with(
                "https://artwork.example.test/300x300.jpg?image=album-one"
            )

    async def test_injected_client_does_not_leak_default_auth_or_cookies(self):
        observed = []

        def handler(request):
            observed.append(dict(request.headers))
            return httpx.Response(200, content=b"safe")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer developer-token",
                "Cookie": "session=secret",
            },
            auth=httpx.BasicAuth("private", "password"),
        ) as client:
            cache = CoverCache(client=client)
            self.assertEqual(
                await cache.get_bytes("https://artwork.example.test/cover.jpg"), b"safe"
            )
            self.assertNotIn("authorization", observed[0])
            self.assertNotIn("cookie", observed[0])
            await cache.aclose()

    async def test_shared_cover_download_one_request_per_url_and_variant(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=b"image-data")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(client=client)
            cover_a = Cover.from_metadata(ARTWORK, cache=cache)
            cover_b = Cover.from_metadata(ARTWORK, cache=cache)
            self.assertEqual(len(requests), 0)
            first = await cover_a.get_cover_bytes()
            second = await cover_b.get_cover_bytes()
            self.assertEqual(first, b"image-data")
            self.assertIs(first, second)
            self.assertEqual(len(requests), 1)
            self.assertIn("/1200x1200.jpg", str(requests[0].url))
            await cover_a.get_cover_bytes(width=600, height=600)
            await cover_b.get_cover_bytes(format="png")
            await cover_a.get_cover_bytes(width=600, height=600)
            self.assertEqual(len(requests), 3)
            cache.clear()
            await cover_b.get_cover_bytes()
            self.assertEqual(len(requests), 4)
            await cache.aclose()
            self.assertFalse(client.is_closed)

    async def test_simultaneous_calls_and_cancelled_waiter_share_download(self):
        began = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(_request):
            nonlocal calls
            calls += 1
            began.set()
            await release.wait()
            return httpx.Response(200, content=b"cover")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(client=client)
            cover = Cover.from_metadata(ARTWORK, cache=cache)
            first = asyncio.create_task(cover.get_cover_bytes())
            await began.wait()
            second = asyncio.create_task(cover.get_cover_bytes())
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual(await second, b"cover")
            self.assertEqual(await cover.get_cover_bytes(), b"cover")
            self.assertEqual(calls, 1)
            await cache.aclose()

    async def test_lru_eviction_and_ttl(self):
        calls = 0

        def handler(_):
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=str(calls).encode())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(maxsize=1, client=client)
            cover_a = Cover.from_metadata(ARTWORK, cache=cache)
            cover_b = Cover.from_metadata(
                {"url": TEMPLATE.replace("album-one", "album-two")}, cache=cache
            )
            self.assertEqual(await cover_a.get_cover_bytes(), b"1")
            self.assertEqual(await cover_b.get_cover_bytes(), b"2")
            self.assertEqual(await cover_a.get_cover_bytes(), b"3")
            await cache.aclose()

            expiring = CoverCache(ttl=0.01, client=client)
            cover = Cover.from_metadata(ARTWORK, cache=expiring)
            self.assertEqual(await cover.get_cover_bytes(), b"4")
            await asyncio.sleep(0.025)
            self.assertEqual(await cover.get_cover_bytes(), b"5")
            await expiring.aclose()

    async def test_404_and_http_failure_are_not_cached(self):
        calls = 0
        secret = "artwork-secret-token"

        def handler(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(404)
            if calls == 2:
                return httpx.Response(503, text=secret)
            return httpx.Response(200, content=b"recovered")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(client=client)
            url = f"https://artwork.example.test/file.jpg?token={secret}"
            self.assertIsNone(await cache.get_bytes(url))
            with self.assertRaises(GamdlMediaError) as failure:
                await cache.get_bytes(url)
            self.assertNotIn(secret, str(failure.exception))
            self.assertEqual(await cache.get_bytes(url), b"recovered")
            self.assertEqual(await cache.get_bytes(url), b"recovered")
            self.assertEqual(calls, 3)
            await cache.aclose()

    async def test_invalid_url_and_cache_size_are_rejected(self):
        with self.assertRaises(ValueError):
            CoverCache(maxsize=0)
        cache = CoverCache()
        for url in ("http://example.com/image.jpg", "https://user:pass@example.com/a"):
            with self.subTest(url=url):
                with self.assertRaises(GamdlMediaError):
                    await cache.get_bytes(url)
        await cache.aclose()


class ResolverCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_invalidation_does_not_evict_shared_image_bytes(self):
        artwork_requests = 0

        def handler(_request):
            nonlocal artwork_requests
            artwork_requests += 1
            return httpx.Response(200, content=b"still-cached")

        class FakeApi:
            async def get_song(self, media_id):
                return {"data": [metadata(media_id)]}

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = CoverCache(client=client)
            song = Song.from_id("one", api=FakeApi(), cover_cache=cache)
            first = await song.get_cover()
            self.assertEqual(await first.get_cover_bytes(), b"still-cached")
            song.invalidate()
            refreshed = await song.get_cover()
            self.assertEqual(await refreshed.get_cover_bytes(), b"still-cached")
            self.assertEqual(artwork_requests, 1)
            await cache.aclose()

    async def test_full_album_cover_reuse_with_attached_resolver_cache(self):
        requests = 0

        def handler(_request):
            nonlocal requests
            requests += 1
            return httpx.Response(200, content=b"shared-cover")

        class FakeApi:
            async def iter_album_tracks(self, *_args, **_kwargs):
                yield metadata("track-one")
                yield metadata("track-two")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            shared = CoverCache(client=client)
            resolver = MediaResolver(FakeApi(), cover_cache=shared)
            album = [media async for media in resolver.from_album("album-id")]
            covers = [await item.get_cover() for item in album]
            self.assertIs(covers[0].cache, covers[1].cache)
            data = await asyncio.gather(
                *(cover.get_cover_bytes(format="png") for cover in covers)
            )
            self.assertEqual(data, [b"shared-cover", b"shared-cover"])
            self.assertEqual(requests, 1)
            await shared.aclose()

    async def test_resolver_creates_one_cache_for_all_media_types(self):
        class FakeApi:
            async def iter_album_tracks(self, *_args, **_kwargs):
                yield metadata("one")
                yield metadata("two")

        resolver = MediaResolver(FakeApi())
        self.assertIsInstance(resolver.cover_cache, CoverCache)
        album = [item async for item in resolver.from_album("album")]
        direct = [item async for item in resolver.from_song("three")]
        video = [item async for item in resolver.from_music_video("video")]
        uploaded = [item async for item in resolver.from_uploaded_video("upload")]
        all_media = album + direct + video + uploaded
        self.assertEqual(len(all_media), 5)
        for item in all_media:
            self.assertIs(item.cover_cache, resolver.cover_cache)
        for item in album:
            cover = await item.get_cover()
            self.assertIs(cover.cache, resolver.cover_cache)
        await resolver.cover_cache.aclose()

    async def test_cache_override_does_not_change_cached_cover_metadata(self):
        class FakeApi:
            def __init__(self):
                self.requests = 0

            async def get_song(self, media_id: str):
                self.requests += 1
                return {"data": [metadata(media_id)]}

        api = FakeApi()
        attached = CoverCache()
        override = CoverCache()
        song = Song.from_id("one", api=api, cover_cache=attached)
        initial = await song.get_cover()
        alternative = await song.get_cover(cache=override)
        self.assertIs(initial.cache, attached)
        self.assertIs(alternative.cache, override)
        self.assertIs((await song.get_cover()).cache, attached)
        self.assertEqual(api.requests, 1)
        self.assertEqual(initial.url, alternative.url)
        self.assertEqual(initial, alternative)
        await attached.aclose()
        await override.aclose()


if __name__ == "__main__":
    unittest.main()
