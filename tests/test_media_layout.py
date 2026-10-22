"""Media package boundaries and compatibility with pre-refactor imports."""

import inspect
import unittest

from gamdl import media
from gamdl.media import base, enums, models, stream
from gamdl.media import drm, lyrics, song_stream as legacy_song_stream, tags, video
from gamdl.media import song, music_video, uploaded_video
from gamdl.media.drm import models as drm_models
from gamdl.media.drm import service as drm_service
from gamdl.media.song import constants as song_constants
from gamdl.media.song import enums as song_enums
from gamdl.media.song import models as song_models
from gamdl.media.song import stream as song_stream


class MediaPackageTests(unittest.TestCase):
    def test_all_public_exports_refer_to_canonical_classes(self):
        self.assertIs(media.Media, base.Media)
        self.assertIs(media.MediaSource, models.MediaSource)
        self.assertIs(media.Song, song.Song)
        self.assertIs(media.MusicVideo, music_video.MusicVideo)
        self.assertIs(media.UploadedVideo, uploaded_video.UploadedVideo)
        self.assertIs(media.SongCodec, song_enums.SongCodec)
        self.assertIs(media.DrmBackend, enums.DrmBackend)
        self.assertIs(media.HlsType, enums.HlsType)
        self.assertIs(media.StreamOrigin, enums.StreamOrigin)
        self.assertIs(media.StreamSource, enums.StreamSource)
        self.assertIs(media.SelectionKind, enums.SelectionKind)
        self.assertIs(media.DRMInfo, models.DRMInfo)
        self.assertIs(media.StreamSegment, models.StreamSegment)
        self.assertIs(media.SelectionOption, models.SelectionOption)
        self.assertIs(media.SelectionRequest, models.SelectionRequest)
        self.assertIs(media.ContentKey, drm_models.ContentKey)

    def test_existing_deep_imports_remain_usable(self):
        self.assertIs(video.MusicVideo, media.MusicVideo)
        self.assertIs(video.UploadedVideo, media.UploadedVideo)
        self.assertIs(lyrics.Lyrics, media.Lyrics)
        self.assertIs(tags.Tags, media.Tags)
        self.assertIs(legacy_song_stream.SongStreamResolver, song.SongStreamResolver)
        self.assertIs(
            legacy_song_stream.StreamSelectionRequest,
            song_models.StreamSelectionRequest,
        )
        self.assertIs(
            legacy_song_stream.StreamSourceSelectionRequest,
            song_models.StreamSourceSelectionRequest,
        )
        self.assertIs(stream.SongCodec, song_enums.SongCodec)
        self.assertIs(stream.WEB_FLAVORS, song_constants.WEB_FLAVORS)
        self.assertIs(stream.SONG_CODEC_REGEX_MAP, song_constants.SONG_CODEC_REGEX_MAP)
        self.assertIs(stream.match_hls_codec, song_stream.match_hls_codec)
        self.assertIs(drm.DrmBackend, enums.DrmBackend)
        self.assertIs(drm.ContentKey, drm_models.ContentKey)
        self.assertIs(drm.get_stream_keys, drm_service.get_stream_keys)

    def test_song_specific_implementations_are_not_defined_in_generic_modules(self):
        self.assertEqual(media.Song.__module__, "gamdl.media.song.media")
        self.assertEqual(media.SongCodec.__module__, "gamdl.media.song.enums")
        self.assertEqual(media.SongStreamResolver.__module__, "gamdl.media.song.stream")
        self.assertEqual(media.Lyrics.__module__, "gamdl.media.song.lyrics")
        self.assertEqual(media.Tags.__module__, "gamdl.media.song.tags")
        self.assertEqual(media.MusicVideo.__module__, "gamdl.media.music_video.media")
        self.assertEqual(
            media.UploadedVideo.__module__, "gamdl.media.uploaded_video.media"
        )
        self.assertEqual(media.ContentKey.__module__, "gamdl.media.drm.models")
        self.assertEqual(media.DrmBackend.__module__, "gamdl.media.enums")
        self.assertEqual(media.AudioStream.__module__, "gamdl.media.stream")

    def test_shared_stream_only_uses_song_dependencies_lazily(self):
        self.assertIs(media.AudioStream, stream.AudioStream)
        self.assertTrue(inspect.iscoroutinefunction(media.AudioStream.get_drm_info))
        self.assertEqual(
            song_stream.match_hls_codec("audio-alac-96-24", "alac"),
            media.SongCodec.ALAC,
        )
        self.assertEqual(
            song_stream.match_hls_codec(None, "mp4a.40.5"),
            media.SongCodec.AAC_HE,
        )


if __name__ == "__main__":
    unittest.main()
