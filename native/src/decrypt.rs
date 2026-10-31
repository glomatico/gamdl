//! Strict CENC (AES-CTR) and CBCS (AES-CBC) decryption of fragmented MP4.
//! Never outputs partially decrypted media; unknown schemes fail closed.
//! Only a single protected audio or video track is supported initially.

use crate::wrapper::{BATCH_SIZE, WrapperSession};
use aes::Aes128;
use aes::cipher::{BlockDecrypt, KeyInit, generic_array::GenericArray};
use ctr::Ctr128BE;
use ctr::cipher::{KeyIvInit, StreamCipher};

type Result<T> = std::result::Result<T, &'static str>;
const MALFORMED: &str = "Malformed fragmented MP4 encryption metadata";
const UNSUPPORTED: &str = "Unsupported fragmented MP4 encryption layout";
// Legacy Gamdl 3.x fixed prefetch key used ONLY by enhanced-HLS CBCS songs.
const ENHANCED_HLS_PREFETCH_KEY: [u8; 16] = [
    0x32, 0xb8, 0xad, 0xe1, 0x76, 0x9e, 0x26, 0xb1, 0xff, 0xb8, 0x98, 0x63, 0x52, 0x79, 0x3f, 0xc6,
];

#[derive(Clone, Copy)]
struct BoxPos {
    start: usize,
    data: usize,
    end: usize,
    kind: [u8; 4],
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: [u8; 16] = [
        0x2b, 0x7e, 0x15, 0x16, 0x28, 0xae, 0xd2, 0xa6, 0xab, 0xf7, 0x15, 0x88, 0x09, 0xcf, 0x4f,
        0x3c,
    ];
    const IV: [u8; 16] = [
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e,
        0x0f,
    ];
    const CT1: [u8; 16] = [
        0x76, 0x49, 0xab, 0xac, 0x81, 0x19, 0xb2, 0x46, 0xce, 0xe9, 0x8e, 0x9b, 0x12, 0xe9, 0x19,
        0x7d,
    ];
    const CT2: [u8; 16] = [
        0x50, 0x86, 0xcb, 0x9b, 0x50, 0x72, 0x19, 0xee, 0x95, 0xdb, 0x11, 0x3a, 0x91, 0x76, 0x78,
        0xb2,
    ];
    const PT1: [u8; 16] = [
        0x6b, 0xc1, 0xbe, 0xe2, 0x2e, 0x40, 0x9f, 0x96, 0xe9, 0x3d, 0x7e, 0x11, 0x73, 0x93, 0x17,
        0x2a,
    ];
    const PT2: [u8; 16] = [
        0xae, 0x2d, 0x8a, 0x57, 0x1e, 0x03, 0xac, 0x9c, 0x9e, 0xb7, 0x6f, 0xac, 0x45, 0xaf, 0x8e,
        0x51,
    ];

    #[test]
    fn cbcs_unpatterned_matches_nist_sp800_38a_cbc_vector() {
        let cipher = Aes128::new((&KEY).into());
        let mut chain = IV;
        let mut data = [CT1.as_slice(), CT2.as_slice(), b"tail".as_slice()].concat();
        decrypt_cbcs_range(&mut data, &cipher, &mut chain, 0, 0);
        assert_eq!(
            data,
            [PT1.as_slice(), PT2.as_slice(), b"tail".as_slice()].concat()
        );
        assert_eq!(chain, CT2);
    }

    #[test]
    fn cbcs_pattern_skips_clear_blocks_without_advancing_chain() {
        let cipher = Aes128::new((&KEY).into());
        let mut chain = IV;
        let skipped = b"clear skip block";
        let mut data = [CT1.as_slice(), skipped, CT2.as_slice(), b"last".as_slice()].concat();
        decrypt_cbcs_range(&mut data, &cipher, &mut chain, 1, 1);
        assert_eq!(
            data,
            [PT1.as_slice(), skipped, PT2.as_slice(), b"last".as_slice()].concat()
        );
        assert_eq!(chain, CT2);
    }

