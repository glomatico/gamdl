use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

#[derive(Clone, Debug)]
pub struct SampleInfo {
    pub size: u64,
    pub duration: u32,
    pub desc_index: usize,
    pub composition_time_offset: i32,
    pub is_sync: bool,
}

#[derive(Clone, Debug)]
pub struct TrackInfo {
    pub samples: Vec<SampleInfo>,
    pub moov_data: Vec<u8>,
    pub ftyp_data: Vec<u8>,
    pub handler_type: [u8; 4],
}

#[derive(Clone, Debug)]
pub enum PayloadSource {
    Memory(Vec<u8>),
    File {
        path: String,
        offset: u64,
        size: u64,
    },
}

impl PayloadSource {
    pub fn len(&self) -> u64 {
        match self {
            PayloadSource::Memory(data) => data.len() as u64,
            PayloadSource::File { size, .. } => *size,
        }
    }
}

fn be_u32(data: &[u8], offset: usize) -> Option<u32> {
    data.get(offset..offset + 4)
        .map(|b| u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

fn be_u64(data: &[u8], offset: usize) -> Option<u64> {
    data.get(offset..offset + 8)
        .map(|b| u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]))
}

fn put_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_be_bytes());
}

fn put_u16(out: &mut Vec<u8>, value: u16) {
    out.extend_from_slice(&value.to_be_bytes());
}

fn put_i32(out: &mut Vec<u8>, value: i32) {
    out.extend_from_slice(&value.to_be_bytes());
}

fn patch_u32(data: &mut [u8], offset: usize, value: u32) {
    if offset + 4 <= data.len() {
        data[offset..offset + 4].copy_from_slice(&value.to_be_bytes());
    }
}

fn patch_u64(data: &mut [u8], offset: usize, value: u64) {
    if offset + 8 <= data.len() {
        data[offset..offset + 8].copy_from_slice(&value.to_be_bytes());
    }
}

fn fourcc(value: &[u8]) -> [u8; 4] {
    [value[0], value[1], value[2], value[3]]
}

fn push_box(out: &mut Vec<u8>, typ: &[u8; 4], content: &[u8]) -> io::Result<()> {
    let size = content
        .len()
        .checked_add(8)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "mp4: box size overflow"))?;
    if size > u32::MAX as usize {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "mp4: 64-bit boxes are not supported by writer",
        ));
    }
    put_u32(out, size as u32);
    out.extend_from_slice(typ);
    out.extend_from_slice(content);
    Ok(())
}

fn push_full_box(
    out: &mut Vec<u8>,
    typ: &[u8; 4],
    version: u8,
    flags: u32,
    content: &[u8],
) -> io::Result<()> {
    let mut full = Vec::with_capacity(content.len() + 4);
    full.push(version);
    full.extend_from_slice(&flags.to_be_bytes()[1..]);
    full.extend_from_slice(content);
    push_box(out, typ, &full)
}

fn wrap_box(typ: &[u8; 4], content: Vec<u8>) -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    push_box(&mut out, typ, &content)?;
    Ok(out)
}

fn next_box(data: &[u8], offset: usize, end: usize) -> Option<([u8; 4], usize, usize, usize)> {
    if offset + 8 > end {
        return None;
    }
    let raw_size = be_u32(data, offset)?;
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
        raw_size as usize
    };
    if size < header_size || offset + size > end {
        return None;
    }
    Some((typ, offset, size, header_size))
}

pub fn extract_top_level_box(data: &[u8], target: &[u8; 4]) -> Option<Vec<u8>> {
    let mut offset = 0usize;
    while let Some((typ, box_offset, size, _)) = next_box(data, offset, data.len()) {
        if &typ == target {
            return Some(data[box_offset..box_offset + size].to_vec());
        }
        offset = box_offset + size;
    }
    None
}

pub fn find_child_box(container: &[u8], target: &[u8; 4], skip_header: usize) -> Option<Vec<u8>> {
    let mut offset = skip_header;
    while let Some((typ, box_offset, size, _)) = next_box(container, offset, container.len()) {
        if &typ == target {
            return Some(container[box_offset..box_offset + size].to_vec());
        }
        offset = box_offset + size;
    }
    None
}

pub fn find_box_offset_recursive(data: &[u8], target: &[u8; 4]) -> Option<usize> {
    fn walk(data: &[u8], target: &[u8; 4], start: usize, end: usize) -> Option<usize> {
        let containers: &[[u8; 4]] = &[
            *b"moov", *b"trak", *b"mdia", *b"minf", *b"stbl", *b"dinf", *b"edts", *b"udta",
            *b"meta",
        ];
        let mut offset = start;
        while let Some((typ, box_offset, size, header_size)) = next_box(data, offset, end) {
            if &typ == target {
                return Some(box_offset);
            }
            if containers.contains(&typ) {
                let mut child_start = box_offset + header_size;
                if &typ == b"meta" {
                    child_start += 4;
                }
                if child_start <= box_offset + size {
                    if let Some(found) = walk(data, target, child_start, box_offset + size) {
                        return Some(found);
                    }
                }
            }
            offset = box_offset + size;
        }
        None
    }
    walk(data, target, 0, data.len())
}

