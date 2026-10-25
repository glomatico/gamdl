//! Losslessly mux independently decrypted video and audio fMP4 tracks.
//!
//! Rebuilds decode/composition timing, sync-frame and chunk tables. Each input
//! is parsed separately; the output has one seekable moov and a single mdat.
//! Clear caption tracks embedded in the video input are preserved. Reject
//! encrypted sample entries or unknown extra tracks instead of transcoding.

use std::ops::Range;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::remux::{
    Atom, atoms, boxed, children, file_type, find, fullbox, set_duration, u32_at, u64_at,
};

type Result<T> = std::result::Result<T, &'static str>;
const BAD: &str = "Malformed fragmented MP4 for audio/video mux";
const UNSUPPORTED: &str = "Unsupported audio/video fragmented MP4 mux layout";
const MOVIE_SCALE: u32 = 1000;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Video,
    Audio,
    Captions,
}

#[derive(Clone)]
struct Packet {
    bytes: Range<usize>,
    duration: u32,
    composition: i32,
    sync: bool,
    description: u32,
}

struct Track {
    src: usize,
    trak: Atom,
    old_id: u32,
    new_id: u32,
    kind: Kind,
    scale: u32,
    descriptions: u32,
    default_desc: u32,
    default_duration: u32,
    default_size: u32,
    default_flags: u32,
    first_decode: Option<u64>,
    next_decode: Option<u64>,
    packets: Vec<Packet>,
}

fn original_id(input: &[u8], trak: Atom) -> Result<u32> {
    let tkhd = find(input, trak, b"tkhd")?;
    let at = match input.get(tkhd.data) {
        Some(0) => tkhd.data + 12,
        Some(1) => tkhd.data + 20,
        _ => return Err(UNSUPPORTED),
    };
    u32_at(input, at)
}

fn scale(input: &[u8], mdhd: Atom) -> Result<u32> {
    let at = match input.get(mdhd.data) {
        Some(0) => mdhd.data + 12,
        Some(1) => mdhd.data + 20,
        _ => return Err(UNSUPPORTED),
    };
    let value = u32_at(input, at)?;
    if value == 0 { Err(BAD) } else { Ok(value) }
}

fn locate(input: &[u8]) -> Result<(Atom, Atom)> {
    let top = atoms(input, 0, input.len())?;
    let ftyp = *top.iter().find(|a| &a.name == b"ftyp").ok_or(UNSUPPORTED)?;
    let moov = *top.iter().find(|a| &a.name == b"moov").ok_or(UNSUPPORTED)?;
    if top.iter().filter(|a| &a.name == b"moov").count() != 1 {
        return Err(UNSUPPORTED);
    }
    Ok((ftyp, moov))
}

