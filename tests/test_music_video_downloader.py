from types import SimpleNamespace

import pytest

from gamdl.downloader.music_video import AppleMusicMusicVideoDownloader
from gamdl.interface.types import DecryptionKey, DecryptionKeyAv


@pytest.mark.asyncio
async def test_music_video_stage_uses_hex_when_decryption_key_present(monkeypatch):
    calls = []

    async def fake_hex(*args, **kwargs):
        calls.append(("hex", args, kwargs))

    async def fake_wrapper(*args, **kwargs):
        calls.append(("wrapper", args, kwargs))

    monkeypatch.setattr("gamdl.downloader.music_video.decrypt_and_mux_hex", fake_hex)
    monkeypatch.setattr(
        "gamdl.downloader.music_video.decrypt_and_mux_wrapper", fake_wrapper
    )
    downloader = AppleMusicMusicVideoDownloader(base=SimpleNamespace())

    await downloader.stage(
        "video.mp4",
        "audio.m4a",
        "out.m4v",
        "12345",
        DecryptionKeyAv(
            video_track=DecryptionKey(key="video-key"),
            audio_track=DecryptionKey(key="audio-key"),
        ),
        "skd://video",
        "skd://audio",
        True,
    )

    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "hex"
    assert args[:5] == ("audio-key", "audio.m4a", "out.m4v", "video-key", "video.mp4")
    assert kwargs == {"m4v_brand": True}


@pytest.mark.asyncio
async def test_music_video_stage_uses_wrapper_without_decryption_key(monkeypatch):
    calls = []
    wrapper_api = object()
    base = SimpleNamespace(
        interface=SimpleNamespace(base=SimpleNamespace(wrapper_api=wrapper_api))
    )

    async def fake_hex(*args, **kwargs):
        calls.append(("hex", args, kwargs))

    async def fake_wrapper(*args, **kwargs):
        calls.append(("wrapper", args, kwargs))

    monkeypatch.setattr("gamdl.downloader.music_video.decrypt_and_mux_hex", fake_hex)
    monkeypatch.setattr(
        "gamdl.downloader.music_video.decrypt_and_mux_wrapper", fake_wrapper
    )
    downloader = AppleMusicMusicVideoDownloader(base=base)

    await downloader.stage(
        "video.mp4",
        "audio.m4a",
        "out.m4v",
        "12345",
        None,
        "skd://video",
        "skd://audio",
        True,
    )

    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "wrapper"
    assert args == (wrapper_api, "12345", "audio.m4a", "out.m4v")
    assert kwargs == {
        "fairplay_key_audio": "skd://audio",
        "input_video_path": "video.mp4",
        "fairplay_key_video": "skd://video",
        "use_single_content_key": True,
        "m4v_brand": True,
    }
