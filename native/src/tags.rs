//! Native MP4 metadata writing, including bounded-memory file-backed tagging.
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};

use mp4ameta::{Data, Fourcc, Img, ReadConfig, Tag, WriteConfig};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

struct TagFields {
    strings: Vec<([u8; 4], String)>,
    integers: Vec<([u8; 4], Vec<u8>)>,
    flags: Vec<([u8; 4], bool)>,
    track: Option<(u16, u16)>,
    disc: Option<(u16, u16)>,
    genres: Vec<String>,
    isrc: Option<String>,
    artwork: Option<Artwork>,
}

enum Artwork {
    Jpeg(Vec<u8>),
    Png(Vec<u8>),
}

fn text(tags: &Bound<'_, PyDict>, field: &str) -> PyResult<Option<String>> {
    tags.get_item(field)?
        .filter(|value| !value.is_none())
        .map(|value| value.extract::<String>())
        .transpose()
}

fn number(tags: &Bound<'_, PyDict>, field: &str) -> PyResult<Option<u64>> {
    tags.get_item(field)?
        .filter(|value| !value.is_none())
        .map(|value| {
            if value.is_instance_of::<pyo3::types::PyBool>() {
                return Err(PyValueError::new_err(format!(
                    "{field} must be an integer, not a boolean"
                )));
            }
            value.extract::<u64>()
        })
        .transpose()
}

fn flag(tags: &Bound<'_, PyDict>, field: &str) -> PyResult<Option<bool>> {
    tags.get_item(field)?
        .filter(|value| !value.is_none())
        .map(|value| value.extract::<bool>())
        .transpose()
}

fn pair(tags: &Bound<'_, PyDict>, a: &str, b: &str) -> PyResult<Option<(u16, u16)>> {
    let number_value = number(tags, a)?;
    let total = number(tags, b)?;
    if number_value.is_none() && total.is_none() {
        return Ok(None);
    }
    let n = u16::try_from(number_value.unwrap_or(0))
        .map_err(|_| PyValueError::new_err(format!("{a} exceeds the MP4 16-bit limit")))?;
    let t = u16::try_from(total.unwrap_or(0))
        .map_err(|_| PyValueError::new_err(format!("{b} exceeds the MP4 16-bit limit")))?;
    Ok(Some((n, t)))
}

