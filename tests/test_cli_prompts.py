"""Terminal selection requests, Questionary lifecycle and credential paths."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import click

from gamdl.cli.prompts import ask_file, select_media, select_stream, stream_choices
from gamdl.cli.tui.components import MenuChoice
from gamdl.cli.tui.wizard import FullScreenWizard
from gamdl.media.enums import SelectionKind, StreamOrigin
from gamdl.media.models import CollectionOption, CollectionSelectionRequest
from gamdl.media.music_video.enums import MusicVideoCodec
from gamdl.media.music_video.models import (
    AudioSelectionOption,
    AudioSelectionRequest,
    VideoSelectionOption,
    VideoSelectionRequest,
)
from gamdl.media.song.enums import SongCodec
from gamdl.media.song.models import (
    StreamSelectionOption,
    StreamSelectionRequest,
    StreamSourceOption,
    StreamSourceSelectionRequest,
)
from gamdl.media.uploaded_video.stream import (
    UploadedVideoQuality,
    UploadedVideoSelectionOption,
    UploadedVideoSelectionRequest,
)


class QuestionaryTests(unittest.IsolatedAsyncioTestCase):
    def test_authentication_wizard_cursor_stays_inside_current_screen(self):
        wizard = FullScreenWizard()
        wizard.title = "Configure authentication"
        wizard.description = "Choose an authentication method"
        wizard.choices = [
            MenuChoice("Wrapper", 0),
            MenuChoice("Cookies", 1),
            MenuChoice("Media User Token", 2),
        ]
        wizard.cursor = 2
        wizard.stage = "choice"
        ui = wizard.body_window.content.create_content(width=80, height=10)
        self.assertEqual(ui.cursor_position.y, 6)
        self.assertLess(ui.cursor_position.y, ui.line_count)

        for stage in ("busy", "entry"):
            wizard.stage = stage
            ui = wizard.body_window.content.create_content(width=80, height=10)
            self.assertEqual(ui.cursor_position.y, 4)
            self.assertLess(ui.cursor_position.y, ui.line_count)

    async def test_collection_multiselect_uses_full_screen_router(self):
        request = CollectionSelectionRequest(
            SelectionKind.ARTIST,
            "1272779",
            "Select content for Janet:",
            (CollectionOption(0, "Main Albums"), CollectionOption(1, "Music Videos")),
        )
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts.select_many",
                new_callable=AsyncMock,
                return_value=[0, 1],
            ) as selector,
        ):
            self.assertEqual(await select_media(request), [0, 1])
        self.assertEqual(selector.await_args.args[1][1].value, 1)
        with (
            patch("gamdl.cli.prompts.interactive", return_value=False),
            self.assertRaises(click.ClickException),
        ):
            await select_media(request)
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts.select_many",
                new_callable=AsyncMock,
                side_effect=click.Abort(),
            ),
            self.assertRaises(click.Abort),
        ):
            await select_media(request)

    def requests(self):
        audio = Mock(codec=SongCodec.ALAC_HIRES, bitrate=995_000)
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
                "alac-hires",
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

    async def test_each_stream_request_routes_to_full_screen_selector(self):
        for request, word, visible in self.requests():
            with self.subTest(word=word):
                title, choices = stream_choices(request)
                self.assertIn(word, title.lower())
                self.assertIn(visible, choices[0].title + " " + choices[0].detail)
                with (
                    patch("gamdl.cli.prompts.interactive", return_value=True),
                    patch(
                        "gamdl.cli.prompts._select_stream",
                        new_callable=AsyncMock,
                        return_value=0,
                    ) as selection,
                ):
                    result = await select_stream(request)
                self.assertEqual(result, 0)
                selection.assert_awaited_once_with(request, title=None)

    async def test_prompt_cancellation_and_noninteractive_mode(self):
        request = self.requests()[0][0]
        with (
            patch("gamdl.cli.prompts.interactive", return_value=False),
            self.assertRaises(click.ClickException),
        ):
            await select_stream(request)
        with (
            patch("gamdl.cli.prompts.interactive", return_value=True),
            patch(
                "gamdl.cli.prompts._select_stream",
                new_callable=AsyncMock,
                side_effect=click.Abort(),
            ),
            self.assertRaises(click.Abort),
        ):
            await select_stream(request)

    async def test_missing_cookies_and_playready_device_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "device.prd"
            file.write_bytes(b"mock-device")
            self.assertEqual(
                await ask_file(str(file), label="PlayReady .prd", extension=".prd"),
                file,
            )
            with (
                patch("gamdl.cli.prompts.interactive", return_value=False),
                self.assertRaisesRegex(click.ClickException, "PlayReady"),
            ):
                await ask_file(
                    str(file) + "missing", label="PlayReady .prd", extension=".prd"
                )
            with (
                patch("gamdl.cli.prompts.interactive", return_value=True),
                patch(
                    "gamdl.cli.prompts.ask_text",
                    new_callable=AsyncMock,
                    return_value=str(file),
                ),
            ):
                result = await ask_file(None, label="PlayReady .prd", extension=".prd")
            self.assertEqual(result, file)
