"""Thin async shims for gamdl's native Rust media engine."""

from __future__ import annotations

import asyncio

from .. import _amdecrypt
from ..api.wrapper import WrapperApi


async def decrypt_and_mux_hex(
    decryption_key_audio: str,
    input_audio_path: str,
    output_path: str,
    decryption_key_video: str | None = None,
    input_video_path: str | None = None,
    *,
    use_cenc: bool = False,
    use_single_content_key: bool = False,
    m4v_brand: bool = False,
) -> None:
    """Decrypt local-key media and mux the final file in one Rust call."""
    await asyncio.to_thread(
        _amdecrypt.decrypt_and_mux_hex_native,
        decryption_key_audio,
        input_audio_path,
        output_path,
        decryption_key_video,
        input_video_path,
        use_cenc,
        use_single_content_key,
        m4v_brand,
    )


async def decrypt_and_mux_wrapper(
    wrapper_api: WrapperApi,
    track_id: str,
    input_audio_path: str,
    output_path: str,
    fairplay_key_audio: str,
    *,
    input_video_path: str | None = None,
    fairplay_key_video: str | None = None,
    use_single_content_key: bool = False,
    m4v_brand: bool = False,
) -> None:
    """Decrypt wrapper-v2 FairPlay media and mux the final file in one Rust call."""
    await asyncio.to_thread(
        _amdecrypt.decrypt_and_mux_wrapper_native,
        wrapper_api.decrypt_host,
        wrapper_api.decrypt_port,
        track_id,
        input_audio_path,
        output_path,
        fairplay_key_audio,
        input_video_path,
        fairplay_key_video,
        use_single_content_key,
        m4v_brand,
    )