impl TagFields {
    fn parse(tags: &Bound<'_, PyDict>, cover: Option<Vec<u8>>) -> PyResult<Self> {
        // Four-character atoms are literal bytes: © is 0xA9, not UTF-8.
        const TEXT: [(&str, [u8; 4]); 14] = [
            ("title", *b"\xa9nam"),
            ("artist", *b"\xa9ART"),
            ("album", *b"\xa9alb"),
            ("album_artist", *b"aART"),
            ("title_sort", *b"sonm"),
            ("artist_sort", *b"soar"),
            ("album_sort", *b"soal"),
            ("composer", *b"\xa9wrt"),
            ("composer_sort", *b"soco"),
            ("copyright", *b"cprt"),
            ("comment", *b"\xa9cmt"),
            ("release_date", *b"\xa9day"),
            ("lyrics", *b"\xa9lyr"),
            ("xid", *b"xid "),
        ];
        let mut strings = Vec::new();
        for (field, atom) in TEXT {
            if let Some(value) = text(tags, field)? {
                strings.push((atom, value));
            }
        }

        // iTunes normally writes 32-bit IDs (and 64-bit playlist IDs), but
        // newer Apple Music content IDs exceed i32::MAX. As in Mutagen's
        // integer renderer, widen these IDs to signed 64-bit rather than
        // truncating them or failing after the media has downloaded.
        const NUMBERS: [(&str, [u8; 4], usize); 6] = [
            ("title_id", *b"cnID", 4),
            ("artist_id", *b"atID", 4),
            ("album_id", *b"plID", 8),
            ("composer_id", *b"cmID", 4),
            ("genre_id", *b"geID", 4),
            ("storefront", *b"sfID", 4),
        ];
        let mut integers = Vec::new();
        for (field, atom, minimum_bytes) in NUMBERS {
            if let Some(value) = number(tags, field)? {
                // Apple's iTunes integer atoms carry big-endian signed data.
                if value > i64::MAX as u64 {
                    return Err(PyValueError::new_err(format!(
                        "{field} exceeds 8-byte signed MP4 limit"
                    )));
                }
                let bytes = if minimum_bytes == 4 && value > i32::MAX as u64 {
                    8
                } else {
                    minimum_bytes
                };
                let raw = value.to_be_bytes();
                integers.push((atom, raw[8 - bytes..].to_vec()));
            }
        }
        for (field, atom) in [("media_type", *b"stik"), ("rating", *b"rtng")] {
            if let Some(value) = number(tags, field)? {
                let small = u8::try_from(value).map_err(|_| {
                    PyValueError::new_err(format!("{field} exceeds MP4 byte limit"))
                })?;
                integers.push((atom, vec![small]));
            }
        }
        let mut flags = Vec::new();
        for (field, atom) in [("compilation", *b"cpil"), ("gapless", *b"pgap")] {
            if let Some(value) = flag(tags, field)? {
                flags.push((atom, value));
            }
        }

        let genres = tags
            .get_item("genre")?
            .filter(|value| !value.is_none())
            .map(|value| value.extract::<Vec<String>>())
            .transpose()?
            .unwrap_or_default();

        let artwork = match cover {
            Some(bytes) if bytes.starts_with(&[0xff, 0xd8, 0xff]) => Some(Artwork::Jpeg(bytes)),
            Some(bytes) if bytes.starts_with(b"\x89PNG\r\n\x1a\n") => Some(Artwork::Png(bytes)),
            Some(_) => {
                return Err(PyValueError::new_err(
                    "cover_bytes must contain JPEG or PNG image bytes",
                ));
            }
            None => None,
        };

        Ok(Self {
            strings,
            integers,
            flags,
            track: pair(tags, "track_number", "track_total")?,
            disc: pair(tags, "disc_number", "disc_total")?,
            genres,
            isrc: text(tags, "isrc")?,
            artwork,
        })
    }

    fn apply(self, mp4: &mut Tag) {
        for (atom, value) in self.strings {
            mp4.set_data(Fourcc(atom), Data::Utf8(value));
        }
        for (atom, value) in self.integers {
            mp4.set_data(Fourcc(atom), Data::BeSigned(value));
        }
        for (atom, value) in self.flags {
            mp4.set_data(Fourcc(atom), Data::BeSigned(vec![u8::from(value)]));
        }
        if let Some((track, total)) = self.track {
            mp4.set_track(track, total);
        }
        if let Some((disc, total)) = self.disc {
            mp4.set_disc(disc, total);
        }
        if !self.genres.is_empty() {
            mp4.set_genres(self.genres);
        }
        if let Some(isrc) = self.isrc {
            mp4.set_isrc(isrc);
        }
        if let Some(artwork) = self.artwork {
            match artwork {
                Artwork::Jpeg(data) => mp4.set_artwork(Img::jpeg(data)),
                Artwork::Png(data) => mp4.set_artwork(Img::png(data)),
            }
        }
    }

    fn write(self, path: &str) -> PyResult<()> {
        let mut mp4 = Tag::read_from_path(path)
            .map_err(|err| PyValueError::new_err(format!("Could not read MP4 metadata: {err}")))?;
        self.apply(&mut mp4);
        mp4.write_to_path(path)
            .map_err(|err| PyOSError::new_err(format!("Could not write MP4 metadata: {err}")))
    }