pub fn find_track_by_handler(moov_data: &[u8], handler_type: &[u8; 4]) -> Option<Vec<u8>> {
    let mut offset = 8usize;
    while let Some((typ, box_offset, size, _)) = next_box(moov_data, offset, moov_data.len()) {
        if &typ == b"trak" {
            let trak = &moov_data[box_offset..box_offset + size];
            if let Some(hdlr_offset) = find_subslice(trak, b"hdlr") {
                let handler_offset = hdlr_offset + 12;
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

fn find_first_trak(moov_data: &[u8]) -> Option<Vec<u8>> {
    find_child_box(moov_data, b"trak", 8)
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn extract_moov_from_orig_data(orig_data: &[u8]) -> Option<Vec<u8>> {
    if let Some(moov_idx) = find_subslice(orig_data, b"moov") {
        if moov_idx >= 4 {
            let size = be_u32(orig_data, moov_idx - 4)? as usize;
            if size >= 8 && moov_idx - 4 + size <= orig_data.len() {
                return Some(orig_data[moov_idx - 4..moov_idx - 4 + size].to_vec());
            }
        }
    }
    None
}

pub fn extract_mdat_payload(data: &[u8]) -> io::Result<Vec<u8>> {
    let mdat = extract_top_level_box(data, b"mdat").ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "mux: missing mdat box in decrypted track file",
        )
    })?;
    let header_size = if be_u32(&mdat, 0) == Some(1) { 16 } else { 8 };
    Ok(mdat[header_size..].to_vec())
}

fn extract_sample_rate_from_stsd(stsd_content: Option<&[u8]>) -> Option<u32> {
    let data = stsd_content?;
    if data.len() < 44 {
        return None;
    }
    let sample_rate = be_u32(data, 40)? >> 16;
    if (8000..=384000).contains(&sample_rate) {
        Some(sample_rate)
    } else {
        None
    }
}

fn extract_track_timescale(data: &[u8], handler_type: &[u8; 4], default: u32) -> u32 {
    let Some(moov_data) = extract_moov_from_orig_data(data) else {
        return default;
    };
    let Some(trak) = find_track_by_handler(&moov_data, handler_type) else {
        return default;
    };
    let Some(mdia) = find_child_box(&trak, b"mdia", 8) else {
        return default;
    };
    let Some(mdhd) = find_child_box(&mdia, b"mdhd", 8) else {
        return default;
    };
    if mdhd.len() < 28 {
        return default;
    }
    let version = mdhd[8];
    if version == 0 && mdhd.len() >= 24 {
        be_u32(&mdhd, 20).unwrap_or(default)
    } else if version == 1 && mdhd.len() >= 32 {
        be_u32(&mdhd, 28).unwrap_or(default)
    } else {
        default
    }
}

fn sample_entry_header_size(entry_type: &[u8]) -> usize {
    match entry_type {
        b"encv" | b"avc1" | b"avc3" | b"hvc1" | b"hev1" | b"dvh1" | b"dvhe" => 86,
        b"enca" | b"mp4a" | b"alac" | b"ac-3" | b"ec-3" => 36,
        b"c608" | b"c708" | b"text" | b"tx3g" | b"wvtt" | b"stpp" => 8,
        _ => 36,
    }
}

fn find_original_format(entry_data: &[u8]) -> Option<[u8; 4]> {
    let sinf_idx = find_subslice(entry_data, b"sinf")?;
    if sinf_idx < 4 {
        return None;
    }
    let sinf_size = be_u32(entry_data, sinf_idx - 4)? as usize;
    if sinf_size < 16 || sinf_idx + sinf_size > entry_data.len() + 4 {
        return None;
    }
    let sinf = &entry_data[sinf_idx - 4..sinf_idx - 4 + sinf_size];
    let frma_idx = find_subslice(sinf, b"frma")?;
    if frma_idx < 4 || be_u32(sinf, frma_idx - 4)? != 12 {
        return None;
    }
    Some(fourcc(sinf.get(frma_idx + 4..frma_idx + 8)?))
}

fn remove_sinf_from_entry(entry_data: &[u8]) -> Vec<u8> {
    if entry_data.len() < 16 {
        return entry_data.to_vec();
    }
    let entry_type = &entry_data[4..8];
    let header_size = sample_entry_header_size(entry_type);
    if entry_data.len() < header_size {
        return entry_data.to_vec();
    }
    let mut out = entry_data[..header_size].to_vec();
    let mut child_offset = header_size;
    while let Some((typ, box_offset, size, _)) =
        next_box(entry_data, child_offset, entry_data.len())
    {
        if &typ != b"sinf" {
            out.extend_from_slice(&entry_data[box_offset..box_offset + size]);
        }
        child_offset = box_offset + size;
    }
    let out_len = out.len() as u32;
    patch_u32(&mut out, 0, out_len);
    out
}

fn clean_encrypted_sample_entry(entry_data: &[u8]) -> Vec<u8> {
    if entry_data.len() < 16 {
        return entry_data.to_vec();
    }
    let entry_type = &entry_data[4..8];
    let header_size = sample_entry_header_size(entry_type);
    if entry_data.len() < header_size {
        return entry_data.to_vec();
    }
    let original_format = find_original_format(entry_data).unwrap_or_else(|| match entry_type {
        b"enca" => *b"mp4a",
        b"encv" => *b"avc1",
        _ => fourcc(entry_type),
    });

    let mut out = Vec::new();
    out.extend_from_slice(&entry_data[..4]);
    out.extend_from_slice(&original_format);
    out.extend_from_slice(&entry_data[8..header_size]);
    let mut child_offset = header_size;
    while let Some((typ, box_offset, size, _)) =
        next_box(entry_data, child_offset, entry_data.len())
    {
        if &typ != b"sinf" {
            out.extend_from_slice(&entry_data[box_offset..box_offset + size]);
        }
        child_offset = box_offset + size;
    }
    let out_len = out.len() as u32;
    patch_u32(&mut out, 0, out_len);
    out
}

pub fn clean_stsd_content(stsd_content: &[u8], preferred_desc_index: Option<usize>) -> Vec<u8> {
    if stsd_content.len() < 8 {
        return stsd_content.to_vec();
    }
    let version_flags = &stsd_content[..4];
    let entry_count = be_u32(stsd_content, 4).unwrap_or(0);
    let mut entries = Vec::new();
    let mut offset = 8usize;
    for _ in 0..entry_count {
        let Some(entry_size) = be_u32(stsd_content, offset).map(|v| v as usize) else {
            break;
        };
        if entry_size < 8 || offset + entry_size > stsd_content.len() {
            break;
        }
        let entry = &stsd_content[offset..offset + entry_size];
        let cleaned = match &entry[4..8] {
            b"enca" | b"encv" | b"encs" | b"encm" => clean_encrypted_sample_entry(entry),
            _ => remove_sinf_from_entry(entry),
        };
        entries.push(cleaned);
        offset += entry_size;
    }
    if let Some(index) = preferred_desc_index {
        if !entries.is_empty() {
            let chosen = entries
                .get(index)
                .cloned()
                .unwrap_or_else(|| entries[0].clone());
            entries = vec![chosen];
        }
    }
    let mut out = Vec::new();
    out.extend_from_slice(version_flags);
    put_u32(&mut out, entries.len() as u32);
    for entry in entries {
        out.extend_from_slice(&entry);
    }
    out
}

pub fn extract_stsd_content(
    data: &[u8],
    preferred_desc_index: Option<usize>,
    handler_type: &[u8; 4],
) -> Option<Vec<u8>> {
    let moov_data = extract_moov_from_orig_data(data)?;
    let trak = find_track_by_handler(&moov_data, handler_type)?;
    let mdia = find_child_box(&trak, b"mdia", 8)?;
    let minf = find_child_box(&mdia, b"minf", 8)?;
    let stbl = find_child_box(&minf, b"stbl", 8)?;
    let stsd = find_child_box(&stbl, b"stsd", 8)?;
    if stsd.len() < 16 {
        return None;
    }
    Some(clean_stsd_content(&stsd[8..], preferred_desc_index))
}

fn preferred_sample_description_index(samples: &[SampleInfo]) -> usize {
    let mut counts: Vec<(usize, usize)> = Vec::new();
    for sample in samples.iter().filter(|s| s.size > 0) {
        if let Some((_, count)) = counts.iter_mut().find(|(idx, _)| *idx == sample.desc_index) {
            *count += 1;
        } else {
            counts.push((sample.desc_index, 1));
        }
    }
    counts
        .into_iter()
        .max_by_key(|(_, count)| *count)
        .map(|(idx, _)| idx)
        .unwrap_or(0)
}

fn patch_mvhd_duration(data: &[u8], duration: u64, timescale: u32) -> Vec<u8> {
    let mut out = data.to_vec();
    if out.len() < 32 {
        return out;
    }
    if out[8] == 0 {
        patch_u32(&mut out, 20, timescale);
        patch_u32(&mut out, 24, duration.min(u32::MAX as u64) as u32);
    } else {
        patch_u32(&mut out, 28, timescale);
        patch_u64(&mut out, 32, duration);
    }
    out
}

fn patch_tkhd_duration(data: &[u8], duration: u64) -> Vec<u8> {
    let mut out = data.to_vec();
    if out.len() < 12 {
        return out;
    }
    out[9..12].copy_from_slice(&7u32.to_be_bytes()[1..]);
    if out[8] == 0 {
        patch_u32(&mut out, 28, duration.min(u32::MAX as u64) as u32);
    } else {
        patch_u64(&mut out, 36, duration);
    }
    out
}

fn patch_mdhd_duration(data: &[u8], duration: u64, timescale: u32) -> Vec<u8> {
    let mut out = data.to_vec();
    if out.len() < 32 {
        return out;
    }
    if out[8] == 0 {
        patch_u32(&mut out, 20, timescale);
        patch_u32(&mut out, 24, duration.min(u32::MAX as u64) as u32);
    } else {
        patch_u32(&mut out, 28, timescale);
        patch_u64(&mut out, 32, duration);
    }
    out
}

pub fn patch_mvhd_next_track_id(data: &[u8], next_track_id: u32) -> Vec<u8> {
    let mut out = data.to_vec();
    if out.len() < 112 {
        return out;
    }
    let offset = if out[8] == 0 { 108 } else { 120 };
    patch_u32(&mut out, offset, next_track_id);
    out
}

pub fn extract_mvhd_timescale(mvhd: &[u8]) -> u32 {
    if mvhd.len() < 32 {
        return 1000;
    }
    if mvhd[8] == 0 && mvhd.len() >= 24 {
        be_u32(mvhd, 20).unwrap_or(1000)
    } else if mvhd[8] == 1 && mvhd.len() >= 32 {
        be_u32(mvhd, 28).unwrap_or(1000)
    } else {
        1000
    }
}

fn extract_mdhd_duration_timescale(trak: &[u8]) -> (u64, u32) {
    let Some(offset) = find_box_offset_recursive(trak, b"mdhd") else {
        return (0, 1);
    };
    if offset + 32 > trak.len() {
        return (0, 1);
    }
    if trak[offset + 8] == 0 {
        (
            be_u32(trak, offset + 24).unwrap_or(0) as u64,
            be_u32(trak, offset + 20).unwrap_or(1).max(1),
        )
    } else {
        (
            be_u64(trak, offset + 32).unwrap_or(0),
            be_u32(trak, offset + 28).unwrap_or(1).max(1),
        )
    }
}

pub fn patch_trak_duration_to_movie_timescale(trak: &[u8], movie_timescale: u32) -> Vec<u8> {
    let (media_duration, media_timescale) = extract_mdhd_duration_timescale(trak);
    if media_duration == 0 {
        return trak.to_vec();
    }
    let movie_duration = ((media_duration as f64) * (movie_timescale as f64)
        / (media_timescale as f64))
        .round() as u64;
    patch_trak_tkhd_duration(trak, movie_duration)
}

fn patch_trak_tkhd_duration(trak: &[u8], duration: u64) -> Vec<u8> {
    let mut out = trak.to_vec();
    let Some(tkhd_offset) = find_box_offset_recursive(&out, b"tkhd") else {
        return out;
    };
    if tkhd_offset + 12 > out.len() {
        return out;
    }
    out[tkhd_offset + 9..tkhd_offset + 12].copy_from_slice(&7u32.to_be_bytes()[1..]);
    if out[tkhd_offset + 8] == 0 {
        patch_u32(
            &mut out,
            tkhd_offset + 28,
            duration.min(u32::MAX as u64) as u32,
        );
    } else {
        patch_u64(&mut out, tkhd_offset + 36, duration);
    }
    out
}

pub fn patch_trak_track_id(trak: &[u8], track_id: u32) -> Vec<u8> {
    let mut out = trak.to_vec();
    let Some(tkhd_offset) = find_box_offset_recursive(&out, b"tkhd") else {
        return out;
    };
    if tkhd_offset + 12 > out.len() {
        return out;
    }
    let track_id_offset = if out[tkhd_offset + 8] == 0 {
        tkhd_offset + 20
    } else {
        tkhd_offset + 28
    };
    patch_u32(&mut out, track_id_offset, track_id);
    out
}

pub fn patch_first_chunk_offset(trak: &[u8], chunk_offset: u64) -> io::Result<Vec<u8>> {
    let mut out = trak.to_vec();
    if let Some(stco_offset) = find_box_offset_recursive(&out, b"stco") {
        let entry_count_offset = stco_offset + 12;
        let first_entry_offset = stco_offset + 16;
        if first_entry_offset + 4 <= out.len() && be_u32(&out, entry_count_offset).unwrap_or(0) > 0
        {
            if chunk_offset > u32::MAX as u64 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "mux: chunk offset too large for stco",
                ));
            }
            patch_u32(&mut out, first_entry_offset, chunk_offset as u32);
            return Ok(out);
        }
    }
    if let Some(co64_offset) = find_box_offset_recursive(&out, b"co64") {
        let entry_count_offset = co64_offset + 12;
        let first_entry_offset = co64_offset + 16;
        if first_entry_offset + 8 <= out.len() && be_u32(&out, entry_count_offset).unwrap_or(0) > 0
        {
            patch_u64(&mut out, first_entry_offset, chunk_offset);
            return Ok(out);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "mux: unable to patch chunk offset",
    ))
}