fn discover(input: &[u8], src: usize, moov: Atom, tracks: &mut Vec<Track>) -> Result<()> {
    let mvex = find(input, moov, b"mvex")?;
    for trak in children(input, moov)?
        .into_iter()
        .filter(|a| &a.name == b"trak")
    {
        let mdia = find(input, trak, b"mdia")?;
        let hdlr = find(input, mdia, b"hdlr")?;
        let kind = match input.get(hdlr.data + 8..hdlr.data + 12) {
            Some(b"vide") if src == 0 => Kind::Video,
            Some(b"soun") if src == 1 => Kind::Audio,
            Some(b"clcp" | b"text" | b"sbtl" | b"subt") if src == 0 => Kind::Captions,
            _ => return Err("Unexpected media track for audio/video mux"),
        };
        if tracks
            .iter()
            .any(|track| track.src == src && track.kind == kind && kind != Kind::Captions)
        {
            return Err(UNSUPPORTED);
        }
        let id = original_id(input, trak)?;
        if id == 0
            || tracks
                .iter()
                .any(|track| track.src == src && track.old_id == id)
        {
            return Err(BAD);
        }
        let stbl = find(input, find(input, mdia, b"minf")?, b"stbl")?;
        let stsd = find(input, stbl, b"stsd")?;
        let descriptions = u32_at(input, stsd.data + 4)?;
        if descriptions == 0 || descriptions > 16 {
            return Err(UNSUPPORTED);
        }
        let entries = atoms(input, stsd.data + 8, stsd.end)?;
        if entries.len() != descriptions as usize {
            return Err(BAD);
        }
        for entry in &entries {
            let valid = match kind {
                Kind::Video => matches!(
                    &entry.name,
                    b"avc1" | b"avc3" | b"hvc1" | b"hev1" | b"dvh1" | b"dvhe" | b"av01" | b"mp4v"
                ),
                Kind::Audio => matches!(&entry.name, b"mp4a" | b"alac" | b"ac-3" | b"ec-3"),
                Kind::Captions => matches!(
                    &entry.name,
                    b"c608" | b"c708" | b"wvtt" | b"stpp" | b"tx3g" | b"text"
                ),
            };
            if !valid {
                return Err("Encrypted or unsupported MP4 sample description for mux");
            }
        }
        let trex = children(input, mvex)?
            .into_iter()
            .find(|a| &a.name == b"trex" && u32_at(input, a.data + 4).ok() == Some(id))
            .ok_or(UNSUPPORTED)?;
        let default_desc = u32_at(input, trex.data + 8)?;
        if default_desc == 0 || default_desc > descriptions {
            return Err(UNSUPPORTED);
        }
        let mdhd = find(input, mdia, b"mdhd")?;
        tracks.push(Track {
            src,
            trak,
            old_id: id,
            new_id: tracks.len() as u32 + 1,
            kind,
            scale: scale(input, mdhd)?,
            descriptions,
            default_desc,
            default_duration: u32_at(input, trex.data + 12)?,
            default_size: u32_at(input, trex.data + 16)?,
            default_flags: u32_at(input, trex.data + 20)?,
            first_decode: None,
            next_decode: None,
            packets: Vec::new(),
        });
    }
    Ok(())
}

