"""Offline uploaded-video quality and direct-download regression tests."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from gamdl.download import TagResolver
from gamdl.media import (
    HttpStream,
    StreamOrigin,
    UploadedVideo,
    UploadedVideoQuality,
    UploadedVideoSelectionRequest,
)
from gamdl.media.exceptions import SelectionRequiredError, StreamNotAvailableError


def uploaded_metadata(**tokens):
    return {
        "id": "9876543210",
        "type": "uploaded-videos",
        "attributes": {
            "name": "Original Upload",
            "artistName": "Test Artist",
            "uploadDate": "2020-05-01T20:30:00Z",
            "assetTokens": tokens,
        },
    }


class UploadedVideoStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock()
        self.api.get_uploaded_video = AsyncMock()
        self.media = UploadedVideo.from_metadata(
            uploaded_metadata(
                **{
                    "720pHdVideo": "https://cdn.example.test/720.mp4?sig=signed",
                    "sdVideo": "https://cdn.example.test/480.mp4",
                }
            ),
            api=self.api,
        )

    async def test_best_quality_priority_and_exact_selection(self):
        best = await self.media.get_stream()
        self.assertIsInstance(best, HttpStream)
        self.assertEqual(best.origin, StreamOrigin.UPLOADED_VIDEO)
        self.assertEqual(best.codec, UploadedVideoQuality.HD_720P)
        self.assertIn("sig=signed", best.url)
        self.assertIs(await self.media.get_stream(), best)
        self.assertEqual(
            (
                await self.media.get_stream(
                    quality_priority=[
                        UploadedVideoQuality.SD,
                        UploadedVideoQuality.HD_720P,
                    ]
                )
            ).codec,
            UploadedVideoQuality.SD,
        )
        self.assertEqual(
            (await self.media.get_stream(quality=UploadedVideoQuality.SD)).codec,
            UploadedVideoQuality.SD,
        )
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_stream(quality=UploadedVideoQuality.HD_1080P)
        self.api.get_uploaded_video.assert_not_awaited()

    async def test_interactive_selection_sync_async_and_validation(self):
        observations = []

        async def choose(request):
            self.assertIsInstance(request, UploadedVideoSelectionRequest)
            observations.append(tuple(option.quality for option in request.options))
            return 1

        chosen = await self.media.get_stream(ask=True, selector=choose)
        self.assertEqual(chosen.codec, UploadedVideoQuality.SD)
        self.assertEqual(
            observations, [(UploadedVideoQuality.HD_720P, UploadedVideoQuality.SD)]
        )
        other = await self.media.get_stream(ask=True, selector=lambda _: 0)
        self.assertEqual(other.codec, UploadedVideoQuality.HD_720P)
        self.assertIsNot(other, chosen)
        for callback in (lambda _: True, lambda _: -1, lambda _: 2, lambda _: "1"):
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                await self.media.get_stream(ask=True, selector=callback)
        with self.assertRaises(SelectionRequiredError):
            await self.media.get_stream(ask=True)
        with self.assertRaises(ValueError):
            await self.media.get_stream(
                ask=True, quality=UploadedVideoQuality.SD, selector=choose
            )

    async def test_invalid_quality_options_rejected_before_network(self):
        for arguments in (
            {"quality_priority": []},
            {"quality_priority": "720pHdVideo"},
            {"quality": "bad"},
            {"quality_priority": ["bad"]},
            {
                "quality": UploadedVideoQuality.SD,
                "quality_priority": [UploadedVideoQuality.HD_720P],
            },
            {"selector": lambda _: 0},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.media.get_stream(**arguments)
        self.api.get_uploaded_video.assert_not_awaited()

    async def test_reject_unsafe_urls_and_missing_asset_tokens(self):
        for tokens in (
            {"1080pHdVideo": "http://cdn.example/video.mp4"},
            {"1080pHdVideo": "https://user:pass@cdn.example/video.mp4"},
            {"720pHdVideo": "file:///tmp/video.mp4"},
            {},
        ):
            uploaded = UploadedVideo.from_metadata(
                uploaded_metadata(**tokens), api=self.api
            )
            with (
                self.subTest(tokens=tokens),
                self.assertRaises(StreamNotAvailableError),
            ):
                await uploaded.get_stream()

    async def test_catalog_tags_do_not_need_webplayback(self):
        tags = await TagResolver().get_tags(self.media)
        self.assertEqual(tags.title, "Original Upload")
        self.assertEqual(tags.artist, "Test Artist")
        self.assertEqual(tags.title_id, 9876543210)
        self.assertEqual(tags.release_date, "2020-05-01T20:30:00Z")
        self.assertEqual(tags.media_type, 6)
        self.api.get_uploaded_video.assert_not_awaited()