fn build_udta() -> io::Result<Vec<u8>> {
    let mut meta = Vec::new();
    put_u32(&mut meta, 0);
    let mut hdlr = Vec::new();
    put_u32(&mut hdlr, 0);
    hdlr.extend_from_slice(b"mdir");
    put_u32(&mut hdlr, 0x6170_706c);
    put_u32(&mut hdlr, 0);
    put_u32(&mut hdlr, 0);
    hdlr.push(0);
    push_full_box(&mut meta, b"hdlr", 0, 0, &hdlr)?;
    push_box(&mut meta, b"ilst", b"")?;
    wrap_box(b"udta", wrap_box(b"meta", meta)?)
}

fn write_stsd(out: &mut Vec<u8>, stsd_content: Option<&[u8]>) -> io::Result<()> {
    if let Some(content) = stsd_content.filter(|c| !c.is_empty()) {
        push_box(out, b"stsd", content)
    } else {
        write_stsd_alac_fallback(out)
    }
}

fn write_stsd_alac_fallback(out: &mut Vec<u8>) -> io::Result<()> {
    let mut alac = Vec::new();
    alac.extend_from_slice(&[0; 6]);
    put_u16(&mut alac, 1);
    alac.extend_from_slice(&[0; 8]);
    put_u16(&mut alac, 2);
    put_u16(&mut alac, 16);
    put_u16(&mut alac, 0);
    put_u16(&mut alac, 0);
    put_u32(&mut alac, 44100 << 16);
    push_box(
        &mut alac,
        b"alac",
        &[
            0x00, 0x00, 0x10, 0x00, 0x00, 0x18, 0x28, 0x28, 0x0A, 0x02, 0x00, 0x00, 0x00, 0x00,
            0xFF, 0xFF, 0x00, 0x0D, 0x00, 0x80, 0x00, 0x00, 0xAC, 0x44,
        ],
    )?;
    let alac_box = wrap_box(b"alac", alac)?;
    let mut stsd = Vec::new();
    put_u32(&mut stsd, 0);
    put_u32(&mut stsd, 1);
    stsd.extend_from_slice(&alac_box);
    push_box(out, b"stsd", &stsd)
}

