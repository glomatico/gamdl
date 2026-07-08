use crate::mp4::{
    write_m4a_file, write_mdat_from_sources, PayloadSource, SampleInfo as Mp4Sample, TrackInfo,
};
use aes::Aes128;
use cbc::cipher::block_padding::NoPadding;
use cbc::cipher::{BlockDecryptMut, KeyIvInit, StreamCipher};
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::net::TcpStream;
use std::time::Duration;
use tempfile::NamedTempFile;

type Aes128CbcDec = cbc::Decryptor<Aes128>;
type Aes128Ctr = ctr::Ctr128BE<Aes128>;

const DEFAULT_SONG_DECRYPTION_KEY: [u8; 16] = [
    0x32, 0xb8, 0xad, 0xe1, 0x76, 0x9e, 0x26, 0xb1, 0xff, 0xb8, 0x98, 0x63, 0x52, 0x79, 0x3f, 0xc6,
];
const PREFETCH_KEY: &str = "skd://itunes.apple.com/P000000000/s1/e1";
const WRAPPER_DECRYPT_BATCH_SIZE: usize = 128;
const DECRYPT_MAGIC: u32 = 0x57563244; // WV2D
const DECRYPT_VERSION: u16 = 1;
const DECRYPT_KIND_BATCH: u16 = 1;
const DECRYPT_KIND_OK: u16 = 2;
const DECRYPT_KIND_ERROR: u16 = 3;
const DECRYPT_KIND_CLOSE: u16 = 9;

#[derive(Clone, Debug)]
struct Sample {
    data: Vec<u8>,
    duration: u32,
    desc_index: usize,
    iv: Vec<u8>,
    subsamples: Vec<(usize, usize)>,
    composition_time_offset: i32,
    is_sync: bool,
    size: usize,
    data_path: Option<String>,
    data_offset: u64,
}

#[derive(Clone, Debug)]
struct EncryptionInfo {
    scheme_type: String,
    crypt_byte_block: u8,
    skip_byte_block: u8,
    per_sample_iv_size: usize,
    constant_iv: Vec<u8>,
    kid: Vec<u8>,
}

impl Default for EncryptionInfo {
    fn default() -> Self {
        Self {
            scheme_type: "cbcs".to_string(),
            crypt_byte_block: 0,
            skip_byte_block: 0,
            per_sample_iv_size: 0,
            constant_iv: Vec::new(),
            kid: Vec::new(),
        }
    }
}

#[derive(Clone, Debug)]
struct SongInfo {
    samples: Vec<Sample>,
    moov_data: Vec<u8>,
    ftyp_data: Vec<u8>,
    encryption_info: Option<EncryptionInfo>,
    handler_type: [u8; 4],
    track_id: u32,
}

struct DecryptedTrack {
    input_path: String,
    track_info: SongInfo,
    payload_path: String,
    payload_size: u64,
}

struct DecryptedMedia {
    audio: DecryptedTrack,
    video: Option<DecryptedTrack>,
    captions: Vec<DecryptedTrack>,
}

fn py_io_error(err: io::Error) -> PyErr {
    PyIOError::new_err(err.to_string())
}

fn py_value_error(message: impl Into<String>) -> PyErr {
    PyValueError::new_err(message.into())
}

fn be_u16(data: &[u8], offset: usize) -> Option<u16> {
    data.get(offset..offset + 2)
        .map(|b| u16::from_be_bytes([b[0], b[1]]))
}

