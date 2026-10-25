//! Lossless conversion of a single-track audio fMP4 to a seekable MP4.
//!
//! Rebuild sample tables from trun/tfhd/trex; copy sample payloads and stsd
//! untouched. This module does not decrypt or transcode. Reject unsupported
//! layouts rather than silently emitting a truncated/corrupt file.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

type Result<T> = std::result::Result<T, &'static str>;
const BAD: &str = "Malformed fragmented MP4";
const UNSUPPORTED: &str = "Unsupported fragmented MP4 remux layout";

#[derive(Clone, Copy)]
pub(crate) struct Atom {
    pub(crate) start: usize,
    pub(crate) data: usize,
    pub(crate) end: usize,
    pub(crate) name: [u8; 4],
}

pub(crate) fn u32_at(data: &[u8], at: usize) -> Result<u32> {
    let end = at.checked_add(4).ok_or(BAD)?;
    Ok(u32::from_be_bytes(
        data.get(at..end).ok_or(BAD)?.try_into().map_err(|_| BAD)?,
    ))
}
pub(crate) fn u64_at(data: &[u8], at: usize) -> Result<u64> {
    let end = at.checked_add(8).ok_or(BAD)?;
    Ok(u64::from_be_bytes(
        data.get(at..end).ok_or(BAD)?.try_into().map_err(|_| BAD)?,
    ))
}
pub(crate) fn atoms(data: &[u8], mut at: usize, end: usize) -> Result<Vec<Atom>> {
    if at > end || end > data.len() {
        return Err(BAD);
    }
    let mut out = Vec::new();
    while at < end {
        let n = u32_at(data, at)? as usize;
        let kind_end = at.checked_add(8).ok_or(BAD)?;
        let name = data
            .get(at + 4..kind_end)
            .ok_or(BAD)?
            .try_into()
            .map_err(|_| BAD)?;
        let (header, size) = if n == 1 {
            (16, usize::try_from(u64_at(data, at + 8)?).map_err(|_| BAD)?)
        } else if n == 0 {
            (8, end - at)
        } else {
            (8, n)
        };
        if size < header {
            return Err(BAD);
        }
        let next = at.checked_add(size).ok_or(BAD)?;
        if next > end {
            return Err(BAD);
        }
        out.push(Atom {
            start: at,
            data: at + header,
            end: next,
            name,
        });
        at = next;
    }
    Ok(out)
}
pub(crate) fn children(data: &[u8], parent: Atom) -> Result<Vec<Atom>> {
    atoms(data, parent.data, parent.end)
}
pub(crate) fn find(data: &[u8], parent: Atom, name: &[u8; 4]) -> Result<Atom> {
    children(data, parent)?
        .into_iter()
        .find(|a| &a.name == name)
        .ok_or(UNSUPPORTED)
}
pub(crate) fn boxed(name: &[u8; 4], body: &[u8]) -> Result<Vec<u8>> {
    let len = u32::try_from(body.len().checked_add(8).ok_or(BAD)?).map_err(|_| UNSUPPORTED)?;
    let mut out = Vec::with_capacity(len as usize);
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(name);
    out.extend_from_slice(body);
    Ok(out)
}
pub(crate) fn fullbox(name: &[u8; 4], version_flags: u32, payload: &[u8]) -> Result<Vec<u8>> {
    let mut body = Vec::with_capacity(4 + payload.len());
    body.extend(version_flags.to_be_bytes());
    body.extend(payload);
    boxed(name, &body)
}

