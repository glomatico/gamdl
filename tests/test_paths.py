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

    def test_dot_leading_folder_and_file_replaced_with_underscore(self):
        path = self.f.get_media_path(
            self.song,
            Tags(
                artist=".hidden", title=".visible", album=".secret", album_artist=".dot"
            ),
            extension=".m4a",
        )
        self.assertEqual(path, self.root / "_dot/_secret/_visible.m4a")
        video = self.f.get_media_path(
            None,
            Tags(artist="Britney Spears", title="...Baby One More Time"),
            extension=".m4v",
        )
        self.assertEqual(
            video,
            self.root / "Britney Spears/Unknown Album/_..Baby One More Time.m4v",
        )
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

    def test_date_placeholder_supports_strftime_formats(self):
        cases = (
            ("2024-07-09", "2024-07-09", "2024", "2024-07", "09"),
            ("2024-07-09T18:34:56Z", "2024-07-09T18_34_56Z", "2024", "2024-07", "09"),
            ("2004", "2004", "2004", "2004-01", "01"),
            ("2004-06", "2004-06", "2004", "2004-06", "01"),
        )
        for raw, unchanged, year, year_month, day in cases:
            with self.subTest(raw=raw):
                f = PathFormatter(
                    self.root,
                    no_album_file_template="{date} ({date:%Y}) ({date:%Y-%m}) ({date:%d}) {title}",
                )
                path = f.get_media_path(
                    self.song,
                    Tags(artist="Artist", title="Song", release_date=raw),
                    extension=".m4a",
                )
                self.assertEqual(
                    path.name,
                    f"{unchanged} ({year}) ({year_month}) ({day}) Song.m4a",
                )

    def test_date_specifier_works_in_album_folder_without_changing_metadata(self):
        tags = Tags(
            album="Album",
            album_artist="Artist",
            title="Track",
            release_date="1998-10-05",
        )
        formatter = PathFormatter(
            self.root,
            album_folder_template="{album_artist}/{date:%Y}/{album}",
            single_disc_file_template="{date:%Y-%m} {title}",
        )
        path = formatter.get_media_path(self.song, tags, extension=".m4a")
        self.assertEqual(path, self.root / "Artist/1998/Album/1998-10 Track.m4a")
        self.assertEqual(tags.release_date, "1998-10-05")

    def test_missing_or_invalid_dates_fall_back_safely(self):
        f = PathFormatter(
            self.root, no_album_file_template="{date:%Y} - {date:%m} - {title}"
        )
        for raw in (None, "", "not a date", "2024-02-30", "2024-13", "2024-99-99"):
            with self.subTest(raw=raw):
                path = f.get_media_path(
                    self.song,
                    Tags(artist="Artist", title="Song", release_date=raw),
                    extension=".m4a",
                )
                self.assertEqual(path.name, "Unknown Date - Unknown Date - Song.m4a")

    def test_date_strftime_does_not_enable_arbitrary_field_expressions(self):
        for template in ("{date.__class__}", "{date[raw]}"):
            with self.subTest(template=template):
                f = PathFormatter(self.root, no_album_file_template=template)
                with self.assertRaises(ValueError):
                    f.get_media_path(
                        self.song,
                        Tags(artist="Artist", title="Song", release_date="2024-02-29"),
                        extension=".m4a",
                    )
        f = PathFormatter(self.root, no_album_file_template="{title:%Y}")
        path = f.get_media_path(
            self.song,
            Tags(artist="Artist", title="Song", release_date="2024-02-29"),
            extension=".m4a",
        )
        self.assertEqual(path.name, "Unknown Title.m4a")

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

    def test_formatter_does_not_inherit_path(self):
        self.assertNotIsInstance(self.f, Path)