fn be_u32(data: &[u8], offset: usize) -> Option<u32> {
    data.get(offset..offset + 4)
        .map(|b| u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

fn be_i32(data: &[u8], offset: usize) -> Option<i32> {
    data.get(offset..offset + 4)
        .map(|b| i32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

fn be_u64(data: &[u8], offset: usize) -> Option<u64> {
    data.get(offset..offset + 8)
        .map(|b| u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
}

fn fourcc(data: &[u8]) -> [u8; 4] {
    [data[0], data[1], data[2], data[3]]
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn next_box(data: &[u8], offset: usize, end: usize) -> Option<([u8; 4], usize, usize, usize)> {
    if offset + 8 > end {
        return None;
    }
    let raw_size = be_u32(data, offset)? as usize;
    let typ = fourcc(data.get(offset + 4..offset + 8)?);
    let mut header_size = 8usize;
    let size = if raw_size == 1 {
        if offset + 16 > end {
            return None;
        }
        header_size = 16;
        usize::try_from(be_u64(data, offset + 8)?).ok()?
    } else if raw_size == 0 {
        end - offset
    } else {
        raw_size
    };
    if size < header_size || offset + size > end {
        return None;
    }
    Some((typ, offset, size, header_size))
}

fn find_child_box(container: &[u8], target: &[u8; 4], skip_header: usize) -> Option<Vec<u8>> {
    let mut offset = skip_header;
    while let Some((typ, box_offset, size, _)) = next_box(container, offset, container.len()) {
        if &typ == target {
            return Some(container[box_offset..box_offset + size].to_vec());
        }
        offset = box_offset + size;
    }
    None
}

fn find_track_by_handler(moov_data: &[u8], handler_type: &[u8; 4]) -> Option<Vec<u8>> {
    let mut offset = 8usize;
    while let Some((typ, box_offset, size, _)) = next_box(moov_data, offset, moov_data.len()) {
        if &typ == b"trak" {
            let trak = &moov_data[box_offset..box_offset + size];
            if let Some(hdlr_idx) = find_subslice(trak, b"hdlr") {
                let handler_offset = hdlr_idx + 12;
                if handler_offset + 4 <= trak.len()
                    && &trak[handler_offset..handler_offset + 4] == handler_type
                {
                    return Some(trak.to_vec());
                }
            }
        }
        offset = box_offset + size;
    }
    None
}

fn sample_entry_header_size(entry_type: &[u8]) -> usize {
    match entry_type {
        b"encv" | b"avc1" | b"avc3" | b"hvc1" | b"hev1" | b"dvh1" | b"dvhe" => 86,
        b"enca" | b"mp4a" | b"alac" | b"ac-3" | b"ec-3" => 36,
        b"c608" | b"c708" | b"text" | b"tx3g" | b"wvtt" | b"stpp" => 8,
        _ => 36,
    }
}

fn read_sample_data(sample: &Sample) -> io::Result<Vec<u8>> {
    if !sample.data.is_empty() {
        return Ok(sample.data.clone());
    }
    let Some(path) = &sample.data_path else {
        return Ok(Vec::new());
    };
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(sample.data_offset))?;
    let mut data = vec![0u8; sample.size];
    file.read_exact(&mut data)?;
    Ok(data)
}

#[derive(Clone)]
struct BoxRec {
    offset: u64,
    size: u64,
    typ: [u8; 4],
    header_size: u64,
    data: Vec<u8>,
}

fn scan_top_level_boxes(input_path: &str, file_backed_samples: bool) -> io::Result<Vec<BoxRec>> {
    let file_size = std::fs::metadata(input_path)?.len();
    let mut file = File::open(input_path)?;
    let mut boxes = Vec::new();
    let mut offset = 0u64;
    while offset + 8 <= file_size {
        file.seek(SeekFrom::Start(offset))?;
        let mut header = [0u8; 8];
        if file.read_exact(&mut header).is_err() {
            break;
        }
        let raw_size = u32::from_be_bytes([header[0], header[1], header[2], header[3]]) as u64;
        let typ = [header[4], header[5], header[6], header[7]];
        let mut header_size = 8u64;
        let size = if raw_size == 0 {
            file_size - offset
        } else if raw_size == 1 {
            let mut ext = [0u8; 8];
            if file.read_exact(&mut ext).is_err() {
                break;
            }
            header_size = 16;
            u64::from_be_bytes(ext)
        } else {
            raw_size
        };
        if size < header_size || offset + size > file_size {
            break;
        }
        let mut data = Vec::new();
        if !file_backed_samples || matches!(&typ, b"ftyp" | b"moov" | b"moof") {
            file.seek(SeekFrom::Start(offset))?;
            data.resize(size as usize, 0);
            file.read_exact(&mut data)?;
        }
        boxes.push(BoxRec {
            offset,
            size,
            typ,
            header_size,
            data,
        });
        offset += size;
    }
    Ok(boxes)
}

fn extract_track_id(moov: &[u8], handler_type: &[u8; 4], default: u32) -> u32 {
    let Some(trak) = find_track_by_handler(moov, handler_type) else {
        return default;
    };
    let Some(tkhd) = find_child_box(&trak, b"tkhd", 8) else {
        return default;
    };
    if tkhd.len() < 32 {
        return default;
    }
    if tkhd[8] == 0 {
        be_u32(&tkhd, 20).unwrap_or(default)
    } else {
        be_u32(&tkhd, 28).unwrap_or(default)
    }
}

fn extract_trex_defaults(moov: &[u8], target_track_id: u32) -> (u32, usize, u32) {
    let is_alac = find_subslice(moov, b"alac").is_some();
    let mut defaults = (if is_alac { 4096 } else { 1024 }, 0usize, 0u32);
    let Some(mvex) = find_child_box(moov, b"mvex", 8) else {
        return defaults;
    };
    let mut offset = 8usize;
    while let Some((typ, box_offset, size, _)) = next_box(&mvex, offset, mvex.len()) {
        if &typ == b"trex" && size >= 32 {
            let trex = &mvex[box_offset..box_offset + size];
            let track_id = be_u32(trex, 12).unwrap_or(0);
            if target_track_id == 0 || track_id == target_track_id {
                defaults.0 = be_u32(trex, 20).unwrap_or(defaults.0);
                defaults.1 = be_u32(trex, 24).unwrap_or(0) as usize;
                defaults.2 = be_u32(trex, 28).unwrap_or(0);
                return defaults;
            }
        }
        offset = box_offset + size;
    }
    defaults
}

fn parse_tfhd(data: &[u8], info: &mut TfhdInfo) {
    if data.len() < 8 {
        return;
    }
    let flags = ((data[1] as u32) << 16) | ((data[2] as u32) << 8) | data[3] as u32;
    info.track_id = be_u32(data, 4).unwrap_or(0);
    let mut offset = 8usize;
    if flags & 0x01 != 0 && offset + 8 <= data.len() {
        info.base_data_offset = be_u64(data, offset);
        offset += 8;
    }
    if flags & 0x02 != 0 && offset + 4 <= data.len() {
        info.desc_index = be_u32(data, offset).unwrap_or(0) as usize;
        offset += 4;
    }
    if flags & 0x08 != 0 && offset + 4 <= data.len() {
        info.default_duration = be_u32(data, offset).unwrap_or(info.default_duration);
        offset += 4;
    }
    if flags & 0x10 != 0 && offset + 4 <= data.len() {
        info.default_size = be_u32(data, offset).unwrap_or(0) as usize;
        offset += 4;
    }
    if flags & 0x20 != 0 && offset + 4 <= data.len() {
        info.default_sample_flags = be_u32(data, offset).unwrap_or(0);
    }
}

#[derive(Clone, Debug)]
struct TrunEntry {
    duration: Option<u32>,
    size: Option<usize>,
    sample_flags: Option<u32>,
    composition_time_offset: i32,
}

fn parse_trun(data: &[u8]) -> (Vec<TrunEntry>, Option<i32>) {
    let mut entries = Vec::new();
    if data.len() < 8 {
        return (entries, None);
    }
    let version = data[0];
    let flags = ((data[1] as u32) << 16) | ((data[2] as u32) << 8) | data[3] as u32;
    let sample_count = be_u32(data, 4).unwrap_or(0) as usize;
    let mut offset = 8usize;
    let mut data_offset = None;
    if flags & 0x01 != 0 && offset + 4 <= data.len() {
        data_offset = be_i32(data, offset);
        offset += 4;
    }
    let mut first_sample_flags = None;
    if flags & 0x04 != 0 && offset + 4 <= data.len() {
        first_sample_flags = be_u32(data, offset);
        offset += 4;
    }
    for sample_index in 0..sample_count {
        let mut entry = TrunEntry {
            duration: None,
            size: None,
            sample_flags: None,
            composition_time_offset: 0,
        };
        if flags & 0x100 != 0 && offset + 4 <= data.len() {
            entry.duration = be_u32(data, offset);
            offset += 4;
        }
        if flags & 0x200 != 0 && offset + 4 <= data.len() {
            entry.size = be_u32(data, offset).map(|v| v as usize);
            offset += 4;
        }
        if flags & 0x400 != 0 && offset + 4 <= data.len() {
            entry.sample_flags = be_u32(data, offset);
            offset += 4;
        } else if sample_index == 0 {
            entry.sample_flags = first_sample_flags;
        }
        if flags & 0x800 != 0 && offset + 4 <= data.len() {
            entry.composition_time_offset = if version == 1 {
                be_i32(data, offset).unwrap_or(0)
            } else {
                be_u32(data, offset).unwrap_or(0) as i32
            };
            offset += 4;
        }
        entries.push(entry);
    }
    (entries, data_offset)
}

#[derive(Clone, Debug)]
struct SencEntry {
    iv: Vec<u8>,
    subsamples: Vec<(usize, usize)>,
}

fn parse_senc_strict(
    data: &[u8],
    per_sample_iv_size: usize,
    sample_sizes: &[usize],
) -> Option<Vec<SencEntry>> {
    if data.len() < 8 {
        return None;
    }
    let flags = ((data[1] as u32) << 16) | ((data[2] as u32) << 8) | data[3] as u32;
    let sample_count = be_u32(data, 4)? as usize;
    if sample_count > sample_sizes.len() {
        return None;
    }
    let mut offset = 8usize;
    let mut entries = Vec::with_capacity(sample_count);
    for sample_index in 0..sample_count {
        let mut iv = Vec::new();
        if per_sample_iv_size > 0 {
            iv.extend_from_slice(data.get(offset..offset + per_sample_iv_size)?);
            offset += per_sample_iv_size;
        }
        let mut subsamples = Vec::new();
        if flags & 0x02 != 0 {
            let count = be_u16(data, offset)? as usize;
            offset += 2;
            let mut total = 0usize;
            for _ in 0..count {
                let clear = be_u16(data, offset)? as usize;
                let enc = be_u32(data, offset + 2)? as usize;
                offset += 6;
                total = total.checked_add(clear)?.checked_add(enc)?;
                if total > sample_sizes[sample_index] {
                    return None;
                }
                subsamples.push((clear, enc));
            }
        }
        entries.push(SencEntry { iv, subsamples });
    }
    Some(entries)
}

fn parse_senc_for_sample_sizes(
    data: &[u8],
    sample_sizes: &[usize],
    preferred_iv_size: usize,
) -> Vec<SencEntry> {
    let mut candidates = Vec::new();
    for iv_size in [preferred_iv_size, 8, 16, 0] {
        if !candidates.contains(&iv_size) {
            candidates.push(iv_size);
        }
    }
    for iv_size in candidates {
        if let Some(entries) = parse_senc_strict(data, iv_size, sample_sizes) {
            return entries;
        }
    }
    Vec::new()
}

#[derive(Clone, Debug)]
struct TfhdInfo {
    track_id: u32,
    desc_index: usize,
    default_duration: u32,
    default_size: usize,
    default_sample_flags: u32,
    base_data_offset: Option<u64>,
}

fn parse_moof_mdat(
    moof_data: &[u8],
    mdat_data: &[u8],
    default_duration: u32,
    default_size: usize,
    default_flags: u32,
    track_id: u32,
    moof_offset: u64,
    mdat_data_offset: u64,
    per_sample_iv_size: usize,
    mdat_data_size: usize,
    mdat_source_path: Option<&str>,
) -> Vec<Sample> {
    let mut samples = Vec::new();
    let mut offset = 8usize;
    while let Some((typ, traf_offset, traf_size, _)) = next_box(moof_data, offset, moof_data.len())
    {
        if &typ != b"traf" {
            offset = traf_offset + traf_size;
            continue;
        }
        let mut info = TfhdInfo {
            track_id: 0,
            desc_index: 0,
            default_duration,
            default_size,
            default_sample_flags: default_flags,
            base_data_offset: None,
        };
        let mut truns: Vec<(Vec<TrunEntry>, Option<i32>)> = Vec::new();
        let mut raw_senc: Option<Vec<u8>> = None;
        let mut inner = traf_offset + 8;
        let traf_end = traf_offset + traf_size;
        while let Some((inner_type, inner_offset, inner_size, _)) =
            next_box(moof_data, inner, traf_end)
        {
            let payload = &moof_data[inner_offset + 8..inner_offset + inner_size];
            if &inner_type == b"tfhd" {
                parse_tfhd(payload, &mut info);
            } else if &inner_type == b"trun" {
                truns.push(parse_trun(payload));
            } else if &inner_type == b"senc" {
                raw_senc = Some(payload.to_vec());
            }
            inner = inner_offset + inner_size;
        }
        if info.track_id != track_id {
            offset = traf_offset + traf_size;
            continue;
        }
        let base = info.base_data_offset.unwrap_or(moof_offset);
        let desc_index = info.desc_index.saturating_sub(1);
        let sample_sizes: Vec<usize> = truns
            .iter()
            .flat_map(|(entries, _)| entries.iter().map(|e| e.size.unwrap_or(info.default_size)))
            .collect();
        let senc_entries = raw_senc
            .as_ref()
            .map(|d| parse_senc_for_sample_sizes(d, &sample_sizes, per_sample_iv_size))
            .unwrap_or_default();
        let mut mdat_pos: Option<i64> = None;
        let mut sample_index = 0usize;
        for (entries, trun_data_offset) in truns {
            if let Some(data_offset) = trun_data_offset {
                mdat_pos = Some(base as i64 + data_offset as i64 - mdat_data_offset as i64);
            } else if mdat_pos.is_none() {
                mdat_pos = Some(0);
            }
            let mut read_offset = mdat_pos.unwrap_or(0).max(0) as usize;
            for entry in entries {
                let sample_size = entry.size.unwrap_or(info.default_size);
                let duration = entry.duration.unwrap_or(info.default_duration);
                let flags = entry.sample_flags.unwrap_or(info.default_sample_flags);
                if sample_size > 0 && read_offset + sample_size <= mdat_data_size {
                    let senc = senc_entries.get(sample_index);
                    let (data, data_path, data_offset) = if let Some(path) = mdat_source_path {
                        (
                            Vec::new(),
                            Some(path.to_string()),
                            mdat_data_offset + read_offset as u64,
                        )
                    } else {
                        (
                            mdat_data[read_offset..read_offset + sample_size].to_vec(),
                            None,
                            0,
                        )
                    };
                    samples.push(Sample {
                        data,
                        duration,
                        desc_index,
                        iv: senc.map(|e| e.iv.clone()).unwrap_or_default(),
                        subsamples: senc.map(|e| e.subsamples.clone()).unwrap_or_default(),
                        composition_time_offset: entry.composition_time_offset,
                        is_sync: flags & 0x10000 == 0,
                        size: sample_size,
                        data_path,
                        data_offset,
                    });
                    read_offset += sample_size;
                }
                sample_index += 1;
            }
            mdat_pos = Some(read_offset as i64);
        }
        offset = traf_offset + traf_size;
    }
    samples
}

fn extract_encryption_info_from_entry(entry: &[u8]) -> Option<EncryptionInfo> {
    if entry.len() < 16 {
        return None;
    }
    let header_size = sample_entry_header_size(&entry[4..8]);
    let sinf = find_child_box(entry, b"sinf", header_size)?;
    let mut info = EncryptionInfo::default();
    if let Some(schm) = find_child_box(&sinf, b"schm", 8) {
        if schm.len() >= 20 {
            info.scheme_type = String::from_utf8_lossy(&schm[12..16]).to_string();
        }
    }
    if let Some(schi) = find_child_box(&sinf, b"schi", 8) {
        if let Some(tenc) = find_child_box(&schi, b"tenc", 8) {
            if tenc.len() >= 32 {
                if tenc[8] > 0 {
                    info.crypt_byte_block = tenc[13] >> 4;
                    info.skip_byte_block = tenc[13] & 0x0f;
                }
                info.per_sample_iv_size = tenc[15] as usize;
                info.kid = tenc[16..32].to_vec();
                if info.per_sample_iv_size == 0 && tenc.len() > 32 {
                    let iv_size = tenc[32] as usize;
                    if 33 + iv_size <= tenc.len() {
                        info.constant_iv = tenc[33..33 + iv_size].to_vec();
                    }
                }
                return Some(info);
            }
        }
    }
    Some(info)
}

fn extract_encryption_info_per_stsd(
    moov: &[u8],
    handler_type: &[u8; 4],
) -> Option<HashMap<usize, EncryptionInfo>> {
    let trak = find_track_by_handler(moov, handler_type)?;
    let mdia = find_child_box(&trak, b"mdia", 8)?;
    let minf = find_child_box(&mdia, b"minf", 8)?;
    let stbl = find_child_box(&minf, b"stbl", 8)?;
    let stsd = find_child_box(&stbl, b"stsd", 8)?;
    if stsd.len() < 16 {
        return None;
    }
    let entry_count = be_u32(&stsd, 12).unwrap_or(0) as usize;
    let mut offset = 16usize;
    let mut out = HashMap::new();
    for desc_idx in 0..entry_count {
        let Some(entry_size) = be_u32(&stsd, offset).map(|v| v as usize) else {
            break;
        };
        if entry_size < 8 || offset + entry_size > stsd.len() {
            break;
        }
        if let Some(info) = extract_encryption_info_from_entry(&stsd[offset..offset + entry_size]) {
            out.insert(desc_idx, info);
        }
        offset += entry_size;
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn extract_song(
    input_path: &str,
    handler_type: [u8; 4],
    file_backed: bool,
) -> io::Result<SongInfo> {
    let boxes = scan_top_level_boxes(input_path, file_backed)?;
    let mut info = SongInfo {
        samples: Vec::new(),
        moov_data: Vec::new(),
        ftyp_data: Vec::new(),
        encryption_info: None,
        handler_type,
        track_id: 0,
    };
    for b in &boxes {
        if &b.typ == b"ftyp" {
            info.ftyp_data = b.data.clone();
        } else if &b.typ == b"moov" {
            info.moov_data = b.data.clone();
        }
    }
    if info.moov_data.is_empty() {
        return Ok(info);
    }
    info.track_id = extract_track_id(&info.moov_data, &handler_type, 0);
    if info.track_id == 0 {
        return Ok(info);
    }
    let (default_duration, default_size, default_flags) =
        extract_trex_defaults(&info.moov_data, info.track_id);
    let per_desc = extract_encryption_info_per_stsd(&info.moov_data, &handler_type);
    info.encryption_info = per_desc.as_ref().and_then(|m| m.values().next().cloned());

    let iv_size = info
        .encryption_info
        .as_ref()
        .map(|e| e.per_sample_iv_size)
        .unwrap_or(0);
    let mut pending_moof: Option<&BoxRec> = None;
    for b in &boxes {
        if &b.typ == b"moof" {
            pending_moof = Some(b);
        } else if &b.typ == b"mdat" {
            if let Some(moof) = pending_moof.take() {
                let mdat_size = (b.size - b.header_size) as usize;
                let mdat_payload = if file_backed {
                    &[][..]
                } else {
                    &b.data[b.header_size as usize..]
                };
                info.samples.extend(parse_moof_mdat(
                    &moof.data,
                    mdat_payload,
                    default_duration,
                    default_size,
                    default_flags,
                    info.track_id,
                    moof.offset,
                    b.offset + b.header_size,
                    iv_size,
                    mdat_size,
                    file_backed.then_some(input_path),
                ));
            }
        }
    }
    if &handler_type == b"soun"
        && (find_subslice(&info.moov_data, b"alac").is_some()
            || find_subslice(&info.moov_data, b"ALAC").is_some())
    {
        for sample in &mut info.samples {
            if sample.duration == 0 || sample.duration == 1024 {
                sample.duration = 4096;
            }
        }
    }
    Ok(info)
}

fn key_bytes(hex: &str) -> PyResult<[u8; 16]> {
    let clean = hex.trim();
    if clean.len() != 32 {
        return Err(py_value_error("decrypt: AES key must be 32 hex characters"));
    }
    let mut out = [0u8; 16];
    for i in 0..16 {
        out[i] = u8::from_str_radix(&clean[i * 2..i * 2 + 2], 16)
            .map_err(|_| py_value_error("decrypt: invalid AES key hex"))?;
    }
    Ok(out)
}

fn padded_iv(iv: &[u8]) -> [u8; 16] {
    let mut out = [0u8; 16];
    let n = iv.len().min(16);
    out[..n].copy_from_slice(&iv[..n]);
    out
}

fn reassemble_sample(
    sample_data: &[u8],
    plain: &[u8],
    tail: &[u8],
    subsamples: &[(usize, usize)],
) -> io::Result<Vec<u8>> {
    let mut full = Vec::with_capacity(plain.len() + tail.len());
    full.extend_from_slice(plain);
    full.extend_from_slice(tail);
    if subsamples.is_empty() {
        if full.len() != sample_data.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "decrypted sample length mismatch",
            ));
        }
        return Ok(full);
    }
    let encrypted_total: usize = subsamples.iter().map(|(_, enc)| *enc).sum();
    if full.len() != encrypted_total {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "decrypted subsample length mismatch",
        ));
    }
    let mut out = Vec::with_capacity(sample_data.len());
    let mut dec_off = 0usize;
    let mut offset = 0usize;
    for (clear, enc) in subsamples {
        out.extend_from_slice(&sample_data[offset..offset + clear]);
        offset += clear;
        out.extend_from_slice(&full[dec_off..dec_off + enc]);
        dec_off += enc;
        offset += enc;
    }
    if offset < sample_data.len() {
        out.extend_from_slice(&sample_data[offset..]);
    }
    Ok(out)
}

fn cbcs_ciphertext_for_sample(
    sample_data: &[u8],
    subsamples: &[(usize, usize)],
) -> Option<(Vec<u8>, Vec<u8>)> {
    if subsamples.is_empty() {
        let aligned_len = sample_data.len() & !0x0f;
        if aligned_len > 0 {
            return Some((
                sample_data[..aligned_len].to_vec(),
                sample_data[aligned_len..].to_vec(),
            ));
        }
        return None;
    }
    let mut enc_cat = Vec::new();
    let mut offset = 0usize;
    for (clear, enc) in subsamples {
        offset += clear;
        if *enc > 0 {
            enc_cat.extend_from_slice(&sample_data[offset..offset + enc]);
            offset += enc;
        }
    }
    if enc_cat.is_empty() {
        return None;
    }
    let aligned_len = enc_cat.len() & !0x0f;
    Some((
        enc_cat[..aligned_len].to_vec(),
        enc_cat[aligned_len..].to_vec(),
    ))
}

fn decrypt_cbcs_pattern(
    data: &[u8],
    key: &[u8; 16],
    iv: [u8; 16],
    crypt_blocks: u8,
    skip_blocks: u8,
) -> io::Result<Vec<u8>> {
    if crypt_blocks == 0 {
        return Ok(data.to_vec());
    }
    let mut out = Vec::with_capacity(data.len());
    let mut offset = 0usize;
    let crypt_bytes = crypt_blocks as usize * 16;
    let skip_bytes = skip_blocks as usize * 16;
    let mut next_iv = iv;
    while offset < data.len() {
        let remaining = data.len() - offset;
        let crypt_window = crypt_bytes.min(remaining);
        let aligned = crypt_window & !0x0f;
        if aligned > 0 {
            let ciphertext = &data[offset..offset + aligned];
            let mut buf = ciphertext.to_vec();
            let plain = Aes128CbcDec::new(key.into(), (&next_iv).into())
                .decrypt_padded_mut::<NoPadding>(&mut buf)
                .map_err(|_| {
                    io::Error::new(io::ErrorKind::InvalidData, "CBCS pattern decrypt failed")
                })?;
            out.extend_from_slice(plain);
            next_iv.copy_from_slice(&ciphertext[aligned - 16..aligned]);
            offset += aligned;
        }
        let tail = crypt_window - aligned;
        if tail > 0 {
            out.extend_from_slice(&data[offset..offset + tail]);
            offset += tail;
        }
        let skip = skip_bytes.min(data.len() - offset);
        if skip > 0 {
            out.extend_from_slice(&data[offset..offset + skip]);
            offset += skip;
        }
    }
    Ok(out)
}

fn decrypt_sample_hex(
    sample: &Sample,
    key: Option<&[u8; 16]>,
    enc: &EncryptionInfo,
) -> io::Result<Vec<u8>> {
    let data = read_sample_data(sample)?;
    let Some(key) = key else {
        return Ok(data);
    };
    if enc.scheme_type == "cenc" {
        let mut out = data.clone();
        let iv = padded_iv(&sample.iv);
        let mut cipher = Aes128Ctr::new(key.into(), (&iv).into());
        if sample.subsamples.is_empty() {
            cipher.apply_keystream(&mut out);
            return Ok(out);
        }
        let mut offset = 0usize;
        for (clear, enc_bytes) in &sample.subsamples {
            offset += clear;
            cipher.apply_keystream(&mut out[offset..offset + enc_bytes]);
            offset += enc_bytes;
        }
        return Ok(out);
    }

    if enc.crypt_byte_block > 0 && enc.skip_byte_block > 0 {
        let iv = padded_iv(if sample.iv.is_empty() {
            &enc.constant_iv
        } else {
            &sample.iv
        });
        return decrypt_cbcs_pattern(&data, key, iv, enc.crypt_byte_block, enc.skip_byte_block);
    }

    let Some((aligned, tail)) = cbcs_ciphertext_for_sample(&data, &sample.subsamples) else {
        return Ok(data);
    };
    let mut plain = Vec::new();
    if !aligned.is_empty() {
        let iv = padded_iv(if sample.iv.is_empty() {
            &enc.constant_iv
        } else {
            &sample.iv
        });
        let mut buf = aligned.clone();
        let decrypted = Aes128CbcDec::new(key.into(), (&iv).into())
            .decrypt_padded_mut::<NoPadding>(&mut buf)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "CBCS decrypt failed"))?;
        plain.extend_from_slice(decrypted);
    }
    reassemble_sample(&data, &plain, &tail, &sample.subsamples)
}