/// Final-file brands, independent of Apple's HLS fragment/transport brands.
/// Keep the codec sample descriptions (avc1/hvc1/mp4a/ec-3) unchanged.
pub(crate) fn file_type(major_brand: &[u8; 4]) -> Result<Vec<u8>> {
    let compatible: &[u8] = match major_brand {
        b"M4A " => b"M4A mp42isom",
        b"M4V " => b"M4V mp42isom",
        b"mp42" => b"mp42isom",
        _ => return Err(UNSUPPORTED),
    };
    let mut body = Vec::with_capacity(8 + compatible.len());
    body.extend_from_slice(major_brand);
    body.extend_from_slice(&0u32.to_be_bytes());
    body.extend_from_slice(compatible);
    boxed(b"ftyp", &body)
}
pub(crate) fn set_duration(data: &[u8], atom: Atom, field: usize, value: u64) -> Result<Vec<u8>> {
    let mut body = data[atom.data..atom.end].to_vec();
    let version = *body.first().ok_or(BAD)?;
    let (pos, width) = match version {
        0 => (field, 4),
        1 => (field + 8, 8),
        _ => return Err(UNSUPPORTED),
    };
    let bytes = if width == 4 {
        u32::try_from(value)
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes()
            .to_vec()
    } else {
        value.to_be_bytes().to_vec()
    };
    body.get_mut(pos..pos + width)
        .ok_or(BAD)?
        .copy_from_slice(&bytes);
    boxed(&atom.name, &body)
}

struct Info {
    track_id: u32,
    media_scale: u32,
    movie_scale: u32,
    default_desc: u32,
    default_duration: u32,
    default_size: u32,
    entries: u32,
}

#[derive(Clone)]
struct Sample {
    bytes: std::ops::Range<usize>,
    duration: u32,
    description: u32,
}

fn info(bytes: &[u8], moov: Atom) -> Result<Info> {
    let traks = children(bytes, moov)?
        .into_iter()
        .filter(|a| &a.name == b"trak")
        .collect::<Vec<_>>();
    if traks.len() != 1 {
        return Err(UNSUPPORTED);
    }
    let trak = traks[0];
    let tkhd = find(bytes, trak, b"tkhd")?;
    let track_id = match *bytes.get(tkhd.data).ok_or(BAD)? {
        0 => u32_at(bytes, tkhd.data + 12)?,
        1 => u32_at(bytes, tkhd.data + 20)?,
        _ => return Err(UNSUPPORTED),
    };
    let mdia = find(bytes, trak, b"mdia")?;
    let hdlr = find(bytes, mdia, b"hdlr")?;
    if bytes.get(hdlr.data + 8..hdlr.data + 12) != Some(b"soun") {
        return Err(UNSUPPORTED);
    }
    let mdhd = find(bytes, mdia, b"mdhd")?;
    let media_scale = u32_at(
        bytes,
        mdhd.data
            + if *bytes.get(mdhd.data).ok_or(BAD)? == 1 {
                20
            } else {
                12
            },
    )?;
    let mvhd = find(bytes, moov, b"mvhd")?;
    let movie_scale = u32_at(
        bytes,
        mvhd.data
            + if *bytes.get(mvhd.data).ok_or(BAD)? == 1 {
                20
            } else {
                12
            },
    )?;
    if movie_scale == 0 || media_scale == 0 {
        return Err(BAD);
    }
    let stbl = find(bytes, find(bytes, mdia, b"minf")?, b"stbl")?;
    let stsd = find(bytes, stbl, b"stsd")?;
    let entries = u32_at(bytes, stsd.data + 4)?;
    if entries == 0 || entries > 16 {
        return Err(UNSUPPORTED);
    }
    let descriptions = atoms(bytes, stsd.data + 8, stsd.end)?;
    if descriptions.len() != entries as usize
        || descriptions
            .iter()
            .any(|desc| !matches!(&desc.name, b"mp4a" | b"alac" | b"ac-3" | b"ec-3"))
    {
        // Do not mistake encrypted enca samples for already decrypted media.
        return Err(UNSUPPORTED);
    }
    let trex = find(bytes, find(bytes, moov, b"mvex")?, b"trex")?;
    if u32_at(bytes, trex.data + 4)? != track_id {
        return Err(UNSUPPORTED);
    }
    let default_desc = u32_at(bytes, trex.data + 8)?;
    if default_desc == 0 || default_desc > entries {
        return Err(UNSUPPORTED);
    }
    Ok(Info {
        track_id,
        media_scale,
        movie_scale,
        default_desc,
        default_duration: u32_at(bytes, trex.data + 12)?,
        default_size: u32_at(bytes, trex.data + 16)?,
        entries,
    })
}

