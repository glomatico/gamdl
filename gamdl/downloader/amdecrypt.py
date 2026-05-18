"""
This is a modified version of https://github.com/sn0wst0rm/st0rmMusicPlayer/blob/main/scripts/amdecrypt.py
All the modifications made here were AI generated

FairPlay sample decryption talks to wrapper-v2 over HTTP POST /decrypt/sample
(binary frame), not the legacy raw TCP port used by the original wrapper (e.g. 10020).
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional
from urllib.parse import urlparse

import httpx

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# Default decryption key for songs without per-sample keys (legacy AAC)
DEFAULT_SONG_DECRYPTION_KEY = b"2\xb8\xad\xe1v\x9e&\xb1\xff\xb8\x98cRy?\xc6"

# Pre-fetch key used for first sample description
PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"

# wrapper-v2 HTTP API base (no trailing slash). Override via decrypt_file(..., wrapper_ip=...).
DEFAULT_WRAPPER_IP = "http://127.0.0.1:80"

# Max ciphertext blobs per POST /decrypt/sample (same adam_id + uri). Increase for fewer
# round-trips; set to 1 if a given wrapper build mis-handles CBC between chunks.
WRAPPER_DECRYPT_BATCH_SIZE = 128

# wrapper-v2: use one SKD segment per ``adam_id``+``uri`` (do not interleave prefetch
# and main keys in one request). Flush pending ciphertexts before switching keys.


def _wrapper_v2_base_url(wrapper_ip: str) -> str:
    """Normalize config to an HTTP origin for wrapper-v2.

    Accepts:
    - ``http://host:port`` / ``https://host:port``
    - ``host:port`` (becomes ``http://host:port``) — legacy "IP:port" strings
    - ``host`` only (becomes ``http://host:80``)
    """
    s = (wrapper_ip or "").strip().rstrip("/")
    if not s:
        s = DEFAULT_WRAPPER_IP
    if "://" in s:
        u = urlparse(s)
        if not u.scheme or not u.netloc:
            raise ValueError(f"Invalid wrapper URL: {wrapper_ip!r}")
        return f"{u.scheme}://{u.netloc}"
    if ":" in s:
        host, _, port = s.partition(":")
        return f"http://{host}:{port}"
    return f"http://{s}:80"


def _build_decrypt_sample_frame(adam_id: str, skd_uri: str, ciphertexts: List[bytes]) -> bytes:
    """Build wrapper-v2 /decrypt/sample binary request frame."""
    adam_id_bytes = adam_id.encode("utf-8")
    skd_uri_bytes = skd_uri.encode("utf-8")
    if not adam_id_bytes:
        raise ValueError("wrapper-v2: adam_id must not be empty")
    if not skd_uri_bytes:
        raise ValueError("wrapper-v2: skd_uri must not be empty")
    if not ciphertexts:
        raise ValueError("wrapper-v2: ciphertext batch must not be empty")

    frame = bytearray()
    frame += struct.pack(">III", len(adam_id_bytes), len(skd_uri_bytes), len(ciphertexts))
    for ciphertext in ciphertexts:
        frame += struct.pack(">I", len(ciphertext))
    frame += adam_id_bytes
    frame += skd_uri_bytes
    for ciphertext in ciphertexts:
        frame += ciphertext
    return bytes(frame)


def _parse_decrypt_sample_frame(data: bytes, expected_count: int) -> List[bytes]:
    """Parse wrapper-v2 /decrypt/sample binary response frame."""
    if len(data) < 4:
        raise IOError("wrapper-v2: POST /decrypt/sample returned a truncated response")
    (sample_count,) = struct.unpack_from(">I", data, 0)
    if sample_count != expected_count:
        raise IOError(
            f"wrapper-v2: expected {expected_count} samples in response, got {sample_count}"
        )

    table_end = 4 + sample_count * 4
    if len(data) < table_end:
        raise IOError("wrapper-v2: POST /decrypt/sample returned a truncated length table")

    lengths = [struct.unpack_from(">I", data, 4 + i * 4)[0] for i in range(sample_count)]
    offset = table_end
    out: List[bytes] = []
    for i, length in enumerate(lengths):
        end = offset + length
        if end > len(data):
            raise IOError(f"wrapper-v2: POST /decrypt/sample returned truncated sample {i}")
        out.append(data[offset:end])
        offset = end

    if offset != len(data):
        raise IOError("wrapper-v2: POST /decrypt/sample returned trailing bytes")
    return out


async def _post_decrypt_batch(
    client: httpx.AsyncClient,
    base_url: str,
    adam_id: str,
    skd_uri: str,
    ciphertexts: List[bytes],
) -> List[bytes]:
    """One POST /decrypt/sample; ciphertexts and returned plaintexts are in order."""
    frame = _build_decrypt_sample_frame(adam_id, skd_uri, ciphertexts)
    r = await client.post(
        f"{base_url}/decrypt/sample",
        content=frame,
        headers={
            "content-type": "application/octet-stream",
            "accept": "application/octet-stream",
        },
    )
    if r.status_code == 401:
        raise IOError(
            "wrapper-v2: POST /decrypt/sample returned 401 — log in with POST /login "
            "or restore a session on the daemon first"
        )
    if r.status_code == 503:
        raise IOError(
            "wrapper-v2: decrypt unavailable (503) — check daemon logs /health "
            "for playback_ready and Apple lib init"
        )
    if r.status_code != 200:
        detail = ""
        try:
            j = r.json()
            detail = (j.get("detail") or j.get("error") or str(j)) or ""
        except Exception:
            detail = (r.text or "")[:500]
        raise IOError(
            f"wrapper-v2: POST /decrypt/sample failed HTTP {r.status_code}: {detail}"
        )

    return _parse_decrypt_sample_frame(r.content, len(ciphertexts))


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
        raise IOError(f"reassembled sample length mismatch: expected {len(data)}, got {len(out)}")
    return bytes(out)


def _append_reassembled_sample(decrypted_data: bytearray, sample: SampleInfo, plain: bytes, tail: bytes) -> None:
    """Append one decrypted CBCS sample to the output stream."""
    decrypted_data.extend(_reassemble_cbcs_sample(sample, plain, tail))


@dataclass
class SampleInfo:
    """Information about a single audio sample."""

    data: bytes
    duration: int
    desc_index: int
    iv: bytes = b""  # Per-sample IV from senc (empty if constant IV)
    subsamples: List[tuple] = field(
        default_factory=list
    )  # [(clear_bytes, encrypted_bytes), ...]


@dataclass
class EncryptionInfo:
    """Encryption scheme info extracted from sinf/schm + sinf/schi/tenc."""

    scheme_type: str = "cbcs"  # 'cenc' or 'cbcs'
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


def extract_song(input_path: str) -> SongInfo:
    """
    Extract song samples and metadata from encrypted MP4 file.

    This parses the MP4 structure to extract:
    - ftyp and moov boxes (for reassembly)
    - Individual audio samples from mdat boxes
    - Sample durations and description indices from moof boxes
    """
    with open(input_path, "rb") as f:
        raw_data = f.read()

    song_info = SongInfo()

    # First pass: collect all top-level boxes
    boxes = []
    offset = 0
    while offset < len(raw_data) - 8:
        size = struct.unpack(">I", raw_data[offset : offset + 4])[0]
        box_type = raw_data[offset + 4 : offset + 8].decode("ascii", errors="replace")

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

    logger.debug(f"Found {len(boxes)} top-level boxes")

    # Extract ftyp and moov
    for box in boxes:
        if box["type"] == "ftyp":
            song_info.ftyp_data = box["data"]
        elif box["type"] == "moov":
            song_info.moov_data = box["data"]

    # Determine which track is the audio track
    audio_track_id = (
        _extract_audio_track_id(song_info.moov_data) if song_info.moov_data else 1
    )
    logger.debug(f"Audio track ID: {audio_track_id}")

    # Get default sample info from trex (inside moov/mvex)
    trex_defaults = (
        _extract_trex_defaults(song_info.moov_data, audio_track_id)
        if song_info.moov_data
        else None
    )
    if trex_defaults:
        default_sample_duration = trex_defaults["default_sample_duration"]
        default_sample_size = trex_defaults["default_sample_size"]
    else:
        # Fallback defaults. ALAC typically uses 4096 samples per frame,
        # while AAC uses 1024. Default to 4096 if the track contains 'alac'.
        is_alac = song_info.moov_data and b"alac" in song_info.moov_data
        default_sample_duration = 4096 if is_alac else 1024
        default_sample_size = 0
    logger.debug(
        f"Default sample duration: {default_sample_duration}, "
        f"default sample size: {default_sample_size}"
    )

    # Extract encryption scheme info from moov (sinf/schm + sinf/schi/tenc)
    if song_info.moov_data:
        song_info.encryption_info = _extract_encryption_info(song_info.moov_data)

    # Parse moof/mdat pairs
    moof_box = None
    for box in boxes:
        if box["type"] == "moof":
            moof_box = box
        elif box["type"] == "mdat" and moof_box is not None:
            # Parse this moof/mdat pair
            moof_data = moof_box["data"]
            mdat_data = box["data"][box["header_size"] :]  # Skip mdat header

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
                audio_track_id=audio_track_id,
                moof_offset=moof_box["offset"],
                mdat_data_offset=box["offset"] + box["header_size"],
                per_sample_iv_size=_iv_size,
            )
            song_info.samples.extend(samples_from_pair)
            moof_box = None

    # Post-process samples: if this is ALAC, ensure all samples have duration 4096.
    # Apple Music fragments often report 1024 in trex/tfhd defaults, but 
    # ALAC frames are actually 4096 samples long. This mismatch is the 
    # root cause of the 1:16 duration reporting for 5-minute tracks.
    is_alac = song_info.moov_data and (b"alac" in song_info.moov_data or b"ALAC" in song_info.moov_data)
    if is_alac:
        logger.debug("ALAC detected: forcing all sample durations to 4096")
        for sample in song_info.samples:
            # Only override if it was 0 or the common incorrect default of 1024
            if sample.duration in (0, 1024):
                sample.duration = 4096

    logger.debug(f"Extracted {len(song_info.samples)} samples from {input_path}")
    return song_info


def _parse_moof_mdat(
    moof_data: bytes,
    mdat_data: bytes,
    default_sample_duration: int,
    default_sample_size: int,
    audio_track_id: int = 1,
    moof_offset: int = 0,
    mdat_data_offset: int = 0,
    per_sample_iv_size: int = 0,
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
                "flags": 0,
                "base_data_offset": None,
            }
            # Each 'trun' has its own optional data_offset; concatenating entry lists
            # and keeping only the first data_offset breaks multi-trun fragments.
            trun_runs: List[tuple] = []
            senc_entries: List[dict] = []

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
                    senc_entries = _parse_senc(
                        moof_data[traf_offset + 8 : traf_offset + inner_size],
                        per_sample_iv_size,
                    )

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

                    if sample_size > 0 and mdat_read_offset + sample_size <= len(
                        mdat_data
                    ):
                        sample_iv = b""
                        sample_subsamples: List[tuple] = []
                        if sample_index_in_traf < len(senc_entries):
                            sample_iv = senc_entries[sample_index_in_traf]["iv"]
                            sample_subsamples = senc_entries[sample_index_in_traf][
                                "subsamples"
                            ]

                        sample = SampleInfo(
                            data=mdat_data[
                                mdat_read_offset : mdat_read_offset + sample_size
                            ],
                            duration=sample_duration,
                            desc_index=desc_index,
                            iv=sample_iv,
                            subsamples=sample_subsamples,
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
    if flags & 0x04:  # first_sample_flags present
        offset += 4

    for _ in range(sample_count):
        entry = {}
        if flags & 0x100 and offset + 4 <= len(data):  # sample_duration
            entry["duration"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x200 and offset + 4 <= len(data):  # sample_size
            entry["size"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x400:  # sample_flags
            offset += 4
        if flags & 0x800:  # sample_composition_time_offset
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


async def decrypt_samples(
    wrapper_ip: str,
    track_id: str,
    fairplay_key: str,
    samples: List[SampleInfo],
    progress_callback=None,
) -> bytes:
    """
    Send samples to wrapper-v2 (HTTP POST /decrypt/sample) for CBCS decryption and
    return decrypted bytes.

    Ciphertext is sent in batches of up to :data:`WRAPPER_DECRYPT_BATCH_SIZE` MP4 samples
    per request (same ``adam_id`` and ``uri``). Literal or tail-only samples are applied
    between batches so output order matches the input.

    Subsample layout matches ``decrypt_samples_hex`` (concat encrypted runs, decrypt
    ``len & ~0xF``, reassemble).

    Requires an authenticated wrapper-v2 session (POST /login or restored session).

    Args:
        wrapper_ip: wrapper-v2 base URL, e.g. ``http://127.0.0.1:80`` or legacy ``host:port``
        progress_callback: Optional callback(current_sample, total_samples, bytes_processed, speed)
    """
    base_url = _wrapper_v2_base_url(wrapper_ip)
    keys = [PREFETCH_KEY, fairplay_key]
    decrypted_data = bytearray()
    last_desc_index: int = 255
    total_samples = len(samples)
    bytes_processed = 0
    start_time = time.time()
    last_progress_time = start_time

    segment_adam: Optional[str] = None
    segment_uri: Optional[str] = None
    # Pending (sample, aligned_cbc, tail) for one SKD segment, flushed in batches.
    crypto_batch: List[tuple] = []

    async def flush_crypto_batch(client: httpx.AsyncClient) -> None:
        if not crypto_batch:
            return
        if segment_adam is None or segment_uri is None:
            raise IOError("wrapper-v2: internal error (segment without adam/uri)")
        chunks = [t[1] for t in crypto_batch]
        tails = [t[2] for t in crypto_batch]
        sources = [t[0] for t in crypto_batch]
        plains = await _post_decrypt_batch(
            client, base_url, segment_adam, segment_uri, chunks
        )
        if len(plains) != len(chunks):
            raise IOError("wrapper-v2: plaintext batch count mismatch")
        for s, plain, tail in zip(sources, plains, tails):
            _append_reassembled_sample(decrypted_data, s, plain, tail)
        crypto_batch.clear()

    timeout = httpx.Timeout(600.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i, sample in enumerate(samples):
            if last_desc_index != sample.desc_index:
                await flush_crypto_batch(client)
                key_uri = keys[min(sample.desc_index, len(keys) - 1)]
                segment_adam = "0" if key_uri == PREFETCH_KEY else track_id
                segment_uri = key_uri
                last_desc_index = sample.desc_index

            parts = _cbcs_ciphertext_for_sample(sample)
            if parts is None:
                await flush_crypto_batch(client)
                decrypted_data.extend(sample.data)
            else:
                aligned, tail = parts
                if len(aligned) == 0:
                    await flush_crypto_batch(client)
                    _append_reassembled_sample(decrypted_data, sample, b"", tail)
                else:
                    crypto_batch.append((sample, aligned, tail))
                    if len(crypto_batch) >= WRAPPER_DECRYPT_BATCH_SIZE:
                        await flush_crypto_batch(client)

            bytes_processed += len(sample.data)

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

        await flush_crypto_batch(client)

    logger.debug(f"Decrypted {len(samples)} samples ({len(decrypted_data)} bytes)")
    return bytes(decrypted_data)


def write_decrypted_m4a(
    output_path: str,
    song_info: SongInfo,
    decrypted_data: bytes,
    original_path: str = None,
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

    if original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
    elif song_info.moov_data:
        orig_data = song_info.ftyp_data + song_info.moov_data
    else:
        orig_data = None

    if orig_data:
        stsd_content = _extract_stsd_content(orig_data, preferred_desc_index)
        # Extract the REAL sample rate from the codec configuration
        timescale = _extract_sample_rate_from_stsd(stsd_content) or _extract_timescale(orig_data)

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
        _write_mdat(f, decrypted_data)

    logger.debug(f"Wrote decrypted file to {output_path}")


def _preferred_sample_description_index(samples: List[SampleInfo]) -> int:
    """Return the 0-based sample description index to keep in flattened output."""
    counts = Counter(sample.desc_index for sample in samples if sample.data)
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
    orig_dinf: Optional[bytes] = None,
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

    # smhd (sound media header)
    if orig_smhd:
        f.write(orig_smhd)
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
        stsz_content += struct.pack(">I", len(sample.data))
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
    sample_rate_fixed = struct.unpack(">I", stsd_content[samplerate_offset : samplerate_offset + 4])[0]
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


def _extract_stsd_content(data: bytes, preferred_desc_index: Optional[int] = None) -> Optional[bytes]:
    """Extract cleaned stsd box content from moov box (supports any codec)."""
    # Find stsd box in the data
    idx = data.find(b"stsd")
    if idx < 4:
        return None

    # Get stsd box size
    size = struct.unpack(">I", data[idx - 4 : idx])[0]
    if size < 16 or size > 10000:  # Reasonable stsd size range
        return None

    # Return stsd content (after box header = size + type)
    raw_content = data[idx + 4 : idx - 4 + size]

    # Clean the stsd content to remove encryption metadata
    return _clean_stsd_content(raw_content, preferred_desc_index)


def _clean_stsd_content(stsd_content: bytes, preferred_desc_index: Optional[int] = None) -> bytes:
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
    if len(entry_data) < 36:  # Minimum audio sample entry size
        return entry_data

    entry_size = struct.unpack(">I", entry_data[:4])[0]
    entry_type = entry_data[4:8]

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

    # Audio sample entry structure:
    # - size (4) + type (4) + reserved (6) + data_ref_index (2) + audio_data (20) = 36 bytes
    # - Then child boxes start at offset 36

    # Copy the fixed header part, replacing the type
    new_entry = entry_data[:4] + original_format + entry_data[8:36]

    # Process child boxes, removing sinf
    child_offset = 36
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
    if len(entry_data) < 36:
        return entry_data

    # Check if sinf exists
    if b"sinf" not in entry_data:
        return entry_data

    # Rebuild entry without sinf
    new_entry = entry_data[:36]  # Keep header and audio data

    child_offset = 36
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


def _find_audio_trak(moov_data: bytes) -> Optional[bytes]:
    """Find the audio trak box in moov data.

    Iterates trak children and returns the first one whose hdlr has
    handler_type == 'soun'. Returns full trak box bytes or None.
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
                    if trak_data[handler_offset : handler_offset + 4] == b"soun":
                        return trak_data
        offset += size
    return None


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

                defaults["default_sample_size"] = struct.unpack(">I", trex_data[24:28])[0]
                defaults["default_sample_flags"] = struct.unpack(
                    ">I", trex_data[28:32]
                )[0]
                logger.debug(
                    f"trex defaults for track {track_id}: "
                    f"duration={defaults['default_sample_duration']}, "
                    f"size={defaults['default_sample_size']}"
                )
                break
        offset += size

    return defaults

def _extract_encryption_info(moov_data: bytes) -> Optional[EncryptionInfo]:
    """Extract encryption scheme info from the audio track's sinf box.

    Walks moov → trak (audio) → mdia → minf → stbl → stsd → sample_entry → sinf,
    then reads sinf/schm for scheme_type and sinf/schi/tenc for IV size, constant IV,
    and default KID.

    Returns EncryptionInfo or None if no sinf is found.
    """
    trak_data = _find_audio_trak(moov_data)
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
    # Audio sample entries have a 36-byte fixed header:
    #   size(4) + type(4) + reserved(6) + data_ref_index(2) + audio_data(20)
    # Child boxes (including sinf) start at offset 36
    sinf = _find_child_box(entry_data, b"sinf", skip_header=36)
    if sinf is None:
        return None

    info = EncryptionInfo()

    # Parse schm (Scheme Type Box) inside sinf
    schm = _find_child_box(sinf, b"schm")
    if schm and len(schm) >= 20:
        # schm: 4(size) + 4(type) + 4(ver+flags) + 4(scheme_type) + 4(scheme_version)
        info.scheme_type = schm[12:16].decode("ascii", errors="replace")
        logger.debug(f"Encryption scheme: {info.scheme_type}")

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
            per_sample_iv_size = tenc[15]
            kid = tenc[16:32]

            info.per_sample_iv_size = per_sample_iv_size
            info.kid = kid
            logger.debug(
                f"tenc: per_sample_iv_size={per_sample_iv_size}, " f"kid={kid.hex()}"
            )

            # If per_sample_iv_size is 0, a constant IV follows the KID
            if per_sample_iv_size == 0 and len(tenc) > 32:
                constant_iv_size = tenc[32]
                if len(tenc) >= 33 + constant_iv_size:
                    info.constant_iv = tenc[33 : 33 + constant_iv_size]
                    logger.debug(f"Constant IV: {info.constant_iv.hex()}")

    return info


def _extract_encryption_info_per_stsd(moov_data: bytes) -> Optional[dict]:
    """Extract encryption scheme info for each stsd entry (sample description).

    Returns a dict mapping desc_index (0-based) → EncryptionInfo, or None if no
    encryption found. This handles cases where different sample descriptions have
    different encryption parameters (e.g., different IVs or key schemes).
    """
    trak_data = _find_audio_trak(moov_data)
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

        # Find sinf inside this sample entry
        # Audio sample entries have 36-byte fixed header before child boxes
        sinf = _find_child_box(entry_data, b"sinf", skip_header=36)
        if sinf is not None:
            # Extract encryption info for this stsd entry
            info = EncryptionInfo()

            # Parse schm
            schm = _find_child_box(sinf, b"schm")
            if schm and len(schm) >= 20:
                info.scheme_type = schm[12:16].decode("ascii", errors="replace")
                logger.debug(
                    f"Encryption scheme for desc_index {desc_idx}: {info.scheme_type}"
                )

            # Parse tenc
            schi = _find_child_box(sinf, b"schi")
            if schi:
                tenc = _find_child_box(schi, b"tenc")
                if tenc and len(tenc) >= 32:
                    per_sample_iv_size = tenc[15]
                    kid = tenc[16:32]

                    info.per_sample_iv_size = per_sample_iv_size
                    info.kid = kid
                    logger.debug(
                        f"tenc (desc {desc_idx}): per_sample_iv_size={per_sample_iv_size}"
                    )

                    # If per_sample_iv_size is 0, extract constant IV
                    if per_sample_iv_size == 0 and len(tenc) > 32:
                        constant_iv_size = tenc[32]
                        if len(tenc) >= 33 + constant_iv_size:
                            info.constant_iv = tenc[33 : 33 + constant_iv_size]
                            logger.debug(
                                f"Constant IV (desc {desc_idx}): {info.constant_iv.hex()}"
                            )

            encryption_info_per_desc[desc_idx] = info

        entry_offset += entry_size

    return encryption_info_per_desc if encryption_info_per_desc else None


def _extract_audio_track_id(moov_data: bytes) -> int:
    """Extract the track ID of the audio track from the moov box.

    Parses trak boxes in moov to find one with handler_type 'soun' (sound),
    then returns its track_id from tkhd. Defaults to 1 if not found.
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
                    handler_type = trak_data[handler_offset : handler_offset + 4]
                    if handler_type == b"soun":
                        # Found audio track, extract track_id from tkhd
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

    return 1  # Default to track 1


async def decrypt_file(
    wrapper_ip: str,
    track_id: str,
    fairplay_key: str,
    input_path: str,
    output_path: str,
    progress_callback=None,
) -> None:
    """
    Main decryption function - decrypt an encrypted MP4 file via the wrapper.

    This is the Python equivalent of the amdecrypt tool:
    1. Extract samples from encrypted MP4
    2. Send samples to wrapper for FairPlay decryption
    3. Reassemble decrypted MP4 with clean metadata

    Args:
        wrapper_ip: wrapper-v2 base URL (e.g. ``http://127.0.0.1:80``) or ``host:port``
            (``http://`` is assumed). The daemon must be logged in and FairPlay init
            must succeed (see GET /health).
        track_id: Apple Music track ID
        fairplay_key: FairPlay key URI (skd://...)
        input_path: Path to encrypted MP4 file
        output_path: Path for decrypted output file
        progress_callback: Optional callback(current, total, bytes, speed) for decryption progress
    """
    logger.debug(f"Decrypting {input_path} -> {output_path}")

    # Extract samples (run in thread to not block)
    song_info = await asyncio.to_thread(extract_song, input_path)

    # Decrypt samples via wrapper
    decrypted_data = await decrypt_samples(
        wrapper_ip,
        track_id,
        fairplay_key,
        song_info.samples,
        progress_callback,
    )

    # Write output file (preserve original structure, replace mdat content)
    # Encryption metadata is automatically cleaned during stsd extraction
    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,  # Pass original path for codec info extraction
    )


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
    is_cenc = encryption_info.scheme_type == "cenc"
    decrypted = bytearray()

    for sample in samples:
        key = keys.get(sample.desc_index)
        if key is None:
            # No key for this desc_index — keep data as-is (shouldn't happen)
            decrypted.extend(sample.data)
            continue

        # Get encryption info for this sample's desc_index (if per-description info exists)
        if encryption_info_per_desc and sample.desc_index in encryption_info_per_desc:
            enc_info = encryption_info_per_desc[sample.desc_index]
        else:
            enc_info = encryption_info

        if is_cenc:
            # AES-128-CTR: per-sample IV from senc, zero-padded to 16 bytes
            iv = sample.iv
            if len(iv) < 16:
                iv = iv + b"\x00" * (16 - len(iv))
            cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

            if sample.subsamples:
                plaintext = bytearray()
                offset = 0
                for clear_bytes, encrypted_bytes in sample.subsamples:
                    plaintext.extend(sample.data[offset : offset + clear_bytes])
                    offset += clear_bytes
                    plaintext.extend(
                        cipher.decrypt(sample.data[offset : offset + encrypted_bytes])
                    )
                    offset += encrypted_bytes
                plaintext.extend(sample.data[offset:])
                decrypted.extend(plaintext)
            else:
                decrypted.extend(cipher.decrypt(sample.data))

        else:
            # CBCS (AES-128-CBC): constant IV or per-sample IV
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

    logger.debug(
        f"Decrypted {len(samples)} samples ({len(decrypted)} bytes) with hex keys"
    )
    return bytes(decrypted)


async def decrypt_file_hex(
    input_path: str,
    output_path: str,
    decryption_key: str,
    legacy: bool = False,
) -> None:
    """Decrypt an encrypted MP4 file using a hex AES key (no wrapper/mp4decrypt).

    This replaces the mp4decrypt + remux pipeline with pure-Python decryption:
    1. Extract samples and encryption info from MP4
    2. Decrypt samples using AES (CTR for cenc / CBC for cbcs)
    3. Write clean decrypted M4A output

    Args:
        input_path: Path to encrypted MP4 file.
        output_path: Path for decrypted output file.
        decryption_key: Hex-encoded 128-bit AES key (32 hex chars).
        legacy: If True, treat as legacy AAC (cenc, single key).
    """
    logger.debug(f"Hex-key decrypt: {input_path} -> {output_path}")

    # Extract samples (run in thread to not block)
    song_info = await asyncio.to_thread(extract_song, input_path)

    # Build key mapping: desc_index → raw AES key bytes
    track_key = bytes.fromhex(decryption_key)

    if legacy:
        # Legacy AAC (cenc): single key for all samples (all desc_index 0)
        keys = {0: track_key}
    else:
        # Non-legacy (cbcs): two sample descriptions
        #   desc_index 0 → DEFAULT_SONG_DECRYPTION_KEY (prefetch samples)
        #   desc_index 1 → track key (from Widevine CDM)
        keys = {0: DEFAULT_SONG_DECRYPTION_KEY, 1: track_key}

    # Use encryption info from the file (fall back to sensible defaults)
    enc_info = song_info.encryption_info or EncryptionInfo(
        scheme_type="cenc" if legacy else "cbcs"
    )

    # Try to extract per-description encryption info (for non-legacy files)
    # This handles cases where desc_index 0 and 1 have different encryption parameters
    enc_info_per_desc = None
    if song_info.moov_data and not legacy:
        enc_info_per_desc = await asyncio.to_thread(
            _extract_encryption_info_per_stsd, song_info.moov_data
        )
        if enc_info_per_desc:
            logger.debug(
                f"Found per-description encryption info: {list(enc_info_per_desc.keys())}"
            )

    # Decrypt
    decrypted_data = decrypt_samples_hex(
        song_info.samples, keys, enc_info, enc_info_per_desc
    )

    # Write output (preserves original metadata boxes)
    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,
    )