fn decrypt_track_hex(
    input_path: &str,
    key_hex: &str,
    handler_type: [u8; 4],
    use_cenc: bool,
    use_single_content_key: bool,
    file_backed: bool,
) -> PyResult<DecryptedTrack> {
    let mut track = extract_song(input_path, handler_type, true).map_err(py_io_error)?;
    let mut enc_info = track.encryption_info.clone().unwrap_or_default();
    if use_cenc {
        enc_info.scheme_type = "cenc".to_string();
        enc_info.crypt_byte_block = 0;
        enc_info.skip_byte_block = 0;
    }
    let per_desc = extract_encryption_info_per_stsd(&track.moov_data, &handler_type);
    let track_key = key_bytes(key_hex)?;
    let mut keys = HashMap::new();
    if use_single_content_key {
        for sample in &track.samples {
            keys.insert(sample.desc_index, track_key);
        }
    } else {
        keys.insert(0usize, DEFAULT_SONG_DECRYPTION_KEY);
        keys.insert(1usize, track_key);
    }
    let mut temp = NamedTempFile::new().map_err(py_io_error)?;
    let mut written = 0u64;
    for sample in &mut track.samples {
        let effective = per_desc
            .as_ref()
            .and_then(|m| m.get(&sample.desc_index))
            .unwrap_or(&enc_info);
        let decrypted = decrypt_sample_hex(sample, keys.get(&sample.desc_index), effective)
            .map_err(py_io_error)?;
        temp.write_all(&decrypted).map_err(py_io_error)?;
        sample.size = decrypted.len();
        written += decrypted.len() as u64;
        if file_backed {
            sample.data.clear();
            sample.subsamples.clear();
            sample.iv.clear();
        }
    }
    let (_file, path) = temp.keep().map_err(|e| py_io_error(e.error))?;
    Ok(DecryptedTrack {
        input_path: input_path.to_string(),
        track_info: track,
        payload_path: path.to_string_lossy().to_string(),
        payload_size: written,
    })
}

