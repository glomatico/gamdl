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


def brands(path: Path) -> tuple[bytes, list[bytes]]:
    """Read major/compatible MP4 brands independently of FFprobe."""
    atom_type, body = next(atoms(path.read_bytes()))
    if atom_type != b"ftyp":
        raise AssertionError("Expected ftyp as first MP4 box")
    return body[:4], [body[offset : offset + 4] for offset in range(8, len(body), 4)]


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


def packet_hashes(path: Path):
    """Compare encoded samples, independent of container edit-list padding."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_data_hash",
            "md5",
            "-show_entries",
            "packet=size,data_hash",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [tuple(row.split(",")[:2]) for row in result.stdout.splitlines()]


def track_hashes(path: Path, kind: str) -> list[str]:
    """Verify lossless muxing by hashing encoded packets, not decoded frames."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            f"{kind}:0",
            "-show_packets",
            "-show_data_hash",
            "sha256",
            "-show_entries",
            "packet=data_hash",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.startswith("SHA256:")]


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for MP4 mux fixtures",
)
class NativeAvMuxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.video = self.directory / "video.mp4"
        self.audio = self.directory / "audio.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=duration=0.9:size=160x96:rate=24",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-x264-params",
                "bframes=2:keyint=12",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration",
                "180000",
                str(self.video),
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=900:sample_rate=48000:duration=0.9",
                "-c:a",
                "aac",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration",
                "150000",
                str(self.audio),
            ],
            capture_output=True,
            check=True,
        )

    def mux(self, video: bytes | None = None, audio: bytes | None = None) -> Path:
        output = self.directory / "muxed.mp4"
        output.write_bytes(
            Muxer().mux(
                video=self.video.read_bytes() if video is None else video,
                audio=self.audio.read_bytes() if audio is None else audio,
            )
        )
        return output

    def test_video_audio_lossless_mux_with_b_frames(self):
        output = self.mux()
        self.assertEqual(brands(output), (b"M4V ", [b"M4V ", b"mp42", b"isom"]))
        self.assertEqual(
            [kind for kind, _ in atoms(output.read_bytes())],
            [b"ftyp", b"moov", b"mdat"],
        )
        self.assertEqual(track_hashes(self.video, "v"), track_hashes(output, "v"))
        self.assertEqual(track_hashes(self.audio, "a"), track_hashes(output, "a"))
        self.assertGreater(len(track_hashes(output, "v")), 15)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(output),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=True,
        )

    def test_explicit_mp4_brand_keeps_encoded_samples_unchanged(self):
        default = self.mux()
        standard = self.directory / "standard.mp4"
        standard.write_bytes(
            Muxer().mux(
                video=self.video.read_bytes(),
                audio=self.audio.read_bytes(),
                container="mp4",
            )
        )
        self.assertEqual(brands(default)[0], b"M4V ")
        self.assertEqual(
            brands(standard),
            (b"mp42", [b"mp42", b"isom"]),
        )
        for kind in ("v", "a"):
            self.assertEqual(track_hashes(default, kind), track_hashes(standard, kind))
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(standard),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=True,
        )

    def test_mux_preserves_track_offset_with_edit_list(self):
        audio = bytearray(self.audio.read_bytes())
        position = 0
        count = 0
        while (position := audio.find(b"tfdt", position)) >= 0:
            # Keep fragment timestamps contiguous, but shift the whole audio
            # presentation by one second relative to the video.
            self.assertEqual(audio[position + 4], 1)
            at = position + 8
            value = int.from_bytes(audio[at : at + 8], "big")
            audio[at : at + 8] = (value + 48000).to_bytes(8, "big")
            count += 1
            position = at + 8
        self.assertGreater(count, 1)
        output = self.mux(audio=bytes(audio))
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=start_time",
                "-of",
                "default=nk=1:nw=1",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertGreater(float(result.stdout.strip()), 0.9)
        self.assertEqual(track_hashes(self.audio, "a"), track_hashes(output, "a"))

    def test_rejects_bad_or_encrypted_track_inputs(self):
        with self.assertRaisesRegex(TypeError, "bytes"):
            Muxer().mux(video="bad", audio=self.audio.read_bytes())
        with self.assertRaisesRegex(ValueError, "container must be"):
            Muxer().mux(
                video=self.video.read_bytes(),
                audio=self.audio.read_bytes(),
                container="mov",
            )
        with self.assertRaisesRegex(ValueError, "Malformed|Unsupported"):
            self.mux(video=b"not mp4")
        with self.assertRaisesRegex(ValueError, "Unexpected media track"):
            self.mux(video=self.audio.read_bytes())
        encrypted = self.video.read_bytes().replace(b"avc1", b"encv", 1)
        self.assertNotEqual(encrypted, self.video.read_bytes())
        with self.assertRaisesRegex(ValueError, "Encrypted or unsupported"):
            self.mux(video=encrypted)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for MP4 remux fixtures",
)
class NativeMp4RemuxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.muxer = Muxer()

    def fixture(self, codec):
        fragmented = self.directory / f"fragmented-{codec}.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=0.4",
                "-c:a",
                codec,
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration",
                "100000",
                "-y",
                str(fragmented),
            ],
            check=True,
            capture_output=True,
        )
        return fragmented

    def test_lossless_flatten_for_aac_and_alac(self):
        # FFmpeg's empty_moov muxer cannot emit the required E-AC-3 codec
        # initialization. Real E-AC-3/Atmos is exercised by the live suite.
        for codec, probe_codec in (("aac", "aac"), ("alac", "alac")):
            with self.subTest(codec=codec):
                original = self.fixture(codec)
                input_bytes = original.read_bytes()
                self.assertIn(b"moof", input_bytes)
                finished = self.directory / f"finished-{codec}.m4a"
                finished.write_bytes(self.muxer.remux(input_bytes))
                self.assertEqual(
                    brands(finished),
                    (b"M4A ", [b"M4A ", b"mp42", b"isom"]),
                )
                top = [kind for kind, _ in atoms(finished.read_bytes())]
                self.assertEqual(top, [b"ftyp", b"moov", b"mdat"])
                self.assertEqual(packet_hashes(original), packet_hashes(finished))
                result = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "default=nk=1:nw=1",
                        str(finished),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), probe_codec)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-i",
                        str(finished),
                        "-map",
                        "0:a:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    check=True,
                )

    def test_remux_then_tag(self):
        original = self.fixture("aac")
        dest = self.directory / "taggable.m4a"
        dest.write_bytes(self.muxer.remux(original.read_bytes()))
        self.muxer.tag(dest, tags=Tags(title="Remuxed AAC", artist="Gamdl"))
        self.assertEqual(mp4_tag_atoms(dest)[b"\xa9nam"], [(1, b"Remuxed AAC")])
        self.assertEqual(packet_hashes(original), packet_hashes(dest))

    def test_nonzero_fragment_decode_timestamps_and_discontinuity_rejection(self):
        original = self.fixture("aac")
        data = bytearray(original.read_bytes())
        positions = []
        at = 0
        while (at := data.find(b"tfdt", at)) >= 0:
            # Locate the timestamp in FFmpeg's version-1 tfdt full box.
            timestamp_at = at + 8
            if data[at + 4] != 1:
                self.skipTest("Expected a version-1 tfdt fixture")
            positions.append(timestamp_at)
            value = int.from_bytes(data[timestamp_at : timestamp_at + 8], "big")
            data[timestamp_at : timestamp_at + 8] = (value + 48000).to_bytes(8, "big")
            at = timestamp_at + 8
        self.assertGreater(len(positions), 1, "Fixture must have several fragments")
        output = self.directory / "nonzero.m4a"
        output.write_bytes(self.muxer.remux(bytes(data)))
        self.assertEqual(packet_hashes(original), packet_hashes(output))

        value = int.from_bytes(data[positions[1] : positions[1] + 8], "big")
        data[positions[1] : positions[1] + 8] = (value + 1).to_bytes(8, "big")
        with self.assertRaisesRegex(ValueError, "Unsupported fragmented MP4"):
            self.muxer.remux(bytes(data))

    def test_bad_and_unfragmented_inputs_fail_without_output(self):
        for data in (
            b"",
            b"not an mp4",
            b"\0" * 16,
            self.fixture("aac").read_bytes()[:80],
        ):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                self.muxer.remux(data)
        with self.assertRaises(TypeError):
            self.muxer.remux("not bytes")
        regular = self.directory / "regular.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "0.2",
                "-c:a",
                "aac",
                "-y",
                str(regular),
            ],
            capture_output=True,
            check=True,
        )
        with self.assertRaises(ValueError):
            self.muxer.remux(regular.read_bytes())


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

    def test_large_apple_music_ids_use_lossless_signed_64_bit_atoms(self):
        """Apple Music catalog IDs now exceed 32 bits (e.g. video 6805195215)."""
        self.muxer.tag(
            self.path,
            tags=Tags(
                title="Say Say Say",
                title_id=6805195215,
                artist_id=2**31,
                album_id=2**31,
                composer_id=2**32,
                genre_id=2**31 - 1,
                storefront=143503,
            ),
        )
        boxes = mp4_tag_atoms(self.path)
        for fourcc, value, width in (
            (b"cnID", 6805195215, 8),
            (b"atID", 2**31, 8),
            (b"plID", 2**31, 8),
            (b"cmID", 2**32, 8),
            (b"geID", 2**31 - 1, 4),
            (b"sfID", 143503, 4),
        ):
            with self.subTest(fourcc=fourcc):
                self.assertEqual(boxes[fourcc], [(21, value.to_bytes(width, "big"))])
        self.assertEqual(boxes[b"\xa9nam"], [(1, b"Say Say Say")])

    def test_signed_integer_boundary_preserves_4_byte_default(self):
        self.muxer.tag(self.path, tags=Tags(title_id=2**31 - 1))
        self.assertEqual(
            mp4_tag_atoms(self.path)[b"cnID"],
            [(21, (2**31 - 1).to_bytes(4, "big"))],
        )
        self.muxer.tag(self.path, tags=Tags(title_id=2**31))
        self.assertEqual(
            mp4_tag_atoms(self.path)[b"cnID"],
            [(21, (2**31).to_bytes(8, "big"))],
        )

    def test_jpeg_artwork_metadata(self):
        self.muxer.tag(self.path, tags=Tags(), cover_bytes=JPEG)
        self.assertEqual(mp4_tag_atoms(self.path)[b"covr"], [(13, JPEG)])

    def test_invalid_art_and_numbers_leave_input_untouched(self):
        before = self.path.read_bytes()
        for tag, cover in (
            (Tags(title="bad art"), b"not an image"),
            (Tags(track_number=65536), None),
            (Tags(title_id=2**63), None),
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
