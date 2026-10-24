//! Strict AES-CTR CENC decryption of complete fragmented-MP4 HLS parts.
//! Never outputs partially decrypted media; unknown schemes fail closed.
//! Only a single protected audio or video track is supported initially.

use aes::Aes128;
use ctr::Ctr128BE;
use ctr::cipher::{KeyIvInit, StreamCipher};

type Result<T> = std::result::Result<T, &'static str>;
const MALFORMED: &str = "Malformed fragmented MP4 encryption metadata";
const UNSUPPORTED: &str = "Unsupported fragmented MP4 encryption layout";

#[derive(Clone, Copy)]
struct BoxPos {
    start: usize,
    data: usize,
    end: usize,
    kind: [u8; 4],
}

fn u16_at(buf: &[u8], at: usize) -> Result<u16> {
    Ok(u16::from_be_bytes(
        buf.get(at..at + 2)
            .ok_or(MALFORMED)?
            .try_into()
            .map_err(|_| MALFORMED)?,
    ))
}
fn u32_at(buf: &[u8], at: usize) -> Result<u32> {
    Ok(u32::from_be_bytes(
        buf.get(at..at + 4)
            .ok_or(MALFORMED)?
            .try_into()
            .map_err(|_| MALFORMED)?,
    ))
}
fn u64_at(buf: &[u8], at: usize) -> Result<u64> {
    Ok(u64::from_be_bytes(
        buf.get(at..at + 8)
            .ok_or(MALFORMED)?
            .try_into()
            .map_err(|_| MALFORMED)?,
    ))
}
fn boxes(buf: &[u8], mut at: usize, end: usize) -> Result<Vec<BoxPos>> {
    if end > buf.len() || at > end {
        return Err(MALFORMED);
    }
    let mut out = Vec::new();
    while at < end {
        if at.checked_add(8).ok_or(MALFORMED)? > end {
            return Err(MALFORMED);
        }
        let small = u32_at(buf, at)? as usize;
        let kind: [u8; 4] = buf[at + 4..at + 8].try_into().map_err(|_| MALFORMED)?;
        let (header, size) = if small == 1 {
            (
                16usize,
                usize::try_from(u64_at(buf, at + 8)?).map_err(|_| MALFORMED)?,
            )
        } else if small == 0 {
            (8, end - at)
        } else {
            (8, small)
        };
        if size < header {
            return Err(MALFORMED);
        }
        let next = at.checked_add(size).ok_or(MALFORMED)?;
        if next > end {
            return Err(MALFORMED);
        }
        out.push(BoxPos {
            start: at,
            data: at + header,
            end: next,
            kind,
        });
        at = next;
    }
    Ok(out)
}
fn child(buf: &[u8], parent: BoxPos, name: &[u8; 4], skip: usize) -> Result<BoxPos> {
    boxes(buf, parent.data + skip, parent.end)?
        .into_iter()
        .find(|a| &a.kind == name)
        .ok_or(UNSUPPORTED)
}

struct EncryptionConfig {
    track_id: u32,
    iv_size: usize,
    default_size: usize,
}