struct WrapperTcpSession {
    stream: TcpStream,
    next_request_id: u32,
}

impl WrapperTcpSession {
    fn connect(host: &str, port: u16) -> io::Result<Self> {
        let stream = TcpStream::connect((host, port))?;
        stream.set_nodelay(true)?;
        stream.set_read_timeout(Some(Duration::from_secs(600)))?;
        stream.set_write_timeout(Some(Duration::from_secs(600)))?;
        Ok(Self {
            stream,
            next_request_id: 1,
        })
    }

    fn write_frame(&mut self, kind: u16, request_id: u32, payload: &[u8]) -> io::Result<()> {
        if payload.len() > u32::MAX as usize {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "wrapper-v2: decrypt frame is too large",
            ));
        }
        self.stream.write_all(&DECRYPT_MAGIC.to_be_bytes())?;
        self.stream.write_all(&DECRYPT_VERSION.to_be_bytes())?;
        self.stream.write_all(&kind.to_be_bytes())?;
        self.stream.write_all(&request_id.to_be_bytes())?;
        self.stream
            .write_all(&(payload.len() as u32).to_be_bytes())?;
        self.stream.write_all(payload)?;
        Ok(())
    }

    fn read_frame(&mut self) -> io::Result<(u16, u32, Vec<u8>)> {
        let mut h = [0u8; 16];
        self.stream.read_exact(&mut h)?;
        let magic = u32::from_be_bytes([h[0], h[1], h[2], h[3]]);
        let version = u16::from_be_bytes([h[4], h[5]]);
        if magic != DECRYPT_MAGIC || version != DECRYPT_VERSION {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "wrapper-v2: bad decrypt response frame",
            ));
        }
        let kind = u16::from_be_bytes([h[6], h[7]]);
        let request_id = u32::from_be_bytes([h[8], h[9], h[10], h[11]]);
        let payload_len = u32::from_be_bytes([h[12], h[13], h[14], h[15]]) as usize;
        let mut payload = vec![0u8; payload_len];
        self.stream.read_exact(&mut payload)?;
        Ok((kind, request_id, payload))
    }

    fn decrypt_batch(
        &mut self,
        adam_id: &str,
        skd_uri: &str,
        samples: &[Vec<u8>],
    ) -> io::Result<Vec<Vec<u8>>> {
        if adam_id.is_empty() || skd_uri.is_empty() || samples.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "wrapper-v2: invalid decrypt batch",
            ));
        }
        if adam_id.len() > u16::MAX as usize || skd_uri.len() > u16::MAX as usize {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "wrapper-v2: decrypt label too long",
            ));
        }
        let mut payload = Vec::new();
        payload.extend_from_slice(&(adam_id.len() as u16).to_be_bytes());
        payload.extend_from_slice(&(skd_uri.len() as u16).to_be_bytes());
        payload.extend_from_slice(&(samples.len() as u32).to_be_bytes());
        for sample in samples {
            payload.extend_from_slice(&(sample.len() as u32).to_be_bytes());
        }
        payload.extend_from_slice(adam_id.as_bytes());
        payload.extend_from_slice(skd_uri.as_bytes());
        for sample in samples {
            payload.extend_from_slice(sample);
        }

        let request_id = self.next_request_id;
        self.next_request_id = self.next_request_id.wrapping_add(1).max(1);
        self.write_frame(DECRYPT_KIND_BATCH, request_id, &payload)?;
        let (kind, response_id, response) = self.read_frame()?;
        if response_id != request_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "wrapper-v2: mismatched decrypt response id",
            ));
        }
        if kind == DECRYPT_KIND_ERROR {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!(
                    "wrapper-v2: decrypt failed: {}",
                    String::from_utf8_lossy(&response)
                ),
            ));
        }
        if kind != DECRYPT_KIND_OK || response.len() < 4 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "wrapper-v2: bad decrypt response",
            ));
        }
        let count = be_u32(&response, 0).unwrap_or(0) as usize;
        let table_end = 4 + count * 4;
        if response.len() < table_end {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "wrapper-v2: truncated decrypt response",
            ));
        }
        let mut out = Vec::with_capacity(count);
        let mut offset = table_end;
        for i in 0..count {
            let len = be_u32(&response, 4 + i * 4).unwrap_or(0) as usize;
            if offset + len > response.len() {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "wrapper-v2: truncated plaintext sample",
                ));
            }
            out.push(response[offset..offset + len].to_vec());
            offset += len;
        }
        if out.len() != samples.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "wrapper-v2: plaintext batch count mismatch",
            ));
        }
        Ok(out)
    }
}