fn write_stts(out: &mut Vec<u8>, samples: &[SampleInfo]) -> io::Result<()> {
    let mut entries: Vec<(u32, u32)> = Vec::new();
    for sample in samples {
        if let Some(last) = entries
            .last_mut()
            .filter(|(_, delta)| *delta == sample.duration)
        {
            last.0 += 1;
        } else {
            entries.push((1, sample.duration));
        }
    }
    let mut content = Vec::new();
    put_u32(&mut content, entries.len() as u32);
    for (count, delta) in entries {
        put_u32(&mut content, count);
        put_u32(&mut content, delta);
    }
    push_full_box(out, b"stts", 0, 0, &content)
}

fn write_ctts(out: &mut Vec<u8>, samples: &[SampleInfo]) -> io::Result<()> {
    if !samples.iter().any(|s| s.composition_time_offset != 0) {
        return Ok(());
    }
    let mut entries: Vec<(u32, i32)> = Vec::new();
    for sample in samples {
        if let Some(last) = entries
            .last_mut()
            .filter(|(_, offset)| *offset == sample.composition_time_offset)
        {
            last.0 += 1;
        } else {
            entries.push((1, sample.composition_time_offset));
        }
    }
    let version = if entries.iter().any(|(_, offset)| *offset < 0) {
        1
    } else {
        0
    };
    let mut content = Vec::new();
    put_u32(&mut content, entries.len() as u32);
    for (count, offset) in entries {
        put_u32(&mut content, count);
        if version == 1 {
            put_i32(&mut content, offset);
        } else {
            put_u32(&mut content, offset as u32);
        }
    }
    push_full_box(out, b"ctts", version, 0, &content)
}

