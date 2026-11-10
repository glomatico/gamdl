"""Media package contracts for the Gamdl 4.x canonical module layout."""

import importlib.util
import inspect
import unittest

from gamdl import media
from gamdl import drm
from gamdl import download
from gamdl.download import tags
from gamdl.media import base, enums, models, stream
from gamdl.media import music_video, song, uploaded_video
from gamdl.drm import models as drm_models
from gamdl.drm import service as drm_service
from gamdl.media.song import constants as song_constants
from gamdl.media.song import enums as song_enums
from gamdl.media.song import lyrics as song_lyrics
from gamdl.media.song import models as song_models
from gamdl.media.song import stream as song_stream


class MediaPackageTests(unittest.TestCase):
    def test_public_exports_refer_to_canonical_classes(self):
        expected = (
            (media.Media, base.Media),
            (media.MediaSource, models.MediaSource),
            (media.Song, song.Song),
            (media.MusicVideo, music_video.MusicVideo),
            (media.UploadedVideo, uploaded_video.UploadedVideo),
            (media.SongCodec, song_enums.SongCodec),
            (drm.DrmBackend, drm_service.DrmBackend),
            (media.HlsType, enums.HlsType),
            (media.StreamOrigin, enums.StreamOrigin),
            (media.StreamSource, enums.StreamSource),
            (media.SelectionKind, enums.SelectionKind),
            (drm.DRMInfo, drm_models.DRMInfo),
            (media.StreamSegment, models.StreamSegment),
            (media.SelectionOption, models.SelectionOption),
            (media.SelectionRequest, models.SelectionRequest),
            (drm.ContentKey, drm_models.ContentKey),
            (media.Lyrics, song_lyrics.Lyrics),
            (media.SongStreamResolver, song_stream.SongStreamResolver),
            (download.Tags, tags.Tags),
            (drm.Drm, drm_service.Drm),
            (media.StreamSelectionRequest, song_models.StreamSelectionRequest),
            (
                media.StreamSourceSelectionRequest,
                song_models.StreamSourceSelectionRequest,
            ),
        )
        for public, canonical in expected:
            with self.subTest(symbol=canonical.__name__):
                self.assertIs(public, canonical)

    def test_canonical_package_boundaries(self):
        expected_modules = {
            media.Song: "gamdl.media.song.media",
            media.SongCodec: "gamdl.media.song.enums",
            media.SongStreamResolver: "gamdl.media.song.stream",
            media.Lyrics: "gamdl.media.song.lyrics",
            download.Tags: "gamdl.download.tags",
            download.TagResolver: "gamdl.download.tag_resolver",
            media.MusicVideo: "gamdl.media.music_video.media",
            media.UploadedVideo: "gamdl.media.uploaded_video.media",
            drm.ContentKey: "gamdl.drm.models",
            drm.DRMInfo: "gamdl.drm.models",
            drm.Drm: "gamdl.drm.service",
            drm.DrmBackend: "gamdl.drm.enums",
            media.AudioStream: "gamdl.media.stream",
        }
        for symbol, module in expected_modules.items():
            with self.subTest(symbol=symbol.__name__):
                self.assertEqual(symbol.__module__, module)

    def test_obsolete_compatibility_modules_and_aliases_are_absent(self):
        for module in (
            "gamdl.media.song_stream",
            "gamdl.media.lyrics",
            "gamdl.media.video",
            "gamdl.media.song.tags",
            "gamdl.media.drm",
            "gamdl.media.tags",
        ):
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(module))
        for name in (
            "SongCodec",
            "WEB_FLAVORS",
            "SONG_CODEC_REGEX_MAP",
            "match_hls_codec",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(stream, name))
        for name in ("DrmBackend", "DRMInfo", "ContentKey", "DrmLicenseError"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(media, name))
        self.assertFalse(hasattr(media.Media, "get_keys"))
        self.assertFalse(hasattr(media.Song, "get_tags"))
        self.assertFalse(hasattr(media, "Tags"))

    def test_generic_stream_and_song_codec_matching_are_separate(self):
        self.assertIs(media.AudioStream, stream.AudioStream)
        self.assertTrue(inspect.iscoroutinefunction(media.AudioStream.get_drm_info))
        self.assertEqual(
            song_constants.WEB_FLAVORS[song_enums.SongCodec.AAC_WEB], "28:ctrp256"
        )
        self.assertEqual(
            song_stream.match_hls_codec("audio-alac-96-24", "alac"),
            media.SongCodec.ALAC_HIRES,
        )
        self.assertEqual(
            song_stream.match_hls_codec("audio-alac-48-24", "alac"),
            media.SongCodec.ALAC_LOSSLESS,
        )
        self.assertEqual(
            song_stream.match_hls_codec("audio-alac-44100-16", "alac"),
            media.SongCodec.ALAC_LOSSLESS,
        )
        self.assertEqual(
            song_stream.match_hls_codec("audio-alac-88.2-24", "alac"),
            media.SongCodec.ALAC_HIRES,
        )
        self.assertIsNone(song_stream.match_hls_codec("audio-alac-invalid", "alac"))
        self.assertNotIn(
            media.SongCodec.ALAC_LOSSLESS, song_constants.SONG_CODEC_REGEX_MAP
        )
        self.assertNotIn(
            media.SongCodec.ALAC_HIRES, song_constants.SONG_CODEC_REGEX_MAP
        )
        with self.assertRaises(ValueError):
            media.SongCodec("alac")
        self.assertEqual(
            song_stream.match_hls_codec(None, "mp4a.40.5"),
            media.SongCodec.AAC_HE,
        )

    def test_shared_asset_tags_work_with_music_video_metadata(self):
        wrapper_asset_metadata = {
            "itemName": "Music Video Title",
            "artistName": "Music Video Artist",
            "playlistName": "Music Video Album",
            "itemId": "123456789",
            "genre": "Pop",
            "trackNumber": "2",
        }
        shared = download.Tags.from_asset_metadata(wrapper_asset_metadata)
        self.assertIsInstance(shared, tags.Tags)
        self.assertEqual(shared.title, "Music Video Title")
        self.assertEqual(shared.artist, "Music Video Artist")
        self.assertEqual(shared.album, "Music Video Album")
        self.assertEqual(shared.title_id, 123456789)
        self.assertEqual(shared.track_number, 2)
        self.assertEqual(shared.genre, ("Pop",))


if __name__ == "__main__":
    unittest.main()