impl Drop for WrapperTcpSession {
    fn drop(&mut self) {
        let _ = self.write_frame(DECRYPT_KIND_CLOSE, 0, &[]);
    }
}

struct PendingWrapperSample {
    data: Vec<u8>,
    aligned: Vec<u8>,
    tail: Vec<u8>,
    subsamples: Vec<(usize, usize)>,
}

fn decrypt_track_wrapper(
    host: &str,
    port: u16,
    track_id: &str,
    input_path: &str,
    fairplay_key: &str,
    handler_type: [u8; 4],
    use_single_content_key: bool,
    file_backed: bool,
) -> PyResult<DecryptedTrack> {
    let mut track = extract_song(input_path, handler_type, true).map_err(py_io_error)?;
    let enc_info = track.encryption_info.clone().unwrap_or_default();
    let per_desc = extract_encryption_info_per_stsd(&track.moov_data, &handler_type);
    let mut wrapper = WrapperTcpSession::connect(host, port).map_err(py_io_error)?;
    let mut temp = NamedTempFile::new().map_err(py_io_error)?;
    let mut written = 0u64;
    let mut current_adam: Option<String> = None;
    let mut current_uri: Option<String> = None;
    let mut batch: Vec<PendingWrapperSample> = Vec::new();

    let flush = |batch: &mut Vec<PendingWrapperSample>,
                 wrapper: &mut WrapperTcpSession,
                 temp: &mut NamedTempFile,
                 written: &mut u64,
                 adam: &Option<String>,
                 uri: &Option<String>|
     -> PyResult<()> {
        if batch.is_empty() {
            return Ok(());
        }
        let adam = adam.as_deref().ok_or_else(|| {
            py_io_error(io::Error::new(
                io::ErrorKind::Other,
                "wrapper-v2: missing adam id",
            ))
        })?;
        let uri = uri.as_deref().ok_or_else(|| {
            py_io_error(io::Error::new(
                io::ErrorKind::Other,
                "wrapper-v2: missing skd uri",
            ))
        })?;
        let ciphertexts: Vec<Vec<u8>> = batch.iter().map(|item| item.aligned.clone()).collect();
        let plains = wrapper
            .decrypt_batch(adam, uri, &ciphertexts)
            .map_err(py_io_error)?;
        for (item, plain) in batch.drain(..).zip(plains) {
            let sample = reassemble_sample(&item.data, &plain, &item.tail, &item.subsamples)
                .map_err(py_io_error)?;
            temp.write_all(&sample).map_err(py_io_error)?;
            *written += sample.len() as u64;
        }
        Ok(())
    };

    let mut last_desc_index = usize::MAX;
    for sample in &mut track.samples {
        if last_desc_index != sample.desc_index {
            flush(
                &mut batch,
                &mut wrapper,
                &mut temp,
                &mut written,
                &current_adam,
                &current_uri,
            )?;
            if use_single_content_key {
                current_adam = Some(track_id.to_string());
                current_uri = Some(fairplay_key.to_string());
            } else if sample.desc_index == 0 {
                current_adam = Some("0".to_string());
                current_uri = Some(PREFETCH_KEY.to_string());
            } else {
                current_adam = Some(track_id.to_string());
                current_uri = Some(fairplay_key.to_string());
            }
            last_desc_index = sample.desc_index;
        }

        let effective = per_desc
            .as_ref()
            .and_then(|m| m.get(&sample.desc_index))
            .unwrap_or(&enc_info);
        let data = read_sample_data(sample).map_err(py_io_error)?;
        let decrypted = if !use_single_content_key && current_adam.as_deref() == Some("0") {
            decrypt_sample_hex(sample, Some(&DEFAULT_SONG_DECRYPTION_KEY), effective)
                .map_err(py_io_error)?
        } else {
            if effective.crypt_byte_block > 0 && effective.skip_byte_block > 0 {
                return Err(py_io_error(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "wrapper-v2 pattern CBCS decrypt is not supported by wrapper batch path",
                )));
            }
            match cbcs_ciphertext_for_sample(&data, &sample.subsamples) {
                None => data,
                Some((aligned, tail)) if aligned.is_empty() => {
                    reassemble_sample(&data, &[], &tail, &sample.subsamples).map_err(py_io_error)?
                }
                Some((aligned, tail)) => {
                    batch.push(PendingWrapperSample {
                        data,
                        aligned,
                        tail,
                        subsamples: sample.subsamples.clone(),
                    });
                    if batch.len() >= WRAPPER_DECRYPT_BATCH_SIZE {
                        flush(
                            &mut batch,
                            &mut wrapper,
                            &mut temp,
                            &mut written,
                            &current_adam,
                            &current_uri,
                        )?;
                    }
                    if file_backed {
                        sample.data.clear();
                        sample.subsamples.clear();
                        sample.iv.clear();
                    }
                    continue;
                }
            }
        };
        temp.write_all(&decrypted).map_err(py_io_error)?;
        sample.size = decrypted.len();
        written += decrypted.len() as u64;
        if file_backed {
            sample.data.clear();
            sample.subsamples.clear();
            sample.iv.clear();
        }
    }
    flush(
        &mut batch,
        &mut wrapper,
        &mut temp,
        &mut written,
        &current_adam,
        &current_uri,
    )?;
    let (_file, path) = temp.keep().map_err(|e| py_io_error(e.error))?;
    Ok(DecryptedTrack {
        input_path: input_path.to_string(),
        track_info: track,
        payload_path: path.to_string_lossy().to_string(),
        payload_size: written,
    })
}