    #[test]
    fn cbcs_cipher_chain_continues_across_subsamples() {
        let cipher = Aes128::new((&KEY).into());
        let mut chain = IV;
        let mut one = CT1;
        let mut two = CT2;
        decrypt_cbcs_range(&mut one, &cipher, &mut chain, 0, 0);
        decrypt_cbcs_range(&mut two, &cipher, &mut chain, 0, 0);
        assert_eq!(one, PT1);
        assert_eq!(two, PT2);
    }
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
    auxiliary_track_ids: Vec<u32>,
    default_description: usize,
    default_size: usize,
    descriptions: Vec<SampleEncryption>,
    enhanced_song_prefetch: bool,
}

struct SampleEncryption {
    scheme: EncryptionScheme,
    iv_size: usize,
    constant_iv: Option<[u8; 16]>,
    crypt_blocks: u8,
    skip_blocks: u8,
}

struct WrapperPending {
    start: usize,
    ranges: Vec<(usize, usize)>,
    aligned: Vec<u8>,
    tail: Vec<u8>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum EncryptionScheme {
    Cenc,
    Cbcs,
}

/// CBCS crypt/skip patterns restart at the beginning of each encrypted
/// subsample, with a fresh CBC IV per protected range supplied by the caller.
/// Within that range, the chain advances only on encrypted blocks.
/// Partial final blocks are always plaintext. No padding is added or removed.
fn decrypt_cbcs_range(
    data: &mut [u8],
    cipher: &Aes128,
    chain: &mut [u8; 16],
    crypt_blocks: u8,
    skip_blocks: u8,
) {
    let mut block_index = 0usize;
    let period = crypt_blocks as usize + skip_blocks as usize;
    for block in data.chunks_exact_mut(16) {
        let is_encrypted = period == 0 || block_index % period < crypt_blocks as usize;
        if is_encrypted {
            let mut ciphertext = [0u8; 16];
            ciphertext.copy_from_slice(block);
            let mut work = GenericArray::clone_from_slice(block);
            cipher.decrypt_block(&mut work);
            for (index, plain) in block.iter_mut().enumerate() {
                *plain = work[index] ^ chain[index];
            }
            *chain = ciphertext;
        }
        block_index += 1;
    }
}

pub(crate) struct Mp4Decryptor {
    key: [u8; 16],
    expected_kid: Option<[u8; 16]>,
    enhanced_hls_song: bool,
    asset_song_cbcs: bool,
    music_video_cbcs: bool,
    wrapper_mode: bool,
    config: Option<EncryptionConfig>,
    input_offset: u64,
}
impl Mp4Decryptor {
    pub(crate) fn new(
        key: Option<Vec<u8>>,
        kid: Option<Vec<u8>>,
        enhanced_hls_song: bool,
        wrapper_mode: bool,
        asset_song_cbcs: bool,
        music_video_cbcs: bool,
    ) -> std::result::Result<Option<Self>, &'static str> {
        if wrapper_mode && key.is_none() && kid.is_none() {
            return Ok(Some(Self {
                key: [0u8; 16],
                expected_kid: None,
                enhanced_hls_song,
                asset_song_cbcs,
                music_video_cbcs,
                wrapper_mode,
                config: None,
                input_offset: 0,
            }));
        }
        match (key, kid) {
            (None, None) => Ok(None),
            (Some(key), kid) => {
                let key: [u8; 16] = key
                    .try_into()
                    .map_err(|_| "MP4 AES key must contain exactly 16 bytes")?;
                let expected_kid = kid
                    .map(|kid| {
                        kid.try_into()
                            .map_err(|_| "MP4 key ID must contain exactly 16 bytes")
                    })
                    .transpose()?;
                Ok(Some(Self {
                    key,
                    expected_kid,
                    enhanced_hls_song,
                    asset_song_cbcs,
                    music_video_cbcs,
                    wrapper_mode,
                    config: None,
                    input_offset: 0,
                }))
            }
            (None, Some(_)) => Err("An MP4 key ID requires a decryption key"),
        }
    }
    fn parse_init(&mut self, buf: &mut [u8], moov: BoxPos) -> Result<()> {
        if self.config.is_some() {
            return Err(UNSUPPORTED);
        }
        let mut selected = None;
        let mut auxiliary_track_ids = Vec::new();
        for trak in boxes(buf, moov.data, moov.end)?
            .into_iter()
            .filter(|b| &b.kind == b"trak")
        {
            let mdia = child(buf, trak, b"mdia", 0)?;
            let hdlr = child(buf, mdia, b"hdlr", 0)?;
            let handler = buf.get(hdlr.data + 8..hdlr.data + 12);
            let caption = matches!(handler, Some(b"clcp" | b"text" | b"sbtl" | b"subt"));
            if !caption && handler != Some(b"soun") && handler != Some(b"vide") {
                continue;
            }
            let tkhd = child(buf, trak, b"tkhd", 0)?;
            let track_id = match *buf.get(tkhd.data).ok_or(MALFORMED)? {
                0 => u32_at(buf, tkhd.data + 12)?,
                1 => u32_at(buf, tkhd.data + 20)?,
                _ => return Err(UNSUPPORTED),
            };
            if caption {
                if track_id == 0 || auxiliary_track_ids.contains(&track_id) {
                    return Err(MALFORMED);
                }
                auxiliary_track_ids.push(track_id);
                continue;
            }
            let minf = child(buf, mdia, b"minf", 0)?;
            let stbl = child(buf, minf, b"stbl", 0)?;
            let stsd = child(buf, stbl, b"stsd", 0)?;
            let count = u32_at(buf, stsd.data + 4)? as usize;
            if count == 0 || count > 16 {
                return Err(UNSUPPORTED);
            }
            let entries = boxes(buf, stsd.data + 8, stsd.end)?;
            if entries.len() != count {
                return Err(MALFORMED);
            }
            let video = handler == Some(b"vide");
            let mut descriptions = Vec::with_capacity(count);
            let mut first_kid = None;
            for entry in entries {
                if (video && &entry.kind != b"encv") || (!video && &entry.kind != b"enca") {
                    return Err(UNSUPPORTED);
                }
                // Audio entry header: 36 bytes; visual entry header: 86 bytes.
                // The box data starts after the first 8 bytes of either header.
                let sinf = child(buf, entry, b"sinf", if video { 78 } else { 28 })?;
                let schm = child(buf, sinf, b"schm", 0)?;
                let scheme = match buf.get(schm.data + 4..schm.data + 8) {
                    Some(b"cenc") => EncryptionScheme::Cenc,
                    Some(b"cbcs") => EncryptionScheme::Cbcs,
                    _ => return Err("Only CENC/AES-CTR and CBCS/AES-CBC are supported"),
                };
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
                let version = *buf.get(tenc.data).ok_or(MALFORMED)?;
                if version > 1 {
                    return Err(UNSUPPORTED);
                }
                let pattern = if version == 1 {
                    // FullBox: version (1), flags (3), reserved (1),
                    // default_crypt_byte_block/default_skip_byte_block (1).
                    // The reserved byte is at +4; the pattern is at +5.
                    *buf.get(tenc.data + 5).ok_or(MALFORMED)?
                } else {
                    0
                };
                let crypt_blocks = pattern >> 4;
                let skip_blocks = pattern & 0x0f;
                if scheme == EncryptionScheme::Cenc && pattern != 0 {
                    return Err(UNSUPPORTED);
                }
                if scheme == EncryptionScheme::Cbcs && crypt_blocks == 0 && skip_blocks != 0 {
                    return Err(UNSUPPORTED);
                }
                if buf.get(tenc.data + 6) != Some(&1) {
                    return Err(UNSUPPORTED);
                }
                let iv_size = *buf.get(tenc.data + 7).ok_or(MALFORMED)? as usize;
                if iv_size != 0 && iv_size != 8 && iv_size != 16 {
                    return Err(UNSUPPORTED);
                }
                if scheme == EncryptionScheme::Cenc && iv_size == 0 {
                    return Err(UNSUPPORTED);
                }
                if scheme == EncryptionScheme::Cbcs && iv_size != 0 && iv_size != 16 {
                    return Err(UNSUPPORTED);
                }
                let constant_iv = if iv_size == 0 {
                    let size = *buf.get(tenc.data + 24).ok_or(MALFORMED)? as usize;
                    if size != 16 {
                        return Err(UNSUPPORTED);
                    }
                    Some(
                        buf.get(tenc.data + 25..tenc.data + 25 + size)
                            .ok_or(MALFORMED)?
                            .try_into()
                            .map_err(|_| MALFORMED)?,
                    )
                } else {
                    None
                };
                let kid: [u8; 16] = buf
                    .get(tenc.data + 8..tenc.data + 24)
                    .ok_or(MALFORMED)?
                    .try_into()
                    .map_err(|_| MALFORMED)?;
                // A single Stream currently accepts one content key. Distinct
                // KIDs across sample descriptions require future multi-key API.
                if first_kid.is_some_and(|previous| previous != kid) {
                    return Err("Multiple content key IDs require a multi-key decryptor");
                }
                first_kid = Some(kid);
                descriptions.push(SampleEncryption {
                    scheme,
                    iv_size,
                    constant_iv,
                    crypt_blocks,
                    skip_blocks,
                });
                // Metadata remains the exact same byte length as the original
                // initialization segment so all fragment offsets stay valid.
                buf[entry.start + 4..entry.start + 8].copy_from_slice(&format);
                buf[sinf.start + 4..sinf.start + 8].copy_from_slice(b"free");
            }
            // Apple enhanced-HLS CBCS songs use the fixed prefetch key for
            // description 1 and the licensed key for description 2. The
            // license's KID may differ from both MP4 tenc KIDs. Other audio
            // and all video streams must never use this legacy key mapping.
            let enhanced_song_prefetch = self.enhanced_hls_song
                && !video
                && descriptions.len() == 2
                && descriptions
                    .iter()
                    .all(|desc| desc.scheme == EncryptionScheme::Cbcs);
            // Apple's CBCS Asset HLS license KID is not always the tenc
            // KID, even for a standard (single-description) AAC stream.
            // Only allow this mismatch for explicitly identified Asset
            // HLS song audio, never for web AAC or video.
            let asset_song_cbcs = self.asset_song_cbcs
                && !video
                && descriptions
                    .iter()
                    .all(|desc| desc.scheme == EncryptionScheme::Cbcs);
            // Older Gamdl video decryption uses its separately licensed
            // video and audio content keys without matching the KID against
            // tenc. On Apple Music video HLS, tenc may contain a zero KID
            // placeholder for either track. The Python caller limits this
            // exception to music-video HLS source descriptors.
            let music_video_cbcs = self.music_video_cbcs
                && first_kid == Some([0u8; 16])
                && descriptions
                    .iter()
                    .all(|desc| desc.scheme == EncryptionScheme::Cbcs);
            if self.wrapper_mode
                && (video
                    || descriptions.iter().any(|desc| {
                        desc.scheme != EncryptionScheme::Cbcs
                            || (desc.crypt_blocks != 0 && desc.skip_blocks != 0)
                    }))
            {
                return Err("Wrapper sample delivery supports only non-pattern CBCS audio");
            }
            if !enhanced_song_prefetch
                && !asset_song_cbcs
                && !music_video_cbcs
                && self
                    .expected_kid
                    .is_some_and(|expected| Some(expected) != first_kid)
            {
                return Err("Decryption key ID does not match the encrypted media");
            }
            let mut default_size = 0;
            let mut default_description = 1;
            if let Ok(mvex) = child(buf, moov, b"mvex", 0) {
                for trex in boxes(buf, mvex.data, mvex.end)?
                    .into_iter()
                    .filter(|b| &b.kind == b"trex")
                {
                    if u32_at(buf, trex.data + 4)? == track_id {
                        default_description = u32_at(buf, trex.data + 8)? as usize;
                        default_size = u32_at(buf, trex.data + 16)? as usize;
                    }
                }
            }
            if default_description == 0 || default_description > descriptions.len() {
                return Err(UNSUPPORTED);
            }
            if selected.is_some() {
                return Err(UNSUPPORTED);
            }
            selected = Some(EncryptionConfig {
                track_id,
                auxiliary_track_ids: Vec::new(),
                default_description,
                default_size,
                descriptions,
                enhanced_song_prefetch,
            });
        }
        let mut config = selected.ok_or("Expected an encrypted audio or video MP4 track")?;
        if auxiliary_track_ids.contains(&config.track_id) {
            return Err(MALFORMED);
        }
        config.auxiliary_track_ids = auxiliary_track_ids;
        self.config = Some(config);
        Ok(())
    }
    async fn decrypt_fragment(
        &self,
        buf: &mut [u8],
        moof: BoxPos,
        mdat: BoxPos,
        mut wrapper: Option<&mut WrapperSession>,
        media_id: &str,
        fairplay_key: &str,
    ) -> Result<()> {
        let cfg = self
            .config
            .as_ref()
            .ok_or("Encrypted MP4 requires an initialization section")?;
        let mut handled = false;
        let mut saw_auxiliary = false;
        let mut pending_wrapper = Vec::new();
        for traf in boxes(buf, moof.data, moof.end)?
            .into_iter()
            .filter(|b| &b.kind == b"traf")
        {
            let tfhd = child(buf, traf, b"tfhd", 0)?;
            let flags = u32_at(buf, tfhd.data)? & 0x00ff_ffff;
            let track = u32_at(buf, tfhd.data + 4)?;
            if track != cfg.track_id {
                if cfg.auxiliary_track_ids.contains(&track) {
                    // Music-video HLS can interleave clear CEA-608/708 or
                    // subtitle data alongside encrypted video. Preserve its
                    // track and traf/mdat bytes without attempting decryption.
                    saw_auxiliary = true;
                    continue;
                }
                return Err("Unexpected non-caption track in encrypted media fragment");
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
            let description_index = if flags & 0x02 != 0 {
                let index = u32_at(buf, pos)? as usize;
                pos += 4;
                index
            } else {
                cfg.default_description
            };
            let desc = cfg
                .descriptions
                .get(description_index.checked_sub(1).ok_or(UNSUPPORTED)?)
                .ok_or(UNSUPPORTED)?;
            let key = if cfg.enhanced_song_prefetch && description_index == 1 {
                &ENHANCED_HLS_PREFETCH_KEY
            } else {
                &self.key
            };
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
            let mut run_end = None;
            let mut senc = None;
            for atom in boxes(buf, traf.data, traf.end)? {
                match &atom.kind {
                    b"sbgp" | b"sgpd" => {
                        // seig groups can override the per-sample KID,
                        // pattern or IV. Refuse them until implemented.
                        if buf.get(atom.data + 4..atom.data + 8) == Some(b"seig") {
                            return Err("Sample-group encryption overrides are not supported");
                        }
                    }
                    b"trun" => {
                        seen_trun = true;
                        let flags = u32_at(buf, atom.data)? & 0x00ff_ffff;
                        let count = u32_at(buf, atom.data + 4)? as usize;
                        if count > 100_000 || samples.len() > 100_000 - count {
                            return Err(UNSUPPORTED);
                        }
                        let mut at = atom.data + 8;
                        let mut run_position = if flags & 1 != 0 {
                            let relative = u32_at(buf, at)? as i32 as i64;
                            let abs = (base as i64).checked_add(relative).ok_or(MALFORMED)?;
                            if abs < 0 {
                                return Err(MALFORMED);
                            }
                            at += 4;
                            usize::try_from(abs).map_err(|_| MALFORMED)?
                        } else {
                            run_end.unwrap_or(mdat.data)
                        };
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
                            samples.push((run_position, size));
                            run_position = run_position.checked_add(size).ok_or(MALFORMED)?;
                        }
                        run_end = Some(run_position);
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
            let senc = senc.ok_or("Encrypted fragment has no sample encryption metadata")?;
            let flags = u32_at(buf, senc.data)? & 0x00ff_ffff;
            if flags & !2 != 0 {
                return Err(UNSUPPORTED);
            }
            let count = u32_at(buf, senc.data + 4)? as usize;
            if count != samples.len() {
                return Err(MALFORMED);
            }
            let mut senpos = senc.data + 8;
            for (samplepos, size) in samples {
                let mut full_iv = desc.constant_iv.unwrap_or([0u8; 16]);
                let iv = buf.get(senpos..senpos + desc.iv_size).ok_or(MALFORMED)?;
                if desc.iv_size > 0 {
                    full_iv[..desc.iv_size].copy_from_slice(iv);
                }
                senpos += desc.iv_size;
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
                // Every sample starts with a fresh IV. Transparent regions
                // in subsample encryption do not advance the cipher.
                if subsamples.is_empty() {
                    subsamples.push((0, sample.len()));
                }
                // The old Gamdl CBCS path assembled the encrypted regions of
                // an entire sample before AES-CBC processing. Individual senc
                // subsample ranges may split a 16-byte CBC block: decrypting
                // them separately would leave such a block as ciphertext.
                // For unpatterned CBCS, compact, decrypt, then scatter the
                // same-length bytes while preserving all clear regions.
                if !self.wrapper_mode
                    && desc.scheme == EncryptionScheme::Cbcs
                    && desc.crypt_blocks == 0
                    && desc.skip_blocks == 0
                    && subsamples.len() > 1
                {
                    let mut encrypted = Vec::new();
                    let mut positions = Vec::new();
                    let mut cursor = 0usize;
                    for &(clear, size) in &subsamples {
                        cursor = cursor.checked_add(clear).ok_or(MALFORMED)?;
                        let end = cursor.checked_add(size).ok_or(MALFORMED)?;
                        if end > sample.len() {
                            return Err(MALFORMED);
                        }
                        encrypted.extend_from_slice(&sample[cursor..end]);
                        positions.push((cursor, size));
                        cursor = end;
                    }
                    let cipher = Aes128::new(key.into());
                    let mut chain = full_iv;
                    decrypt_cbcs_range(&mut encrypted, &cipher, &mut chain, 0, 0);
                    let mut offset = 0;
                    for (start, size) in positions {
                        sample[start..start + size]
                            .copy_from_slice(&encrypted[offset..offset + size]);
                        offset += size;
                    }
                    continue;
                }
                if self.wrapper_mode && !(cfg.enhanced_song_prefetch && description_index == 1) {
                    let mut cipher_bytes = Vec::new();
                    let mut cursor = 0usize;
                    let mut ranges = Vec::new();
                    for &(clear, encrypted) in &subsamples {
                        cursor = cursor.checked_add(clear).ok_or(MALFORMED)?;
                        let next = cursor.checked_add(encrypted).ok_or(MALFORMED)?;
                        if next > sample.len() {
                            return Err(MALFORMED);
                        }
                        cipher_bytes.extend_from_slice(&sample[cursor..next]);
                        ranges.push((cursor, encrypted));
                        cursor = next;
                    }
                    let aligned_len = cipher_bytes.len() & !15;
                    if aligned_len != 0 {
                        pending_wrapper.push(WrapperPending {
                            start: samplepos,
                            ranges,
                            aligned: cipher_bytes[..aligned_len].to_vec(),
                            tail: cipher_bytes[aligned_len..].to_vec(),
                        });
                    }
                    continue;
                }
                let mut cursor = 0usize;
                let mut ctr = (desc.scheme == EncryptionScheme::Cenc)
                    .then(|| Ctr128BE::<Aes128>::new(key.into(), (&full_iv).into()));
                let cbc = (desc.scheme == EncryptionScheme::Cbcs).then(|| Aes128::new(key.into()));
                for (clear, encrypted) in subsamples {
                    cursor = cursor.checked_add(clear).ok_or(MALFORMED)?;
                    let next = cursor.checked_add(encrypted).ok_or(MALFORMED)?;
                    if next > sample.len() {
                        return Err(MALFORMED);
                    }
                    if let Some(cipher) = ctr.as_mut() {
                        cipher.apply_keystream(&mut sample[cursor..next]);
                    } else if let Some(cipher) = cbc.as_ref() {
                        // Each patterned CBCS subsample starts its own CBC
                        // chain from the sample IV. CENC/CTR instead keeps
                        // its cipher state across protected subsamples.
                        let mut chain = full_iv;
                        decrypt_cbcs_range(
                            &mut sample[cursor..next],
                            cipher,
                            &mut chain,
                            desc.crypt_blocks,
                            desc.skip_blocks,
                        );
                    }
                    cursor = next;
                }
            }
            if senpos != senc.end {
                return Err(MALFORMED);
            }
            if !pending_wrapper.is_empty() {
                let session = wrapper
                    .as_deref_mut()
                    .ok_or("Wrapper sample delivery requires a wrapper session")?;
                for batch in pending_wrapper.chunks(BATCH_SIZE) {
                    let payloads = batch
                        .iter()
                        .map(|item| item.aligned.clone())
                        .collect::<Vec<_>>();
                    let plaintexts = session
                        .decrypt_batch(media_id, fairplay_key, &payloads)
                        .await?;
                    for ((item, ciphertext), plain) in batch.iter().zip(&payloads).zip(plaintexts) {
                        // A wrapper may acknowledge the request while returning
                        // untouched protected samples (observed for certain
                        // iTunes music-video renditions). Treat that as a
                        // decryption failure, never publish an encrypted file
                        // with its protection boxes stripped.
                        if plain == *ciphertext {
                            return Err("Wrapper returned encrypted samples unchanged");
                        }
                        let sample_len = item
                            .ranges
                            .last()
                            .map(|(offset, size)| offset + size)
                            .ok_or(MALFORMED)?;
                        let mut encrypted_plain = plain;
                        encrypted_plain.extend_from_slice(&item.tail);
                        let mut cursor = 0usize;
                        for (offset, size) in &item.ranges {
                            let end = item
                                .start
                                .checked_add(*offset)
                                .and_then(|n| n.checked_add(*size))
                                .ok_or(MALFORMED)?;
                            if end > buf.len() || cursor + size > encrypted_plain.len() {
                                return Err(MALFORMED);
                            }
                            buf[item.start + offset..end]
                                .copy_from_slice(&encrypted_plain[cursor..cursor + size]);
                            cursor += size;
                        }
                        if cursor != encrypted_plain.len() || sample_len == 0 {
                            return Err(MALFORMED);
                        }
                    }
                }
            }
            for atom in boxes(buf, traf.data, traf.end)? {
                if matches!(&atom.kind, b"senc" | b"saiz" | b"saio") {
                    buf[atom.start + 4..atom.start + 8].copy_from_slice(b"free");
                }
            }
        }
        if !handled && !saw_auxiliary {
            return Err(UNSUPPORTED);
        }
        Ok(())
    }
    pub(crate) async fn decrypt_part_with_wrapper(
        &mut self,
        bytes: &mut [u8],
        mut wrapper: Option<&mut WrapperSession>,
        media_id: &str,
        fairplay_key: &str,
    ) -> Result<()> {
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
                    self.decrypt_fragment(
                        bytes,
                        moof,
                        atom,
                        wrapper.as_deref_mut(),
                        media_id,
                        fairplay_key,
                    )
                    .await?;
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
