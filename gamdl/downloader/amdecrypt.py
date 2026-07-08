"""
This is a modified version of https://github.com/sn0wst0rm/st0rmMusicPlayer/blob/main/scripts/amdecrypt.py
All the modifications made here were AI generated

FairPlay sample decryption talks to wrapper-v2 over the raw TCP decrypt port
while HTTP remains reserved for account/playback control calls.
"""

from __future__ import annotations

import asyncio
import io
import os
import struct
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional

import structlog

from Crypto.Cipher import AES

from .. import _amdecrypt
from ..api.wrapper import WrapperApi

logger = structlog.get_logger(__name__)

# Default decryption key for songs without per-sample keys (legacy AAC)
DEFAULT_SONG_DECRYPTION_KEY = b"2\xb8\xad\xe1v\x9e&\xb1\xff\xb8\x98cRy?\xc6"

# Pre-fetch key used for first sample description
PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"

# Max ciphertext blobs per TCP decrypt batch (same adam_id + uri). Increase for
# fewer round-trips; set to 1 if a given wrapper build mis-handles CBC between chunks.
WRAPPER_DECRYPT_BATCH_SIZE = 128

# wrapper-v2: use one SKD segment per ``adam_id``+``uri`` (do not interleave prefetch
# and main keys in one request). Flush pending ciphertexts before switching keys.


def _cbcs_ciphertext_for_sample(sample: SampleInfo) -> Optional[tuple[bytes, bytes]]:
    """Payload for FairPlay CBC: aligned decrypt input and trailing pass-through bytes.

    Matches ``decrypt_samples_hex``: all encrypted subsample runs in one MP4 sample
    are **concatenated**, then ``len & ~0xF`` bytes are decrypted as **one** CBC
    stream; the remainder is copied (see hex path). Clear subsample bytes must never
    be fed to the decryptor.

    Returns:
        ``None`` — no AES input for this sample (emit literal ``sample.data``).
        ``(aligned, tail)`` — ``aligned`` may be empty if only a short tail exists;
        then ``tail`` is the full encrypted concatenation (pass-through).
    """
    data = sample.data
    if not sample.subsamples:
        n = len(data)
        t = n & ~0xF
        tail = data[t:] if t < n else b""
        if t > 0:
            return (data[:t], tail)
        return None

    enc_cat = bytearray()
    offset = 0
    for clear_b, enc_b in sample.subsamples:
        offset += clear_b
        if enc_b:
            enc_cat.extend(data[offset : offset + enc_b])
            offset += enc_b
    if not enc_cat:
        return None
    blob = bytes(enc_cat)
    cbc_len = len(blob) & ~0xF
    tail = blob[cbc_len:]
    aligned = blob[:cbc_len]
    return (aligned, tail)


def _reassemble_cbcs_sample(sample: SampleInfo, plain: bytes, tail: bytes) -> bytes:
    """Place decrypted CBCS bytes back into the original MP4 sample layout."""
    full_dec = plain + tail
    data = sample.data
    if not sample.subsamples:
        if len(full_dec) != len(data):
            raise IOError(
                f"decrypted sample length mismatch: expected {len(data)}, got {len(full_dec)}"
            )
        return full_dec

    encrypted_total = sum(enc_b for _, enc_b in sample.subsamples)
    if len(full_dec) != encrypted_total:
        raise IOError(
            "decrypted subsample length mismatch: "
            f"expected {encrypted_total}, got {len(full_dec)}"
        )

    out = bytearray()
    dec_off = 0
    offset = 0
    for clear_b, enc_b in sample.subsamples:
        if clear_b:
            out.extend(data[offset : offset + clear_b])
            offset += clear_b
        if enc_b:
            out.extend(full_dec[dec_off : dec_off + enc_b])
            dec_off += enc_b
            offset += enc_b
    if offset < len(data):
        out.extend(data[offset:])
    if len(out) != len(data):
        raise IOError(
            f"reassembled sample length mismatch: expected {len(data)}, got {len(out)}"
        )
    return bytes(out)


def _append_reassembled_sample(
    decrypted_data: bytearray, sample: SampleInfo, plain: bytes, tail: bytes
) -> None:
    """Append one decrypted CBCS sample to the output stream."""
    decrypted_data.extend(_reassemble_cbcs_sample(sample, plain, tail))


def _sample_size(sample: SampleInfo) -> int:
    """Return sample payload size even after payload bytes have been released."""
    return sample.size or len(sample.data)


def _sample_data(sample: SampleInfo) -> bytes:
    """Return sample payload bytes, loading file-backed samples on demand."""
    if sample.data:
        return sample.data
    if sample.data_path and sample.size:
        with open(sample.data_path, "rb") as f:
            f.seek(sample.data_offset)
            data = f.read(sample.size)
        if len(data) != sample.size:
            raise IOError(
                f"unexpected EOF while reading sample at {sample.data_offset} "
                f"from {sample.data_path}"
            )
        return data
    return sample.data


def _with_sample_data(sample: SampleInfo, data: bytes) -> SampleInfo:
    """Return a copy of a sample with materialized payload bytes."""
    return SampleInfo(
        data=data,
        duration=sample.duration,
        desc_index=sample.desc_index,
        iv=sample.iv,
        subsamples=sample.subsamples,
        composition_time_offset=sample.composition_time_offset,
        sample_flags=sample.sample_flags,
        is_sync=sample.is_sync,
        size=sample.size or len(data),
        data_path=sample.data_path,
        data_offset=sample.data_offset,
    )


def _decrypt_cbcs_sample_with_key(
    sample: SampleInfo, key: bytes, enc_info: EncryptionInfo
) -> bytes:
    """Decrypt one CBCS sample with a raw AES key."""
    if enc_info.crypt_byte_block and enc_info.skip_byte_block:
        return _decrypt_cbcs_sample_with_pattern(sample, key, enc_info)

    parts = _cbcs_ciphertext_for_sample(sample)
    if parts is None:
        return sample.data

    aligned, tail = parts
    plain = b""
    if aligned:
        iv = sample.iv if sample.iv else enc_info.constant_iv
        if len(iv) < 16:
            iv = iv + b"\x00" * (16 - len(iv))
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        plain = cipher.decrypt(aligned)
    return _reassemble_cbcs_sample(sample, plain, tail)


def _decrypt_cbcs_protected_range_with_pattern(
    protected_data: bytes, key: bytes, iv: bytes, crypt_blocks: int, skip_blocks: int
) -> bytes:
    """Decrypt one CBCS pattern-encrypted protected byte stream."""
    if crypt_blocks <= 0:
        return protected_data
    if len(iv) < 16:
        iv = iv + b"\x00" * (16 - len(iv))

    out = bytearray()
    offset = 0
    crypt_bytes = crypt_blocks * 16
    skip_bytes = skip_blocks * 16
    next_iv = iv

    while offset < len(protected_data):
        remaining = len(protected_data) - offset
        crypt_window = min(crypt_bytes, remaining)
        aligned_crypt_len = crypt_window & ~0xF

        if aligned_crypt_len:
            ciphertext = protected_data[offset : offset + aligned_crypt_len]
            cipher = AES.new(key, AES.MODE_CBC, iv=next_iv)
            out.extend(cipher.decrypt(ciphertext))
            next_iv = ciphertext[-16:]
            offset += aligned_crypt_len

        crypt_tail_len = crypt_window - aligned_crypt_len
        if crypt_tail_len:
            out.extend(protected_data[offset : offset + crypt_tail_len])
            offset += crypt_tail_len

        if offset >= len(protected_data):
            break

        clear_len = min(skip_bytes, len(protected_data) - offset)
        if clear_len:
            out.extend(protected_data[offset : offset + clear_len])
            offset += clear_len
        else:
            # Avoid spinning forever if a malformed tenc reports no skip phase.
            remaining = len(protected_data) - offset
            aligned_crypt_len = remaining & ~0xF
            if aligned_crypt_len:
                ciphertext = protected_data[offset : offset + aligned_crypt_len]
                cipher = AES.new(key, AES.MODE_CBC, iv=next_iv)
                out.extend(cipher.decrypt(ciphertext))
                next_iv = ciphertext[-16:]
                offset += aligned_crypt_len
            if offset < len(protected_data):
                out.extend(protected_data[offset:])
                break

    return bytes(out)


def _decrypt_cbcs_sample_with_pattern(
    sample: SampleInfo, key: bytes, enc_info: EncryptionInfo
) -> bytes:
    """Decrypt CBCS pattern-encrypted samples, preserving skipped video blocks.

    CBCS pattern state and AES-CBC state reset for each protected subsample
    range. Carrying either one into the next range corrupts later video regions.
    """
    iv = sample.iv if sample.iv else enc_info.constant_iv
    if not sample.subsamples:
        return _decrypt_cbcs_protected_range_with_pattern(
            sample.data,
            key,
            iv,
            enc_info.crypt_byte_block,
            enc_info.skip_byte_block,
        )

    out = bytearray()
    offset = 0
    for clear_bytes, encrypted_bytes in sample.subsamples:
        if clear_bytes:
            out.extend(sample.data[offset : offset + clear_bytes])
            offset += clear_bytes
        if encrypted_bytes:
            protected_data = sample.data[offset : offset + encrypted_bytes]
            out.extend(
                _decrypt_cbcs_protected_range_with_pattern(
                    protected_data,
                    key,
                    iv,
                    enc_info.crypt_byte_block,
                    enc_info.skip_byte_block,
                )
            )
            offset += encrypted_bytes
    if offset < len(sample.data):
        out.extend(sample.data[offset:])
    return bytes(out)


@dataclass
class SampleInfo:
    """Information about a single media sample."""

    data: bytes
    duration: int
    desc_index: int
    iv: bytes = b""  # Per-sample IV from senc (empty if constant IV)
    subsamples: List[tuple] = field(
        default_factory=list
    )  # [(clear_bytes, encrypted_bytes), ...]
    composition_time_offset: int = 0
    sample_flags: int = 0
    is_sync: bool = True
    size: int = 0
    data_path: Optional[str] = None
    data_offset: int = 0


@dataclass
class EncryptionInfo:
    """Encryption scheme info extracted from sinf/schm + sinf/schi/tenc."""

    scheme_type: str = "cbcs"  # 'cenc' or 'cbcs'
    crypt_byte_block: int = 0  # CBCS pattern encrypted 16-byte blocks
    skip_byte_block: int = 0  # CBCS pattern clear 16-byte blocks
    per_sample_iv_size: int = 0  # 0, 8, or 16
    constant_iv: bytes = b""  # Constant IV (when per_sample_iv_size == 0)
    kid: bytes = b""  # Default Key ID (16 bytes)


@dataclass
class SongInfo:
    """Extracted song information from MP4 file."""

    samples: List[SampleInfo] = field(default_factory=list)
    moov_data: bytes = b""
    ftyp_data: bytes = b""
    encryption_info: Optional[EncryptionInfo] = None
    handler_type: bytes = b"soun"
    track_id: int = 1


@dataclass
class DecryptedTrack:
    """A decrypted media track and the source metadata needed to write it."""

    input_path: str
    track_info: SongInfo
    data: bytes = b""
    data_path: Optional[str] = None
    data_size: int = 0


@dataclass
class DecryptedMedia:
    """Decrypted audio, optional video, and optional timed text tracks."""

    audio: DecryptedTrack
    video: Optional[DecryptedTrack] = None
    captions: List[DecryptedTrack] = field(default_factory=list)


def _format_handler_type(handler_type: bytes) -> str:
    return handler_type.decode("ascii", errors="replace")


def read_box_header(f: BinaryIO) -> tuple[int, str, int]:
    """Read MP4 box header, return (size, type, header_size)."""
    header = f.read(8)
    if len(header) < 8:
        return 0, "", 0

    size = struct.unpack(">I", header[:4])[0]
    box_type = header[4:8].decode("ascii", errors="replace")
    header_size = 8

    if size == 1:  # Extended size
        ext_size = f.read(8)
        size = struct.unpack(">Q", ext_size)[0]
        header_size = 16
    elif size == 0:  # Box extends to end of file
        pos = f.tell()
        f.seek(0, 2)  # Seek to end
        size = f.tell() - pos + header_size
        f.seek(pos)

    return size, box_type, header_size


def find_box(data: bytes, box_path: List[str]) -> Optional[bytes]:
    """Find a box in MP4 data by path (e.g., ['moov', 'trak', 'mdia'])."""
    f = io.BytesIO(data)

    for target_type in box_path:
        found = False
        while True:
            pos = f.tell()
            size, box_type, header_size = read_box_header(f)
            if size == 0:
                break

            if box_type == target_type:
                f.seek(pos + header_size)  # Skip past header
                found = True
                break
            else:
                f.seek(pos + size)  # Skip this box

        if not found:
            return None

    # Return remaining data from current position
    return f.read()