fn track_to_mp4_info(track: &SongInfo) -> TrackInfo {
    TrackInfo {
        samples: track
            .samples
            .iter()
            .map(|sample| Mp4Sample {
                size: sample.size as u64,
                duration: sample.duration,
                desc_index: sample.desc_index,
                composition_time_offset: sample.composition_time_offset,
                is_sync: sample.is_sync,
            })
            .collect(),
        moov_data: track.moov_data.clone(),
        ftyp_data: track.ftyp_data.clone(),
        handler_type: track.handler_type,
    }
}

fn payload_source(track: &DecryptedTrack) -> PayloadSource {
    PayloadSource::File {
        path: track.payload_path.clone(),
        offset: 0,
        size: track.payload_size,
    }
}

fn write_decrypted_media_native(
    media: DecryptedMedia,
    output_path: &str,
    m4v_brand: bool,
) -> io::Result<()> {
    struct Cleanup(Vec<String>);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            for path in &self.0 {
                let _ = std::fs::remove_file(path);
            }
        }
    }
    let mut cleanup = Cleanup(vec![media.audio.payload_path.clone()]);
    if let Some(video) = &media.video {
        cleanup.0.push(video.payload_path.clone());
    }
    for caption in &media.captions {
        cleanup.0.push(caption.payload_path.clone());
    }

    if media.video.is_none() {
        let info = track_to_mp4_info(&media.audio.track_info);
        write_m4a_file(
            output_path,
            &info,
            Some(&media.audio.input_path),
            &payload_source(&media.audio),
        )?;
        return Ok(());
    }
    let video = media.video.as_ref().unwrap();
    let video_info = track_to_mp4_info(&video.track_info);
    let audio_info = track_to_mp4_info(&media.audio.track_info);
    let video_moov = crate::mp4::build_decrypted_track_moov(&video_info, Some(&video.input_path))?;
    let audio_moov =
        crate::mp4::build_decrypted_track_moov(&audio_info, Some(&media.audio.input_path))?;
    let mvhd = crate::mp4::find_child_box(&video_moov, b"mvhd", 8).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "mux: missing required audio/video track metadata",
        )
    })?;
    let video_trak = crate::mp4::find_track_by_handler(&video_moov, b"vide").ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "mux: missing required audio/video track metadata",
        )
    })?;
    let mut audio_trak =
        crate::mp4::find_track_by_handler(&audio_moov, b"soun").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
    let movie_timescale = crate::mp4::extract_mvhd_timescale(&mvhd);
    audio_trak = crate::mp4::patch_trak_track_id(&audio_trak, 2);
    audio_trak = crate::mp4::patch_trak_duration_to_movie_timescale(&audio_trak, movie_timescale);

    let mut traks = vec![video_trak, audio_trak];
    let mut sources = vec![payload_source(video), payload_source(&media.audio)];
    for (idx, caption) in media.captions.iter().enumerate() {
        let info = track_to_mp4_info(&caption.track_info);
        let moov = crate::mp4::build_decrypted_track_moov(&info, Some(&caption.input_path))?;
        if let Some(mut trak) = crate::mp4::find_child_box(&moov, b"trak", 8) {
            trak = crate::mp4::patch_trak_track_id(&trak, idx as u32 + 3);
            trak = crate::mp4::patch_trak_duration_to_movie_timescale(&trak, movie_timescale);
            traks.push(trak);
            sources.push(payload_source(caption));
        }
    }
    let ftyp = if m4v_brand {
        crate::mp4::ftyp_m4v()?
    } else {
        crate::mp4::ftyp_mp4()?
    };
    let moov_probe = crate::mp4::build_muxed_moov(&mvhd, &traks)?;
    let mut mdat_offset = ftyp.len() as u64 + moov_probe.len() as u64 + 8;
    let mut patched_traks = Vec::new();
    for (trak, source) in traks.iter().zip(sources.iter()) {
        patched_traks.push(crate::mp4::patch_first_chunk_offset(trak, mdat_offset)?);
        mdat_offset += source.len();
    }
    let moov = crate::mp4::build_muxed_moov(&mvhd, &patched_traks)?;
    let mut file = File::create(output_path)?;
    file.write_all(&ftyp)?;
    file.write_all(&moov)?;
    write_mdat_from_sources(&mut file, &sources)
}

