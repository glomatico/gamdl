"""End-to-end tests for the native MP4 tagger (no Mutagen dependency)."""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gamdl.download import Tags
from gamdl.muxer import Muxer

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/KuoAAAAASUVORK5CYII="
)
JPEG = b"\xff\xd8\xff\xe0" + b"example-jpeg-header"


# Independent atom reader for precise on-disk type and width assertions.
def atoms(content: bytes):
    at = 0
    while at + 8 <= len(content):
        size = int.from_bytes(content[at : at + 4], "big")
        name = content[at + 4 : at + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(content[at + 8 : at + 16], "big")
            header = 16
        elif size == 0:
            size = len(content) - at
        if size < header or at + size > len(content):
            raise AssertionError("Invalid MP4 box size")
        yield name, content[at + header : at + size]
        at += size


def mp4_tag_atoms(path: Path):
    data = path.read_bytes()
    moov = next(body for key, body in atoms(data) if key == b"moov")
    udta = next(body for key, body in atoms(moov) if key == b"udta")
    meta = next(body for key, body in atoms(udta) if key == b"meta")
    ilst = next(body for key, body in atoms(meta[4:]) if key == b"ilst")
    return {
        key: [
            (int.from_bytes(item[0:4], "big"), item[8:])
            for inner, item in atoms(payload)
            if inner == b"data"
        ]
        for key, payload in atoms(ilst)
    }


def decoded_pcm_md5(path: Path):
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@unittest.skipUnless(
    shutil.which("ffmpeg"), "FFmpeg is required for MP4 fixture generation"
)
class NativeMp4TagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "untagged.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t",
                "0.2",
                "-c:a",
                "aac",
                "-metadata",
                "comment=original",
                "-y",
                str(self.path),
            ],
            check=True,
            capture_output=True,
        )
        self.muxer = Muxer()

    def test_all_legacy_apple_atoms_and_unicode(self):
        original_pcm = decoded_pcm_md5(self.path)
        self.muxer.tag(
            self.path,
            tags=Tags(
                title="Title 𝄞",
                artist="Artist",
                album="Album",
                album_artist="Album Artist",
                title_sort="title",
                artist_sort="artist",
                album_sort="album",
                composer="Composer",
                composer_sort="composer",
                copyright="© Copyright",
                comment="Comment",
                release_date="1987-11-12",
                lyrics="One line\nTwo lines",
                xid="abc:xid",
                title_id=1559885421,
                artist_id=669771,
                album_id=1559885420,
                composer_id=12345,
                genre_id=14,
                storefront=143503,
                track_number=2,
                track_total=10,
                disc_number=1,
                disc_total=2,
                genre=("Pop", "Rock"),
                compilation=False,
                gapless=True,
                rating=1,
                media_type=1,
                isrc="GBARL9300135",
            ),
            cover_bytes=PNG,
        )
        boxes = mp4_tag_atoms(self.path)

        for fourcc, value in [
            (b"\xa9nam", "Title 𝄞"),
            (b"\xa9ART", "Artist"),
            (b"\xa9alb", "Album"),
            (b"aART", "Album Artist"),
            (b"sonm", "title"),
            (b"soar", "artist"),
            (b"soal", "album"),
            (b"\xa9wrt", "Composer"),
            (b"soco", "composer"),
            (b"cprt", "© Copyright"),
            (b"\xa9cmt", "Comment"),
            (b"\xa9day", "1987-11-12"),
            (b"\xa9lyr", "One line\nTwo lines"),
            (b"xid ", "abc:xid"),
        ]:
            with self.subTest(fourcc=fourcc):
                self.assertEqual(boxes[fourcc], [(1, value.encode("utf-8"))])

        for fourcc, value, width in [
            (b"cnID", 1559885421, 4),
            (b"atID", 669771, 4),
            (b"plID", 1559885420, 8),
            (b"cmID", 12345, 4),
            (b"geID", 14, 4),
            (b"sfID", 143503, 4),
        ]:
            with self.subTest(fourcc=fourcc):
                self.assertEqual(boxes[fourcc], [(21, value.to_bytes(width, "big"))])
        self.assertEqual(boxes[b"cpil"], [(21, b"\x00")])
        self.assertEqual(boxes[b"pgap"], [(21, b"\x01")])
        self.assertEqual(boxes[b"stik"], [(21, b"\x01")])
        self.assertEqual(boxes[b"rtng"], [(21, b"\x01")])
        self.assertEqual(boxes[b"trkn"][0][1][2:6], b"\x00\x02\x00\x0a")
        self.assertEqual(boxes[b"disk"][0][1][2:6], b"\x00\x01\x00\x02")
        self.assertEqual(boxes[b"\xa9gen"], [(1, b"Pop"), (1, b"Rock")])
        self.assertEqual(boxes[b"covr"], [(14, PNG)])
        self.assertEqual(decoded_pcm_md5(self.path), original_pcm)
        self.assertIn(b"ISRC", self.path.read_bytes())
        self.assertIn(b"GBARL9300135", self.path.read_bytes())

    def test_missing_fields_preserve_existing_metadata(self):
        self.muxer.tag(self.path, tags=Tags(title="first"))
        self.muxer.tag(self.path, tags=Tags(artist="second"))
        boxes = mp4_tag_atoms(self.path)
        self.assertEqual(boxes[b"\xa9nam"], [(1, b"first")])
        self.assertEqual(boxes[b"\xa9ART"], [(1, b"second")])
        self.assertEqual(boxes[b"\xa9cmt"], [(1, b"original")])

    def test_jpeg_artwork_metadata(self):
        self.muxer.tag(self.path, tags=Tags(), cover_bytes=JPEG)
        self.assertEqual(mp4_tag_atoms(self.path)[b"covr"], [(13, JPEG)])

    def test_invalid_art_and_numbers_leave_input_untouched(self):
        before = self.path.read_bytes()
        for tag, cover in (
            (Tags(title="bad art"), b"not an image"),
            (Tags(track_number=65536), None),
            (Tags(title_id=2**40), None),
            (Tags(rating=256), None),
        ):
            with self.subTest(tag=tag, cover=cover):
                with self.assertRaises(ValueError):
                    self.muxer.tag(self.path, tags=tag, cover_bytes=cover)
                self.assertEqual(self.path.read_bytes(), before)

    def test_non_mp4_rejected(self):
        path = Path(self.tmp.name) / "not-mp4.m4a"
        path.write_bytes(b"not a media file")
        with self.assertRaises(ValueError):
            self.muxer.tag(path, tags=Tags(title="Error"))

    def test_requires_tags_instance(self):
        with self.assertRaises(TypeError):
            self.muxer.tag(self.path, tags={"title": "not Tags"})

    def test_video_file_supported(self):
        video = Path(self.tmp.name) / "test.m4v"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:r=25:d=0.3",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-y",
                str(video),
            ],
            capture_output=True,
            check=True,
        )
        self.muxer.tag(video, tags=Tags(title="Music Video", media_type=6))
        boxes = mp4_tag_atoms(video)
        self.assertEqual(boxes[b"\xa9nam"], [(1, b"Music Video")])
        self.assertEqual(boxes[b"stik"], [(21, b"\x06")])
