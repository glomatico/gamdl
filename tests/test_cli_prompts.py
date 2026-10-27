"""Terminal selection requests, Questionary lifecycle and credential paths."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import click

from gamdl.cli.prompts import ask_file, select_media, select_stream, stream_choices
from gamdl.media.models import CollectionOption, CollectionSelectionRequest
from gamdl.media.enums import SelectionKind
from gamdl.media.enums import StreamOrigin
from gamdl.media.song.enums import SongCodec
from gamdl.media.song.models import (
    StreamSourceOption,
    StreamSourceSelectionRequest,
    StreamSelectionOption,
    StreamSelectionRequest,
)
from gamdl.media.music_video.enums import MusicVideoCodec
from gamdl.media.music_video.models import (
    AudioSelectionOption,
    AudioSelectionRequest,
    VideoSelectionOption,
    VideoSelectionRequest,
)
from gamdl.media.uploaded_video.stream import (
    UploadedVideoQuality,
    UploadedVideoSelectionOption,
    UploadedVideoSelectionRequest,
)


class QuestionaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_artist_multiselect_checkbox_and_abort(self):
        request = CollectionSelectionRequest(
            SelectionKind.ARTIST,
            "1272779",
            "Select content for Janet:",
            (CollectionOption(0, "Main Albums"), CollectionOption(1, "Music Videos")),
        )
        prompt = Mock(ask_async=AsyncMock(return_value=[0, 1]))
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts.questionary.checkbox", return_value=prompt
            ) as checkbox,
        ):
            self.assertEqual(await select_media(request), [0, 1])
        self.assertEqual(checkbox.call_args.kwargs["choices"][1].value, 1)
        self.assertIn("Space", checkbox.call_args.kwargs["instruction"])
        with patch("gamdl.cli.prompts.interactive", return_value=False):
            with self.assertRaises(click.ClickException):
                await select_media(request)
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts.questionary.checkbox",
                return_value=Mock(ask_async=AsyncMock(return_value=None)),
            ),
        ):
            with self.assertRaises(click.Abort):
                await select_media(request)

    def requests(self):
        audio = Mock(codec=SongCodec.ALAC, bitrate=995_000)
        video = Mock(
            codec=MusicVideoCodec.H265,
            width=1920,
            height=1080,
            frame_rate=29.97,
            bitrate=4_000_000,
        )
        return (
            (
                StreamSourceSelectionRequest(
                    "123",
                    (
                        StreamSourceOption(0, StreamOrigin.WEBPLAYBACK),
                        StreamSourceOption(1, StreamOrigin.ASSET_ENHANCED_HLS),
                    ),
                ),
                "source",
                "webplayback",
            ),
            (
                StreamSelectionRequest(
                    "123", StreamOrigin.WEBPLAYBACK, (StreamSelectionOption(0, audio),)
                ),
                "codec",
                "alac",
            ),
            (
                VideoSelectionRequest("123", (VideoSelectionOption(0, video),)),
                "codec",
                "1920×1080",
            ),
            (
                AudioSelectionRequest(
                    "123",
                    video,
                    (AudioSelectionOption(0, audio, name="English", language="en"),),
                ),
                "audio",
                "English",
            ),
            (
                UploadedVideoSelectionRequest(
                    (UploadedVideoSelectionOption(0, UploadedVideoQuality.HD_1080P),)
                ),
                "quality",
                "1080pHdVideo",
            ),
        )

    async def test_each_stream_request_uses_arrow_selection_and_async_questionary(self):
        for request, word, visible in self.requests():
            with self.subTest(word=word):
                title, choices = stream_choices(request)
                self.assertIn(word, title.lower())
                self.assertIn(visible, choices[0].title)
                prompt = Mock(ask_async=AsyncMock(return_value=0))
                with (
                    patch("gamdl.cli.prompts.interactive", return_value=True),
                    patch(
                        "gamdl.cli.prompts.questionary.select", return_value=prompt
                    ) as selection,
                ):
                    result = await select_stream(request)
                self.assertEqual(result, 0)
                self.assertTrue(selection.call_args.kwargs["use_arrow_keys"])
                selected = selection.call_args.kwargs["choices"]
                self.assertEqual(
                    [(choice.title, choice.value) for choice in selected],
                    [(choice.title, choice.value) for choice in choices],
                )
                prompt.ask_async.assert_awaited_once()

    async def test_prompt_cancellation_and_noninteractive_mode(self):
        request = self.requests()[0][0]
        with patch("gamdl.cli.prompts.interactive", return_value=False):
            with self.assertRaises(click.ClickException):
                await select_stream(request)
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts.questionary.select",
                return_value=Mock(ask_async=AsyncMock(return_value=None)),
            ),
        ):
            with self.assertRaises(click.Abort):
                await select_stream(request)

    async def test_missing_cookies_and_playready_device_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "device.prd"
            file.write_bytes(b"mock-device")
            self.assertEqual(
                await ask_file(str(file), label="PlayReady .prd", extension=".prd"),
                file,
            )
            with patch("gamdl.cli.prompts.interactive", return_value=False):
                with self.assertRaisesRegex(click.ClickException, "PlayReady"):
                    await ask_file(
                        str(file) + "missing", label="PlayReady .prd", extension=".prd"
                    )
            replacement = Mock(ask_async=AsyncMock(return_value=str(file)))
            with (
                patch("gamdl.cli.prompts.interactive", return_value=True),
                patch(
                    "gamdl.cli.prompts.questionary.text",
                    return_value=replacement,
                ),
            ):
                result = await ask_file(None, label="PlayReady .prd", extension=".prd")
            self.assertEqual(result, file)