#[pyfunction]
#[pyo3(signature = (decryption_key_audio, input_audio_path, output_path, decryption_key_video=None, input_video_path=None, use_cenc=false, use_single_content_key=false, m4v_brand=false))]
pub fn decrypt_and_mux_hex_native(
    py: Python<'_>,
    decryption_key_audio: String,
    input_audio_path: String,
    output_path: String,
    decryption_key_video: Option<String>,
    input_video_path: Option<String>,
    use_cenc: bool,
    use_single_content_key: bool,
    m4v_brand: bool,
) -> PyResult<()> {
    py.allow_threads(move || {
        let audio = decrypt_track_hex(
            &input_audio_path,
            &decryption_key_audio,
            *b"soun",
            use_cenc,
            use_single_content_key || input_video_path.is_some(),
            input_video_path.is_some(),
        )?;
        let video = if let Some(video_path) = input_video_path.as_ref() {
            Some(decrypt_track_hex(
                video_path,
                decryption_key_video
                    .as_deref()
                    .unwrap_or(&decryption_key_audio),
                *b"vide",
                use_cenc,
                true,
                true,
            )?)
        } else {
            None
        };
        let mut captions = Vec::new();
        if let Some(video_path) = input_video_path.as_ref() {
            for handler in [*b"clcp", *b"text", *b"sbtl", *b"subt"] {
                let mut caption_track =
                    extract_song(video_path, handler, true).map_err(py_io_error)?;
                if caption_track.samples.is_empty() {
                    continue;
                }
                let mut temp = NamedTempFile::new().map_err(py_io_error)?;
                let mut written = 0u64;
                for sample in &mut caption_track.samples {
                    let data = read_sample_data(sample).map_err(py_io_error)?;
                    temp.write_all(&data).map_err(py_io_error)?;
                    sample.size = data.len();
                    written += data.len() as u64;
                }
                let (_file, path) = temp.keep().map_err(|e| py_io_error(e.error))?;
                captions.push(DecryptedTrack {
                    input_path: video_path.clone(),
                    track_info: caption_track,
                    payload_path: path.to_string_lossy().to_string(),
                    payload_size: written,
                });
            }
        }
        write_decrypted_media_native(
            DecryptedMedia {
                audio,
                video,
                captions,
            },
            &output_path,
            m4v_brand,
        )
        .map_err(py_io_error)
    })
}