pub(crate) struct CencDecryptor {
    key: [u8; 16],
    expected_kid: Option<[u8; 16]>,
    config: Option<EncryptionConfig>,
    input_offset: u64,
}
impl CencDecryptor {
    pub(crate) fn new(
        key: Option<Vec<u8>>,
        kid: Option<Vec<u8>>,
    ) -> std::result::Result<Option<Self>, &'static str> {
        match (key, kid) {
            (None, None) => Ok(None),
            (Some(key), kid) => {
                let key: [u8; 16] = key
                    .try_into()
                    .map_err(|_| "CENC key must contain exactly 16 bytes")?;
                let expected_kid = kid
                    .map(|kid| {
                        kid.try_into()
                            .map_err(|_| "CENC key ID must contain exactly 16 bytes")
                    })
                    .transpose()?;
                Ok(Some(Self {
                    key,
                    expected_kid,
                    config: None,
                    input_offset: 0,
                }))
            }
            (None, Some(_)) => Err("A CENC key ID requires a decryption key"),
        }
    }
    fn parse_init(&mut self, buf: &mut [u8], moov: BoxPos) -> Result<()> {
        if self.config.is_some() {
            return Err(UNSUPPORTED);
        }
        let mut selected = None;
        for trak in boxes(buf, moov.data, moov.end)?
            .into_iter()
            .filter(|b| &b.kind == b"trak")
        {
            let mdia = child(buf, trak, b"mdia", 0)?;
            let hdlr = child(buf, mdia, b"hdlr", 0)?;
            let handler = buf.get(hdlr.data + 8..hdlr.data + 12);
            if handler != Some(b"soun") && handler != Some(b"vide") {
                continue;
            }
            let tkhd = child(buf, trak, b"tkhd", 0)?;
            let track_id = match *buf.get(tkhd.data).ok_or(MALFORMED)? {
                0 => u32_at(buf, tkhd.data + 12)?,
                1 => u32_at(buf, tkhd.data + 20)?,
                _ => return Err(UNSUPPORTED),
            };
            let minf = child(buf, mdia, b"minf", 0)?;
            let stbl = child(buf, minf, b"stbl", 0)?;
            let stsd = child(buf, stbl, b"stsd", 0)?;
            if u32_at(buf, stsd.data + 4)? != 1 {
                return Err(UNSUPPORTED);
            }
            let entry = boxes(buf, stsd.data + 8, stsd.end)?
                .into_iter()
                .next()
                .ok_or(MALFORMED)?;
            let video = handler == Some(b"vide");
            if (video && &entry.kind != b"encv") || (!video && &entry.kind != b"enca") {
                return Err(UNSUPPORTED);
            }
            // Audio entry header: 36 bytes; visual entry header: 86 bytes.
            // The box data starts after the first 8 bytes of either header.
            let sinf = child(buf, entry, b"sinf", if video { 78 } else { 28 })?;
            let schm = child(buf, sinf, b"schm", 0)?;
            if buf.get(schm.data + 4..schm.data + 8) != Some(b"cenc") {
                return Err("Only AES-CTR/CENC decryption is supported");
            }
            let frma = child(buf, sinf, b"frma", 0)?;
            let format: [u8; 4] = buf
                .get(frma.data..frma.data + 4)
                .ok_or(MALFORMED)?
                .try_into()
                .map_err(|_| MALFORMED)?;
            let supported = if video {
                matches!(
                    &format,
                    b"avc1" | b"avc3" | b"hvc1" | b"hev1" | b"av01" | b"mp4v"
                )
            } else {
                matches!(&format, b"mp4a" | b"alac" | b"ac-3" | b"ec-3")
            };
            if !supported {
                return Err(UNSUPPORTED);
            }
            let schi = child(buf, sinf, b"schi", 0)?;
            let tenc = child(buf, schi, b"tenc", 0)?;
            if buf.get(tenc.data) != Some(&0) {
                return Err(UNSUPPORTED);
            }
            if buf.get(tenc.data + 6) != Some(&1) {
                return Err(UNSUPPORTED);
            }
            let iv_size = *buf.get(tenc.data + 7).ok_or(MALFORMED)? as usize;
            if iv_size != 8 && iv_size != 16 {
                return Err(UNSUPPORTED);
            }
            let kid: [u8; 16] = buf
                .get(tenc.data + 8..tenc.data + 24)
                .ok_or(MALFORMED)?
                .try_into()
                .map_err(|_| MALFORMED)?;
            if self.expected_kid.is_some_and(|expected| expected != kid) {
                return Err("Decryption key ID does not match the encrypted media");
            }
            let mut default_size = 0;
            if let Ok(mvex) = child(buf, moov, b"mvex", 0) {
                for trex in boxes(buf, mvex.data, mvex.end)?
                    .into_iter()
                    .filter(|b| &b.kind == b"trex")
                {
                    if u32_at(buf, trex.data + 4)? == track_id {
                        default_size = u32_at(buf, trex.data + 16)? as usize;
                    }
                }
            }
            // No box sizes or fragment offsets change when hiding encryption metadata.
            buf[entry.start + 4..entry.start + 8].copy_from_slice(&format);
            buf[sinf.start + 4..sinf.start + 8].copy_from_slice(b"free");
            if selected.is_some() {
                return Err(UNSUPPORTED);
            }
            selected = Some(EncryptionConfig {
                track_id,
                iv_size,
                default_size,
            });
        }
        self.config = Some(selected.ok_or("Expected an encrypted audio or video MP4 track")?);
        Ok(())
    }
    fn decrypt_fragment(&self, buf: &mut [u8], moof: BoxPos, mdat: BoxPos) -> Result<()> {
        let cfg = self
            .config
            .as_ref()
            .ok_or("Encrypted MP4 requires an initialization section")?;
        let mut handled = false;
        for traf in boxes(buf, moof.data, moof.end)?
            .into_iter()
            .filter(|b| &b.kind == b"traf")
        {
            let tfhd = child(buf, traf, b"tfhd", 0)?;
            let flags = u32_at(buf, tfhd.data)? & 0x00ff_ffff;
            let track = u32_at(buf, tfhd.data + 4)?;
            if track != cfg.track_id {
                return Err(UNSUPPORTED);
            }
            if handled {
                return Err(UNSUPPORTED);
            }
            handled = true;
            let mut pos = tfhd.data + 8;
            let base = if flags & 1 != 0 {
                let absolute = u64_at(buf, pos)?;
                pos += 8;
                usize::try_from(absolute.checked_sub(self.input_offset).ok_or(UNSUPPORTED)?)
                    .map_err(|_| UNSUPPORTED)?
            } else {
                moof.start
            };
            if flags & 0x02 != 0 {
                if u32_at(buf, pos)? != 1 {
                    return Err(UNSUPPORTED);
                }
                pos += 4;
            }
            if flags & 0x08 != 0 {
                pos += 4;
            }
            let sample_default = if flags & 0x10 != 0 {
                u32_at(buf, pos)? as usize
            } else {
                cfg.default_size
            };
            let mut samples = Vec::new();
            let mut seen_trun = false;
            let mut offset = None;
            let mut senc = None;
            for atom in boxes(buf, traf.data, traf.end)? {
                match &atom.kind {
                    b"trun" => {
                        if seen_trun {
                            return Err(UNSUPPORTED);
                        }
                        seen_trun = true;
                        let flags = u32_at(buf, atom.data)? & 0x00ff_ffff;
                        let count = u32_at(buf, atom.data + 4)? as usize;
                        if count > 100_000 {
                            return Err(UNSUPPORTED);
                        }
                        let mut at = atom.data + 8;
                        if flags & 1 != 0 {
                            let relative = u32_at(buf, at)? as i32 as i64;
                            let abs = (base as i64).checked_add(relative).ok_or(MALFORMED)?;
                            if abs < 0 {
                                return Err(MALFORMED);
                            }
                            offset = Some(abs as usize);
                            at += 4;
                        }
                        if flags & 0x04 != 0 {
                            at += 4;
                        }
                        for _ in 0..count {
                            if flags & 0x100 != 0 {
                                at += 4;
                            }
                            let size = if flags & 0x200 != 0 {
                                let n = u32_at(buf, at)? as usize;
                                at += 4;
                                n
                            } else {
                                sample_default
                            };
                            if flags & 0x400 != 0 {
                                at += 4;
                            }
                            if flags & 0x800 != 0 {
                                at += 4;
                            }
                            if size == 0 || at > atom.end {
                                return Err(MALFORMED);
                            }
                            samples.push(size);
                        }
                        if at > atom.end {
                            return Err(MALFORMED);
                        }
                    }
                    b"senc" => senc = Some(atom),
                    _ => {}
                }
            }
            if !seen_trun {
                return Err(UNSUPPORTED);
            }
            let senc = senc.ok_or("CENC fragment has no sample encryption metadata")?;
            let flags = u32_at(buf, senc.data)? & 0x00ff_ffff;
            if flags & !2 != 0 {
                return Err(UNSUPPORTED);
            }
            let count = u32_at(buf, senc.data + 4)? as usize;
            if count != samples.len() {
                return Err(MALFORMED);
            }
            let mut senpos = senc.data + 8;
            let mut samplepos = offset.unwrap_or(mdat.data);
            for size in samples {
                let iv = buf.get(senpos..senpos + cfg.iv_size).ok_or(MALFORMED)?;
                let mut full_iv = [0u8; 16];
                full_iv[..cfg.iv_size].copy_from_slice(iv);
                senpos += cfg.iv_size;
                let mut subsamples = Vec::new();
                if flags & 2 != 0 {
                    let sub_count = u16_at(buf, senpos)? as usize;
                    senpos += 2;
                    if sub_count > 65535 {
                        return Err(MALFORMED);
                    }
                    for _ in 0..sub_count {
                        let clear = u16_at(buf, senpos)? as usize;
                        let encrypted = u32_at(buf, senpos + 2)? as usize;
                        senpos += 6;
                        subsamples.push((clear, encrypted));
                    }
                }
                let end = samplepos.checked_add(size).ok_or(MALFORMED)?;
                if samplepos < mdat.data || end > mdat.end {
                    return Err(MALFORMED);
                }
                let sample = &mut buf[samplepos..end];
                let mut cipher = Ctr128BE::<Aes128>::new((&self.key).into(), (&full_iv).into());
                if subsamples.is_empty() {
                    cipher.apply_keystream(sample);
                } else {
                    let mut cursor = 0usize;
                    for (clear, encrypted) in subsamples {
                        cursor = cursor.checked_add(clear).ok_or(MALFORMED)?;
                        let next = cursor.checked_add(encrypted).ok_or(MALFORMED)?;
                        if next > sample.len() {
                            return Err(MALFORMED);
                        }
                        cipher.apply_keystream(&mut sample[cursor..next]);
                        cursor = next;
                    }
                    if cursor > sample.len() {
                        return Err(MALFORMED);
                    }
                }
                samplepos = end;
            }
            if senpos != senc.end {
                return Err(MALFORMED);
            }
            for atom in boxes(buf, traf.data, traf.end)? {
                if matches!(&atom.kind, b"senc" | b"saiz" | b"saio") {
                    buf[atom.start + 4..atom.start + 8].copy_from_slice(b"free");
                }
            }
        }
        if !handled {
            return Err(UNSUPPORTED);
        }
        Ok(())
    }
    pub(crate) fn decrypt_part(&mut self, bytes: &mut [u8]) -> Result<()> {
        let top = boxes(bytes, 0, bytes.len())?;
        let mut pending = None;
        let mut fragments = 0;
        let mut saw_init = false;
        for atom in top {
            match &atom.kind {
                b"moov" => {
                    self.parse_init(bytes, atom)?;
                    saw_init = true;
                }
                b"moof" => {
                    if pending.replace(atom).is_some() {
                        return Err(UNSUPPORTED);
                    }
                }
                b"mdat" => {
                    let moof = pending.take().ok_or(UNSUPPORTED)?;
                    self.decrypt_fragment(bytes, moof, atom)?;
                    fragments += 1;
                }
                _ => {}
            }
        }
        if pending.is_some() {
            return Err(UNSUPPORTED);
        }
        if self.config.is_none() {
            return Err("Encrypted MP4 requires an initialization section");
        }
        if fragments == 0 && !saw_init {
            return Err(UNSUPPORTED);
        }
        self.input_offset = self
            .input_offset
            .checked_add(bytes.len() as u64)
            .ok_or(MALFORMED)?;
        Ok(())
    }
}
