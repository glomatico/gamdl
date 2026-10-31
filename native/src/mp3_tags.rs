//! ID3v2 tagging for progressive MP3 uploads, without audio transcoding.
use id3::{
    Tag, TagLike, Version,
    frame::{Picture, PictureType},
};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn string(tags: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    tags.get_item(key)?
        .filter(|v| !v.is_none())
        .map(|v| v.extract())
        .transpose()
}

fn number(tags: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u32>> {
    tags.get_item(key)?
        .filter(|v| !v.is_none())
        .map(|v| v.extract())
        .transpose()
}

#[pyfunction]
#[pyo3(signature = (path, tags, cover_bytes=None))]
pub(crate) fn write_mp3_tags(
    py: Python<'_>,
    path: String,
    tags: &Bound<'_, PyDict>,
    cover_bytes: Option<Vec<u8>>,
) -> PyResult<()> {
    let mut tag = Tag::read_from_path(&path).unwrap_or_else(|_| Tag::new());
    for (field, frame) in [
        ("title", "TIT2"),
        ("artist", "TPE1"),
        ("album", "TALB"),
        ("album_artist", "TPE2"),
        ("composer", "TCOM"),
        ("copyright", "TCOP"),
        ("title_sort", "TSOT"),
        ("artist_sort", "TSOP"),
        ("album_sort", "TSOA"),
        ("composer_sort", "TSOC"),
        ("isrc", "TSRC"),
    ] {
        if let Some(value) = string(tags, field)? {
            tag.set_text(frame, value);
        }
    }
    if let Some(value) = string(tags, "release_date")? {
        tag.set_text("TDRC", value);
    }
    if let Some(track) = number(tags, "track_number")? {
        let value = match number(tags, "track_total")? {
            Some(total) => format!("{track}/{total}"),
            None => track.to_string(),
        };
        tag.set_text("TRCK", value);
    }
    if let Some(disc) = number(tags, "disc_number")? {
        let value = match number(tags, "disc_total")? {
            Some(total) => format!("{disc}/{total}"),
            None => disc.to_string(),
        };
        tag.set_text("TPOS", value);
    }
    if let Some(genres) = tags.get_item("genre")? {
        let genres: Vec<String> = genres.extract()?;
        if !genres.is_empty() {
            tag.set_genre(genres.join("; "));
        }
    }
    if let Some(cover) = cover_bytes {
        let mime = if cover.starts_with(&[0xff, 0xd8, 0xff]) {
            "image/jpeg"
        } else if cover.starts_with(b"\x89PNG\r\n\x1a\n") {
            "image/png"
        } else {
            return Err(PyValueError::new_err(
                "cover_bytes must contain JPEG or PNG image bytes",
            ));
        };
        tag.add_frame(Picture {
            mime_type: mime.into(),
            picture_type: PictureType::CoverFront,
            description: String::new(),
            data: cover,
        });
    }
    py.detach(move || tag.write_to_path(path, Version::Id3v24))
        .map_err(|err| PyOSError::new_err(format!("Could not write MP3 metadata: {err}")))
}

#[pyfunction]
pub(crate) fn read_mp3_organize_tags<'py>(
    py: Python<'py>,
    path: String,
) -> PyResult<Bound<'py, PyDict>> {
    let tag = py
        .detach(|| Tag::read_from_path(path))
        .map_err(|err| PyValueError::new_err(format!("Could not read MP3 metadata: {err}")))?;
    let result = PyDict::new(py);
    for (name, value) in [
        ("title", tag.title()),
        ("artist", tag.artist()),
        ("album", tag.album()),
        ("album_artist", tag.album_artist()),
        ("composer", tag.text_for_frame_id("TCOM")),
        ("release_date", tag.text_for_frame_id("TDRC")),
    ] {
        if let Some(value) = value {
            result.set_item(name, value)?;
        }
    }
    for (name, value) in [
        ("track_number", tag.track()),
        ("track_total", tag.total_tracks()),
        ("disc_number", tag.disc()),
        ("disc_total", tag.total_discs()),
    ] {
        if let Some(value) = value {
            result.set_item(name, value)?;
        }
    }
    Ok(result)
}