fn fragment(input: &[u8], moof: Atom, mdat: Atom, tracks: &mut [Track], src: usize) -> Result<()> {
    for traf in children(input, moof)?
        .into_iter()
        .filter(|a| &a.name == b"traf")
    {
        let tfhd = find(input, traf, b"tfhd")?;
        let tf_flags = u32_at(input, tfhd.data)? & 0xffffff;
        let id = u32_at(input, tfhd.data + 4)?;
        let track = tracks
            .iter_mut()
            .find(|t| t.src == src && t.old_id == id)
            .ok_or(UNSUPPORTED)?;
        let mut at = tfhd.data + 8;
        let base = if tf_flags & 1 != 0 {
            let v = usize::try_from(u64_at(input, at)?).map_err(|_| UNSUPPORTED)?;
            at += 8;
            v
        } else {
            moof.start
        };
        let desc = if tf_flags & 2 != 0 {
            let v = u32_at(input, at)?;
            at += 4;
            v
        } else {
            track.default_desc
        };
        if desc == 0 || desc > track.descriptions {
            return Err(UNSUPPORTED);
        }
        let duration = if tf_flags & 8 != 0 {
            let v = u32_at(input, at)?;
            at += 4;
            v
        } else {
            track.default_duration
        };
        let size = if tf_flags & 16 != 0 {
            let v = u32_at(input, at)?;
            at += 4;
            v
        } else {
            track.default_size
        };
        let sample_flags = if tf_flags & 32 != 0 {
            let v = u32_at(input, at)?;
            at += 4;
            v
        } else {
            track.default_flags
        };
        if at != tfhd.end {
            return Err(UNSUPPORTED);
        }
        let start = if let Ok(tfdt) = find(input, traf, b"tfdt") {
            match input.get(tfdt.data) {
                Some(0) => u32_at(input, tfdt.data + 4)? as u64,
                Some(1) => u64_at(input, tfdt.data + 4)?,
                _ => return Err(UNSUPPORTED),
            }
        } else {
            track.next_decode.unwrap_or(0)
        };
        if let Some(next) = track.next_decode {
            if start != next {
                // Caption streams are often sparse. Preserve their placement
                // by lengthening the previous packet through the clear gap.
                if track.kind != Kind::Captions || start < next {
                    return Err(UNSUPPORTED);
                }
                let gap = u32::try_from(start - next).map_err(|_| UNSUPPORTED)?;
                let prev = track.packets.last_mut().ok_or(UNSUPPORTED)?;
                prev.duration = prev.duration.checked_add(gap).ok_or(UNSUPPORTED)?;
            }
        } else {
            track.first_decode = Some(start);
        }
        let mut current_decode = start;
        let mut previous_end = None;
        let truns = children(input, traf)?
            .into_iter()
            .filter(|a| &a.name == b"trun")
            .collect::<Vec<_>>();
        if truns.is_empty() {
            return Err(UNSUPPORTED);
        }
        for trun in truns {
            let raw_flags = u32_at(input, trun.data)?;
            let tr_flags = raw_flags & 0xffffff;
            let version = (raw_flags >> 24) as u8;
            if version > 1 {
                return Err(UNSUPPORTED);
            }
            let count = u32_at(input, trun.data + 4)? as usize;
            if count == 0 || count > 1_000_000 {
                return Err(UNSUPPORTED);
            }
            let mut cursor = trun.data + 8;
            let offset = if tr_flags & 1 != 0 {
                let v = u32_at(input, cursor)? as i32;
                cursor += 4;
                Some(v)
            } else {
                None
            };
            let first_flags = if tr_flags & 4 != 0 {
                let v = u32_at(input, cursor)?;
                cursor += 4;
                Some(v)
            } else {
                None
            };
            let mut pos = if let Some(off) = offset {
                base.checked_add_signed(off as isize).ok_or(BAD)?
            } else {
                previous_end.unwrap_or(mdat.data)
            };
            for i in 0..count {
                let dur = if tr_flags & 0x100 != 0 {
                    let v = u32_at(input, cursor)?;
                    cursor += 4;
                    v
                } else {
                    duration
                };
                let len = if tr_flags & 0x200 != 0 {
                    let v = u32_at(input, cursor)?;
                    cursor += 4;
                    v
                } else {
                    size
                };
                let flags = if tr_flags & 0x400 != 0 {
                    let v = u32_at(input, cursor)?;
                    cursor += 4;
                    v
                } else if i == 0 {
                    first_flags.unwrap_or(sample_flags)
                } else {
                    sample_flags
                };
                let composition = if tr_flags & 0x800 != 0 {
                    let v = u32_at(input, cursor)?;
                    cursor += 4;
                    if version == 1 {
                        v as i32
                    } else {
                        i32::try_from(v).map_err(|_| UNSUPPORTED)?
                    }
                } else {
                    0
                };
                if dur == 0 || len == 0 || cursor > trun.end {
                    return Err(UNSUPPORTED);
                }
                let end = pos.checked_add(len as usize).ok_or(BAD)?;
                if pos < mdat.data || end > mdat.end {
                    return Err(BAD);
                }
                let sync = flags & 0x10000 == 0;
                track.packets.push(Packet {
                    bytes: pos..end,
                    duration: dur,
                    composition,
                    sync,
                    description: desc,
                });
                pos = end;
                current_decode = current_decode.checked_add(dur as u64).ok_or(UNSUPPORTED)?;
            }
            if cursor != trun.end {
                return Err(UNSUPPORTED);
            }
            previous_end = Some(pos);
        }
        track.next_decode = Some(current_decode);
    }
    Ok(())
}

fn parse_parts(input: &[u8], tracks: &mut [Track], src: usize) -> Result<()> {
    let mut pending = None;
    for atom in atoms(input, 0, input.len())? {
        match &atom.name {
            b"moof" => {
                if pending.replace(atom).is_some() {
                    return Err(UNSUPPORTED);
                }
            }
            b"mdat" => {
                let moof = pending.take().ok_or(UNSUPPORTED)?;
                fragment(input, moof, atom, tracks, src)?;
            }
            _ => {}
        }
    }
    if pending.is_some() {
        return Err(UNSUPPORTED);
    }
    Ok(())
}

