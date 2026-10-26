"""Exact legacy template defaults with portable and safe path generation."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from gamdl.download import PathFormatter, PlaylistTags, Tags
from gamdl.media import Song


class PathFormatterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/tmp/Apple Music")
        self.f = PathFormatter(self.root)
        self.song = Song.from_id("1", api=Mock())

    def test_exact_gamdl3_defaults_and_album_disc_compilation(self):
        self.assertEqual(self.f.album_folder_template, "{album_artist}/{album}")
        self.assertEqual(self.f.compilation_folder_template, "Compilations/{album}")
        self.assertEqual(self.f.no_album_folder_template, "{artist}/Unknown Album")
        self.assertEqual(self.f.playlist_folder_template, "Playlists/{playlist_artist}")
        self.assertEqual(self.f.single_disc_file_template, "{track:02d} {title}")
        self.assertEqual(self.f.multi_disc_file_template, "{disc}-{track:02d} {title}")
        self.assertEqual(self.f.no_album_file_template, "{title}")
        self.assertEqual(self.f.playlist_file_template, "{playlist_title}")
        self.assertEqual(
            self.f.get_media_path(
                self.song,
                Tags(
                    title="Genesis",
                    album="Dua Lipa",
                    album_artist="Dua Lipa",
                    track_number=1,
                ),
                extension=".m4a",
            ),
            self.root / "Dua Lipa/Dua Lipa/01 Genesis.m4a",
        )
        self.assertEqual(
            self.f.get_media_path(
                self.song,
                Tags(
                    title="Finale",
                    album="Compilation",
                    compilation=True,
                    disc_number=2,
                    disc_total=3,
                    track_number=4,
                ),
                extension=".m4a",
            ),
            self.root / "Compilations/Compilation/2-04 Finale.m4a",
        )
        self.assertEqual(
            self.f.get_media_path(
                self.song, Tags(title="Loose", artist="Solo"), extension=".m4a"
            ),
            self.root / "Solo/Unknown Album/Loose.m4a",
        )
        self.assertEqual(
            self.f.get_playlist_path(PlaylistTags(title="Favorites", artist="Me")),
            self.root / "Playlists/Me/Favorites.m3u8",
        )

    def test_dot_leading_folder_replaced_with_underscore_only_for_folders(self):
        path = self.f.get_media_path(
            self.song,
            Tags(
                artist=".hidden", title=".visible", album=".secret", album_artist=".dot"
            ),
            extension=".m4a",
        )
        self.assertEqual(path, self.root / "_dot/_secret/.visible.m4a")
        path = self.f.get_media_path(
            self.song, Tags(artist="..", title="Film"), extension=".m4v"
        )
        self.assertEqual(path, self.root / "__/Unknown Album/Film.m4v")
        playlist = self.f.get_playlist_path(
            PlaylistTags(title="Tracks", artist=".account")
        )
        self.assertEqual(playlist, self.root / "Playlists/_account/Tracks.m3u8")

    def test_path_traversal_control_chars_and_long_unicode_values(self):
        path = self.f.get_media_path(
            self.song,
            Tags(artist="../../etc", album=None, title="one/two\\three:\x00*?"),
            extension=".m4a",
        )
        self.assertEqual(
            path, self.root / "_._.._etc/Unknown Album/one_two_three____.m4a"
        )
        for item in path.relative_to(self.root).parts:
            self.assertNotIn(item, (".", ".."))
        long = self.f.get_media_path(
            self.song, Tags(artist="é" * 200, title="🔥" * 200), extension=".m4a"
        )
        for part in long.relative_to(self.root).parts:
            self.assertLessEqual(len(part.encode("utf-8")), 255)

    def test_missing_track_number_uses_legacy_empty_fallback(self):
        self.assertEqual(
            self.f.get_media_path(
                self.song, Tags(album="Record", title="Unknown"), extension=".m4a"
            ),
            self.root / "Unknown Artist/Record/Unknown.m4a",
        )

    def test_bad_templates_and_extension(self):
        media = Tags(artist="Test", title="Track")
        for template in (
            "../..//folder",
            "{unknown}",
            "{title.__class__}",
            "{title[0]}",
            "/absolute",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                PathFormatter(
                    self.root, no_album_folder_template=template
                ).get_media_path(self.song, media, extension=".m4a")
        for extension in ("m4a", "/escape", ".foo/bar"):
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                self.f.get_media_path(self.song, media, extension=extension)

    def test_formatter_is_pure_and_does_not_inherit_path(self):
        self.assertNotIsInstance(self.f, Path)