fn parse_fragment(
    input: &[u8],
    moof: Atom,
    mdat: Atom,
    info: &Info,
    collected: &mut Vec<Sample>,
    first_decode: &mut Option<u64>,
) -> Result<()> {
    let trafs = children(input, moof)?
        .into_iter()
        .filter(|a| &a.name == b"traf")
        .collect::<Vec<_>>();
    if trafs.len() != 1 {
        return Err(UNSUPPORTED);
    }
    let traf = trafs[0];
    let tfhd = find(input, traf, b"tfhd")?;
    let flags = u32_at(input, tfhd.data)? & 0xffffff;
    if u32_at(input, tfhd.data + 4)? != info.track_id {
        return Err(UNSUPPORTED);
    }
    let mut pos = tfhd.data + 8;
    let base = if flags & 1 != 0 {
        let off = u64_at(input, pos)?;
        pos += 8;
        usize::try_from(off).map_err(|_| UNSUPPORTED)?
    } else {
        moof.start
    };
    let description = if flags & 2 != 0 {
        let n = u32_at(input, pos)?;
        pos += 4;
        n
    } else {
        info.default_desc
    };
    if description == 0 || description > info.entries {
        return Err(UNSUPPORTED);
    }
    let duration = if flags & 8 != 0 {
        let n = u32_at(input, pos)?;
        pos += 4;
        n
    } else {
        info.default_duration
    };
    let size = if flags & 16 != 0 {
        let n = u32_at(input, pos)?;
        pos += 4;
        n
    } else {
        info.default_size
    };
    if flags & 32 != 0 {
        pos += 4;
    }
    if pos > tfhd.end {
        return Err(BAD);
    }
    if let Ok(tfdt) = find(input, traf, b"tfdt") {
        let decode = if *input.get(tfdt.data).ok_or(BAD)? == 1 {
            u64_at(input, tfdt.data + 4)?
        } else {
            u32_at(input, tfdt.data + 4)? as u64
        };
        let previous = collected
            .iter()
            .try_fold(0u64, |n, s| n.checked_add(s.duration as u64))
            .ok_or(UNSUPPORTED)?;
        let start = *first_decode.get_or_insert(decode);
        if decode != start.checked_add(previous).ok_or(UNSUPPORTED)? {
            return Err(UNSUPPORTED);
        }
    }
    let runs = children(input, traf)?
        .into_iter()
        .filter(|a| &a.name == b"trun")
        .collect::<Vec<_>>();
    if runs.is_empty() {
        return Err(UNSUPPORTED);
    }
    let mut previous_end = None;
    for trun in runs {
        let run_flags = u32_at(input, trun.data)?;
        let flags = run_flags & 0xffffff;
        if run_flags >> 24 > 1 {
            return Err(UNSUPPORTED);
        }
        let count = u32_at(input, trun.data + 4)? as usize;
        if count == 0 || count > 1_000_000 {
            return Err(UNSUPPORTED);
        }
        let mut at = trun.data + 8;
        let offset = if flags & 1 != 0 {
            let value = u32_at(input, at)? as i32;
            at += 4;
            Some(value)
        } else {
            None
        };
        if flags & 4 != 0 {
            at += 4;
        }
        let run_start = if let Some(offset) = offset {
            base.checked_add_signed(offset as isize).ok_or(BAD)?
        } else {
            previous_end.unwrap_or(mdat.data)
        };
        let mut cursor = run_start;
        for _ in 0..count {
            let sample_duration = if flags & 0x100 != 0 {
                let v = u32_at(input, at)?;
                at += 4;
                v
            } else {
                duration
            };
            let sample_size = if flags & 0x200 != 0 {
                let v = u32_at(input, at)?;
                at += 4;
                v
            } else {
                size
            };
            if flags & 0x400 != 0 {
                at += 4;
            }
            if flags & 0x800 != 0 {
                let composition = u32_at(input, at)?;
                at += 4;
                if composition != 0 {
                    return Err(UNSUPPORTED);
                }
            }
            if sample_duration == 0 || sample_size == 0 || at > trun.end {
                return Err(UNSUPPORTED);
            }
            let end = cursor.checked_add(sample_size as usize).ok_or(BAD)?;
            if cursor < mdat.data || end > mdat.end {
                return Err(BAD);
            }
            collected.push(Sample {
                bytes: cursor..end,
                duration: sample_duration,
                description,
            });
            cursor = end;
        }
        if at != trun.end {
            return Err(UNSUPPORTED);
        }
        previous_end = Some(cursor);
    }
    Ok(())
}