fn table(name: &[u8; 4], data: &[u8]) -> Result<Vec<u8>> {
    fullbox(name, 0, data)
}
fn sample_tables(track: &Track, offsets: &[u64]) -> Result<Vec<Vec<u8>>> {
    if track.packets.len() != offsets.len() || track.packets.is_empty() {
        return Err(UNSUPPORTED);
    }
    let mut stts = Vec::new();
    let mut duration_groups: Vec<(u32, u32)> = Vec::new();
    let mut composition_groups: Vec<(u32, i32)> = Vec::new();
    let mut sync_numbers = Vec::new();
    for (i, s) in track.packets.iter().enumerate() {
        if let Some(last) = duration_groups
            .last_mut()
            .filter(|(count, v)| *v == s.duration && *count < u32::MAX)
        {
            last.0 += 1;
        } else {
            duration_groups.push((1, s.duration));
        }
        if let Some(last) = composition_groups
            .last_mut()
            .filter(|(count, v)| *v == s.composition && *count < u32::MAX)
        {
            last.0 += 1;
        } else {
            composition_groups.push((1, s.composition));
        }
        if s.sync {
            sync_numbers.push(u32::try_from(i + 1).map_err(|_| UNSUPPORTED)?);
        }
    }
    stts.extend(
        u32::try_from(duration_groups.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for (count, duration) in duration_groups {
        stts.extend(count.to_be_bytes());
        stts.extend(duration.to_be_bytes());
    }
    let mut result = vec![table(b"stts", &stts)?];
    if track.packets.iter().any(|s| s.composition != 0) {
        let mut data = Vec::new();
        data.extend(
            u32::try_from(composition_groups.len())
                .map_err(|_| UNSUPPORTED)?
                .to_be_bytes(),
        );
        for (n, v) in &composition_groups {
            data.extend(n.to_be_bytes());
            data.extend(v.to_be_bytes());
        }
        let version = if composition_groups.iter().any(|(_, v)| *v < 0) {
            1 << 24
        } else {
            0
        };
        result.push(fullbox(b"ctts", version, &data)?);
    }
    if track.kind == Kind::Video && sync_numbers.len() != track.packets.len() {
        if sync_numbers.is_empty() {
            return Err("Video track has no sync samples");
        }
        let mut data = Vec::new();
        data.extend(
            u32::try_from(sync_numbers.len())
                .map_err(|_| UNSUPPORTED)?
                .to_be_bytes(),
        );
        for n in sync_numbers {
            data.extend(n.to_be_bytes());
        }
        result.push(table(b"stss", &data)?);
    }
    let mut stsc = Vec::new();
    let mut changes = Vec::new();
    for (i, s) in track.packets.iter().enumerate() {
        if i == 0 || track.packets[i - 1].description != s.description {
            changes.push((i + 1, s.description));
        }
    }
    stsc.extend(
        u32::try_from(changes.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for (idx, desc) in changes {
        stsc.extend(u32::try_from(idx).map_err(|_| UNSUPPORTED)?.to_be_bytes());
        stsc.extend(1u32.to_be_bytes());
        stsc.extend(desc.to_be_bytes());
    }
    result.push(table(b"stsc", &stsc)?);
    let mut stsz = Vec::new();
    stsz.extend(0u32.to_be_bytes());
    stsz.extend(
        u32::try_from(track.packets.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for s in &track.packets {
        stsz.extend(
            u32::try_from(s.bytes.len())
                .map_err(|_| UNSUPPORTED)?
                .to_be_bytes(),
        );
    }
    result.push(table(b"stsz", &stsz)?);
    let mut co64 = Vec::new();
    co64.extend(
        u32::try_from(offsets.len())
            .map_err(|_| UNSUPPORTED)?
            .to_be_bytes(),
    );
    for offset in offsets {
        co64.extend(offset.to_be_bytes());
    }
    result.push(table(b"co64", &co64)?);
    Ok(result)
}

fn track_duration(track: &Track) -> Result<u64> {
    track.packets.iter().try_fold(0u64, |acc, s| {
        acc.checked_add(s.duration as u64).ok_or(UNSUPPORTED)
    })
}
fn to_movie(ticks: u64, scale: u32) -> Result<u64> {
    u64::try_from(
        (u128::from(ticks) * u128::from(MOVIE_SCALE) + u128::from(scale) - 1) / u128::from(scale),
    )
    .map_err(|_| UNSUPPORTED)
}
fn track_start(track: &Track) -> Result<u64> {
    to_movie(track.first_decode.ok_or(UNSUPPORTED)?, track.scale)
}

fn patch_id(mut atom_bytes: Vec<u8>, id: u32) -> Result<Vec<u8>> {
    let off = match atom_bytes.get(8) {
        Some(0) => 20,
        Some(1) => 28,
        _ => return Err(UNSUPPORTED),
    };
    atom_bytes
        .get_mut(off..off + 4)
        .ok_or(BAD)?
        .copy_from_slice(&id.to_be_bytes());
    Ok(atom_bytes)
}
fn edit_list(delay: u64, length: u64) -> Result<Vec<u8>> {
    if delay == 0 {
        return Ok(Vec::new());
    }
    let mut entries = Vec::new();
    entries.extend(2u32.to_be_bytes());
    for (duration, media) in [(delay, -1i64), (length, 0i64)] {
        entries.extend(duration.to_be_bytes());
        entries.extend(media.to_be_bytes());
        entries.extend(1u16.to_be_bytes());
        entries.extend(0u16.to_be_bytes());
    }
    boxed(b"edts", &fullbox(b"elst", 1 << 24, &entries)?)
}
fn rebuild_stbl(input: &[u8], atom: Atom, track: &Track, offsets: &[u64]) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    let mut has_stsd = false;
    for child in children(input, atom)? {
        match &child.name {
            b"stsd" => {
                has_stsd = true;
                body.extend_from_slice(&input[child.start..child.end]);
            }
            b"stts" | b"ctts" | b"stss" | b"stsc" | b"stsz" | b"stco" | b"co64" | b"sgpd"
            | b"sbgp" | b"senc" | b"saio" | b"saiz" => {}
            _ => body.extend_from_slice(&input[child.start..child.end]),
        }
    }
    if !has_stsd {
        return Err(UNSUPPORTED);
    }
    for table in sample_tables(track, offsets)? {
        body.extend(table);
    }
    boxed(b"stbl", &body)
}
fn rebuild_minf(input: &[u8], atom: Atom, track: &Track, offsets: &[u64]) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    for child in children(input, atom)? {
        if &child.name == b"stbl" {
            body.extend(rebuild_stbl(input, child, track, offsets)?);
        } else {
            body.extend_from_slice(&input[child.start..child.end]);
        }
    }
    boxed(b"minf", &body)
}
fn rebuild_mdia(input: &[u8], atom: Atom, track: &Track, offsets: &[u64]) -> Result<Vec<u8>> {
    let mut body = Vec::new();
    for child in children(input, atom)? {
        match &child.name {
            b"mdhd" => body.extend(set_duration(input, child, 16, track_duration(track)?)?),
            b"minf" => body.extend(rebuild_minf(input, child, track, offsets)?),
            _ => body.extend_from_slice(&input[child.start..child.end]),
        }
    }
    boxed(b"mdia", &body)
}
fn rebuild_trak(input: &[u8], track: &Track, offsets: &[u64], start_ms: u64) -> Result<Vec<u8>> {
    let movie_length = to_movie(track_duration(track)?, track.scale)?;
    let delay = track_start(track)?
        .checked_sub(start_ms)
        .ok_or(UNSUPPORTED)?;
    let mut body = Vec::new();
    let mut saw_tkhd = false;
    for child in children(input, track.trak)? {
        match &child.name {
            b"tkhd" => {
                saw_tkhd = true;
                body.extend(patch_id(
                    set_duration(
                        input,
                        child,
                        20,
                        movie_length.checked_add(delay).ok_or(UNSUPPORTED)?,
                    )?,
                    track.new_id,
                )?);
                // Edit lists belong immediately after tkhd. This also keeps
                // track timing portable beyond FFmpeg's tolerant MOV parser.
                body.extend(edit_list(delay, movie_length)?);
            }
            b"mdia" => body.extend(rebuild_mdia(input, child, track, offsets)?),
            b"edts" => return Err("Existing MP4 edit lists are not supported for mux"),
            _ => body.extend_from_slice(&input[child.start..child.end]),
        }
    }
    if !saw_tkhd {
        return Err(BAD);
    }
    boxed(b"trak", &body)
}
fn mux(video: &[u8], audio: &[u8], mp4_brand: bool) -> Result<Vec<u8>> {
    let (_, video_moov) = locate(video)?;
    let (_, audio_moov) = locate(audio)?;
    // M4V is the GAMDL default even for HEVC; mp42 is an explicit opt-in.
    let ftyp = file_type(if mp4_brand { b"mp42" } else { b"M4V " })?;
    let inputs = [video, audio];
    let mut tracks = Vec::new();
    discover(video, 0, video_moov, &mut tracks)?;
    discover(audio, 1, audio_moov, &mut tracks)?;
    if tracks.iter().filter(|t| t.kind == Kind::Video).count() != 1
        || tracks.iter().filter(|t| t.kind == Kind::Audio).count() != 1
    {
        return Err(UNSUPPORTED);
    }
    parse_parts(video, &mut tracks, 0)?;
    parse_parts(audio, &mut tracks, 1)?;
    if tracks
        .iter()
        .any(|t| t.packets.is_empty() || t.packets.len() > u32::MAX as usize)
    {
        return Err(UNSUPPORTED);
    }
    let start_ms = tracks
        .iter()
        .map(track_start)
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .min()
        .ok_or(UNSUPPORTED)?;
    let mut offsets = tracks
        .iter()
        .map(|t| vec![0u64; t.packets.len()])
        .collect::<Vec<_>>();
    // Build with fixed-width co64 tables: dummy offsets give the exact moov size.
    // Track data is laid out contiguously: video, optional captions, audio.
    let initial = movie_with_inputs(inputs, video_moov, &tracks, &offsets, start_ms)?;
    let mut at = u64::try_from(ftyp.len() + initial.len() + 16).map_err(|_| UNSUPPORTED)?;
    for (track, positions) in tracks.iter().zip(&mut offsets) {
        for (packet, offset) in track.packets.iter().zip(positions) {
            *offset = at;
            at = at
                .checked_add(packet.bytes.len() as u64)
                .ok_or(UNSUPPORTED)?;
        }
    }
    let moov = movie_with_inputs(inputs, video_moov, &tracks, &offsets, start_ms)?;
    if initial.len() != moov.len() {
        return Err(UNSUPPORTED);
    }
    let mdat_length = at
        .checked_sub((ftyp.len() + moov.len()) as u64)
        .ok_or(BAD)?;
    let mut out = Vec::with_capacity(usize::try_from(at).map_err(|_| UNSUPPORTED)?);
    out.extend_from_slice(&ftyp);
    out.extend(moov);
    out.extend(1u32.to_be_bytes());
    out.extend(b"mdat");
    out.extend(mdat_length.to_be_bytes());
    for track in &tracks {
        for packet in &track.packets {
            out.extend_from_slice(&inputs[track.src][packet.bytes.clone()]);
        }
    }
    Ok(out)
}
fn movie_with_inputs(
    inputs: [&[u8]; 2],
    moov: Atom,
    tracks: &[Track],
    offsets: &[Vec<u64>],
    start_ms: u64,
) -> Result<Vec<u8>> {
    let video = inputs[0];
    let duration = tracks
        .iter()
        .map(|track| {
            let delay = track_start(track)?
                .checked_sub(start_ms)
                .ok_or(UNSUPPORTED)?;
            delay
                .checked_add(to_movie(track_duration(track)?, track.scale)?)
                .ok_or(UNSUPPORTED)
        })
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .max()
        .ok_or(UNSUPPORTED)?;
    let mut body = Vec::new();
    let mut saw_mvhd = false;
    for child in children(video, moov)? {
        match &child.name {
            b"mvhd" => {
                saw_mvhd = true;
                let mut b = set_duration(video, child, 16, duration)?;
                let at = match b.get(8) {
                    Some(0) => 20,
                    Some(1) => 28,
                    _ => return Err(UNSUPPORTED),
                };
                b.get_mut(at..at + 4)
                    .ok_or(BAD)?
                    .copy_from_slice(&MOVIE_SCALE.to_be_bytes());
                let len = b.len();
                b.get_mut(len - 4..len)
                    .ok_or(BAD)?
                    .copy_from_slice(&(tracks.len() as u32 + 1).to_be_bytes());
                body.extend(b);
            }
            b"trak" | b"mvex" | b"pssh" => {}
            _ => body.extend_from_slice(&video[child.start..child.end]),
        }
    }
    if !saw_mvhd {
        return Err(BAD);
    }
    for (track, offs) in tracks.iter().zip(offsets) {
        body.extend(rebuild_trak(inputs[track.src], track, offs, start_ms)?);
    }
    boxed(b"moov", &body)
}

#[pyfunction(signature = (video, audio, mp4_brand=false))]
pub(crate) fn mux_av_mp4<'py>(
    py: Python<'py>,
    video: Vec<u8>,
    audio: Vec<u8>,
    mp4_brand: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    let output = py
        .detach(|| mux(&video, &audio, mp4_brand))
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}