fn write_stss(out: &mut Vec<u8>, samples: &[SampleInfo]) -> io::Result<()> {
    if samples.is_empty() || samples.iter().all(|s| s.is_sync) {
        return Ok(());
    }
    let sync_samples: Vec<u32> = samples
        .iter()
        .enumerate()
        .filter_map(|(idx, sample)| sample.is_sync.then_some((idx + 1) as u32))
        .collect();
    if sync_samples.is_empty() {
        return Ok(());
    }
    let mut content = Vec::new();
    put_u32(&mut content, sync_samples.len() as u32);
    for sample_number in sync_samples {
        put_u32(&mut content, sample_number);
    }
    push_full_box(out, b"stss", 0, 0, &content)
}

fn build_mvhd(total_duration: u64, timescale: u32) -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    put_u32(&mut content, 0);
    put_u32(&mut content, 0);
    put_u32(&mut content, timescale);
    put_u32(&mut content, total_duration.min(u32::MAX as u64) as u32);
    put_u32(&mut content, 0x0001_0000);
    put_u16(&mut content, 0x0100);
    content.extend_from_slice(&[0; 10]);
    for value in [0x0001_0000, 0, 0, 0, 0x0001_0000, 0, 0, 0, 0x4000_0000] {
        put_u32(&mut content, value);
    }
    content.extend_from_slice(&[0; 24]);
    put_u32(&mut content, 2);
    let mut out = Vec::new();
    push_full_box(&mut out, b"mvhd", 0, 0, &content)?;
    Ok(out)
}

fn build_tkhd(total_duration: u64) -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    put_u32(&mut content, 0);
    put_u32(&mut content, 0);
    put_u32(&mut content, 1);
    put_u32(&mut content, 0);
    put_u32(&mut content, total_duration.min(u32::MAX as u64) as u32);
    content.extend_from_slice(&[0; 8]);
    put_u16(&mut content, 0);
    put_u16(&mut content, 0);
    put_u16(&mut content, 0x0100);
    put_u16(&mut content, 0);
    for value in [0x0001_0000, 0, 0, 0, 0x0001_0000, 0, 0, 0, 0x4000_0000] {
        put_u32(&mut content, value);
    }
    put_u32(&mut content, 0);
    put_u32(&mut content, 0);
    let mut out = Vec::new();
    push_full_box(&mut out, b"tkhd", 0, 7, &content)?;
    Ok(out)
}