#[pyfunction]
#[pyo3(signature = (wrapper_decrypt_host, wrapper_decrypt_port, track_id, input_audio_path, output_path, fairplay_key_audio, input_video_path=None, fairplay_key_video=None, use_single_content_key=false, m4v_brand=false))]
pub fn decrypt_and_mux_wrapper_native(
    py: Python<'_>,
    wrapper_decrypt_host: String,
    wrapper_decrypt_port: u16,
    track_id: String,
    input_audio_path: String,
    output_path: String,
    fairplay_key_audio: String,
    input_video_path: Option<String>,
    fairplay_key_video: Option<String>,
    use_single_content_key: bool,
    m4v_brand: bool,
) -> PyResult<()> {
    py.allow_threads(move || {
        let audio = decrypt_track_wrapper(
            &wrapper_decrypt_host,
            wrapper_decrypt_port,
            &track_id,
            &input_audio_path,
            &fairplay_key_audio,
            *b"soun",
            use_single_content_key,
            input_video_path.is_some(),
        )?;
        let video = if let Some(video_path) = input_video_path.as_ref() {
            Some(decrypt_track_wrapper(
                &wrapper_decrypt_host,
                wrapper_decrypt_port,
                &track_id,
                video_path,
                fairplay_key_video.as_deref().unwrap_or(&fairplay_key_audio),
                *b"vide",
                true,
                true,
            )?)
        } else {
            None
        };
        let mut captions = Vec::new();
        if let Some(video_path) = input_video_path.as_ref() {
            for handler in [*b"clcp", *b"text", *b"sbtl", *b"subt"] {
                let mut caption_track =
                    extract_song(video_path, handler, true).map_err(py_io_error)?;
                if caption_track.samples.is_empty() {
                    continue;
                }
                let mut temp = NamedTempFile::new().map_err(py_io_error)?;
                let mut written = 0u64;
                for sample in &mut caption_track.samples {
                    let data = read_sample_data(sample).map_err(py_io_error)?;
                    temp.write_all(&data).map_err(py_io_error)?;
                    sample.size = data.len();
                    written += data.len() as u64;
                }
                let (_file, path) = temp.keep().map_err(|e| py_io_error(e.error))?;
                captions.push(DecryptedTrack {
                    input_path: video_path.clone(),
                    track_info: caption_track,
                    payload_path: path.to_string_lossy().to_string(),
                    payload_size: written,
                });
            }
        }
        write_decrypted_media_native(
            DecryptedMedia {
                audio,
                video,
                captions,
            },
            &output_path,
            m4v_brand,
        )
        .map_err(py_io_error)
    })
}
