import struct

import pytest

from gamdl.downloader.amdecrypt import (
    SampleInfo,
    SongInfo,
    mux_decrypted_mp4_tracks,
    write_decrypted_m4a,
    write_decrypted_mp4_track,
)


def _top_box(data: bytes, box_type: bytes) -> bytes:
    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        current = data[offset + 4 : offset + 8]
        assert size >= 8
        if current == box_type:
            return data[offset : offset + size]
        offset += size
    raise AssertionError(f"missing {box_type!r}")


def _mdat_payload(path) -> bytes:
    data = path.read_bytes()
    mdat = _top_box(data, b"mdat")
    return mdat[8:]


def test_native_m4a_writer_creates_flat_mp4(tmp_path):
    output = tmp_path / "song.m4a"
    payload = b"aaaabbbbcccc"
    track = SongInfo(
        samples=[
            SampleInfo(data=b"", duration=1024, desc_index=0, size=4),
            SampleInfo(data=b"", duration=1024, desc_index=0, size=8),
        ],
        handler_type=b"soun",
    )

    write_decrypted_m4a(str(output), track, payload)
    data = output.read_bytes()

    assert _top_box(data, b"ftyp")[8:12] == b"M4A "
    assert _top_box(data, b"moov")
    assert _mdat_payload(output) == payload


def test_native_mp4_track_mux_preserves_payload_order(tmp_path):
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    output = tmp_path / "out.mp4"
    video_payload = b"vvvvvvvv"
    audio_payload = b"aaaa"

    video = SongInfo(
        samples=[SampleInfo(data=b"", duration=3000, desc_index=0, size=8)],
        handler_type=b"vide",
    )
    audio = SongInfo(
        samples=[SampleInfo(data=b"", duration=1024, desc_index=0, size=4)],
        handler_type=b"soun",
    )

    write_decrypted_mp4_track(str(video_path), video, video_payload)
    write_decrypted_mp4_track(str(audio_path), audio, audio_payload)
    mux_decrypted_mp4_tracks(str(video_path), str(audio_path), str(output))

    data = output.read_bytes()
    assert _top_box(data, b"ftyp")[8:12] == b"mp42"
    assert _top_box(data, b"moov")
    assert _mdat_payload(output) == video_payload + audio_payload


def test_native_mp4_track_mux_rejects_missing_mdat(tmp_path):
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    output = tmp_path / "out.mp4"
    video_path.write_bytes(struct.pack(">I4s", 8, b"moov"))
    audio_path.write_bytes(struct.pack(">I4s", 8, b"moov"))

    with pytest.raises(OSError, match="missing mdat"):
        mux_decrypted_mp4_tracks(str(video_path), str(audio_path), str(output))