fn build_mdhd(total_duration: u64, timescale: u32) -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    put_u32(&mut content, 0);
    put_u32(&mut content, 0);
    put_u32(&mut content, timescale);
    put_u32(&mut content, total_duration.min(u32::MAX as u64) as u32);
    put_u16(&mut content, 0x55c4);
    put_u16(&mut content, 0);
    let mut out = Vec::new();
    push_full_box(&mut out, b"mdhd", 0, 0, &content)?;
    Ok(out)
}

fn build_hdlr(handler_type: &[u8; 4]) -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    put_u32(&mut content, 0);
    content.extend_from_slice(handler_type);
    content.extend_from_slice(&[0; 12]);
    let name = if handler_type == b"vide" {
        b"VideoHandler".as_slice()
    } else if handler_type == b"soun" {
        b"SoundHandler".as_slice()
    } else {
        b"TextHandler".as_slice()
    };
    content.push(name.len() as u8);
    content.extend_from_slice(name);
    content.push(0);
    let mut out = Vec::new();
    push_full_box(&mut out, b"hdlr", 0, 0, &content)?;
    Ok(out)
}

fn build_dinf() -> io::Result<Vec<u8>> {
    let mut dref = Vec::new();
    put_u32(&mut dref, 1);
    put_u32(&mut dref, 12);
    dref.extend_from_slice(b"url ");
    put_u32(&mut dref, 1);
    let mut dinf = Vec::new();
    push_full_box(&mut dinf, b"dref", 0, 0, &dref)?;
    wrap_box(b"dinf", dinf)
}

fn build_moov_internal(
    track: &TrackInfo,
    orig_data: Option<&[u8]>,
    stsd_content: Option<Vec<u8>>,
) -> io::Result<Vec<u8>> {
    let samples = &track.samples;
    let total_duration: u64 = samples.iter().map(|sample| sample.duration as u64).sum();
    let mut timescale = if &track.handler_type == b"soun" {
        44100
    } else {
        90000
    };

    let mut orig_mvhd = None;
    let mut orig_tkhd = None;
    let mut orig_mdhd = None;
    let mut orig_hdlr = None;
    let mut orig_smhd = None;
    let mut orig_vmhd = None;
    let mut orig_nmhd = None;
    let mut orig_dinf = None;

    if let Some(data) = orig_data {
        if &track.handler_type == b"soun" {
            timescale = extract_sample_rate_from_stsd(stsd_content.as_deref())
                .unwrap_or_else(|| extract_track_timescale(data, &track.handler_type, timescale));
        } else {
            timescale = extract_track_timescale(data, &track.handler_type, timescale);
        }
        if let Some(moov) = extract_moov_from_orig_data(data) {
            orig_mvhd = find_child_box(&moov, b"mvhd", 8);
            if let Some(trak) = find_track_by_handler(&moov, &track.handler_type) {
                orig_tkhd = find_child_box(&trak, b"tkhd", 8);
                if let Some(mdia) = find_child_box(&trak, b"mdia", 8) {
                    orig_mdhd = find_child_box(&mdia, b"mdhd", 8);
                    orig_hdlr = find_child_box(&mdia, b"hdlr", 8);
                    if let Some(minf) = find_child_box(&mdia, b"minf", 8) {
                        orig_smhd = find_child_box(&minf, b"smhd", 8);
                        orig_vmhd = find_child_box(&minf, b"vmhd", 8);
                        orig_nmhd = find_child_box(&minf, b"nmhd", 8);
                        orig_dinf = find_child_box(&minf, b"dinf", 8);
                    }
                }
            }
        }
    }

    let mut moov = Vec::new();
    if let Some(mvhd) = orig_mvhd {
        moov.extend_from_slice(&patch_mvhd_duration(&mvhd, total_duration, timescale));
    } else {
        moov.extend_from_slice(&build_mvhd(total_duration, timescale)?);
    }

    let mut trak = Vec::new();
    if let Some(tkhd) = orig_tkhd {
        trak.extend_from_slice(&patch_tkhd_duration(&tkhd, total_duration));
    } else {
        trak.extend_from_slice(&build_tkhd(total_duration)?);
    }

    let mut mdia = Vec::new();
    if let Some(mdhd) = orig_mdhd {
        mdia.extend_from_slice(&patch_mdhd_duration(&mdhd, total_duration, timescale));
    } else {
        mdia.extend_from_slice(&build_mdhd(total_duration, timescale)?);
    }
    if let Some(hdlr) = orig_hdlr {
        mdia.extend_from_slice(&hdlr);
    } else {
        mdia.extend_from_slice(&build_hdlr(&track.handler_type)?);
    }

    let mut minf = Vec::new();
    if &track.handler_type == b"vide" {
        if let Some(vmhd) = orig_vmhd {
            minf.extend_from_slice(&vmhd);
        } else {
            let mut vmhd_content = Vec::new();
            put_u16(&mut vmhd_content, 0);
            put_u16(&mut vmhd_content, 0);
            put_u16(&mut vmhd_content, 0);
            put_u16(&mut vmhd_content, 0);
            push_full_box(&mut minf, b"vmhd", 0, 1, &vmhd_content)?;
        }
    } else if &track.handler_type == b"soun" {
        if let Some(smhd) = orig_smhd {
            minf.extend_from_slice(&smhd);
        } else {
            let mut smhd_content = Vec::new();
            put_u16(&mut smhd_content, 0);
            put_u16(&mut smhd_content, 0);
            push_full_box(&mut minf, b"smhd", 0, 0, &smhd_content)?;
        }
    } else if let Some(nmhd) = orig_nmhd {
        minf.extend_from_slice(&nmhd);
    } else {
        push_full_box(&mut minf, b"nmhd", 0, 0, b"")?;
    }
    if let Some(dinf) = orig_dinf {
        minf.extend_from_slice(&dinf);
    } else {
        minf.extend_from_slice(&build_dinf()?);
    }

    let mut stbl = Vec::new();
    write_stsd(&mut stbl, stsd_content.as_deref())?;
    write_stts(&mut stbl, samples)?;
    write_ctts(&mut stbl, samples)?;
    if &track.handler_type == b"vide" {
        write_stss(&mut stbl, samples)?;
    }

    let mut stsc = Vec::new();
    put_u32(&mut stsc, 1);
    put_u32(&mut stsc, 1);
    put_u32(&mut stsc, samples.len() as u32);
    put_u32(&mut stsc, 1);
    push_full_box(&mut stbl, b"stsc", 0, 0, &stsc)?;

    let mut stsz = Vec::new();
    put_u32(&mut stsz, 0);
    put_u32(&mut stsz, samples.len() as u32);
    for sample in samples {
        put_u32(&mut stsz, sample.size.min(u32::MAX as u64) as u32);
    }
    push_full_box(&mut stbl, b"stsz", 0, 0, &stsz)?;

    let mut stco = Vec::new();
    put_u32(&mut stco, 1);
    put_u32(&mut stco, 0);
    push_full_box(&mut stbl, b"stco", 0, 0, &stco)?;

    minf.extend_from_slice(&wrap_box(b"stbl", stbl)?);
    mdia.extend_from_slice(&wrap_box(b"minf", minf)?);
    trak.extend_from_slice(&wrap_box(b"mdia", mdia)?);
    moov.extend_from_slice(&wrap_box(b"trak", trak)?);
    moov.extend_from_slice(&build_udta()?);
    wrap_box(b"moov", moov)
}