    /// Tag the metadata-only portion of Gamdl's fast-start fMP4 output.
    ///
    /// mp4ameta shifts every byte after a growing front moov and normally
    /// collects the whole mdat in a Vec. Instead, give it a tiny proxy with
    /// the original moov (including real sample offsets) and an empty mdat.
    /// It updates the offset tables correctly; we then copy the untouched
    /// original mdat into a new file using an 8-KiB std::io::copy buffer.
    ///
    /// Returns false for layouts other than ftyp+moov+64bit-mdat, so callers
    /// can retain legacy tagging for arbitrary imported MP4 files.
    fn write_bounded(self, path: &str, proxy_path: &str, result_path: &str) -> PyResult<bool> {
        const MAX_HEADER_BYTES: u64 = 64 * 1024 * 1024;
        let mut source = File::open(path).map_err(PyOSError::new_err)?;
        let source_len = source.metadata().map_err(PyOSError::new_err)?.len();

        // Avoid reading media payloads just to decide whether the optimized
        // representation is supported. All sizes are checked against EOF.
        let Some((ftyp_size, ftyp_type)) = atom_header(&mut source, source_len)? else {
            return Ok(false);
        };
        if &ftyp_type != b"ftyp" || !(8..=1024).contains(&ftyp_size) {
            return Ok(false);
        }
        source
            .seek(SeekFrom::Start(ftyp_size))
            .map_err(PyOSError::new_err)?;
        let Some((moov_size, moov_type)) = atom_header(&mut source, source_len - ftyp_size)? else {
            return Ok(false);
        };
        if &moov_type != b"moov" || !(8..=MAX_HEADER_BYTES).contains(&moov_size) {
            return Ok(false);
        }
        let mdat_at = ftyp_size
            .checked_add(moov_size)
            .ok_or_else(|| PyValueError::new_err("Invalid MP4 metadata length"))?;
        if source_len < mdat_at + 16 {
            return Ok(false);
        }
        source
            .seek(SeekFrom::Start(mdat_at))
            .map_err(PyOSError::new_err)?;
        let mut header = [0u8; 16];
        source.read_exact(&mut header).map_err(PyOSError::new_err)?;
        if &header[..4] != 1u32.to_be_bytes().as_slice()
            || &header[4..8] != b"mdat"
            || u64::from_be_bytes(header[8..16].try_into().expect("16-byte header"))
                != source_len - mdat_at
        {
            return Ok(false);
        }
        source
            .seek(SeekFrom::Start(0))
            .map_err(PyOSError::new_err)?;
        let header_len =
            usize::try_from(mdat_at).map_err(|_| PyValueError::new_err("MP4 header too large"))?;
        let mut metadata = vec![0u8; header_len];
        source
            .read_exact(&mut metadata)
            .map_err(PyOSError::new_err)?;

        // Only a small header and an empty 16-byte mdat are visible to the
        // existing mp4ameta writer, so metadata relocation cannot allocate
        // the real payload length.
        let mut proxy = File::create(proxy_path).map_err(PyOSError::new_err)?;
        proxy.write_all(&metadata).map_err(PyOSError::new_err)?;
        proxy
            .write_all(&1u32.to_be_bytes())
            .map_err(PyOSError::new_err)?;
        proxy.write_all(b"mdat").map_err(PyOSError::new_err)?;
        proxy
            .write_all(&16u64.to_be_bytes())
            .map_err(PyOSError::new_err)?;
        proxy.flush().map_err(PyOSError::new_err)?;
        drop(proxy);
        drop(metadata);
        // Chapter tracks store their titles in mdat. Never read or regenerate
        // them through the empty proxy. Preserve both chapter representations
        // verbatim; mp4ameta still shifts every track's chunk offsets below.
        let mut tag = Tag::read_with_path(
            proxy_path,
            &ReadConfig {
                read_chapter_list: false,
                read_chapter_track: false,
                ..ReadConfig::DEFAULT
            },
        )
        .map_err(|err| PyValueError::new_err(format!("Could not read MP4 metadata: {err}")))?;
        self.apply(&mut tag);
        tag.write_with_path(
            proxy_path,
            &WriteConfig {
                write_chapter_list: false,
                write_chapter_track: false,
                ..WriteConfig::DEFAULT
            },
        )
        .map_err(|err| PyOSError::new_err(format!("Could not write MP4 metadata: {err}")))?;

        // Recheck the tagged proxy's shape. Its co64 offsets have already
        // been adjusted by mp4ameta for moov growth/shrinkage.
        if std::fs::metadata(proxy_path)
            .map_err(PyOSError::new_err)?
            .len()
            > MAX_HEADER_BYTES + 1040
        {
            return Err(PyValueError::new_err("Tagged MP4 metadata exceeds limit"));
        }
        let proxy_data = std::fs::read(proxy_path).map_err(PyOSError::new_err)?;
        let ftyp = u32::from_be_bytes(
            proxy_data
                .get(..4)
                .ok_or_else(|| PyValueError::new_err("Invalid tagged proxy"))?
                .try_into()
                .map_err(|_| PyValueError::new_err("Invalid tagged proxy"))?,
        ) as usize;
        if ftyp != ftyp_size as usize || proxy_data.get(4..8) != Some(b"ftyp") {
            return Err(PyValueError::new_err("Unexpected tagged MP4 file type"));
        }
        let moov_size = u32::from_be_bytes(
            proxy_data
                .get(ftyp..ftyp + 4)
                .ok_or_else(|| PyValueError::new_err("Invalid tagged moov"))?
                .try_into()
                .map_err(|_| PyValueError::new_err("Invalid tagged moov"))?,
        ) as usize;
        let payload_at = ftyp
            .checked_add(moov_size)
            .ok_or_else(|| PyValueError::new_err("Invalid tagged moov size"))?;
        if proxy_data.get(ftyp + 4..ftyp + 8) != Some(b"moov")
            || proxy_data.len() != payload_at + 16
            || proxy_data.get(payload_at + 4..payload_at + 8) != Some(b"mdat")
        {
            return Err(PyValueError::new_err("Unexpected tagged proxy layout"));
        }

        let mut result = File::create(result_path).map_err(PyOSError::new_err)?;
        result
            .write_all(&proxy_data[..payload_at])
            .map_err(PyOSError::new_err)?;
        source
            .seek(SeekFrom::Start(mdat_at))
            .map_err(PyOSError::new_err)?;
        let copied = std::io::copy(&mut source, &mut result).map_err(PyOSError::new_err)?;
        if copied != source_len - mdat_at {
            return Err(PyOSError::new_err("Incomplete MP4 payload during tagging"));
        }
        result.flush().map_err(PyOSError::new_err)?;
        Ok(true)
    }
}

