use crate::mp4::{
    build_decrypted_track_moov, build_muxed_moov, extract_mdat_payload, extract_mvhd_timescale,
    extract_top_level_box, find_child_box, find_track_by_handler, ftyp_m4v, ftyp_mp4,
    patch_first_chunk_offset, patch_trak_duration_to_movie_timescale, patch_trak_track_id,
    write_m4a_file, write_mdat_from_sources, write_track_file, PayloadSource, SampleInfo,
    TrackInfo,
};
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyList};
use std::fs::File;
use std::io::{self, Write};

fn py_io_error(err: io::Error) -> PyErr {
    PyIOError::new_err(err.to_string())
}

fn py_value_error(message: impl Into<String>) -> PyErr {
    PyValueError::new_err(message.into())
}

fn bytes_attr(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<u8>> {
    let value = obj.getattr(name)?;
    if value.is_none() {
        Ok(Vec::new())
    } else {
        Ok(value.extract::<Vec<u8>>()?)
    }
}

fn option_string_attr(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<String>> {
    let value = obj.getattr(name)?;
    if value.is_none() {
        Ok(None)
    } else {
        value.extract::<String>().map(Some)
    }
}

fn handler_attr(obj: &Bound<'_, PyAny>) -> PyResult<[u8; 4]> {
    let value = bytes_attr(obj, "handler_type")?;
    if value.len() != 4 {
        return Err(py_value_error("mux: handler_type must be four bytes"));
    }
    Ok([value[0], value[1], value[2], value[3]])
}

fn extract_samples(track_info: &Bound<'_, PyAny>) -> PyResult<Vec<SampleInfo>> {
    let samples = track_info.getattr("samples")?;
    let list = samples.downcast::<PyList>()?;
    let mut out = Vec::with_capacity(list.len());
    for item in list.iter() {
        let size = item.getattr("size")?.extract::<u64>().or_else(|_| {
            item.getattr("data")?
                .extract::<Vec<u8>>()
                .map(|d| d.len() as u64)
        })?;
        out.push(SampleInfo {
            size,
            duration: item.getattr("duration")?.extract::<u32>()?,
            desc_index: item.getattr("desc_index")?.extract::<usize>()?,
            composition_time_offset: item.getattr("composition_time_offset")?.extract::<i32>()?,
            is_sync: item.getattr("is_sync")?.extract::<bool>()?,
        });
    }
    Ok(out)
}

fn extract_track_info(track_info: &Bound<'_, PyAny>) -> PyResult<TrackInfo> {
    Ok(TrackInfo {
        samples: extract_samples(track_info)?,
        moov_data: bytes_attr(track_info, "moov_data")?,
        ftyp_data: bytes_attr(track_info, "ftyp_data")?,
        handler_type: handler_attr(track_info)?,
    })
}

fn payload_source_from_parts(
    data: Vec<u8>,
    data_path: Option<String>,
    data_size: u64,
) -> PyResult<PayloadSource> {
    if let Some(path) = data_path {
        let size = if data_size > 0 {
            data_size
        } else {
            std::fs::metadata(&path).map_err(py_io_error)?.len()
        };
        Ok(PayloadSource::File {
            path,
            offset: 0,
            size,
        })
    } else {
        Ok(PayloadSource::Memory(data))
    }
}

fn extract_decrypted_track(
    track: &Bound<'_, PyAny>,
) -> PyResult<(String, TrackInfo, PayloadSource)> {
    let input_path = track.getattr("input_path")?.extract::<String>()?;
    let track_info = extract_track_info(&track.getattr("track_info")?)?;
    let data = bytes_attr(track, "data")?;
    let data_path = option_string_attr(track, "data_path")?;
    let data_size = track.getattr("data_size")?.extract::<u64>()?;
    let source = payload_source_from_parts(data, data_path, data_size)?;
    Ok((input_path, track_info, source))
}

#[pyfunction]
#[pyo3(signature = (output_path, song_info, decrypted_data, original_path=None, decrypted_data_path=None))]
pub fn write_decrypted_m4a_native(
    py: Python<'_>,
    output_path: String,
    song_info: Bound<'_, PyAny>,
    decrypted_data: Bound<'_, PyBytes>,
    original_path: Option<String>,
    decrypted_data_path: Option<String>,
) -> PyResult<()> {
    let track = extract_track_info(&song_info)?;
    let payload =
        payload_source_from_parts(decrypted_data.as_bytes().to_vec(), decrypted_data_path, 0)?;
    py.allow_threads(move || {
        write_m4a_file(&output_path, &track, original_path.as_deref(), &payload)
            .map_err(py_io_error)
    })
}

#[pyfunction]
#[pyo3(signature = (output_path, track_info, decrypted_data, original_path=None, decrypted_data_path=None))]
pub fn write_decrypted_mp4_track_native(
    py: Python<'_>,
    output_path: String,
    track_info: Bound<'_, PyAny>,
    decrypted_data: Bound<'_, PyBytes>,
    original_path: Option<String>,
    decrypted_data_path: Option<String>,
) -> PyResult<()> {
    let track = extract_track_info(&track_info)?;
    let payload =
        payload_source_from_parts(decrypted_data.as_bytes().to_vec(), decrypted_data_path, 0)?;
    py.allow_threads(move || {
        write_track_file(&output_path, &track, original_path.as_deref(), &payload)
            .map_err(py_io_error)
    })
}

fn build_track_moov_for_decrypted_track(
    track: &Bound<'_, PyAny>,
) -> PyResult<(Vec<u8>, PayloadSource)> {
    let (input_path, track_info, source) = extract_decrypted_track(track)?;
    let moov = build_decrypted_track_moov(&track_info, Some(&input_path)).map_err(py_io_error)?;
    Ok((moov, source))
}

#[pyfunction]
pub fn mux_decrypted_media_direct_native(
    py: Python<'_>,
    decrypted_media: Bound<'_, PyAny>,
    output_path: String,
    m4v_brand: bool,
) -> PyResult<()> {
    let video_obj = decrypted_media.getattr("video")?;
    if video_obj.is_none() {
        return Err(py_value_error("direct AV mux requires a video track"));
    }
    let audio_obj = decrypted_media.getattr("audio")?;
    let captions_obj = decrypted_media.getattr("captions")?;

    let (video_moov, video_source) = build_track_moov_for_decrypted_track(&video_obj)?;
    let (audio_moov, audio_source) = build_track_moov_for_decrypted_track(&audio_obj)?;

    let captions = captions_obj.downcast::<PyList>()?;
    let mut extra_tracks = Vec::new();
    for caption in captions.iter() {
        let (moov, source) = build_track_moov_for_decrypted_track(&caption)?;
        extra_tracks.push((moov, source));
    }

    py.allow_threads(move || {
        let mvhd = find_child_box(&video_moov, b"mvhd", 8).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let video_trak = find_track_by_handler(&video_moov, b"vide").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let mut audio_trak = find_track_by_handler(&audio_moov, b"soun").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let movie_timescale = extract_mvhd_timescale(&mvhd);
        audio_trak = patch_trak_track_id(&audio_trak, 2);
        audio_trak = patch_trak_duration_to_movie_timescale(&audio_trak, movie_timescale);

        let mut patched_extra_traks = Vec::new();
        let mut extra_sources = Vec::new();
        for (idx, (extra_moov, source)) in extra_tracks.into_iter().enumerate() {
            if let Some(mut trak) = find_child_box(&extra_moov, b"trak", 8) {
                trak = patch_trak_track_id(&trak, idx as u32 + 3);
                trak = patch_trak_duration_to_movie_timescale(&trak, movie_timescale);
                patched_extra_traks.push(trak);
                extra_sources.push(source);
            }
        }

        let ftyp = if m4v_brand { ftyp_m4v()? } else { ftyp_mp4()? };
        let mut traks = Vec::new();
        traks.push(video_trak);
        traks.push(audio_trak);
        traks.extend(patched_extra_traks);
        let moov_probe = build_muxed_moov(&mvhd, &traks)?;
        let mut mdat_offset = ftyp.len() as u64 + moov_probe.len() as u64 + 8;
        let mut patched_traks = Vec::new();
        patched_traks.push(patch_first_chunk_offset(&traks[0], mdat_offset)?);
        mdat_offset += video_source.len();
        patched_traks.push(patch_first_chunk_offset(&traks[1], mdat_offset)?);
        mdat_offset += audio_source.len();
        for (trak, source) in traks.iter().skip(2).zip(extra_sources.iter()) {
            patched_traks.push(patch_first_chunk_offset(trak, mdat_offset)?);
            mdat_offset += source.len();
        }
        let moov = build_muxed_moov(&mvhd, &patched_traks)?;

        let mut sources = vec![video_source, audio_source];
        sources.extend(extra_sources);
        let mut file = File::create(&output_path)?;
        file.write_all(&ftyp)?;
        file.write_all(&moov)?;
        write_mdat_from_sources(&mut file, &sources)
    })
    .map_err(py_io_error)
}

#[pyfunction]
#[pyo3(signature = (input_path_video, input_path_audio, output_path, input_path_extra_tracks=None, m4v_brand=false))]
pub fn mux_decrypted_mp4_tracks_native(
    py: Python<'_>,
    input_path_video: String,
    input_path_audio: String,
    output_path: String,
    input_path_extra_tracks: Option<Vec<String>>,
    m4v_brand: bool,
) -> PyResult<()> {
    py.allow_threads(move || {
        let video_data = std::fs::read(&input_path_video)?;
        let audio_data = std::fs::read(&input_path_audio)?;
        let video_moov = extract_top_level_box(&video_data, b"moov").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing moov box in decrypted track file",
            )
        })?;
        let audio_moov = extract_top_level_box(&audio_data, b"moov").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing moov box in decrypted track file",
            )
        })?;
        let video_payload = extract_mdat_payload(&video_data)?;
        let audio_payload = extract_mdat_payload(&audio_data)?;

        let mut extra_tracks = Vec::new();
        for path in input_path_extra_tracks.unwrap_or_default() {
            let data = std::fs::read(&path)?;
            if let Some(moov) = extract_top_level_box(&data, b"moov") {
                let payload = extract_mdat_payload(&data)?;
                extra_tracks.push((moov, payload));
            }
        }

        let mvhd = find_child_box(&video_moov, b"mvhd", 8).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let video_trak = find_track_by_handler(&video_moov, b"vide").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let mut audio_trak = find_track_by_handler(&audio_moov, b"soun").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "mux: missing required audio/video track metadata",
            )
        })?;
        let movie_timescale = extract_mvhd_timescale(&mvhd);
        audio_trak = patch_trak_track_id(&audio_trak, 2);
        audio_trak = patch_trak_duration_to_movie_timescale(&audio_trak, movie_timescale);

        let mut traks = vec![video_trak, audio_trak];
        let mut payloads = vec![video_payload, audio_payload];
        for (idx, (extra_moov, payload)) in extra_tracks.into_iter().enumerate() {
            if let Some(mut trak) = find_child_box(&extra_moov, b"trak", 8) {
                trak = patch_trak_track_id(&trak, idx as u32 + 3);
                trak = patch_trak_duration_to_movie_timescale(&trak, movie_timescale);
                traks.push(trak);
                payloads.push(payload);
            }
        }

        let ftyp = if m4v_brand { ftyp_m4v()? } else { ftyp_mp4()? };
        let moov_probe = build_muxed_moov(&mvhd, &traks)?;
        let mut mdat_offset = ftyp.len() as u64 + moov_probe.len() as u64 + 8;
        let mut patched_traks = Vec::new();
        for (trak, payload) in traks.iter().zip(payloads.iter()) {
            patched_traks.push(patch_first_chunk_offset(trak, mdat_offset)?);
            mdat_offset += payload.len() as u64;
        }
        let moov = build_muxed_moov(&mvhd, &patched_traks)?;
        let sources: Vec<PayloadSource> = payloads.into_iter().map(PayloadSource::Memory).collect();

        let mut file = File::create(&output_path)?;
        file.write_all(&ftyp)?;
        file.write_all(&moov)?;
        write_mdat_from_sources(&mut file, &sources)
    })
    .map_err(py_io_error)
}