pub fn build_decrypted_track_moov(
    track: &TrackInfo,
    original_path: Option<&str>,
) -> io::Result<Vec<u8>> {
    let orig_data = load_orig_data(track, original_path)?;
    let preferred_desc_index = preferred_sample_description_index(&track.samples);
    let stsd = orig_data.as_deref().and_then(|data| {
        extract_stsd_content(data, Some(preferred_desc_index), &track.handler_type)
    });
    build_moov_internal(track, orig_data.as_deref(), stsd)
}

fn load_orig_data(track: &TrackInfo, original_path: Option<&str>) -> io::Result<Option<Vec<u8>>> {
    if !track.moov_data.is_empty() {
        let mut data = Vec::with_capacity(track.ftyp_data.len() + track.moov_data.len());
        data.extend_from_slice(&track.ftyp_data);
        data.extend_from_slice(&track.moov_data);
        Ok(Some(data))
    } else if let Some(path) = original_path {
        std::fs::read(path).map(Some)
    } else {
        Ok(None)
    }
}

fn ftyp_m4a() -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    content.extend_from_slice(b"M4A ");
    put_u32(&mut content, 0);
    content.extend_from_slice(b"M4A mp42isom\0\0\0\0");
    wrap_box(b"ftyp", content)
}

pub fn ftyp_mp4() -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    content.extend_from_slice(b"mp42");
    put_u32(&mut content, 0);
    content.extend_from_slice(b"mp42isomiso6avc1hvc1");
    wrap_box(b"ftyp", content)
}

pub fn ftyp_m4v() -> io::Result<Vec<u8>> {
    let mut content = Vec::new();
    content.extend_from_slice(b"M4V ");
    put_u32(&mut content, 0);
    content.extend_from_slice(b"M4V mp42isom");
    wrap_box(b"ftyp", content)
}

pub fn build_muxed_moov(mvhd: &[u8], traks: &[Vec<u8>]) -> io::Result<Vec<u8>> {
    let mut payload = Vec::new();
    payload.extend_from_slice(&patch_mvhd_next_track_id(mvhd, traks.len() as u32 + 1));
    for trak in traks {
        payload.extend_from_slice(trak);
    }
    payload.extend_from_slice(&build_udta()?);
    wrap_box(b"moov", payload)
}