/// Read a conventional MP4 box header without interpreting the media payload.
fn atom_header(file: &mut File, remaining: u64) -> PyResult<Option<(u64, [u8; 4])>> {
    if remaining < 8 {
        return Ok(None);
    }
    let mut header = [0u8; 8];
    file.read_exact(&mut header).map_err(PyOSError::new_err)?;
    let name = header[4..8].try_into().expect("fourcc length");
    let length = u32::from_be_bytes(header[..4].try_into().expect("box length"));
    let size = if length == 1 {
        if remaining < 16 {
            return Ok(None);
        }
        let mut extended = [0u8; 8];
        file.read_exact(&mut extended).map_err(PyOSError::new_err)?;
        u64::from_be_bytes(extended)
    } else {
        u64::from(length)
    };
    if size < if length == 1 { 16 } else { 8 } || size > remaining {
        return Ok(None);
    }
    Ok(Some((size, name)))
}

#[pyfunction]
#[pyo3(signature = (path, tags, cover_bytes=None))]
pub(crate) fn write_mp4_tags(
    py: Python<'_>,
    path: String,
    tags: &Bound<'_, PyDict>,
    cover_bytes: Option<Vec<u8>>,
) -> PyResult<()> {
    let fields = TagFields::parse(tags, cover_bytes)?;
    // Large container writes must not hold the Python GIL.
    py.detach(move || fields.write(&path))
}