fn table(name: &[u8; 4], payload: &[u8]) -> Result<Vec<u8>> {
    fullbox(name, 0, payload)
}
fn sample_tables(samples: &[Sample], offsets: &[u64]) -> Result<Vec<Vec<u8>>> {
    let mut stts = Vec::new();
    let mut groups = Vec::<(u32, u32)>::new();
    for s in samples {
        if let Some(last) = groups.last_mut() {
            if last.1 == s.duration && last.0 < u32::MAX {
                last.0 += 1;
                continue;
            }
        }
        groups.push((1, s.duration));
    }
    stts.extend((groups.len() as u32).to_be_bytes());
    for (count, duration) in groups {
        stts.extend(count.to_be_bytes());
        stts.extend(duration.to_be_bytes());
    }
    let mut stsc = Vec::new();
    let mut changes = Vec::new();
    for (i, s) in samples.iter().enumerate() {
        if i == 0 || samples[i - 1].description != s.description {
            changes.push((i + 1, s.description));
        }
    }
    stsc.extend((changes.len() as u32).to_be_bytes());
    for (idx, desc) in changes {
        stsc.extend(u32::try_from(idx).map_err(|_| UNSUPPORTED)?.to_be_bytes());
        stsc.extend(1u32.to_be_bytes());
        stsc.extend(desc.to_be_bytes());
    }
    let mut stsz = Vec::new();
    stsz.extend(0u32.to_be_bytes());
    stsz.extend(
        u32::try_from(samples.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for s in samples {
        stsz.extend(
            u32::try_from(s.bytes.len())
                .map_err(|_| UNSUPPORTED)?
                .to_be_bytes(),
        );
    }
    let mut co64 = Vec::new();
    co64.extend(
        u32::try_from(offsets.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for off in offsets {
        co64.extend(off.to_be_bytes());
    }
    Ok(vec![
        table(b"stts", &stts)?,
        table(b"stsc", &stsc)?,
        table(b"stsz", &stsz)?,
        table(b"co64", &co64)?,
    ])
}

fn movie_duration(info: &Info, samples: &[Sample]) -> Result<(u64, u64)> {
    let media = samples
        .iter()
        .try_fold(0u64, |n, s| n.checked_add(s.duration as u64))
        .ok_or(UNSUPPORTED)?;
    let movie = u64::try_from(
        u128::from(media) * u128::from(info.movie_scale) / u128::from(info.media_scale),
    )
    .map_err(|_| UNSUPPORTED)?;
    Ok((media, movie))
}

fn rebuild(
    input: &[u8],
    atom: Atom,
    info: &Info,
    samples: &[Sample],
    offsets: &[u64],
) -> Result<Vec<u8>> {
    let (media_duration, movie_duration) = movie_duration(info, samples)?;
    if &atom.name == b"mvhd" {
        return set_duration(input, atom, 16, movie_duration);
    }
    if &atom.name == b"tkhd" {
        return set_duration(input, atom, 20, movie_duration);
    }
    if &atom.name == b"mdhd" {
        return set_duration(input, atom, 16, media_duration);
    }
    if &atom.name == b"elst" {
        let mut body = input[atom.data..atom.end].to_vec();
        if u32_at(&body, 4)? != 1 {
            return Err(UNSUPPORTED);
        }
        let version = *body.first().ok_or(BAD)?;
        let (width, at) = if version == 0 {
            (4, 8)
        } else if version == 1 {
            (8, 8)
        } else {
            return Err(UNSUPPORTED);
        };
        let duration = movie_duration;
        if width == 4 {
            body.get_mut(at..at + 4).ok_or(BAD)?.copy_from_slice(
                &u32::try_from(duration)
                    .map_err(|_| UNSUPPORTED)?
                    .to_be_bytes(),
            );
        } else {
            body.get_mut(at..at + 8)
                .ok_or(BAD)?
                .copy_from_slice(&duration.to_be_bytes());
        }
        return boxed(b"elst", &body);
    }
    if &atom.name == b"stbl" {
        let mut body = Vec::new();
        let mut has_stsd = false;
        for child in children(input, atom)? {
            match &child.name {
                b"stsd" => {
                    has_stsd = true;
                    body.extend_from_slice(&input[child.start..child.end]);
                }
                b"stts" | b"stsc" | b"stsz" | b"stco" | b"co64" | b"ctts" | b"stss" | b"sgpd"
                | b"sbgp" => {}
                _ => body.extend_from_slice(&input[child.start..child.end]),
            }
        }
        if !has_stsd {
            return Err(UNSUPPORTED);
        }
        for entry in sample_tables(samples, offsets)? {
            body.extend(entry);
        }
        return boxed(b"stbl", &body);
    }
    if matches!(&atom.name, b"moov" | b"trak" | b"mdia" | b"minf" | b"edts") {
        let mut body = Vec::new();
        for child in children(input, atom)? {
            if &atom.name == b"moov" && &child.name == b"mvex" {
                continue;
            }
            body.extend(rebuild(input, child, info, samples, offsets)?);
        }
        return boxed(&atom.name, &body);
    }
    Ok(input[atom.start..atom.end].to_vec())
}

fn remux(input: &[u8]) -> Result<Vec<u8>> {
    let top = atoms(input, 0, input.len())?;
    let _ftyp = top
        .iter()
        .find(|a| &a.name == b"ftyp")
        .copied()
        .ok_or(UNSUPPORTED)?;
    let moov = top
        .iter()
        .find(|a| &a.name == b"moov")
        .copied()
        .ok_or(UNSUPPORTED)?;
    if top.iter().filter(|a| &a.name == b"moov").count() != 1 {
        return Err(UNSUPPORTED);
    }
    let info = info(input, moov)?;
    let mut samples = Vec::new();
    let mut pending = None;
    let mut first_decode = None;
    for atom in top {
        match &atom.name {
            b"moof" => {
                if pending.replace(atom).is_some() {
                    return Err(UNSUPPORTED);
                }
            }
            b"mdat" => {
                let moof = pending.take().ok_or(UNSUPPORTED)?;
                parse_fragment(input, moof, atom, &info, &mut samples, &mut first_decode)?;
            }
            _ => {}
        }
    }
    if pending.is_some() || samples.is_empty() || samples.len() > u32::MAX as usize {
        return Err(UNSUPPORTED);
    }
    // The source's `iso5`/`hlsf` brands identify fragmented HLS media,
    // not the finished, standalone audio-only file.
    let ftyp_bytes = file_type(b"M4A ")?;
    // co64 offsets are fixed width, so a dummy-offset pass determines moov size.
    let mut offsets = vec![0u64; samples.len()];
    let dummy = rebuild(input, moov, &info, &samples, &offsets)?;
    let mut cursor = u64::try_from(ftyp_bytes.len() + dummy.len() + 16).map_err(|_| UNSUPPORTED)?;
    for (slot, s) in offsets.iter_mut().zip(&samples) {
        *slot = cursor;
        cursor = cursor
            .checked_add(s.bytes.len() as u64)
            .ok_or(UNSUPPORTED)?;
    }
    let rebuilt = rebuild(input, moov, &info, &samples, &offsets)?;
    if rebuilt.len() != dummy.len() {
        return Err(UNSUPPORTED);
    }
    let mut out = Vec::with_capacity(usize::try_from(cursor).map_err(|_| UNSUPPORTED)?);
    out.extend_from_slice(&ftyp_bytes);
    out.extend(rebuilt);
    out.extend(1u32.to_be_bytes());
    out.extend(b"mdat");
    out.extend(
        (cursor - u64::try_from(ftyp_bytes.len() + dummy.len()).map_err(|_| UNSUPPORTED)?)
            .to_be_bytes(),
    );
    for s in &samples {
        out.extend_from_slice(&input[s.bytes.clone()]);
    }
    Ok(out)
}

#[pyfunction]
pub(crate) fn remux_mp4(py: Python<'_>, input: Vec<u8>) -> PyResult<Bound<'_, PyBytes>> {
    let output = py.detach(|| remux(&input)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}