pub fn write_track_file(
    output_path: &str,
    track: &TrackInfo,
    original_path: Option<&str>,
    payload: &PayloadSource,
) -> io::Result<()> {
    let ftyp = if &track.handler_type == b"soun" {
        ftyp_m4a()?
    } else {
        ftyp_mp4()?
    };
    let mut moov = build_decrypted_track_moov(track, original_path)?;
    let mdat_data_offset = ftyp.len() as u64 + moov.len() as u64 + 8;
    moov = patch_moov_first_trak_chunk_offset(&moov, mdat_data_offset)?;
    let mut file = File::create(output_path)?;
    file.write_all(&ftyp)?;
    file.write_all(&moov)?;
    write_mdat_from_sources(&mut file, &[payload.clone()])
}

pub fn write_m4a_file(
    output_path: &str,
    track: &TrackInfo,
    original_path: Option<&str>,
    payload: &PayloadSource,
) -> io::Result<()> {
    write_track_file(output_path, track, original_path, payload)
}

fn patch_moov_first_trak_chunk_offset(moov: &[u8], offset: u64) -> io::Result<Vec<u8>> {
    let Some(trak) = find_first_trak(moov) else {
        return Ok(moov.to_vec());
    };
    let patched_trak = patch_first_chunk_offset(&trak, offset)?;
    replace_first_child_box(moov, b"trak", &patched_trak)
}

fn replace_first_child_box(
    container: &[u8],
    typ: &[u8; 4],
    replacement: &[u8],
) -> io::Result<Vec<u8>> {
    let mut offset = 8usize;
    while let Some((child_type, box_offset, size, _)) = next_box(container, offset, container.len())
    {
        if &child_type == typ {
            let mut out = Vec::with_capacity(container.len() - size + replacement.len());
            out.extend_from_slice(&container[..box_offset]);
            out.extend_from_slice(replacement);
            out.extend_from_slice(&container[box_offset + size..]);
            let out_len = out.len() as u32;
            patch_u32(&mut out, 0, out_len);
            return Ok(out);
        }
        offset = box_offset + size;
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "mux: child box not found",
    ))
}

pub fn write_mdat_from_sources<W: Write>(out: &mut W, sources: &[PayloadSource]) -> io::Result<()> {
    let payload_size = sources.iter().try_fold(0u64, |acc, source| {
        acc.checked_add(source.len()).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: mdat too large for 32-bit box size",
            )
        })
    })?;
    if payload_size + 8 > u32::MAX as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "mux: mdat too large for 32-bit box size",
        ));
    }
    out.write_all(&((payload_size + 8) as u32).to_be_bytes())?;
    out.write_all(b"mdat")?;
    for source in sources {
        match source {
            PayloadSource::Memory(data) => out.write_all(data)?,
            PayloadSource::File { path, offset, size } => {
                copy_file_range(out, path, *offset, *size)?;
            }
        }
    }
    Ok(())
}

fn copy_file_range<W: Write>(out: &mut W, path: &str, offset: u64, size: u64) -> io::Result<()> {
    let mut input = File::open(Path::new(path))?;
    input.seek(SeekFrom::Start(offset))?;
    let mut remaining = size;
    let mut buf = vec![0u8; 1024 * 1024];
    while remaining > 0 {
        let to_read = remaining.min(buf.len() as u64) as usize;
        let n = input.read(&mut buf[..to_read])?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("unexpected EOF while reading {path}"),
            ));
        }
        out.write_all(&buf[..n])?;
        remaining -= n as u64;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn simple_box(typ: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        push_box(&mut out, typ, payload).unwrap();
        out
    }

    #[test]
    fn extracts_top_level_box() {
        let mut data = simple_box(b"ftyp", b"abc");
        data.extend_from_slice(&simple_box(b"mdat", b"payload"));
        assert_eq!(
            extract_top_level_box(&data, b"mdat").unwrap()[8..],
            *b"payload"
        );
    }

    #[test]
    fn finds_recursive_box_offset() {
        let stco = simple_box(b"stco", b"\0\0\0\0\0\0\0\x01\0\0\0\0");
        let stbl = simple_box(b"stbl", &stco);
        let minf = simple_box(b"minf", &stbl);
        assert!(find_box_offset_recursive(&minf, b"stco").is_some());
    }

    #[test]
    fn patches_first_stco_offset() {
        let stco = simple_box(b"stco", b"\0\0\0\0\0\0\0\x01\0\0\0\0");
        let stbl = simple_box(b"stbl", &stco);
        let minf = simple_box(b"minf", &stbl);
        let patched = patch_first_chunk_offset(&minf, 123).unwrap();
        let stco_offset = find_box_offset_recursive(&patched, b"stco").unwrap();
        assert_eq!(be_u32(&patched, stco_offset + 16), Some(123));
    }

    #[test]
    fn cleans_encrypted_sample_entry() {
        let frma = simple_box(b"frma", b"alac");
        let sinf = simple_box(b"sinf", &frma);
        let mut entry = Vec::new();
        entry.extend_from_slice(&[0; 36]);
        entry[4..8].copy_from_slice(b"enca");
        entry.extend_from_slice(&sinf);
        let entry_len = entry.len() as u32;
        patch_u32(&mut entry, 0, entry_len);
        let cleaned = clean_encrypted_sample_entry(&entry);
        assert_eq!(&cleaned[4..8], b"alac");
        assert!(find_subslice(&cleaned, b"sinf").is_none());
    }
}