#[pyfunction]
#[pyo3(signature = (path, proxy_path, result_path, tags, cover_bytes=None))]
pub(crate) fn write_mp4_tags_bounded(
    py: Python<'_>,
    path: String,
    proxy_path: String,
    result_path: String,
    tags: &Bound<'_, PyDict>,
    cover_bytes: Option<Vec<u8>>,
) -> PyResult<bool> {
    if path == proxy_path || path == result_path || proxy_path == result_path {
        return Err(PyValueError::new_err("MP4 tagging paths must be different"));
    }
    let fields = TagFields::parse(tags, cover_bytes)?;
    py.detach(move || fields.write_bounded(&path, &proxy_path, &result_path))
}

/// Read the iTunes catalog ID without loading the entire media file into Python.
/// Returns None for missing/unreadable/invalid metadata, since an unrecognized
/// file should be treated as an occupied filename, never silently overwritten.
#[pyfunction]
pub(crate) fn read_mp4_title_id(py: Python<'_>, path: String) -> Option<String> {
    py.detach(move || {
        let tag = Tag::read_from_path(path).ok()?;
        let value = tag.data_of(&Fourcc(*b"cnID")).next()?;
        let bytes = match value {
            Data::BeSigned(value) if (1..=8).contains(&value.len()) => value,
            _ => return None,
        };
        // A leading 1 bit indicates an invalid negative catalog identifier.
        if bytes[0] & 0x80 != 0 {
            return None;
        }
        let mut padded = [0u8; 8];
        padded[8 - bytes.len()..].copy_from_slice(bytes);
        Some(u64::from_be_bytes(padded).to_string())
    })
}

/// Read only filename-relevant tags, without artwork, chapters, or media samples.
/// The organizer uses exactly the same atoms written by TagFields::apply.
#[pyfunction]
pub(crate) fn read_mp4_organize_tags<'py>(
    py: Python<'py>,
    path: String,
) -> PyResult<Bound<'py, PyDict>> {
    let tag = py
        .detach(move || {
            Tag::read_with_path(
                path,
                &ReadConfig {
                    read_meta_items: true,
                    ..ReadConfig::NONE
                },
            )
        })
        .map_err(|err| PyValueError::new_err(format!("Could not read MP4 metadata: {err}")))?;
    let result = PyDict::new(py);
    for (name, value) in [
        ("title", tag.title()),
        ("artist", tag.artist()),
        ("album", tag.album()),
        ("album_artist", tag.album_artist()),
        ("composer", tag.composer()),
        ("release_date", tag.year()),
    ] {
        if let Some(value) = value {
            result.set_item(name, value)?;
        }
    }
    for (name, value) in [
        ("track_number", tag.track_number()),
        ("track_total", tag.total_tracks()),
        ("disc_number", tag.disc_number()),
        ("disc_total", tag.total_discs()),
    ] {
        if let Some(value) = value {
            result.set_item(name, value)?;
        }
    }
    result.set_item("compilation", tag.compilation())?;

    for (name, atom) in [
        ("title_id", *b"cnID"),
        ("artist_id", *b"atID"),
        ("album_id", *b"plID"),
        ("composer_id", *b"cmID"),
        ("media_type", *b"stik"),
    ] {
        if let Some(Data::BeSigned(bytes) | Data::Reserved(bytes)) =
            tag.data_of(&Fourcc(atom)).next()
        {
            if (1..=8).contains(&bytes.len()) && bytes[0] & 0x80 == 0 {
                let mut padded = [0u8; 8];
                padded[8 - bytes.len()..].copy_from_slice(bytes);
                result.set_item(name, u64::from_be_bytes(padded))?;
            }
        }
    }
    Ok(result)
}