def extract_song(
    input_path: str,
    handler_type: bytes = b"soun",
    file_backed_samples: bool = False,
) -> SongInfo:
    """
    Extract media samples and metadata from encrypted MP4 file.

    This parses the MP4 structure to extract:
    - ftyp and moov boxes (for reassembly)
    - Individual audio samples from mdat boxes
    - Sample durations and description indices from moof boxes
    """
    song_info = SongInfo(handler_type=handler_type)

    # First pass: collect all top-level boxes
    boxes = []
    if file_backed_samples:
        file_size = os.path.getsize(input_path)
        with open(input_path, "rb") as f:
            offset = 0
            while offset + 8 <= file_size:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8].decode("ascii", errors="replace")
                header_size = 8
                if size == 0:
                    size = file_size - offset
                elif size == 1:
                    ext_size = f.read(8)
                    if len(ext_size) < 8:
                        break
                    size = struct.unpack(">Q", ext_size)[0]
                    header_size = 16
                if size < header_size or offset + size > file_size:
                    break

                data = b""
                if box_type in ("ftyp", "moov", "moof"):
                    f.seek(offset)
                    data = f.read(size)
                boxes.append(
                    {
                        "offset": offset,
                        "size": size,
                        "type": box_type,
                        "header_size": header_size,
                        "data": data,
                    }
                )
                offset += size
    else:
        with open(input_path, "rb") as f:
            raw_data = f.read()

        offset = 0
        while offset < len(raw_data) - 8:
            size = struct.unpack(">I", raw_data[offset : offset + 4])[0]
            box_type = raw_data[offset + 4 : offset + 8].decode(
                "ascii", errors="replace"
            )

            header_size = 8
            if size == 0:
                break
            if size == 1:
                # Extended size
                if offset + 16 > len(raw_data):
                    break
                size = struct.unpack(">Q", raw_data[offset + 8 : offset + 16])[0]
                header_size = 16

            boxes.append(
                {
                    "offset": offset,
                    "size": size,
                    "type": box_type,
                    "header_size": header_size,
                    "data": raw_data[offset : offset + size],
                }
            )
            offset += size

    # Extract ftyp and moov
    for box in boxes:
        if box["type"] == "ftyp":
            song_info.ftyp_data = box["data"]
        elif box["type"] == "moov":
            song_info.moov_data = box["data"]

    # Determine which track carries the requested media handler.
    track_id = (
        _extract_track_id(song_info.moov_data, handler_type, 0)
        if song_info.moov_data
        else 0
    )
    song_info.track_id = track_id
    if track_id == 0:
        return song_info

    # Get default sample info from trex (inside moov/mvex)
    trex_defaults = (
        _extract_trex_defaults(song_info.moov_data, track_id)
        if song_info.moov_data
        else None
    )
    if trex_defaults:
        default_sample_duration = trex_defaults["default_sample_duration"]
        default_sample_size = trex_defaults["default_sample_size"]
        default_sample_flags = trex_defaults["default_sample_flags"]
    else:
        # Fallback defaults. ALAC typically uses 4096 samples per frame,
        # while AAC uses 1024. Default to 4096 if the track contains 'alac'.
        is_alac = (
            handler_type == b"soun"
            and song_info.moov_data
            and b"alac" in song_info.moov_data
        )
        default_sample_duration = (
            4096 if is_alac else (1024 if handler_type == b"soun" else 0)
        )
        default_sample_size = 0
        default_sample_flags = 0
    # Extract encryption scheme info from moov (sinf/schm + sinf/schi/tenc)
    if song_info.moov_data:
        song_info.encryption_info = _extract_encryption_info(
            song_info.moov_data, handler_type
        )

    # Parse moof/mdat pairs
    moof_box = None
    for box in boxes:
        if box["type"] == "moof":
            moof_box = box
        elif box["type"] == "mdat" and moof_box is not None:
            # Parse this moof/mdat pair
            moof_data = moof_box["data"]
            if file_backed_samples:
                mdat_data = b""
                mdat_data_size = box["size"] - box["header_size"]
            else:
                mdat_data = box["data"][box["header_size"] :]  # Skip mdat header
                mdat_data_size = len(mdat_data)

            # Parse moof for tfhd (sample description index, defaults) and trun (entries)
            _iv_size = (
                song_info.encryption_info.per_sample_iv_size
                if song_info.encryption_info
                else 0
            )
            samples_from_pair = _parse_moof_mdat(
                moof_data,
                mdat_data,
                default_sample_duration,
                default_sample_size,
                default_sample_flags,
                audio_track_id=track_id,
                moof_offset=moof_box["offset"],
                mdat_data_offset=box["offset"] + box["header_size"],
                per_sample_iv_size=_iv_size,
                mdat_data_size=mdat_data_size,
                mdat_source_path=input_path if file_backed_samples else None,
            )
            song_info.samples.extend(samples_from_pair)
            moof_box = None

    # Post-process samples: if this is ALAC, ensure all samples have duration 4096.
    # Apple Music fragments often report 1024 in trex/tfhd defaults, but
    # ALAC frames are actually 4096 samples long. This mismatch is the
    # root cause of the 1:16 duration reporting for 5-minute tracks.
    is_alac = (
        handler_type == b"soun"
        and song_info.moov_data
        and (b"alac" in song_info.moov_data or b"ALAC" in song_info.moov_data)
    )
    if is_alac:
        for sample in song_info.samples:
            # Only override if it was 0 or the common incorrect default of 1024
            if sample.duration in (0, 1024):
                sample.duration = 4096

    return song_info


def _parse_moof_mdat(
    moof_data: bytes,
    mdat_data: bytes,
    default_sample_duration: int,
    default_sample_size: int,
    default_sample_flags: int = 0,
    audio_track_id: int = 1,
    moof_offset: int = 0,
    mdat_data_offset: int = 0,
    per_sample_iv_size: int = 0,
    mdat_data_size: Optional[int] = None,
    mdat_source_path: Optional[str] = None,
) -> List[SampleInfo]:
    """Parse a moof box and extract samples from corresponding mdat.

    Handles multi-track fragmented MP4s by only extracting samples from
    the traf matching the audio track ID.

    Args:
        audio_track_id: Track ID of the audio track to extract.
        moof_offset: Absolute file offset of the moof box.
        mdat_data_offset: Absolute file offset of the mdat content (after header).
        per_sample_iv_size: IV size per sample from tenc (0, 8, or 16).
    """
    samples = []
    available_mdat_bytes = len(mdat_data) if mdat_data_size is None else mdat_data_size

    # Simple box parsing inside moof
    offset = 8  # Skip moof header
    while offset < len(moof_data) - 8:
        size = struct.unpack(">I", moof_data[offset : offset + 4])[0]
        box_type = moof_data[offset + 4 : offset + 8].decode("ascii", errors="replace")

        if size == 0 or offset + size > len(moof_data):
            break

        if box_type == "traf":
            # Parse inside traf with per-traf state
            tfhd_info = {
                "track_id": 0,
                "desc_index": 0,
                "default_duration": default_sample_duration,
                "default_size": default_sample_size,
                "default_sample_flags": default_sample_flags,
                "flags": 0,
                "base_data_offset": None,
            }
            # Each 'trun' has its own optional data_offset; concatenating entry lists
            # and keeping only the first data_offset breaks multi-trun fragments.
            trun_runs: List[tuple] = []
            raw_senc_data: bytes | None = None

            traf_offset = offset + 8
            traf_end = offset + size
            while traf_offset < traf_end - 8:
                inner_size = struct.unpack(
                    ">I", moof_data[traf_offset : traf_offset + 4]
                )[0]
                inner_type = moof_data[traf_offset + 4 : traf_offset + 8].decode(
                    "ascii", errors="replace"
                )

                if inner_size == 0:
                    break

                if inner_type == "tfhd":
                    _parse_tfhd(
                        moof_data[traf_offset + 8 : traf_offset + inner_size], tfhd_info
                    )
                elif inner_type == "trun":
                    entries, data_off = _parse_trun(
                        moof_data[traf_offset + 8 : traf_offset + inner_size], tfhd_info
                    )
                    trun_runs.append((entries, data_off))
                elif inner_type == "senc":
                    raw_senc_data = moof_data[
                        traf_offset + 8 : traf_offset + inner_size
                    ]

                traf_offset += inner_size

            # Only process this traf if it matches the audio track
            if tfhd_info["track_id"] != audio_track_id:
                offset += size
                continue

            base = tfhd_info.get("base_data_offset")
            if base is None:
                base = moof_offset  # Default: first byte of containing moof

            desc_index = tfhd_info["desc_index"]
            if desc_index > 0:
                desc_index -= 1  # Convert to 0-indexed

            sample_sizes = [
                entry.get("size", tfhd_info["default_size"])
                for trun_entries, _ in trun_runs
                for entry in trun_entries
            ]
            senc_entries = (
                _parse_senc_for_sample_sizes(
                    raw_senc_data,
                    sample_sizes,
                    per_sample_iv_size,
                )
                if raw_senc_data is not None
                else []
            )

            mdat_pos: Optional[int] = None
            sample_index_in_traf = 0

            for trun_entries, trun_data_off in trun_runs:
                if trun_data_off is not None:
                    mdat_pos = base + trun_data_off - mdat_data_offset
                elif mdat_pos is None:
                    # Legacy behaviour: first trun without data_offset starts at mdat payload 0.
                    mdat_pos = 0
                mdat_pos = max(0, mdat_pos)
                mdat_read_offset = mdat_pos

                for entry in trun_entries:
                    sample_size = entry.get("size", tfhd_info["default_size"])
                    sample_duration = entry.get(
                        "duration", tfhd_info["default_duration"]
                    )
                    sample_flags = entry.get(
                        "sample_flags", tfhd_info["default_sample_flags"]
                    )

                    if (
                        sample_size > 0
                        and mdat_read_offset + sample_size <= available_mdat_bytes
                    ):
                        sample_iv = b""
                        sample_subsamples: List[tuple] = []
                        if sample_index_in_traf < len(senc_entries):
                            sample_iv = senc_entries[sample_index_in_traf]["iv"]
                            sample_subsamples = senc_entries[sample_index_in_traf][
                                "subsamples"
                            ]
                        if mdat_source_path:
                            sample_data = b""
                            sample_data_offset = mdat_data_offset + mdat_read_offset
                        else:
                            sample_data = mdat_data[
                                mdat_read_offset : mdat_read_offset + sample_size
                            ]
                            sample_data_offset = 0

                        sample = SampleInfo(
                            data=sample_data,
                            duration=sample_duration,
                            desc_index=desc_index,
                            iv=sample_iv,
                            subsamples=sample_subsamples,
                            composition_time_offset=entry.get(
                                "composition_time_offset", 0
                            ),
                            sample_flags=sample_flags,
                            is_sync=not bool(sample_flags & 0x10000),
                            size=sample_size,
                            data_path=mdat_source_path,
                            data_offset=sample_data_offset,
                        )
                        samples.append(sample)
                        mdat_read_offset += sample_size
                    # One senc row per trun sample entry (even if size 0 or read fails)
                    sample_index_in_traf += 1

                mdat_pos = mdat_read_offset

        offset += size

    return samples


def _parse_tfhd(data: bytes, tfhd_info: dict):
    """Parse track fragment header box (FullBox: version + flags + content)."""
    if len(data) < 8:  # version(1) + flags(3) + track_id(4)
        return

    # FullBox: version(1) + flags(3)
    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    tfhd_info["flags"] = flags

    # After version+flags is track_id(4)
    tfhd_info["track_id"] = struct.unpack(">I", data[4:8])[0]
    offset = 4 + 4  # version+flags + track_id

    if flags & 0x01 and offset + 8 <= len(data):  # base_data_offset
        tfhd_info["base_data_offset"] = struct.unpack(">Q", data[offset : offset + 8])[
            0
        ]
        offset += 8
    if flags & 0x02 and offset + 4 <= len(data):  # sample_description_index
        tfhd_info["desc_index"] = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
    if flags & 0x08 and offset + 4 <= len(data):  # default_sample_duration
        tfhd_info["default_duration"] = struct.unpack(">I", data[offset : offset + 4])[
            0
        ]
        offset += 4
    if flags & 0x10 and offset + 4 <= len(data):  # default_sample_size
        tfhd_info["default_size"] = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
    if flags & 0x20 and offset + 4 <= len(data):  # default_sample_flags
        tfhd_info["default_sample_flags"] = struct.unpack(
            ">I", data[offset : offset + 4]
        )[0]


