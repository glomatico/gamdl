"""
This is a modified version of https://github.com/sn0wst0rm/st0rmMusicPlayer/blob/main/scripts/amdecrypt.py
All the modifications made here were AI generated
"""

import asyncio
import io
import logging
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional

logger = logging.getLogger(__name__)

# Pre-fetch key used for first sample description
PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"

# Default wrapper address
DEFAULT_WRAPPER_IP = "127.0.0.1:10020"


@dataclass
class SampleInfo:
    """Information about a single audio sample."""

    data: bytes
    duration: int
    desc_index: int


@dataclass
class SongInfo:
    """Extracted song information from MP4 file."""

    samples: List[SampleInfo] = field(default_factory=list)
    moov_data: bytes = b""
    ftyp_data: bytes = b""


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

    # Get default sample info from trex (inside moov)
    default_sample_duration = 1024
    default_sample_size = 0

    # Determine which track is the audio track
    audio_track_id = (
        _extract_audio_track_id(song_info.moov_data) if song_info.moov_data else 1
    )
    logger.debug(f"Audio track ID: {audio_track_id}")

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
            samples_from_pair = _parse_moof_mdat(
                moof_data,
                mdat_data,
                default_sample_duration,
                default_sample_size,
                audio_track_id=audio_track_id,
                moof_offset=moof_box["offset"],
                mdat_data_offset=box["offset"] + box["header_size"],
            )
            song_info.samples.extend(samples_from_pair)
            moof_box = None

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
) -> List[SampleInfo]:
    """Parse a moof box and extract samples from corresponding mdat.

    Handles multi-track fragmented MP4s by only extracting samples from
    the traf matching the audio track ID.

    Args:
        audio_track_id: Track ID of the audio track to extract.
        moof_offset: Absolute file offset of the moof box.
        mdat_data_offset: Absolute file offset of the mdat content (after header).
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
            trun_entries = []
            first_trun_data_offset = None

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
                    if first_trun_data_offset is None:
                        first_trun_data_offset = data_off
                    trun_entries.extend(entries)

                traf_offset += inner_size

            # Only process this traf if it matches the audio track
            if tfhd_info["track_id"] != audio_track_id:
                offset += size
                continue

            # Compute starting offset in mdat_data
            base = tfhd_info.get("base_data_offset")
            if base is None:
                base = moof_offset  # Default: first byte of containing moof

            if first_trun_data_offset is not None:
                mdat_idx = (base + first_trun_data_offset) - mdat_data_offset
            else:
                mdat_idx = 0

            mdat_read_offset = max(0, mdat_idx)
            desc_index = tfhd_info["desc_index"]
            if desc_index > 0:
                desc_index -= 1  # Convert to 0-indexed

            for entry in trun_entries:
                sample_size = entry.get("size", tfhd_info["default_size"])
                sample_duration = entry.get("duration", tfhd_info["default_duration"])

                if sample_size > 0 and mdat_read_offset + sample_size <= len(mdat_data):
                    sample = SampleInfo(
                        data=mdat_data[
                            mdat_read_offset : mdat_read_offset + sample_size
                        ],
                        duration=sample_duration,
                        desc_index=desc_index,
                    )
                    samples.append(sample)
                    mdat_read_offset += sample_size

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


async def decrypt_samples(
    wrapper_ip: str,
    track_id: str,
    fairplay_key: str,
    samples: List[SampleInfo],
    progress_callback=None,
) -> bytes:
    """
    Send samples to wrapper for CBCS decryption and return decrypted data.

    CBCS full subsample encryption (used by ALAC):
    - Only bytes aligned to 16 are encrypted
    - Remaining bytes (len % 16) are clear and kept as-is

    Protocol:
    - For each new key: [1B id_len][id][1B key_len][key]
    - For each sample: [4B LE truncated_size][truncated_data] -> read back [decrypted_data]
    - Key switch: [0,0,0,0]
    - Close: [0,0,0,0,0]

    Args:
        progress_callback: Optional callback(current_sample, total_samples, bytes_processed) for progress tracking
    """
    import time

    host, port = wrapper_ip.split(":")
    port = int(port)

    reader, writer = await asyncio.open_connection(host, port)

    try:
        decrypted_data = bytearray()
        last_desc_index = 255

        keys = [PREFETCH_KEY, fairplay_key]
        total_samples = len(samples)
        bytes_processed = 0
        start_time = time.time()
        last_progress_time = start_time

        for i, sample in enumerate(samples):
            # Check if we need to switch keys
            if last_desc_index != sample.desc_index:
                if last_desc_index != 255:
                    # Send key switch signal
                    writer.write(struct.pack("<I", 0))
                    await writer.drain()

                # Send new key info
                key_uri = keys[min(sample.desc_index, len(keys) - 1)]

                if key_uri == PREFETCH_KEY:
                    id_bytes = b"0"
                else:
                    id_bytes = track_id.encode("utf-8")
                writer.write(struct.pack("B", len(id_bytes)))
                writer.write(id_bytes)

                key_bytes = key_uri.encode("utf-8")
                writer.write(struct.pack("B", len(key_bytes)))
                writer.write(key_bytes)
                await writer.drain()

                last_desc_index = sample.desc_index

            # CBCS full subsample decryption: truncate to 16-byte boundary
            sample_len = len(sample.data)
            truncated_len = sample_len & ~0xF

            if truncated_len > 0:
                # Send size and data
                writer.write(struct.pack("<I", truncated_len))
                writer.write(sample.data[:truncated_len])
                await writer.drain()

                # Read decrypted data
                decrypted_sample = await reader.readexactly(truncated_len)
                if len(decrypted_sample) != truncated_len:
                    raise IOError(
                        f"Short read: got {len(decrypted_sample)}, expected {truncated_len}"
                    )
                decrypted_data.extend(decrypted_sample)
                bytes_processed += truncated_len

            # Append clear bytes
            if truncated_len < sample_len:
                decrypted_data.extend(sample.data[truncated_len:])
                bytes_processed += sample_len - truncated_len

            # Call progress callback every 50 samples or 0.5s
            now = time.time()
            if progress_callback and (
                i % 50 == 0 or now - last_progress_time > 0.5 or i == total_samples - 1
            ):
                elapsed = now - start_time
                speed = bytes_processed / elapsed if elapsed > 0 else 0
                progress_callback(i + 1, total_samples, bytes_processed, speed)
                last_progress_time = now

        # Send close signal
        writer.write(bytes([0, 0, 0, 0, 0]))
        await writer.drain()

        logger.debug(f"Decrypted {len(samples)} samples ({len(decrypted_data)} bytes)")
        return bytes(decrypted_data)

    finally:
        writer.close()
        await writer.wait_closed()


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
    # Extract stsd content, timescale, and timestamps from original moov
    # Note: _extract_stsd_content automatically cleans encryption metadata
    stsd_content = None
    timescale = 44100  # Default
    mvhd_creation = 0
    mvhd_modification = 0
    tkhd_creation = 0
    tkhd_modification = 0
    mdhd_creation = 0
    mdhd_modification = 0

    if original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
        stsd_content = _extract_stsd_content(orig_data)
        timescale = _extract_timescale(orig_data)
        mvhd_creation, mvhd_modification = _extract_timestamps_from_box(
            orig_data, b"mvhd"
        )
        tkhd_creation, tkhd_modification = _extract_timestamps_from_box(
            orig_data, b"tkhd"
        )
        mdhd_creation, mdhd_modification = _extract_timestamps_from_box(
            orig_data, b"mdhd"
        )
    elif song_info.moov_data:
        stsd_content = _extract_stsd_content(song_info.ftyp_data + song_info.moov_data)
        timescale = _extract_timescale(song_info.moov_data)
        mvhd_creation, mvhd_modification = _extract_timestamps_from_box(
            song_info.moov_data, b"mvhd"
        )
        tkhd_creation, tkhd_modification = _extract_timestamps_from_box(
            song_info.moov_data, b"tkhd"
        )
        mdhd_creation, mdhd_modification = _extract_timestamps_from_box(
            song_info.moov_data, b"mdhd"
        )

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
            mvhd_creation,
            mvhd_modification,
            tkhd_creation,
            tkhd_modification,
            mdhd_creation,
            mdhd_modification,
        )

        # Write mdat
        _write_mdat(f, decrypted_data)

    logger.debug(f"Wrote decrypted file to {output_path}")


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
    mvhd_creation: int = 0,
    mvhd_modification: int = 0,
    tkhd_creation: int = 0,
    tkhd_modification: int = 0,
    mdhd_creation: int = 0,
    mdhd_modification: int = 0,
):
    """Write moov box with sample tables."""
    # First, build all the content
    moov_start = f.tell()

    # Placeholder for moov header
    f.write(b"\x00" * 8)

    # mvhd (movie header)
    mvhd_content = struct.pack(">I", mvhd_creation)  # creation_time
    mvhd_content += struct.pack(">I", mvhd_modification)  # modification_time
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
    tkhd_content = struct.pack(">I", tkhd_creation)  # creation_time
    tkhd_content += struct.pack(">I", tkhd_modification)  # modification_time
    tkhd_content += struct.pack(">I", 1)  # track_id
    tkhd_content += struct.pack(">I", 0)  # reserved
    tkhd_content += struct.pack(">I", total_duration)
    tkhd_content += b"\x00" * 8  # reserved
    tkhd_content += struct.pack(">H", 0)  # layer
    tkhd_content += struct.pack(">H", 0)  # alternate_group
    tkhd_content += struct.pack(">H", 0x0100)  # volume
    tkhd_content += struct.pack(">H", 0)  # reserved
    tkhd_content += struct.pack(
        ">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
    )  # matrix
    tkhd_content += struct.pack(">I", 0)  # width
    tkhd_content += struct.pack(">I", 0)  # height
    _write_fullbox(
        f, b"tkhd", 0, 7, tkhd_content
    )  # flags=7 (enabled, in_movie, in_preview)

    # mdia (media)
    mdia_start = f.tell()
    f.write(b"\x00" * 8)

    # mdhd (media header)
    mdhd_content = struct.pack(">I", mdhd_creation)  # creation_time
    mdhd_content += struct.pack(">I", mdhd_modification)  # modification_time
    mdhd_content += struct.pack(">I", timescale)
    mdhd_content += struct.pack(">I", total_duration)
    mdhd_content += struct.pack(">H", 0x55C4)  # language (und)
    mdhd_content += struct.pack(">H", 0)  # quality
    _write_fullbox(f, b"mdhd", 0, 0, mdhd_content)

    # hdlr (handler)
    hdlr_content = struct.pack(">I", 0)  # pre_defined
    hdlr_content += b"soun"  # handler_type
    hdlr_content += b"\x00" * 12  # reserved
    hdlr_content += b"SoundHandler\x00"
    _write_fullbox(f, b"hdlr", 0, 0, hdlr_content)

    # minf (media info)
    minf_start = f.tell()
    f.write(b"\x00" * 8)

    # smhd (sound media header)
    smhd_content = struct.pack(">H", 0)  # balance
    smhd_content += struct.pack(">H", 0)  # reserved
    _write_fullbox(f, b"smhd", 0, 0, smhd_content)

    # dinf + dref
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
    _fixup_box_size(f, moov_start, b"moov")

    # Fix up stco with correct mdat offset
    mdat_offset = f.tell() + 8  # +8 for mdat header
    f.seek(stco_pos + 16)  # +12 for box header + version/flags, +4 for entry_count
    f.write(struct.pack(">I", mdat_offset))
    f.seek(0, 2)  # Back to end


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


def _extract_stsd_content(data: bytes) -> Optional[bytes]:
    """Extract full stsd box content from moov box (supports any codec)."""
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
    return _clean_stsd_content(raw_content)


def _clean_stsd_content(stsd_content: bytes) -> bytes:
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

    # Parse and clean each sample entry
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
    if idx > 0 and idx + 24 < len(data):
        # mdhd: version(1) + flags(3) + creation(4) + modification(4) + timescale(4)
        return struct.unpack(">I", data[idx + 16 : idx + 20])[0]
    return 44100  # Default


def _extract_timestamps_from_box(data: bytes, box_type: bytes) -> tuple[int, int]:
    """Extract creation_time and modification_time from a FullBox (mvhd, tkhd, mdhd)."""
    idx = data.find(box_type)
    if idx > 0 and idx + 16 < len(data):
        creation_time = struct.unpack(">I", data[idx + 8 : idx + 12])[0]
        modification_time = struct.unpack(">I", data[idx + 12 : idx + 16])[0]
        return creation_time, modification_time
    return 0, 0


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
        wrapper_ip: Wrapper decrypt port address (e.g., "127.0.0.1:10020")
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
