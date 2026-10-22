//! Native MP4 metadata writing. Downloading and muxing will be added later.
use mp4ameta::{Data, Fourcc, Img, Tag};
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

        // iTunes uses 64 bits for playlist/album ID and 32 bits for other IDs.
        const NUMBERS: [(&str, [u8; 4], usize); 6] = [
            ("title_id", *b"cnID", 4),
            ("artist_id", *b"atID", 4),
            ("album_id", *b"plID", 8),
            ("composer_id", *b"cmID", 4),
            ("genre_id", *b"geID", 4),
            ("storefront", *b"sfID", 4),
        ];
        let mut integers = Vec::new();
        for (field, atom, bytes) in NUMBERS {
            if let Some(value) = number(tags, field)? {
                // Apple's iTunes integer atoms carry big-endian signed data.
                let bound = if bytes == 4 {
                    i32::MAX as u64
                } else {
                    i64::MAX as u64
                };
                if value > bound {
                    return Err(PyValueError::new_err(format!(
                        "{field} exceeds {bytes}-byte signed MP4 limit"
                    )));
                }
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

    fn write(self, path: &str) -> PyResult<()> {
        let mut mp4 = Tag::read_from_path(path)
            .map_err(|err| PyValueError::new_err(format!("Could not read MP4 metadata: {err}")))?;
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
        mp4.write_to_path(path)
            .map_err(|err| PyOSError::new_err(format!("Could not write MP4 metadata: {err}")))
    }
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