def _parse_trun(data: bytes, tfhd_info: dict) -> tuple[List[dict], Optional[int]]:
    """Parse track run box to get sample entries and data_offset.

    Returns:
        Tuple of (entries, data_offset). data_offset is the signed offset from
        base_data_offset to the first sample's data, or None if not present.
    """
    entries = []
    data_offset_value = None
    if len(data) < 8:  # version(1) + flags(3) + sample_count(4)
        return entries, data_offset_value

    # FullBox: version(1) + flags(3)
    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    sample_count = struct.unpack(">I", data[4:8])[0]

    # Start reading entries after header fields
    offset = 8  # version+flags(4) + sample_count(4)
    if flags & 0x01:  # data_offset present
        data_offset_value = struct.unpack(">i", data[offset : offset + 4])[0]
        offset += 4
    first_sample_flags = None
    if flags & 0x04:  # first_sample_flags present
        first_sample_flags = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

    for sample_index in range(sample_count):
        entry = {}
        if flags & 0x100 and offset + 4 <= len(data):  # sample_duration
            entry["duration"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x200 and offset + 4 <= len(data):  # sample_size
            entry["size"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x400 and offset + 4 <= len(data):  # sample_flags
            entry["sample_flags"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        elif sample_index == 0 and first_sample_flags is not None:
            entry["sample_flags"] = first_sample_flags
        if flags & 0x800 and offset + 4 <= len(data):  # sample_composition_time_offset
            if version == 1:
                entry["composition_time_offset"] = struct.unpack(
                    ">i", data[offset : offset + 4]
                )[0]
            else:
                entry["composition_time_offset"] = struct.unpack(
                    ">I", data[offset : offset + 4]
                )[0]
            offset += 4
        entries.append(entry)

    return entries, data_offset_value


def _parse_senc(data: bytes, per_sample_iv_size: int) -> List[dict]:
    """Parse Sample Encryption Box (senc) content (after box header).

    Returns a list of dicts, one per sample:
        {"iv": bytes, "subsamples": [(clear_bytes, encrypted_bytes), ...]}

    The data starts after the 8-byte box header (size+type) but includes
    the FullBox header (version 1 byte + flags 3 bytes).

    per_sample_iv_size can be 0 (cbcs with constant IV from tenc) — in that case
    there are 0 IV bytes per sample but subsample info may still be present.
    """
    if len(data) < 8:
        return []

    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    sample_count = struct.unpack(">I", data[4:8])[0]

    entries: List[dict] = []
    offset = 8
    for _ in range(sample_count):
        # Read per-sample IV (0 bytes when per_sample_iv_size == 0)
        iv = b""
        if per_sample_iv_size > 0:
            if offset + per_sample_iv_size > len(data):
                break
            iv = data[offset : offset + per_sample_iv_size]
            offset += per_sample_iv_size

        subsamples = []
        if flags & 0x02:
            # Subsample encryption info present
            if offset + 2 > len(data):
                break
            subsample_count = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            for _ in range(subsample_count):
                if offset + 6 > len(data):
                    break
                clear_bytes = struct.unpack(">H", data[offset : offset + 2])[0]
                encrypted_bytes = struct.unpack(">I", data[offset + 2 : offset + 6])[0]
                subsamples.append((clear_bytes, encrypted_bytes))
                offset += 6

        entries.append({"iv": iv, "subsamples": subsamples})

    return entries


def _parse_senc_strict(
    data: bytes,
    per_sample_iv_size: int,
    sample_sizes: List[int],
) -> tuple[List[dict], bool, str]:
    """Parse senc and reject rows whose subsamples cannot fit their samples."""
    if len(data) < 8:
        return [], False, "truncated_header"

    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    sample_count = struct.unpack(">I", data[4:8])[0]
    if sample_count > len(sample_sizes):
        return [], False, f"sample_count_gt_trun:{sample_count}>{len(sample_sizes)}"

    entries: List[dict] = []
    offset = 8
    for sample_index in range(sample_count):
        iv = b""
        if per_sample_iv_size > 0:
            if offset + per_sample_iv_size > len(data):
                return [], False, f"truncated_iv:index={sample_index}"
            iv = data[offset : offset + per_sample_iv_size]
            offset += per_sample_iv_size

        subsamples = []
        if flags & 0x02:
            if offset + 2 > len(data):
                return [], False, f"truncated_subsample_count:index={sample_index}"
            subsample_count = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            sample_size = sample_sizes[sample_index]
            total_bytes = 0
            for _ in range(subsample_count):
                if offset + 6 > len(data):
                    return [], False, f"truncated_subsample:index={sample_index}"
                clear_bytes = struct.unpack(">H", data[offset : offset + 2])[0]
                encrypted_bytes = struct.unpack(">I", data[offset + 2 : offset + 6])[0]
                total_bytes += clear_bytes + encrypted_bytes
                if total_bytes > sample_size:
                    return (
                        [],
                        False,
                        "subsample_total_gt_sample:"
                        f"index={sample_index},total={total_bytes},sample={sample_size},"
                        f"clear={clear_bytes},enc={encrypted_bytes},count={subsample_count}",
                    )
                subsamples.append((clear_bytes, encrypted_bytes))
                offset += 6

        entries.append({"iv": iv, "subsamples": subsamples})

    return entries, True, "ok"


def _parse_senc_for_sample_sizes(
    data: bytes,
    sample_sizes: List[int],
    preferred_iv_size: int,
) -> List[dict]:
    """Parse senc using the preferred IV size, falling back when it is impossible."""
    iv_size_candidates = []
    for iv_size in (preferred_iv_size, 8, 16, 0):
        if iv_size not in iv_size_candidates:
            iv_size_candidates.append(iv_size)

    for iv_size in iv_size_candidates:
        entries, valid, reason = _parse_senc_strict(data, iv_size, sample_sizes)
        if valid:
            return entries

    logger.warning(
        "senc_parse_failed_validation",
        preferred_iv_size=preferred_iv_size,
        data_len=len(data),
        sample_count=len(sample_sizes),
        last_reason=reason,
    )
    return []


async def decrypt_samples(
    wrapper_api: WrapperApi,
    track_id: str,
    fairplay_key: str,
    samples: List[SampleInfo],
    encryption_info: EncryptionInfo,
    encryption_info_per_desc: Optional[dict] = None,
    *,
    use_single_content_key: bool = False,
    progress_callback=None,
    decrypted_data_path: Optional[str] = None,
) -> bytes:
    """
    Send track-key samples to wrapper-v2 over raw TCP for CBCS decryption and
    decrypt default prefetch-key samples locally.

    Ciphertext is sent in batches of up to :data:`WRAPPER_DECRYPT_BATCH_SIZE` MP4 samples
    per request (same ``adam_id`` and ``uri``). Literal or tail-only samples are applied
    between batches so output order matches the input.

    Subsample layout matches ``decrypt_samples_hex`` (concat encrypted runs, decrypt
    ``len & ~0xF``, reassemble).

    Requires an authenticated wrapper-v2 session (POST /login or restored session).

    Args:
        wrapper_api: Authenticated :class:`~gamdl.api.wrapper.WrapperApi` client
        use_single_content_key: When ``False`` (default), description 0 uses the built-in
            prefetch key locally and description 1+ uses ``fairplay_key`` via the wrapper.
            When ``True``, every sample uses ``fairplay_key`` through the wrapper only.
        progress_callback: Optional callback(current_sample, total_samples, bytes_processed, speed)
    """
    keys = [fairplay_key] if use_single_content_key else [PREFETCH_KEY, fairplay_key]
    decrypted_data = bytearray()
    decrypted_output = open(decrypted_data_path, "wb") if decrypted_data_path else None
    decrypted_bytes = 0
    last_desc_index: int = 255
    total_samples = len(samples)
    bytes_processed = 0
    start_time = time.time()
    last_progress_time = start_time

    segment_adam: Optional[str] = None
    segment_uri: Optional[str] = None
    # Pending (sample, aligned_cbc, tail) for one SKD segment, flushed in batches.
    crypto_batch: List[tuple] = []
    wrapper_decrypt_session = _amdecrypt.WrapperDecryptSession(
        wrapper_api.decrypt_host,
        wrapper_api.decrypt_port,
    )

    def emit(data: bytes) -> None:
        nonlocal decrypted_bytes
        if decrypted_output:
            decrypted_output.write(data)
        else:
            decrypted_data.extend(data)
        decrypted_bytes += len(data)

    async def flush_crypto_batch() -> None:
        if not crypto_batch:
            return
        if segment_adam is None or segment_uri is None:
            raise IOError("wrapper-v2: internal error (segment without adam/uri)")
        native_items = [
            (sample.data, aligned, tail, sample.subsamples)
            for sample, aligned, tail in crypto_batch
        ]
        reassembled = await asyncio.to_thread(
            wrapper_decrypt_session.decrypt_reassemble,
            segment_adam,
            segment_uri,
            native_items,
        )
        if len(reassembled) != len(crypto_batch):
            raise IOError("wrapper-v2: plaintext batch count mismatch")
        for sample_data in reassembled:
            emit(sample_data)
        crypto_batch.clear()

    try:
        for i, original_sample in enumerate(samples):
            sample = (
                _with_sample_data(original_sample, _sample_data(original_sample))
                if not original_sample.data and original_sample.data_path
                else original_sample
            )
            if last_desc_index != sample.desc_index:
                await flush_crypto_batch()
                if use_single_content_key:
                    segment_adam = track_id
                    segment_uri = fairplay_key
                else:
                    key_uri = keys[min(sample.desc_index, len(keys) - 1)]
                    segment_adam = "0" if key_uri == PREFETCH_KEY else track_id
                    segment_uri = key_uri
                last_desc_index = sample.desc_index

            if not use_single_content_key and segment_adam == "0":
                await flush_crypto_batch()
                enc_info = (
                    encryption_info_per_desc.get(sample.desc_index)
                    if encryption_info_per_desc
                    and sample.desc_index in encryption_info_per_desc
                    else encryption_info
                )
                emit(
                    _decrypt_cbcs_sample_with_key(
                        sample, DEFAULT_SONG_DECRYPTION_KEY, enc_info
                    )
                )
                bytes_processed += _sample_size(sample)
                now = time.time()
                if progress_callback and (
                    i % 50 == 0
                    or now - last_progress_time > 0.5
                    or i == total_samples - 1
                ):
                    elapsed = now - start_time
                    speed = bytes_processed / elapsed if elapsed > 0 else 0
                    progress_callback(i + 1, total_samples, bytes_processed, speed)
                    last_progress_time = now
                continue

            enc_info = (
                encryption_info_per_desc.get(sample.desc_index)
                if encryption_info_per_desc
                and sample.desc_index in encryption_info_per_desc
                else encryption_info
            )
            if enc_info.crypt_byte_block and enc_info.skip_byte_block:
                raise IOError(
                    "wrapper-v2 pattern CBCS decrypt is not supported by gamdl's "
                    "batch decrypt path; use hex-key decrypt for this track"
                )

            parts = _cbcs_ciphertext_for_sample(sample)
            if parts is None:
                await flush_crypto_batch()
                emit(sample.data)
            else:
                aligned, tail = parts
                if len(aligned) == 0:
                    await flush_crypto_batch()
                    emit(_reassemble_cbcs_sample(sample, b"", tail))
                else:
                    crypto_batch.append((sample, aligned, tail))
                    if len(crypto_batch) >= WRAPPER_DECRYPT_BATCH_SIZE:
                        await flush_crypto_batch()

            bytes_processed += _sample_size(sample)

            now = time.time()
            if progress_callback and (
                i % 50 == 0 or now - last_progress_time > 0.5 or i == total_samples - 1
            ):
                elapsed = now - start_time
                speed = bytes_processed / elapsed if elapsed > 0 else 0
                progress_callback(i + 1, total_samples, bytes_processed, speed)
                last_progress_time = now

        await flush_crypto_batch()
    finally:
        wrapper_decrypt_session.close()
        if decrypted_output:
            decrypted_output.close()

    logger.debug(f"Decrypted {len(samples)} samples ({decrypted_bytes} bytes)")
    return bytes(decrypted_data)


def write_decrypted_m4a(
    output_path: str,
    song_info: SongInfo,
    decrypted_data: bytes,
    original_path: str = None,
    decrypted_data_path: str | None = None,
) -> None:
    """
    Write decrypted MP4 file as non-fragmented MP4.

    Creates a new MP4 from scratch with:
    - ftyp box (M4A compatible)
    - moov box with proper sample tables (stts, stsc, stsz, stco)
    - Single mdat box with all decrypted samples

    This matches the output format of Go's amdecrypt which is required
    for ALAC playback.
    """
    # Extract original boxes for faithful reproduction
    # Note: _extract_stsd_content automatically cleans encryption metadata
    stsd_content = None
    orig_mvhd = None
    orig_tkhd = None
    orig_mdhd = None
    orig_smhd = None
    orig_dinf = None
    # We will use the actual audio sample rate from the stsd as our
    # master timescale to ensure 100% duration consistency.
    orig_hdlr = None
    timescale = 44100  # Default fallback
    preferred_desc_index = _preferred_sample_description_index(song_info.samples)

    if song_info.moov_data:
        orig_data = song_info.ftyp_data + song_info.moov_data
    elif original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
    else:
        orig_data = None

    if orig_data:
        stsd_content = _extract_stsd_content(orig_data, preferred_desc_index)
        # Extract the REAL sample rate from the codec configuration
        timescale = _extract_sample_rate_from_stsd(stsd_content) or _extract_timescale(
            orig_data
        )

        # Find moov box and extract child boxes
        moov_idx = orig_data.find(b"moov")
        if moov_idx >= 4:
            moov_size = struct.unpack(">I", orig_data[moov_idx - 4 : moov_idx])[0]
            moov_data = orig_data[moov_idx - 4 : moov_idx - 4 + moov_size]

            orig_mvhd = _find_child_box(moov_data, b"mvhd")

            audio_trak = _find_audio_trak(moov_data)
            if audio_trak:
                orig_tkhd = _find_child_box(audio_trak, b"tkhd")
                mdia = _find_child_box(audio_trak, b"mdia")
                if mdia:
                    orig_mdhd = _find_child_box(mdia, b"mdhd")
                    orig_hdlr = _find_child_box(mdia, b"hdlr")
                    minf = _find_child_box(mdia, b"minf")
                    if minf:
                        orig_smhd = _find_child_box(minf, b"smhd")
                        orig_dinf = _find_child_box(minf, b"dinf")

    with open(output_path, "wb") as f:
        # Write ftyp
        _write_ftyp(f)

        # Calculate total duration
        total_duration = sum(s.duration for s in song_info.samples)

        # Write moov with sample tables
        _write_moov(
            f,
            song_info.samples,
            total_duration,
            timescale,
            stsd_content,
            decrypted_data,
            orig_mvhd=orig_mvhd,
            orig_tkhd=orig_tkhd,
            orig_mdhd=orig_mdhd,
            orig_hdlr=orig_hdlr,
            orig_smhd=orig_smhd,
            orig_dinf=orig_dinf,
        )

        # Write mdat
        if decrypted_data_path:
            _write_mdat_from_sources(
                f,
                [(decrypted_data_path, 0, os.path.getsize(decrypted_data_path))],
            )
        else:
            _write_mdat(f, decrypted_data)

    logger.debug(f"Wrote decrypted file to {output_path}")


def write_decrypted_mp4_track(
    output_path: str,
    track_info: SongInfo,
    decrypted_data: bytes,
    original_path: str = None,
    decrypted_data_path: str | None = None,
) -> None:
    """Write one decrypted audio or video track as a flat MP4 file."""
    stsd_content = None
    orig_mvhd = None
    orig_tkhd = None
    orig_mdhd = None
    orig_hdlr = None
    orig_smhd = None
    orig_vmhd = None
    orig_nmhd = None
    orig_dinf = None
    timescale = 44100 if track_info.handler_type == b"soun" else 90000
    preferred_desc_index = _preferred_sample_description_index(track_info.samples)

    if track_info.moov_data:
        orig_data = track_info.ftyp_data + track_info.moov_data
    elif original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
    else:
        orig_data = None

    if orig_data:
        stsd_content = _extract_stsd_content(
            orig_data,
            preferred_desc_index,
            track_info.handler_type,
        )
        if track_info.handler_type == b"soun":
            timescale = _extract_sample_rate_from_stsd(
                stsd_content
            ) or _extract_track_timescale(orig_data, track_info.handler_type, timescale)
        else:
            timescale = _extract_track_timescale(
                orig_data, track_info.handler_type, timescale
            )

        moov_idx = orig_data.find(b"moov")
        if moov_idx >= 4:
            moov_size = struct.unpack(">I", orig_data[moov_idx - 4 : moov_idx])[0]
            moov_data = orig_data[moov_idx - 4 : moov_idx - 4 + moov_size]

            orig_mvhd = _find_child_box(moov_data, b"mvhd")
            trak = _find_track_by_handler(moov_data, track_info.handler_type)
            if trak:
                orig_tkhd = _find_child_box(trak, b"tkhd")
                mdia = _find_child_box(trak, b"mdia")
                if mdia:
                    orig_mdhd = _find_child_box(mdia, b"mdhd")
                    orig_hdlr = _find_child_box(mdia, b"hdlr")
                    minf = _find_child_box(mdia, b"minf")
                    if minf:
                        orig_smhd = _find_child_box(minf, b"smhd")
                        orig_vmhd = _find_child_box(minf, b"vmhd")
                        orig_nmhd = _find_child_box(minf, b"nmhd")
                        orig_dinf = _find_child_box(minf, b"dinf")

    with open(output_path, "wb") as f:
        if track_info.handler_type == b"soun":
            _write_ftyp(f)
        else:
            _write_ftyp_mp4(f)

        total_duration = sum(s.duration for s in track_info.samples)
        _write_moov(
            f,
            track_info.samples,
            total_duration,
            timescale,
            stsd_content,
            decrypted_data,
            orig_mvhd=orig_mvhd,
            orig_tkhd=orig_tkhd,
            orig_mdhd=orig_mdhd,
            orig_hdlr=orig_hdlr,
            orig_smhd=orig_smhd,
            orig_vmhd=orig_vmhd,
            orig_nmhd=orig_nmhd,
            orig_dinf=orig_dinf,
            handler_type=track_info.handler_type,
        )
        if decrypted_data_path:
            _write_mdat_from_sources(
                f,
                [(decrypted_data_path, 0, os.path.getsize(decrypted_data_path))],
            )
        else:
            _write_mdat(f, decrypted_data)

    logger.debug(f"Wrote decrypted track file to {output_path}")


def _build_decrypted_track_moov(
    track_info: SongInfo,
    original_path: str | None = None,
) -> bytes:
    """Build a single-track moov box for a decrypted track without writing mdat."""
    stsd_content = None
    orig_mvhd = None
    orig_tkhd = None
    orig_mdhd = None
    orig_hdlr = None
    orig_smhd = None
    orig_vmhd = None
    orig_nmhd = None
    orig_dinf = None
    timescale = 44100 if track_info.handler_type == b"soun" else 90000
    preferred_desc_index = _preferred_sample_description_index(track_info.samples)

    if track_info.moov_data:
        orig_data = track_info.ftyp_data + track_info.moov_data
    elif original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
    else:
        orig_data = None

    if orig_data:
        stsd_content = _extract_stsd_content(
            orig_data,
            preferred_desc_index,
            track_info.handler_type,
        )
        if track_info.handler_type == b"soun":
            timescale = _extract_sample_rate_from_stsd(
                stsd_content
            ) or _extract_track_timescale(orig_data, track_info.handler_type, timescale)
        else:
            timescale = _extract_track_timescale(
                orig_data, track_info.handler_type, timescale
            )

        moov_idx = orig_data.find(b"moov")
        if moov_idx >= 4:
            moov_size = struct.unpack(">I", orig_data[moov_idx - 4 : moov_idx])[0]
            moov_data = orig_data[moov_idx - 4 : moov_idx - 4 + moov_size]

            orig_mvhd = _find_child_box(moov_data, b"mvhd")
            trak = _find_track_by_handler(moov_data, track_info.handler_type)
            if trak:
                orig_tkhd = _find_child_box(trak, b"tkhd")
                mdia = _find_child_box(trak, b"mdia")
                if mdia:
                    orig_mdhd = _find_child_box(mdia, b"mdhd")
                    orig_hdlr = _find_child_box(mdia, b"hdlr")
                    minf = _find_child_box(mdia, b"minf")
                    if minf:
                        orig_smhd = _find_child_box(minf, b"smhd")
                        orig_vmhd = _find_child_box(minf, b"vmhd")
                        orig_nmhd = _find_child_box(minf, b"nmhd")
                        orig_dinf = _find_child_box(minf, b"dinf")

    buf = io.BytesIO()
    total_duration = sum(s.duration for s in track_info.samples)
    _write_moov(
        buf,
        track_info.samples,
        total_duration,
        timescale,
        stsd_content,
        b"",
        orig_mvhd=orig_mvhd,
        orig_tkhd=orig_tkhd,
        orig_mdhd=orig_mdhd,
        orig_hdlr=orig_hdlr,
        orig_smhd=orig_smhd,
        orig_vmhd=orig_vmhd,
        orig_nmhd=orig_nmhd,
        orig_dinf=orig_dinf,
        handler_type=track_info.handler_type,
    )
    return buf.getvalue()


def _decrypted_track_payload_source(track: DecryptedTrack):
    """Return an mdat source tuple for a decrypted track."""
    if track.data_path:
        size = track.data_size or os.path.getsize(track.data_path)
        return (track.data_path, 0, size)
    return (None, 0, len(track.data), track.data)


def _sample_payload_bytes(samples: List[SampleInfo]) -> bytes:
    """Materialize only the payload bytes for the given samples."""
    return b"".join(_sample_data(sample) for sample in samples)


def mux_decrypted_media_direct(
    decrypted_media: DecryptedMedia,
    output_path: str,
    m4v_brand: bool = False,
) -> None:
    """Mux decrypted media directly to the final file without temp MP4 tracks."""
    if decrypted_media.video is None:
        raise ValueError("direct AV mux requires a video track")

    video_moov = _build_decrypted_track_moov(decrypted_media.video.track_info)
    audio_moov = _build_decrypted_track_moov(decrypted_media.audio.track_info)
    extra_track_files = [
        (
            _build_decrypted_track_moov(caption.track_info),
            _decrypted_track_payload_source(caption),
        )
        for caption in decrypted_media.captions
    ]

    mvhd = _find_child_box(video_moov, b"mvhd")
    video_trak = _find_track_by_handler(video_moov, b"vide")
    audio_trak = _find_track_by_handler(audio_moov, b"soun")
    if not mvhd or not video_trak or not audio_trak:
        raise IOError("mux: missing required audio/video track metadata")

    movie_timescale = _extract_mvhd_timescale(mvhd)
    audio_trak = _patch_trak_track_id(audio_trak, 2)
    audio_trak = _patch_trak_duration_to_movie_timescale(audio_trak, movie_timescale)
    extra_traks = []
    for index, (extra_moov, extra_source) in enumerate(extra_track_files, start=3):
        extra_trak = _find_first_trak(extra_moov)
        if extra_trak:
            extra_trak = _patch_trak_track_id(extra_trak, index)
            extra_trak = _patch_trak_duration_to_movie_timescale(
                extra_trak, movie_timescale
            )
            extra_traks.append((extra_trak, extra_source))

    ftyp = _build_ftyp_m4v_bytes() if m4v_brand else _build_ftyp_mp4_bytes()
    moov = _build_muxed_moov(
        mvhd, [video_trak, audio_trak] + [t for t, _ in extra_traks]
    )
    mdat_data_offset = len(ftyp) + len(moov) + 8

    video_source = _decrypted_track_payload_source(decrypted_media.video)
    audio_source = _decrypted_track_payload_source(decrypted_media.audio)
    video_trak = _patch_first_chunk_offset(video_trak, mdat_data_offset)
    next_mdat_offset = mdat_data_offset + video_source[2]
    audio_trak = _patch_first_chunk_offset(audio_trak, next_mdat_offset)
    next_mdat_offset += audio_source[2]
    patched_extra_traks = []
    for extra_trak, extra_source in extra_traks:
        patched_extra_traks.append(
            _patch_first_chunk_offset(extra_trak, next_mdat_offset)
        )
        next_mdat_offset += extra_source[2]

    moov = _build_muxed_moov(mvhd, [video_trak, audio_trak] + patched_extra_traks)

    with open(output_path, "wb") as f:
        f.write(ftyp)
        f.write(moov)
        _write_mdat_from_sources(
            f,
            [video_source, audio_source] + [source for _, source in extra_traks],
        )

    logger.debug(f"Muxed decrypted AV file to {output_path}")


def mux_decrypted_mp4_tracks(
    input_path_video: str,
    input_path_audio: str,
    output_path: str,
    input_path_extra_tracks: Optional[List[str]] = None,
    m4v_brand: bool = False,
) -> None:
    """Mux one flat video MP4 and one flat audio MP4 into a single MP4/M4V."""
    with open(input_path_video, "rb") as f:
        video_data = f.read()
    with open(input_path_audio, "rb") as f:
        audio_data = f.read()
    input_path_extra_tracks = input_path_extra_tracks or []
    extra_track_files = []
    for input_path_extra_track in input_path_extra_tracks:
        with open(input_path_extra_track, "rb") as f:
            extra_track_data = f.read()
        extra_track_files.append(
            (
                _extract_top_level_box(extra_track_data, b"moov"),
                _extract_mdat_payload(extra_track_data),
            )
        )

    video_moov = _extract_top_level_box(video_data, b"moov")
    audio_moov = _extract_top_level_box(audio_data, b"moov")
    video_mdat_payload = _extract_mdat_payload(video_data)
    audio_mdat_payload = _extract_mdat_payload(audio_data)
    if not video_moov or not audio_moov:
        raise IOError("mux: missing moov box in decrypted track file")

    mvhd = _find_child_box(video_moov, b"mvhd")
    video_trak = _find_track_by_handler(video_moov, b"vide")
    audio_trak = _find_track_by_handler(audio_moov, b"soun")
    if not mvhd or not video_trak or not audio_trak:
        raise IOError("mux: missing required audio/video track metadata")

    movie_timescale = _extract_mvhd_timescale(mvhd)
    audio_trak = _patch_trak_track_id(audio_trak, 2)
    audio_trak = _patch_trak_duration_to_movie_timescale(audio_trak, movie_timescale)
    extra_traks = []
    for index, (extra_moov, extra_mdat_payload) in enumerate(
        extra_track_files, start=3
    ):
        if not extra_moov:
            continue
        extra_trak = _find_first_trak(extra_moov)
        if extra_trak:
            extra_trak = _patch_trak_track_id(extra_trak, index)
            extra_trak = _patch_trak_duration_to_movie_timescale(
                extra_trak, movie_timescale
            )
            extra_traks.append((extra_trak, extra_mdat_payload))
    ftyp = _build_ftyp_m4v_bytes() if m4v_brand else _build_ftyp_mp4_bytes()

    moov = _build_muxed_moov(
        mvhd, [video_trak, audio_trak] + [t for t, _ in extra_traks]
    )
    mdat_data_offset = len(ftyp) + len(moov) + 8
    video_trak = _patch_first_chunk_offset(video_trak, mdat_data_offset)
    next_mdat_offset = mdat_data_offset + len(video_mdat_payload)
    audio_trak = _patch_first_chunk_offset(audio_trak, next_mdat_offset)
    next_mdat_offset += len(audio_mdat_payload)
    patched_extra_traks = []
    for extra_trak, extra_mdat_payload in extra_traks:
        patched_extra_traks.append(
            _patch_first_chunk_offset(extra_trak, next_mdat_offset)
        )
        next_mdat_offset += len(extra_mdat_payload)
    moov = _build_muxed_moov(mvhd, [video_trak, audio_trak] + patched_extra_traks)

    with open(output_path, "wb") as f:
        f.write(ftyp)
        f.write(moov)
        _write_mdat(
            f,
            video_mdat_payload
            + audio_mdat_payload
            + b"".join(payload for _, payload in extra_traks),
        )

    logger.debug(f"Muxed decrypted AV file to {output_path}")


def _encryption_info_for_hex_decrypt(
    track_info: SongInfo,
    *,
    use_cenc: bool,
) -> EncryptionInfo:
    """Resolve encryption scheme for hex decrypt (moov metadata or defaults)."""
    base = track_info.encryption_info or EncryptionInfo(scheme_type="cbcs")
    if not use_cenc:
        return base
    return EncryptionInfo(
        scheme_type="cenc",
        crypt_byte_block=0,
        skip_byte_block=0,
        per_sample_iv_size=base.per_sample_iv_size,
        constant_iv=base.constant_iv,
        kid=base.kid,
    )


async def _decrypt_track_hex(
    input_path: str,
    decryption_key: str,
    handler_type: bytes,
    *,
    use_cenc: bool = False,
    use_single_content_key: bool = False,
    file_backed: bool = False,
) -> DecryptedTrack:
    """Decrypt one audio/video/text track with a raw AES key.

    ``use_single_content_key``:
        ``False`` (default for catalog audio): description 0 uses the built-in
        prefetch AES key, description 1+ uses ``decryption_key`` (Apple CBCS layout).
        ``True`` (web AAC, muxed MV audio): every sample description uses
        ``decryption_key``.
    """
    track_info = await asyncio.to_thread(
        extract_song, input_path, handler_type, file_backed
    )
    track_key = bytes.fromhex(decryption_key)

    if use_single_content_key:
        keys = {sample.desc_index: track_key for sample in track_info.samples}
    elif handler_type == b"soun":
        keys = {0: DEFAULT_SONG_DECRYPTION_KEY, 1: track_key}
    else:
        keys = {sample.desc_index: track_key for sample in track_info.samples}

    enc_info = _encryption_info_for_hex_decrypt(track_info, use_cenc=use_cenc)
    enc_info_per_desc = None
    if track_info.moov_data:
        enc_info_per_desc = await asyncio.to_thread(
            _extract_encryption_info_per_stsd,
            track_info.moov_data,
            handler_type,
        )

    if file_backed:
        temp_file = tempfile.NamedTemporaryFile(
            prefix="gamdl_decrypted_", suffix=".bin", delete=False
        )
        temp_path = temp_file.name
        temp_file.close()
        try:
            data_size = decrypt_samples_hex_to_file(
                track_info.samples,
                keys,
                enc_info,
                temp_path,
                enc_info_per_desc,
                release_sample_data=True,
            )
        except Exception:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
            raise
        return DecryptedTrack(
            input_path,
            track_info,
            data_path=temp_path,
            data_size=data_size,
        )

    decrypted_data = decrypt_samples_hex(
        track_info.samples,
        keys,
        enc_info,
        enc_info_per_desc,
    )
    return DecryptedTrack(
        input_path,
        track_info,
        decrypted_data,
        data_size=len(decrypted_data),
    )


async def decrypt_file_hex(
    decryption_key_audio: str,
    input_audio_path: str,
    decryption_key_video: str | None = None,
    input_video_path: str | None = None,
    *,
    use_cenc: bool = False,
    use_single_content_key: bool = False,
) -> DecryptedMedia:
    """Decrypt audio and optional video with raw AES hex keys."""
    audio = await _decrypt_track_hex(
        input_audio_path,
        decryption_key_audio,
        b"soun",
        use_cenc=use_cenc,
        use_single_content_key=use_single_content_key or input_video_path is not None,
        file_backed=input_video_path is not None,
    )
    if input_video_path is None:
        return DecryptedMedia(audio=audio)

    video_key = decryption_key_video or decryption_key_audio
    video_task = asyncio.create_task(
        _decrypt_track_hex(
            input_video_path,
            video_key,
            b"vide",
            use_cenc=use_cenc,
            file_backed=True,
        )
    )
    caption_tracks = [
        track
        for track in await asyncio.gather(
            asyncio.to_thread(extract_song, input_video_path, b"clcp", True),
            asyncio.to_thread(extract_song, input_video_path, b"text", True),
            asyncio.to_thread(extract_song, input_video_path, b"sbtl", True),
            asyncio.to_thread(extract_song, input_video_path, b"subt", True),
        )
        if track.samples
    ]
    captions = []
    for caption_track in caption_tracks:
        caption_data = _sample_payload_bytes(caption_track.samples)
        if caption_track.encryption_info:
            caption_key = bytes.fromhex(video_key)
            caption_enc_info_per_desc = await asyncio.to_thread(
                _extract_encryption_info_per_stsd,
                caption_track.moov_data,
                caption_track.handler_type,
            )
            caption_data = decrypt_samples_hex(
                caption_track.samples,
                {sample.desc_index: caption_key for sample in caption_track.samples},
                caption_track.encryption_info,
                caption_enc_info_per_desc,
            )
        captions.append(DecryptedTrack(input_video_path, caption_track, caption_data))

    return DecryptedMedia(
        audio=audio,
        video=await video_task,
        captions=captions,
    )


async def write_decrypted_media(
    decrypted_media: DecryptedMedia,
    output_path: str,
    m4v_brand: bool = False,
) -> None:
    """Write decrypted audio as M4A, or mux decrypted audio/video/text tracks."""
    if decrypted_media.video is None:
        await asyncio.to_thread(
            write_decrypted_m4a,
            output_path,
            decrypted_media.audio.track_info,
            decrypted_media.audio.data,
            decrypted_media.audio.input_path,
            decrypted_media.audio.data_path,
        )
        return

    try:
        await asyncio.to_thread(
            mux_decrypted_media_direct,
            decrypted_media,
            output_path,
            m4v_brand,
        )
    finally:
        for track in (
            decrypted_media.audio,
            decrypted_media.video,
            *decrypted_media.captions,
        ):
            if track and track.data_path:
                try:
                    os.remove(track.data_path)
                except FileNotFoundError:
                    pass
                track.data_path = None


async def decrypt_av_files_hex(
    input_path_video: str,
    input_path_audio: str,
    output_path: str,
    decryption_key_video: str,
    decryption_key_audio: str,
    m4v_brand: bool = False,
) -> None:
    """Decrypt separate encrypted video/audio MP4s and mux them in Python."""
    decrypted_media = await decrypt_file_hex(
        decryption_key_audio,
        input_path_audio,
        decryption_key_video,
        input_path_video,
    )
    await write_decrypted_media(decrypted_media, output_path, m4v_brand)


def _preferred_sample_description_index(samples: List[SampleInfo]) -> int:
    """Return the 0-based sample description index to keep in flattened output."""
    counts = Counter(sample.desc_index for sample in samples if _sample_size(sample))
    if not counts:
        return 0
    return counts.most_common(1)[0][0]


def _write_box(f, box_type: bytes, content: bytes):
    """Write a simple MP4 box."""
    size = len(content) + 8
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.write(content)


def _write_ftyp(f):
    """Write ftyp box for M4A."""
    content = b"M4A " + struct.pack(">I", 0)  # major brand + minor version
    content += b"M4A mp42isom\x00\x00\x00\x00"  # compatible brands
    _write_box(f, b"ftyp", content)


def _write_ftyp_mp4(f):
    """Write ftyp box for MP4/M4V video."""
    content = b"mp42" + struct.pack(">I", 0)
    content += b"mp42isomiso6avc1hvc1"
    _write_box(f, b"ftyp", content)


def _write_ftyp_m4v(f):
    """Write an iTunes-like ftyp box for M4V outputs."""
    content = b"M4V " + struct.pack(">I", 0)
    content += b"M4V mp42isom"
    _write_box(f, b"ftyp", content)


def _build_ftyp_mp4_bytes() -> bytes:
    buf = io.BytesIO()
    _write_ftyp_mp4(buf)
    return buf.getvalue()


def _build_ftyp_m4v_bytes() -> bytes:
    buf = io.BytesIO()
    _write_ftyp_m4v(buf)
    return buf.getvalue()


def _write_fullbox(f, box_type: bytes, version: int, flags: int, content: bytes):
    """Write a FullBox (with version and flags)."""
    size = len(content) + 12
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.write(struct.pack("B", version))
    f.write(struct.pack(">I", flags)[1:])  # 3 bytes for flags
    f.write(content)


def _write_moov(
    f,
    samples: List[SampleInfo],
    total_duration: int,
    timescale: int,
    stsd_content: bytes,
    decrypted_data: bytes,
    orig_mvhd: Optional[bytes] = None,
    orig_tkhd: Optional[bytes] = None,
    orig_mdhd: Optional[bytes] = None,
    orig_hdlr: Optional[bytes] = None,
    orig_smhd: Optional[bytes] = None,
    orig_vmhd: Optional[bytes] = None,
    orig_nmhd: Optional[bytes] = None,
    orig_dinf: Optional[bytes] = None,
    handler_type: bytes = b"soun",
):
    """Write moov box with sample tables.

    When original box data is available, it is copied verbatim (with duration
    fields patched) to faithfully reproduce the source file's metadata.
    This matches the Go amdecrypt behavior of copying boxes from the original.
    """
    moov_start = f.tell()
    f.write(b"\x00" * 8)  # moov header placeholder

    # mvhd (movie header)
    if orig_mvhd:
        f.write(_patch_mvhd_duration(orig_mvhd, total_duration, timescale))
    else:
        mvhd_content = struct.pack(">II", 0, 0)  # creation, modification
        mvhd_content += struct.pack(">I", timescale)
        mvhd_content += struct.pack(">I", total_duration)
        mvhd_content += struct.pack(">I", 0x00010000)  # rate (1.0)
        mvhd_content += struct.pack(">H", 0x0100)  # volume (1.0)
        mvhd_content += b"\x00" * 10  # reserved
        mvhd_content += struct.pack(
            ">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )  # matrix
        mvhd_content += b"\x00" * 24  # pre_defined
        mvhd_content += struct.pack(">I", 2)  # next_track_id
        _write_fullbox(f, b"mvhd", 0, 0, mvhd_content)

    # trak (track)
    trak_start = f.tell()
    f.write(b"\x00" * 8)  # trak header placeholder

    # tkhd (track header)
    if orig_tkhd:
        f.write(_patch_tkhd_duration(orig_tkhd, total_duration))
    else:
        tkhd_content = struct.pack(">II", 0, 0)  # creation, modification
        tkhd_content += struct.pack(">I", 1)  # track_id
        tkhd_content += struct.pack(">I", 0)  # reserved
        tkhd_content += struct.pack(">I", total_duration)
        tkhd_content += b"\x00" * 8  # reserved
        tkhd_content += struct.pack(">HH", 0, 0)  # layer, alternate_group
        tkhd_content += struct.pack(">H", 0x0100)  # volume
        tkhd_content += struct.pack(">H", 0)  # reserved
        tkhd_content += struct.pack(
            ">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )  # matrix
        tkhd_content += struct.pack(">II", 0, 0)  # width, height
        _write_fullbox(f, b"tkhd", 0, 7, tkhd_content)

    # mdia (media)
    mdia_start = f.tell()
    f.write(b"\x00" * 8)

    # mdhd (media header) - preserves original language code
    if orig_mdhd:
        f.write(_patch_mdhd_duration(orig_mdhd, total_duration, timescale))
    else:
        mdhd_content = struct.pack(">II", 0, 0)  # creation, modification
        mdhd_content += struct.pack(">I", timescale)
        mdhd_content += struct.pack(">I", total_duration)
        mdhd_content += struct.pack(">H", 0x55C4)  # language (und)
        mdhd_content += struct.pack(">H", 0)  # quality
        _write_fullbox(f, b"mdhd", 0, 0, mdhd_content)

    # hdlr (handler) - preserves original handler name (e.g. "Core Media Audio")
    if orig_hdlr:
        f.write(orig_hdlr)
    else:
        hdlr_content = struct.pack(">I", 0)  # pre_defined
        hdlr_content += b"soun"  # handler_type
        hdlr_content += b"\x00" * 12  # reserved
        handler_name = b"SoundHandler"
        hdlr_content += struct.pack("B", len(handler_name)) + handler_name + b"\x00"
        _write_fullbox(f, b"hdlr", 0, 0, hdlr_content)

    # minf (media info)
    minf_start = f.tell()
    f.write(b"\x00" * 8)

    # media header
    if handler_type == b"vide" and orig_vmhd:
        f.write(orig_vmhd)
    elif handler_type == b"soun" and orig_smhd:
        f.write(orig_smhd)
    elif orig_nmhd:
        f.write(orig_nmhd)
    elif handler_type == b"vide":
        vmhd_content = struct.pack(">HHHH", 0, 0, 0, 0)
        _write_fullbox(f, b"vmhd", 0, 1, vmhd_content)
    elif handler_type not in (b"soun", b"vide"):
        _write_fullbox(f, b"nmhd", 0, 0, b"")
    else:
        smhd_content = struct.pack(">HH", 0, 0)  # balance, reserved
        _write_fullbox(f, b"smhd", 0, 0, smhd_content)

    # dinf + dref
    if orig_dinf:
        f.write(orig_dinf)
    else:
        dinf_start = f.tell()
        f.write(b"\x00" * 8)
        dref_content = struct.pack(">I", 1)  # entry_count
        dref_content += (
            struct.pack(">I", 12) + b"url " + struct.pack(">I", 1)
        )  # url entry (self-contained)
        _write_fullbox(f, b"dref", 0, 0, dref_content)
        _fixup_box_size(f, dinf_start, b"dinf")

    # stbl (sample table)
    stbl_start = f.tell()
    f.write(b"\x00" * 8)

    # stsd (sample description) - use content from original file
    _write_stsd(f, stsd_content)

    # stts (time-to-sample)
    _write_stts(f, samples)

    # ctts (composition time-to-sample) preserves B-frame presentation timing.
    _write_ctts(f, samples)

    # stss (sync samples) helps players seek/decode video keyframes correctly.
    if handler_type == b"vide":
        _write_stss(f, samples)

    # stsc (sample-to-chunk) - all samples in one chunk
    stsc_content = struct.pack(">I", 1)  # entry_count
    stsc_content += struct.pack(
        ">III", 1, len(samples), 1
    )  # first_chunk, samples_per_chunk, sample_description_index
    _write_fullbox(f, b"stsc", 0, 0, stsc_content)

    # stsz (sample size)
    stsz_content = struct.pack(">I", 0)  # sample_size (0 = variable)
    stsz_content += struct.pack(">I", len(samples))  # sample_count
    for sample in samples:
        stsz_content += struct.pack(">I", _sample_size(sample))
    _write_fullbox(f, b"stsz", 0, 0, stsz_content)

    # stco (chunk offset) - will be fixed up later
    stco_pos = f.tell()
    stco_content = struct.pack(">I", 1)  # entry_count
    stco_content += struct.pack(">I", 0)  # chunk_offset (placeholder)
    _write_fullbox(f, b"stco", 0, 0, stco_content)

    _fixup_box_size(f, stbl_start, b"stbl")
    _fixup_box_size(f, minf_start, b"minf")
    _fixup_box_size(f, mdia_start, b"mdia")
    _fixup_box_size(f, trak_start, b"trak")

    # udta > meta > hdlr(mdir) + ilst - metadata container
    _write_udta(f)

    _fixup_box_size(f, moov_start, b"moov")

    # Fix up stco with correct mdat offset
    mdat_offset = f.tell() + 8  # +8 for mdat header
    f.seek(stco_pos + 16)  # +12 for box header + version/flags, +4 for entry_count
    f.write(struct.pack(">I", mdat_offset))
    f.seek(0, 2)  # Back to end


def _extract_sample_rate_from_stsd(stsd_content: bytes) -> Optional[int]:
    """Extract the actual audio sample rate from the stsd box content."""
    # Header: version(1)+flags(3)+count(4) + Entry: size(4)+type(4) = 16 bytes
    # AudioSampleEntry v0: reserved(6)+dref(2)+ver(2)+rev(2)+vend(4)+chan(2)+size(2)+comp(2)+pack(2)+rate(4)
    # The fixed-point sample_rate field is at offset 16 + 24 = 40.
    if not stsd_content or len(stsd_content) < 44:
        return None

    samplerate_offset = 40
    sample_rate_fixed = struct.unpack(
        ">I", stsd_content[samplerate_offset : samplerate_offset + 4]
    )[0]
    sample_rate = sample_rate_fixed >> 16

    # Sanity check: standard audio sample rates should be between 8000 and 384000
    if 8000 <= sample_rate <= 384000:
        return sample_rate
    return None


def _write_stsd(f, stsd_content: bytes):
    """Write sample description box using content from original file.
    This preserves the original codec info (ALAC, EC-3, AAC, etc.).
    """
    if stsd_content:
        # Write the full stsd box with its content from the source file
        size = len(stsd_content) + 8
        f.write(struct.pack(">I", size))
        f.write(b"stsd")
        f.write(stsd_content)
    else:
        # Fallback: write a basic ALAC stsd if no source content available
        _write_stsd_alac_fallback(f)


def _write_stsd_alac_fallback(f):
    """Write a default ALAC sample description box (fallback)."""
    stsd_start = f.tell()
    f.write(b"\x00" * 12)  # box header + version/flags placeholder

    f.write(struct.pack(">I", 1))  # entry_count

    # alac sample entry
    alac_start = f.tell()
    f.write(b"\x00" * 8)  # alac box header placeholder

    f.write(b"\x00" * 6)  # reserved
    f.write(struct.pack(">H", 1))  # data_reference_index
    f.write(b"\x00" * 8)  # reserved
    f.write(struct.pack(">H", 2))  # channel_count
    f.write(struct.pack(">H", 16))  # sample_size (bits)
    f.write(struct.pack(">H", 0))  # pre_defined
    f.write(struct.pack(">H", 0))  # reserved
    f.write(struct.pack(">I", 44100 << 16))  # sample_rate (16.16 fixed point)

    # alac magic cookie box
    # Default ALAC config for 44.1kHz stereo 24-bit
    default_config = bytes(
        [
            0x00,
            0x00,
            0x10,
            0x00,  # frame_length
            0x00,  # compatible_version
            0x18,  # bit_depth (24)
            0x28,
            0x28,
            0x0A,  # pb, mb, kb
            0x02,  # num_channels
            0x00,
            0x00,  # max_run
            0x00,
            0x00,
            0xFF,
            0xFF,  # max_frame_bytes
            0x00,
            0x0D,
            0x00,
            0x80,  # avg_bit_rate
            0x00,
            0x00,
            0xAC,
            0x44,  # sample_rate
        ]
    )
    _write_box(f, b"alac", default_config)

    _fixup_box_size(f, alac_start, b"alac")

    # Fix stsd size
    end_pos = f.tell()
    size = end_pos - stsd_start
    f.seek(stsd_start)
    f.write(struct.pack(">I", size))
    f.write(b"stsd")
    f.write(struct.pack(">I", 0))  # version + flags
    f.seek(end_pos)


def _write_stts(f, samples: List[SampleInfo]):
    """Write time-to-sample box (run-length encoded)."""
    # Run-length encode durations
    entries = []
    for sample in samples:
        if entries and entries[-1][1] == sample.duration:
            entries[-1] = (entries[-1][0] + 1, sample.duration)
        else:
            entries.append((1, sample.duration))

    content = struct.pack(">I", len(entries))
    for count, delta in entries:
        content += struct.pack(">II", count, delta)
    _write_fullbox(f, b"stts", 0, 0, content)


def _write_ctts(f, samples: List[SampleInfo]):
    """Write composition time-to-sample box when samples have composition offsets."""
    if not any(sample.composition_time_offset for sample in samples):
        return

    entries = []
    for sample in samples:
        offset = sample.composition_time_offset
        if entries and entries[-1][1] == offset:
            entries[-1] = (entries[-1][0] + 1, offset)
        else:
            entries.append((1, offset))

    version = 1 if any(offset < 0 for _, offset in entries) else 0
    content = struct.pack(">I", len(entries))
    for count, offset in entries:
        if version == 1:
            content += struct.pack(">Ii", count, offset)
        else:
            content += struct.pack(">II", count, offset)
    _write_fullbox(f, b"ctts", version, 0, content)


def _write_stss(f, samples: List[SampleInfo]):
    """Write sync sample box when video sample flags identify non-sync samples."""
    if not samples or all(sample.is_sync for sample in samples):
        return

    sync_sample_numbers = [
        index for index, sample in enumerate(samples, start=1) if sample.is_sync
    ]
    if not sync_sample_numbers:
        return

    content = struct.pack(">I", len(sync_sample_numbers))
    for sample_number in sync_sample_numbers:
        content += struct.pack(">I", sample_number)
    _write_fullbox(f, b"stss", 0, 0, content)


def _fixup_box_size(f, start_pos: int, box_type: bytes):
    """Fix up the size field of a box that was written with placeholder."""
    end_pos = f.tell()
    size = end_pos - start_pos
    f.seek(start_pos)
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.seek(end_pos)


def _write_mdat(f, data: bytes):
    """Write mdat box with decrypted data."""
    size = len(data) + 8
    f.write(struct.pack(">I", size))
    f.write(b"mdat")
    f.write(data)


def _copy_file_range(
    output_file: BinaryIO,
    input_path: str,
    offset: int,
    size: int,
    chunk_size: int = 1024 * 1024,
) -> None:
    """Copy a byte range without loading it all into memory."""
    remaining = size
    with open(input_path, "rb") as input_file:
        input_file.seek(offset)
        while remaining > 0:
            chunk = input_file.read(min(chunk_size, remaining))
            if not chunk:
                raise IOError(f"unexpected EOF while reading {input_path}")
            output_file.write(chunk)
            remaining -= len(chunk)


def _write_mdat_from_sources(f, sources: List[tuple]) -> None:
    """Write mdat by streaming file-backed sources and small in-memory sources."""
    payload_size = sum(source[2] for source in sources)
    if payload_size + 8 > 0xFFFFFFFF:
        raise IOError("mux: mdat too large for 32-bit box size")
    f.write(struct.pack(">I", payload_size + 8))
    f.write(b"mdat")
    for source in sources:
        input_path, offset, size = source[:3]
        if input_path is None:
            data = source[3]
            if len(data) != size:
                raise IOError("mux: in-memory mdat source size mismatch")
            f.write(data)
        else:
            _copy_file_range(f, input_path, offset, size)


def _extract_top_level_box(data: bytes, box_type: bytes) -> Optional[bytes]:
    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        current_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(data):
                return None
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or offset + size > len(data):
            return None
        if current_type == box_type:
            return data[offset : offset + size]
        offset += size
    return None


def _extract_mdat_payload(data: bytes) -> bytes:
    mdat = _extract_top_level_box(data, b"mdat")
    if not mdat:
        raise IOError("mux: missing mdat box in decrypted track file")
    size = struct.unpack(">I", mdat[:4])[0]
    header_size = 16 if size == 1 else 8
    return mdat[header_size:]


def _build_muxed_moov(mvhd: bytes, traks: List[bytes]) -> bytes:
    payload = bytearray()
    payload.extend(_patch_mvhd_next_track_id(mvhd, len(traks) + 1))
    for trak in traks:
        payload.extend(trak)
    udta_buf = io.BytesIO()
    _write_udta(udta_buf)
    payload.extend(udta_buf.getvalue())
    return struct.pack(">I", len(payload) + 8) + b"moov" + bytes(payload)


def _patch_mvhd_next_track_id(mvhd_data: bytes, next_track_id: int) -> bytes:
    """Return a copy of mvhd with next_track_id set past the muxed tracks."""
    data = bytearray(mvhd_data)
    if len(data) < 112:
        return bytes(data)
    version = data[8]
    next_track_id_offset = 108 if version == 0 else 120
    if next_track_id_offset + 4 <= len(data):
        struct.pack_into(">I", data, next_track_id_offset, next_track_id)
    return bytes(data)


def _extract_mvhd_timescale(mvhd_data: bytes) -> int:
    """Extract movie timescale from an mvhd box."""
    if len(mvhd_data) < 32:
        return 1000
    version = mvhd_data[8]
    if version == 0 and len(mvhd_data) >= 24:
        return struct.unpack(">I", mvhd_data[20:24])[0]
    if version == 1 and len(mvhd_data) >= 32:
        return struct.unpack(">I", mvhd_data[28:32])[0]
    return 1000


def _extract_mdhd_duration_timescale(trak_data: bytes) -> tuple[int, int]:
    """Extract media duration and timescale from a trak's mdhd box."""
    mdhd_offset = _find_box_offset_recursive(trak_data, b"mdhd")
    if mdhd_offset < 0 or mdhd_offset + 32 > len(trak_data):
        return 0, 1
    version = trak_data[mdhd_offset + 8]
    if version == 0:
        timescale = struct.unpack(">I", trak_data[mdhd_offset + 20 : mdhd_offset + 24])[
            0
        ]
        duration = struct.unpack(">I", trak_data[mdhd_offset + 24 : mdhd_offset + 28])[
            0
        ]
    else:
        timescale = struct.unpack(">I", trak_data[mdhd_offset + 28 : mdhd_offset + 32])[
            0
        ]
        duration = struct.unpack(">Q", trak_data[mdhd_offset + 32 : mdhd_offset + 40])[
            0
        ]
    return duration, timescale or 1


def _patch_trak_duration_to_movie_timescale(
    trak_data: bytes, movie_timescale: int
) -> bytes:
    """Patch tkhd duration to movie timescale while preserving mdhd duration."""
    media_duration, media_timescale = _extract_mdhd_duration_timescale(trak_data)
    if media_duration <= 0:
        return trak_data
    movie_duration = round(media_duration * movie_timescale / media_timescale)
    return _patch_trak_tkhd_duration(trak_data, movie_duration)


def _patch_trak_tkhd_duration(trak_data: bytes, duration: int) -> bytes:
    """Patch the nested tkhd duration inside a full trak box."""
    data = bytearray(trak_data)
    tkhd_offset = _find_box_offset_recursive(data, b"tkhd")
    if tkhd_offset < 0:
        return bytes(data)
    version = data[tkhd_offset + 8]
    data[tkhd_offset + 9 : tkhd_offset + 12] = struct.pack(">I", 7)[1:]
    if version == 0:
        duration_offset = tkhd_offset + 28
        if duration_offset + 4 <= len(data):
            struct.pack_into(">I", data, duration_offset, duration)
    else:
        duration_offset = tkhd_offset + 36
        if duration_offset + 8 <= len(data):
            struct.pack_into(">Q", data, duration_offset, duration)
    return bytes(data)


def _find_first_trak(moov_data: bytes) -> Optional[bytes]:
    offset = 8
    while offset + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[offset : offset + 4])[0]
        box_type = moov_data[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(moov_data):
            break
        if box_type == b"trak":
            return moov_data[offset : offset + size]
        offset += size
    return None


def _find_box_offset_recursive(
    data: bytes,
    target_type: bytes,
    start: int = 0,
    end: Optional[int] = None,
) -> int:
    """Find a box by walking MP4 box boundaries instead of byte substrings."""
    end = len(data) if end is None else min(end, len(data))
    offset = start
    container_types = {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"dinf",
        b"edts",
        b"udta",
        b"meta",
    }

    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        box_type = bytes(data[offset + 4 : offset + 8])
        header_size = 8
        if size == 1:
            if offset + 16 > end:
                break
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header_size = 16
        elif size == 0:
            size = end - offset

        if size < header_size or offset + size > end:
            break
        if box_type == target_type:
            return offset
        if box_type in container_types:
            child_start = offset + header_size
            if box_type == b"meta":
                child_start += 4
            found = _find_box_offset_recursive(
                data, target_type, child_start, offset + size
            )
            if found >= 0:
                return found
        offset += size

    return -1


def _patch_first_chunk_offset(trak_data: bytes, chunk_offset: int) -> bytes:
    data = bytearray(trak_data)
    stco_offset = _find_box_offset_recursive(data, b"stco")
    co64_offset = _find_box_offset_recursive(data, b"co64")
    if stco_offset >= 0:
        entry_count_offset = stco_offset + 12
        first_entry_offset = stco_offset + 16
        if first_entry_offset + 4 <= len(data):
            entry_count = struct.unpack(
                ">I", data[entry_count_offset:first_entry_offset]
            )[0]
            if entry_count > 0:
                struct.pack_into(">I", data, first_entry_offset, chunk_offset)
                return bytes(data)
    if co64_offset >= 0:
        entry_count_offset = co64_offset + 12
        first_entry_offset = co64_offset + 16
        if first_entry_offset + 8 <= len(data):
            entry_count = struct.unpack(
                ">I", data[entry_count_offset:first_entry_offset]
            )[0]
            if entry_count > 0:
                struct.pack_into(">Q", data, first_entry_offset, chunk_offset)
                return bytes(data)
    raise IOError("mux: unable to patch chunk offset")


def _patch_trak_track_id(trak_data: bytes, track_id: int) -> bytes:
    data = bytearray(trak_data)
    tkhd_offset = _find_box_offset_recursive(data, b"tkhd")
    if tkhd_offset < 0:
        return bytes(data)
    version = data[tkhd_offset + 8]
    track_id_offset = tkhd_offset + 20 if version == 0 else tkhd_offset + 28
    if track_id_offset + 4 <= len(data):
        struct.pack_into(">I", data, track_id_offset, track_id)
    return bytes(data)


def _extract_stsd_content(
    data: bytes,
    preferred_desc_index: Optional[int] = None,
    handler_type: bytes = b"soun",
) -> Optional[bytes]:
    """Extract cleaned stsd box content from moov box (supports any codec)."""
    moov_idx = data.find(b"moov")
    if moov_idx < 4:
        return None

    moov_size = struct.unpack(">I", data[moov_idx - 4 : moov_idx])[0]
    if moov_size < 8 or moov_idx - 4 + moov_size > len(data):
        return None
    moov_data = data[moov_idx - 4 : moov_idx - 4 + moov_size]
    trak_data = _find_track_by_handler(moov_data, handler_type)
    if trak_data is None:
        return None

    mdia = _find_child_box(trak_data, b"mdia")
    minf = _find_child_box(mdia, b"minf") if mdia else None
    stbl = _find_child_box(minf, b"stbl") if minf else None
    stsd = _find_child_box(stbl, b"stsd") if stbl else None
    if stsd is None or len(stsd) < 16:
        return None

    # Return stsd content (after box header = size + type)
    raw_content = stsd[8:]

    # Clean the stsd content to remove encryption metadata
    return _clean_stsd_content(raw_content, preferred_desc_index)


def _clean_stsd_content(
    stsd_content: bytes, preferred_desc_index: Optional[int] = None
) -> bytes:
    """
    Clean stsd content by removing encryption metadata.

    This replaces the mp4decrypt cleanup step. For encrypted files, the stsd
    contains encrypted sample entries (enca, encv) with sinf boxes that describe
    the encryption scheme. We need to:
    1. Convert encrypted entries (enca -> original format from frma)
    2. Remove sinf boxes entirely
    """
    if len(stsd_content) < 8:
        return stsd_content

    # stsd content: version(1) + flags(3) + entry_count(4) + entries...
    version_flags = stsd_content[:4]
    entry_count = struct.unpack(">I", stsd_content[4:8])[0]

    # Parse and clean each sample entry.
    cleaned_entries = []
    offset = 8

    for _ in range(entry_count):
        if offset + 8 > len(stsd_content):
            break

        entry_size = struct.unpack(">I", stsd_content[offset : offset + 4])[0]
        entry_type = stsd_content[offset + 4 : offset + 8]

        if entry_size < 8 or offset + entry_size > len(stsd_content):
            break

        entry_data = stsd_content[offset : offset + entry_size]

        # Check if this is an encrypted entry
        if entry_type in (b"enca", b"encv", b"encs", b"encm"):
            # Clean the encrypted entry
            cleaned_entry = _clean_encrypted_sample_entry(entry_data)
            cleaned_entries.append(cleaned_entry)
        else:
            # Keep as-is but still remove any sinf boxes that might be present
            cleaned_entry = _remove_sinf_from_entry(entry_data)
            cleaned_entries.append(cleaned_entry)

        offset += entry_size

    # The writer emits one chunk and one stsc entry with sample_description_index=1.
    # Keep only the dominant source description so that the flattened MP4's sample
    # table and stsd agree. iTunes is stricter about this than many players.
    if preferred_desc_index is not None and cleaned_entries:
        if 0 <= preferred_desc_index < len(cleaned_entries):
            cleaned_entries = [cleaned_entries[preferred_desc_index]]
        else:
            cleaned_entries = [cleaned_entries[0]]

    # Rebuild stsd content
    result = version_flags + struct.pack(">I", len(cleaned_entries))
    for entry in cleaned_entries:
        result += entry

    return result


def _sample_entry_header_size(entry_type: bytes) -> int:
    """Return fixed sample-entry bytes before child boxes."""
    if entry_type in (b"encv", b"avc1", b"avc3", b"hvc1", b"hev1", b"dvh1", b"dvhe"):
        return 86
    if entry_type in (b"enca", b"mp4a", b"alac", b"ac-3", b"ec-3"):
        return 36
    if entry_type in (b"c608", b"c708", b"text", b"tx3g", b"wvtt", b"stpp"):
        return 8
    return 36


def _clean_encrypted_sample_entry(entry_data: bytes) -> bytes:
    """
    Clean an encrypted sample entry (enca, encv, etc.).

    Structure of encrypted audio entry (enca):
    - Box header (8 bytes): size + 'enca'
    - Reserved (6 bytes)
    - Data reference index (2 bytes)
    - Audio specific data (varies by codec)
    - Child boxes including:
      - sinf (protection scheme info) - REMOVE THIS
      - Original codec box (alac, esds, etc.) - KEEP

    We need to:
    1. Find frma box inside sinf to get original format
    2. Replace 'enca' with original format
    3. Remove sinf box
    """
    if len(entry_data) < 16:
        return entry_data

    entry_size = struct.unpack(">I", entry_data[:4])[0]
    entry_type = entry_data[4:8]
    sample_entry_header_size = _sample_entry_header_size(entry_type)
    if len(entry_data) < sample_entry_header_size:
        return entry_data

    # Find the original format from sinf/frma
    original_format = _find_original_format(entry_data)
    if not original_format:
        # If we can't find frma, try common mappings
        if entry_type == b"enca":
            original_format = b"mp4a"  # Default to AAC
        elif entry_type == b"encv":
            original_format = b"avc1"  # Default to H.264
        else:
            original_format = entry_type  # Keep as-is

    # Copy the fixed header part, replacing the type
    new_entry = (
        entry_data[:4] + original_format + entry_data[8:sample_entry_header_size]
    )

    # Process child boxes, removing sinf
    child_offset = sample_entry_header_size
    while child_offset + 8 <= len(entry_data):
        child_size = struct.unpack(">I", entry_data[child_offset : child_offset + 4])[0]
        child_type = entry_data[child_offset + 4 : child_offset + 8]

        if child_size < 8 or child_offset + child_size > len(entry_data):
            break

        # Skip sinf box (encryption metadata)
        if child_type != b"sinf":
            new_entry += entry_data[child_offset : child_offset + child_size]

        child_offset += child_size

    # Update the entry size
    new_size = len(new_entry)
    new_entry = struct.pack(">I", new_size) + new_entry[4:]

    return new_entry


def _find_original_format(entry_data: bytes) -> Optional[bytes]:
    """
    Find the original format (frma) from sinf box in an encrypted entry.

    sinf box contains:
    - frma: original format (4 bytes codec type)
    - schm: scheme type
    - schi: scheme info (contains tenc for CENC)
    """
    # Look for sinf box
    sinf_idx = entry_data.find(b"sinf")
    if sinf_idx < 4:
        return None

    sinf_size = struct.unpack(">I", entry_data[sinf_idx - 4 : sinf_idx])[0]
    if sinf_size < 16 or sinf_idx + sinf_size > len(entry_data) + 4:
        return None

    sinf_data = entry_data[sinf_idx - 4 : sinf_idx - 4 + sinf_size]

    # Look for frma box inside sinf
    frma_idx = sinf_data.find(b"frma")
    if frma_idx < 4:
        return None

    frma_size = struct.unpack(">I", sinf_data[frma_idx - 4 : frma_idx])[0]
    if frma_size != 12:  # frma is always 12 bytes: size(4) + type(4) + format(4)
        return None

    # Extract the original format
    return sinf_data[frma_idx + 4 : frma_idx + 8]


def _remove_sinf_from_entry(entry_data: bytes) -> bytes:
    """
    Remove sinf box from a sample entry (if present).
    Used for non-encrypted entries that might still have protection info.
    """
    if len(entry_data) < 16:
        return entry_data
    sample_entry_header_size = _sample_entry_header_size(entry_data[4:8])
    if len(entry_data) < sample_entry_header_size:
        return entry_data

    # Check if sinf exists
    if b"sinf" not in entry_data:
        return entry_data

    # Rebuild entry without sinf
    new_entry = entry_data[:sample_entry_header_size]

    child_offset = sample_entry_header_size
    while child_offset + 8 <= len(entry_data):
        child_size = struct.unpack(">I", entry_data[child_offset : child_offset + 4])[0]
        child_type = entry_data[child_offset + 4 : child_offset + 8]

        if child_size < 8 or child_offset + child_size > len(entry_data):
            break

        if child_type != b"sinf":
            new_entry += entry_data[child_offset : child_offset + child_size]

        child_offset += child_size

    # Update size
    new_size = len(new_entry)
    new_entry = struct.pack(">I", new_size) + new_entry[4:]

    return new_entry


def _extract_alac_config(data: bytes) -> Optional[bytes]:
    """Extract ALAC configuration from moov/stsd box (for backwards compatibility)."""
    # Simple search for 'alac' box in data
    idx = data.find(b"alac")
    if idx < 4:
        return None

    # Check if it's inside stsd (look for full structure)
    # The 'alac' cookie box follows the sample entry
    alac_idx = idx
    while alac_idx < len(data) - 100:
        if data[alac_idx : alac_idx + 4] == b"alac":
            size = struct.unpack(">I", data[alac_idx - 4 : alac_idx])[0]
            if 20 < size < 100:  # Reasonable ALAC config size
                return data[alac_idx + 4 : alac_idx - 4 + size]
        alac_idx += 1
        if alac_idx > idx + 200:
            break
    return None


def _extract_timescale(data: bytes) -> int:
    """Extract timescale from moov/mvhd or mdhd box."""
    # Look for mdhd box (media header has the audio timescale)
    idx = data.find(b"mdhd")
    if idx > 0 and idx + 28 < len(data):
        # mdhd: size(4) + type(b'mdhd') + version(1) + flags(3)
        version = data[idx + 4]
        if version == 0:
            # v0: ver+flags(4) + creation(4) + modification(4) + timescale(4)
            return struct.unpack(">I", data[idx + 16 : idx + 20])[0]
        else:
            # v1: ver+flags(4) + creation(8) + modification(8) + timescale(4)
            return struct.unpack(">I", data[idx + 24 : idx + 28])[0]
    return 44100  # Default fallback


def _extract_track_timescale(
    data: bytes, handler_type: bytes = b"soun", default: int = 44100
) -> int:
    """Extract mdhd timescale from the selected track."""
    moov_idx = data.find(b"moov")
    if moov_idx < 4:
        return default
    moov_size = struct.unpack(">I", data[moov_idx - 4 : moov_idx])[0]
    if moov_size < 8 or moov_idx - 4 + moov_size > len(data):
        return default
    moov_data = data[moov_idx - 4 : moov_idx - 4 + moov_size]
    trak = _find_track_by_handler(moov_data, handler_type)
    mdia = _find_child_box(trak, b"mdia") if trak else None
    mdhd = _find_child_box(mdia, b"mdhd") if mdia else None
    if not mdhd or len(mdhd) < 28:
        return default
    version = mdhd[8]
    if version == 0 and len(mdhd) >= 24:
        return struct.unpack(">I", mdhd[20:24])[0]
    if version == 1 and len(mdhd) >= 32:
        return struct.unpack(">I", mdhd[28:32])[0]
    return default


def _find_child_box(
    container_data: bytes, target_type: bytes, skip_header: int = 8
) -> Optional[bytes]:
    """Find a direct child box in container data.

    Args:
        container_data: Raw bytes of the container box (including its own header).
        target_type: 4-byte box type to search for.
        skip_header: Bytes to skip at start (8 for plain box, 12 for FullBox).

    Returns:
        Full box bytes (size + type + content) or None.
    """
    offset = skip_header
    while offset + 8 <= len(container_data):
        size = struct.unpack(">I", container_data[offset : offset + 4])[0]
        box_type = container_data[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(container_data):
            break
        if box_type == target_type:
            return container_data[offset : offset + size]
        offset += size
    return None


def _find_track_by_handler(moov_data: bytes, handler_type: bytes) -> Optional[bytes]:
    """Find the trak box in moov data for a media handler.

    Iterates trak children and returns the first one whose mdia/hdlr has
    the requested handler type. Returns full trak box bytes or None.
    """
    offset = 8  # Skip moov header
    while offset + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[offset : offset + 4])[0]
        box_type = moov_data[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(moov_data):
            break
        if box_type == b"trak":
            trak_data = moov_data[offset : offset + size]
            hdlr_idx = trak_data.find(b"hdlr")
            if hdlr_idx > 0:
                # hdlr FullBox: version+flags(4) + pre_defined(4) + handler_type(4)
                handler_offset = hdlr_idx + 4 + 4 + 4
                if handler_offset + 4 <= len(trak_data):
                    if trak_data[handler_offset : handler_offset + 4] == handler_type:
                        return trak_data
        offset += size
    return None


def _find_audio_trak(moov_data: bytes) -> Optional[bytes]:
    """Find the audio trak box in moov data."""
    return _find_track_by_handler(moov_data, b"soun")


def _patch_mvhd_duration(box_data: bytes, duration: int, timescale: int) -> bytes:
    """Return a copy of the mvhd box with its duration and timescale fields patched."""
    data = bytearray(box_data)
    version = data[8]  # After size(4) + type(4)
    if version == 0:
        # v0: ver+flags(4) + creation(4) + modification(4) + timescale(4) + duration(4)
        struct.pack_into(">I", data, 20, timescale)
        struct.pack_into(">I", data, 24, duration)
    else:
        # v1: ver+flags(4) + creation(8) + modification(8) + timescale(4) + duration(8)
        struct.pack_into(">I", data, 28, timescale)
        struct.pack_into(">Q", data, 32, duration)
    return bytes(data)


def _patch_tkhd_duration(box_data: bytes, duration: int) -> bytes:
    """Return a copy of the tkhd box with duration patched and flags set to 7."""
    data = bytearray(box_data)
    version = data[8]
    # Set flags = 7 (enabled | in_movie | in_preview)
    data[9:12] = struct.pack(">I", 7)[1:]  # 3-byte flags
    if version == 0:
        # v0: ver+flags(4) + creation(4) + modification(4) + track_id(4) + reserved(4) + duration(4)
        struct.pack_into(">I", data, 28, duration)
    else:
        # v1: ver+flags(4) + creation(8) + modification(8) + track_id(4) + reserved(4) + duration(8)
        struct.pack_into(">Q", data, 36, duration)
    return bytes(data)


def _patch_mdhd_duration(box_data: bytes, duration: int, timescale: int) -> bytes:
    """Return a copy of the mdhd box with its duration and timescale fields patched."""
    data = bytearray(box_data)
    version = data[8]
    if version == 0:
        # v0: ver+flags(4) + creation(4) + modification(4) + timescale(4) + duration(4)
        struct.pack_into(">I", data, 20, timescale)
        struct.pack_into(">I", data, 24, duration)
    else:
        # v1: ver+flags(4) + creation(8) + modification(8) + timescale(4) + duration(8)
        struct.pack_into(">I", data, 28, timescale)
        struct.pack_into(">Q", data, 32, duration)
    return bytes(data)


def _write_udta(f):
    """Write udta > meta > hdlr(mdir) + ilst metadata container.

    This matches the Go amdecrypt output which creates an empty metadata
    container so tools can find and write metadata atoms.
    """
    udta_start = f.tell()
    f.write(b"\x00" * 8)  # udta placeholder

    meta_start = f.tell()
    f.write(b"\x00" * 8)  # meta placeholder
    # meta is a FullBox: version(1) + flags(3)
    f.write(struct.pack(">I", 0))  # version + flags

    # hdlr for metadata (handler_type = 'mdir', reserved = 'appl' + zeros)
    hdlr_content = struct.pack(">I", 0)  # pre_defined
    hdlr_content += b"mdir"  # handler_type
    hdlr_content += struct.pack(">III", 0x6170706C, 0, 0)  # reserved ('appl', 0, 0)
    hdlr_content += b"\x00"  # empty name (null terminator)
    _write_fullbox(f, b"hdlr", 0, 0, hdlr_content)

    # ilst (empty)
    _write_box(f, b"ilst", b"")

    _fixup_box_size(f, meta_start, b"meta")
    _fixup_box_size(f, udta_start, b"udta")


def _extract_trex_defaults(moov_data: bytes, target_track_id: int = 0) -> dict:
    """Extract default sample values from moov/mvex/trex box.

    The trex (Track Extends) box provides default values for sample duration,
    size, description index, and flags used by track fragments (traf/trun)
    when those fields are not explicitly present.

    Args:
        moov_data: Raw bytes of the moov box.
        target_track_id: If > 0, only return defaults for this track.
                         If 0, return the first trex found.

    Returns:
        Dict with keys: default_sample_duration, default_sample_size,
        default_sample_description_index, default_sample_flags.
    """
    # Determine fallback duration based on codec
    # ALAC frames are 4096 samples, AAC frames are 1024 samples
    is_alac = b"alac" in moov_data or b"ALAC" in moov_data
    fallback_duration = 4096 if is_alac else 1024

    defaults = {
        "default_sample_duration": fallback_duration,
        "default_sample_size": 0,
        "default_sample_description_index": 1,
        "default_sample_flags": 0,
    }

    # Find mvex box inside moov
    mvex = _find_child_box(moov_data, b"mvex")
    if mvex is None:
        return defaults

    # Iterate trex children inside mvex
    offset = 8  # Skip mvex box header
    while offset + 8 <= len(mvex):
        size = struct.unpack(">I", mvex[offset : offset + 4])[0]
        box_type = mvex[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(mvex):
            break
        if box_type == b"trex" and size >= 32:
            # trex FullBox: size(4) + type(4) + version(1) + flags(3)
            #   + track_id(4) + default_sample_description_index(4)
            #   + default_sample_duration(4) + default_sample_size(4)
            #   + default_sample_flags(4)
            trex_data = mvex[offset : offset + size]
            track_id = struct.unpack(">I", trex_data[12:16])[0]
            if target_track_id == 0 or track_id == target_track_id:
                defaults["default_sample_description_index"] = struct.unpack(
                    ">I", trex_data[16:20]
                )[0]

                # Extract duration and protect against Apple's dummy values
                parsed_duration = struct.unpack(">I", trex_data[20:24])[0]

                # Override if the provider wrote 0, or if they incorrectly wrote 1024 for an ALAC track
                if parsed_duration == 0 or (is_alac and parsed_duration == 1024):
                    defaults["default_sample_duration"] = fallback_duration
                else:
                    defaults["default_sample_duration"] = parsed_duration

                defaults["default_sample_size"] = struct.unpack(">I", trex_data[24:28])[
                    0
                ]
                defaults["default_sample_flags"] = struct.unpack(
                    ">I", trex_data[28:32]
                )[0]
                break
        offset += size

    return defaults


def _extract_encryption_info(
    moov_data: bytes, handler_type: bytes = b"soun"
) -> Optional[EncryptionInfo]:
    """Extract encryption scheme info from the selected track's sinf box.

    Walks moov → trak (audio) → mdia → minf → stbl → stsd → sample_entry → sinf,
    then reads sinf/schm for scheme_type and sinf/schi/tenc for IV size, constant IV,
    and default KID.

    Returns EncryptionInfo or None if no sinf is found.
    """
    trak_data = _find_track_by_handler(moov_data, handler_type)
    if trak_data is None:
        return None

    # Navigate trak → mdia → minf → stbl → stsd
    mdia = _find_child_box(trak_data, b"mdia")
    if mdia is None:
        return None
    minf = _find_child_box(mdia, b"minf")
    if minf is None:
        return None
    stbl = _find_child_box(minf, b"stbl")
    if stbl is None:
        return None
    stsd = _find_child_box(stbl, b"stsd")
    if stsd is None:
        return None

    # stsd is a FullBox: 4 (size) + 4 (type) + 4 (version+flags) + 4 (entry_count)
    # Then the first sample entry immediately follows
    if len(stsd) < 16:
        return None
    entry_offset = 16  # past header+version+flags+entry_count
    if entry_offset + 8 > len(stsd):
        return None
    entry_size = struct.unpack(">I", stsd[entry_offset : entry_offset + 4])[0]
    entry_data = stsd[entry_offset : entry_offset + entry_size]

    # Find sinf inside this sample entry
    sample_entry_header_size = _sample_entry_header_size(entry_data[4:8])
    sinf = _find_child_box(entry_data, b"sinf", skip_header=sample_entry_header_size)
    if sinf is None:
        return None

    info = EncryptionInfo()

    # Parse schm (Scheme Type Box) inside sinf
    schm = _find_child_box(sinf, b"schm")
    if schm and len(schm) >= 20:
        # schm: 4(size) + 4(type) + 4(ver+flags) + 4(scheme_type) + 4(scheme_version)
        info.scheme_type = schm[12:16].decode("ascii", errors="replace")

    # Parse tenc (Track Encryption Box) inside sinf/schi
    schi = _find_child_box(sinf, b"schi")
    if schi:
        tenc = _find_child_box(schi, b"tenc")
        if tenc and len(tenc) >= 32:
            # tenc FullBox layout (offsets include 8-byte box header):
            #   [0:4]  size
            #   [4:8]  type "tenc"
            #   [8]    version
            #   [9:12] flags (3 bytes)
            #   [12]   reserved
            #   [13]   reserved (v0) / crypt_byte_block|skip_byte_block (v1)
            #   [14]   default_isProtected
            #   [15]   default_Per_Sample_IV_Size
            #   [16:32] default_KID (16 bytes)
            #   if per_sample_iv_size==0:
            #     [32]   default_constant_IV_size
            #     [33..] default_constant_IV
            tenc_version = tenc[8]
            if tenc_version > 0:
                pattern = tenc[13]
                info.crypt_byte_block = pattern >> 4
                info.skip_byte_block = pattern & 0x0F
                per_sample_iv_size = tenc[15]
                kid = tenc[16:32]
                constant_iv_offset = 32
            else:
                per_sample_iv_size = tenc[15]
                kid = tenc[16:32]
                constant_iv_offset = 32
            info.per_sample_iv_size = per_sample_iv_size
            info.kid = kid

            # If per_sample_iv_size is 0, a constant IV follows the KID
            if per_sample_iv_size == 0 and len(tenc) > constant_iv_offset:
                constant_iv_size = tenc[constant_iv_offset]
                if len(tenc) >= constant_iv_offset + 1 + constant_iv_size:
                    info.constant_iv = tenc[
                        constant_iv_offset
                        + 1 : constant_iv_offset
                        + 1
                        + constant_iv_size
                    ]

    return info


def _extract_encryption_info_per_stsd(
    moov_data: bytes, handler_type: bytes = b"soun"
) -> Optional[dict]:
    """Extract encryption scheme info for each stsd entry (sample description).

    Returns a dict mapping desc_index (0-based) → EncryptionInfo, or None if no
    encryption found. This handles cases where different sample descriptions have
    different encryption parameters (e.g., different IVs or key schemes).
    """
    trak_data = _find_track_by_handler(moov_data, handler_type)
    if trak_data is None:
        return None

    # Navigate trak → mdia → minf → stbl → stsd
    mdia = _find_child_box(trak_data, b"mdia")
    if mdia is None:
        return None
    minf = _find_child_box(mdia, b"minf")
    if minf is None:
        return None
    stbl = _find_child_box(minf, b"stbl")
    if stbl is None:
        return None
    stsd = _find_child_box(stbl, b"stsd")
    if stsd is None:
        return None

    if len(stsd) < 16:
        return None

    entry_count = struct.unpack(">I", stsd[12:16])[0]
    if entry_count == 0:
        return None

    encryption_info_per_desc = {}
    entry_offset = 16  # past header+version+flags+entry_count

    for desc_idx in range(entry_count):
        if entry_offset + 8 > len(stsd):
            break

        entry_size = struct.unpack(">I", stsd[entry_offset : entry_offset + 4])[0]
        if entry_size < 8 or entry_offset + entry_size > len(stsd):
            break

        entry_data = stsd[entry_offset : entry_offset + entry_size]

        sample_entry_header_size = _sample_entry_header_size(entry_data[4:8])
        sinf = _find_child_box(
            entry_data, b"sinf", skip_header=sample_entry_header_size
        )
        if sinf is not None:
            # Extract encryption info for this stsd entry
            info = EncryptionInfo()

            # Parse schm
            schm = _find_child_box(sinf, b"schm")
            if schm and len(schm) >= 20:
                info.scheme_type = schm[12:16].decode("ascii", errors="replace")

            # Parse tenc
            schi = _find_child_box(sinf, b"schi")
            if schi:
                tenc = _find_child_box(schi, b"tenc")
                if tenc and len(tenc) >= 32:
                    tenc_version = tenc[8]

                    if tenc_version > 0:
                        pattern = tenc[13]
                        info.crypt_byte_block = pattern >> 4
                        info.skip_byte_block = pattern & 0x0F
                        per_sample_iv_size = tenc[15]
                        kid = tenc[16:32]
                        constant_iv_offset = 32
                    else:
                        per_sample_iv_size = tenc[15]
                        kid = tenc[16:32]
                        constant_iv_offset = 32
                    info.per_sample_iv_size = per_sample_iv_size
                    info.kid = kid

                    # If per_sample_iv_size is 0, extract constant IV
                    if per_sample_iv_size == 0 and len(tenc) > constant_iv_offset:
                        constant_iv_size = tenc[constant_iv_offset]
                        if len(tenc) >= constant_iv_offset + 1 + constant_iv_size:
                            info.constant_iv = tenc[
                                constant_iv_offset
                                + 1 : constant_iv_offset
                                + 1
                                + constant_iv_size
                            ]

            encryption_info_per_desc[desc_idx] = info

        entry_offset += entry_size

    return encryption_info_per_desc if encryption_info_per_desc else None


def _extract_track_id(
    moov_data: bytes, handler_type: bytes = b"soun", default_track_id: int = 1
) -> int:
    """Extract the track ID for the requested handler from the moov box.

    Parses trak boxes in moov to find one with the requested handler, then
    returns its track_id from tkhd.
    """
    offset = 8  # Skip moov box header
    while offset < len(moov_data) - 8:
        size = struct.unpack(">I", moov_data[offset : offset + 4])[0]
        box_type = moov_data[offset + 4 : offset + 8]

        if size < 8 or offset + size > len(moov_data):
            break

        if box_type == b"trak":
            trak_data = moov_data[offset : offset + size]

            # Check handler type in hdlr box
            hdlr_idx = trak_data.find(b"hdlr")
            if hdlr_idx > 0:
                # hdlr FullBox: after 'hdlr' type comes version+flags(4) + pre_defined(4) + handler_type(4)
                handler_offset = hdlr_idx + 4 + 4 + 4
                if handler_offset + 4 <= len(trak_data):
                    parsed_handler_type = trak_data[handler_offset : handler_offset + 4]
                    if parsed_handler_type == handler_type:
                        # Found requested track, extract track_id from tkhd
                        tkhd_idx = trak_data.find(b"tkhd")
                        if tkhd_idx > 0:
                            version = trak_data[tkhd_idx + 4]
                            if version == 0:
                                # v0: ver+flags(4) + creation(4) + modification(4) + track_id(4)
                                tid_offset = tkhd_idx + 4 + 4 + 4 + 4
                            else:
                                # v1: ver+flags(4) + creation(8) + modification(8) + track_id(4)
                                tid_offset = tkhd_idx + 4 + 4 + 8 + 8
                            if tid_offset + 4 <= len(trak_data):
                                return struct.unpack(
                                    ">I", trak_data[tid_offset : tid_offset + 4]
                                )[0]

        offset += size

    return default_track_id


def _extract_audio_track_id(moov_data: bytes) -> int:
    """Extract the track ID of the audio track from the moov box."""
    return _extract_track_id(moov_data, b"soun", 1)


async def _decrypt_track_wrapper(
    wrapper_api: WrapperApi,
    track_id: str,
    fairplay_key: str,
    input_path: str,
    handler_type: bytes = b"soun",
    *,
    use_single_content_key: bool = False,
    file_backed: bool = False,
    progress_callback=None,
) -> DecryptedTrack:
    """Decrypt one track through wrapper-v2 (CBCS via FairPlay SKD)."""
    song_info = await asyncio.to_thread(
        extract_song, input_path, handler_type, file_backed
    )
    enc_info = song_info.encryption_info or EncryptionInfo(scheme_type="cbcs")
    enc_info_per_desc = None
    if song_info.moov_data:
        enc_info_per_desc = await asyncio.to_thread(
            _extract_encryption_info_per_stsd,
            song_info.moov_data,
            handler_type,
        )

    temp_path = None
    if file_backed:
        temp_file = tempfile.NamedTemporaryFile(
            prefix="gamdl_decrypted_", suffix=".bin", delete=False
        )
        temp_path = temp_file.name
        temp_file.close()
    try:
        decrypted_data = await decrypt_samples(
            wrapper_api,
            track_id,
            fairplay_key,
            song_info.samples,
            enc_info,
            enc_info_per_desc,
            use_single_content_key=use_single_content_key,
            progress_callback=progress_callback,
            decrypted_data_path=temp_path,
        )
    except Exception:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
        raise
    if temp_path:
        return DecryptedTrack(
            input_path,
            song_info,
            data_path=temp_path,
            data_size=os.path.getsize(temp_path),
        )
    return DecryptedTrack(input_path, song_info, decrypted_data)


async def decrypt_wrapper(
    wrapper_api: WrapperApi,
    track_id: str,
    input_audio_path: str,
    input_video_path: str | None = None,
    fairplay_key_video: str | None = None,
    *,
    fairplay_key_audio: str | None = None,
    use_single_content_key: bool = False,
    progress_callback=None,
) -> DecryptedMedia:
    """Decrypt audio and optional video through wrapper-v2 (CBCS)."""
    if fairplay_key_audio is None:
        if input_video_path is None and fairplay_key_video is not None:
            fairplay_key_audio = fairplay_key_video
        else:
            raise ValueError("fairplay_key_audio is required for wrapper audio decrypt")

    audio = await _decrypt_track_wrapper(
        wrapper_api,
        track_id,
        fairplay_key_audio,
        input_audio_path,
        b"soun",
        use_single_content_key=use_single_content_key,
        progress_callback=progress_callback,
    )
    if input_video_path is None:
        return DecryptedMedia(audio=audio)

    if fairplay_key_video is None:
        raise ValueError("fairplay_key_video is required for wrapper video decrypt")

    video_task = asyncio.create_task(
        _decrypt_track_wrapper(
            wrapper_api,
            track_id,
            fairplay_key_video,
            input_video_path,
            b"vide",
            use_single_content_key=use_single_content_key,
            file_backed=True,
            progress_callback=progress_callback,
        )
    )
    caption_tracks = [
        track
        for track in await asyncio.gather(
            asyncio.to_thread(extract_song, input_video_path, b"clcp", True),
            asyncio.to_thread(extract_song, input_video_path, b"text", True),
            asyncio.to_thread(extract_song, input_video_path, b"sbtl", True),
            asyncio.to_thread(extract_song, input_video_path, b"subt", True),
        )
        if track.samples
    ]
    captions = [
        DecryptedTrack(
            input_video_path,
            caption_track,
            _sample_payload_bytes(caption_track.samples),
        )
        for caption_track in caption_tracks
    ]

    return DecryptedMedia(
        audio=audio,
        video=await video_task,
        captions=captions,
    )


decrypt_file = decrypt_wrapper


def decrypt_samples_hex(
    samples: List[SampleInfo],
    keys: dict,
    encryption_info: EncryptionInfo,
    encryption_info_per_desc: Optional[dict] = None,
) -> bytes:
    """Decrypt samples using hex AES keys (no wrapper needed).

    Supports both CENC (AES-128-CTR) and CBCS (AES-128-CBC) schemes.

    Args:
        samples: List of SampleInfo with data, desc_index, iv, subsamples.
        keys: Mapping of desc_index (int) → AES key (16 bytes, raw).
        encryption_info: EncryptionInfo with scheme_type, constant_iv, etc.
        encryption_info_per_desc: Optional dict mapping desc_index → EncryptionInfo
                                   (used when different stsd entries have different params).

    Returns:
        Concatenated decrypted sample data.
    """
    decrypted = bytearray()

    for sample in samples:
        key = keys.get(sample.desc_index)
        if key is None:
            # No key for this desc_index — keep data as-is (shouldn't happen)
            decrypted.extend(_sample_data(sample))
            continue

        # Get encryption info for this sample's desc_index (if per-description info exists)
        if encryption_info_per_desc and sample.desc_index in encryption_info_per_desc:
            enc_info = encryption_info_per_desc[sample.desc_index]
        else:
            enc_info = encryption_info

        if not sample.data and sample.data_path:
            sample = _with_sample_data(sample, _sample_data(sample))

        is_cenc = enc_info.scheme_type == "cenc"
        if is_cenc:
            # AES-128-CTR: per-sample IV from senc, zero-padded to 16 bytes
            data = sample.data
            iv = sample.iv
            if len(iv) < 16:
                iv = iv + b"\x00" * (16 - len(iv))
            cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

            if sample.subsamples:
                plaintext = bytearray()
                offset = 0
                for clear_bytes, encrypted_bytes in sample.subsamples:
                    plaintext.extend(data[offset : offset + clear_bytes])
                    offset += clear_bytes
                    plaintext.extend(
                        cipher.decrypt(data[offset : offset + encrypted_bytes])
                    )
                    offset += encrypted_bytes
                plaintext.extend(data[offset:])
                decrypted.extend(plaintext)
            else:
                decrypted.extend(cipher.decrypt(data))

        else:
            # CBCS (AES-128-CBC): constant IV or per-sample IV
            if enc_info.crypt_byte_block and enc_info.skip_byte_block:
                decrypted.extend(
                    _decrypt_cbcs_sample_with_pattern(sample, key, enc_info)
                )
                continue

            iv = sample.iv if sample.iv else enc_info.constant_iv
            if len(iv) < 16:
                iv = iv + b"\x00" * (16 - len(iv))

            parts = _cbcs_ciphertext_for_sample(sample)
            if parts is None:
                decrypted.extend(sample.data)
                continue

            aligned, tail = parts
            plain = b""
            if aligned:
                cipher = AES.new(key, AES.MODE_CBC, iv=iv)
                plain = cipher.decrypt(aligned)
            decrypted.extend(_reassemble_cbcs_sample(sample, plain, tail))

    return bytes(decrypted)


def _decrypt_sample_hex(
    sample: SampleInfo,
    key: Optional[bytes],
    encryption_info: EncryptionInfo,
) -> bytes:
    """Decrypt one sample with a raw AES key."""
    data = _sample_data(sample)
    if data is not sample.data:
        sample = _with_sample_data(sample, data)

    if key is None:
        return data

    is_cenc = encryption_info.scheme_type == "cenc"
    if is_cenc:
        iv = sample.iv
        if len(iv) < 16:
            iv = iv + b"\x00" * (16 - len(iv))
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

        if not sample.subsamples:
            return cipher.decrypt(data)

        plaintext = bytearray()
        offset = 0
        for clear_bytes, encrypted_bytes in sample.subsamples:
            plaintext.extend(data[offset : offset + clear_bytes])
            offset += clear_bytes
            plaintext.extend(
                cipher.decrypt(data[offset : offset + encrypted_bytes])
            )
            offset += encrypted_bytes
        plaintext.extend(data[offset:])
        return bytes(plaintext)

    if encryption_info.crypt_byte_block and encryption_info.skip_byte_block:
        return _decrypt_cbcs_sample_with_pattern(sample, key, encryption_info)

    iv = sample.iv if sample.iv else encryption_info.constant_iv
    if len(iv) < 16:
        iv = iv + b"\x00" * (16 - len(iv))

    parts = _cbcs_ciphertext_for_sample(sample)
    if parts is None:
        return sample.data

    aligned, tail = parts
    plain = b""
    if aligned:
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        plain = cipher.decrypt(aligned)
    return _reassemble_cbcs_sample(sample, plain, tail)


def decrypt_samples_hex_to_file(
    samples: List[SampleInfo],
    keys: dict,
    encryption_info: EncryptionInfo,
    output_path: str,
    encryption_info_per_desc: Optional[dict] = None,
    release_sample_data: bool = False,
) -> int:
    """Decrypt samples to a raw payload file without building one large bytes object."""
    bytes_written = 0
    with open(output_path, "wb") as f:
        for sample in samples:
            enc_info = (
                encryption_info_per_desc[sample.desc_index]
                if encryption_info_per_desc
                and sample.desc_index in encryption_info_per_desc
                else encryption_info
            )
            decrypted_sample = _decrypt_sample_hex(
                sample,
                keys.get(sample.desc_index),
                enc_info,
            )
            f.write(decrypted_sample)
            sample.size = len(decrypted_sample)
            bytes_written += len(decrypted_sample)
            if release_sample_data:
                sample.data = b""
                sample.subsamples = []
                sample.iv = b""
    return bytes_written
