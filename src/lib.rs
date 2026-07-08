use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

#[pyfunction]
fn native_available() -> bool {
    true
}

type BatchItem = (Vec<u8>, Vec<u8>, Vec<u8>, Vec<(usize, usize)>);

fn value_error(message: impl Into<String>) -> PyErr {
    PyValueError::new_err(message.into())
}

fn io_error(message: impl Into<String>) -> PyErr {
    PyIOError::new_err(message.into())
}

fn validate_label(name: &str, value: &str) -> PyResult<Vec<u8>> {
    let bytes = value.as_bytes();
    if bytes.is_empty() {
        return Err(value_error(format!("wrapper-v2: {name} must not be empty")));
    }
    if bytes.len() > u8::MAX as usize {
        return Err(value_error(format!(
            "wrapper-v2: {name} is too long for TCP decrypt protocol"
        )));
    }
    Ok(bytes.to_vec())
}

fn reassemble_sample(
    data: &[u8],
    plain: &[u8],
    tail: &[u8],
    subsamples: &[(usize, usize)],
) -> PyResult<Vec<u8>> {
    let mut full_dec = Vec::with_capacity(plain.len() + tail.len());
    full_dec.extend_from_slice(plain);
    full_dec.extend_from_slice(tail);

    if subsamples.is_empty() {
        if full_dec.len() != data.len() {
            return Err(io_error(format!(
                "decrypted sample length mismatch: expected {}, got {}",
                data.len(),
                full_dec.len()
            )));
        }
        return Ok(full_dec);
    }

    let encrypted_total = subsamples
        .iter()
        .try_fold(0usize, |acc, (_, enc)| acc.checked_add(*enc))
        .ok_or_else(|| value_error("subsample encrypted byte count overflow"))?;
    if full_dec.len() != encrypted_total {
        return Err(io_error(format!(
            "decrypted subsample length mismatch: expected {}, got {}",
            encrypted_total,
            full_dec.len()
        )));
    }

    let mut out = Vec::with_capacity(data.len());
    let mut dec_off = 0usize;
    let mut offset = 0usize;
    for (clear_b, enc_b) in subsamples {
        let clear_end = offset
            .checked_add(*clear_b)
            .ok_or_else(|| value_error("subsample clear byte offset overflow"))?;
        if clear_end > data.len() {
            return Err(value_error("subsample clear range exceeds sample size"));
        }
        if *clear_b > 0 {
            out.extend_from_slice(&data[offset..clear_end]);
        }
        offset = clear_end;

        let dec_end = dec_off
            .checked_add(*enc_b)
            .ok_or_else(|| value_error("subsample encrypted byte offset overflow"))?;
        let enc_end = offset
            .checked_add(*enc_b)
            .ok_or_else(|| value_error("subsample encrypted range overflow"))?;
        if dec_end > full_dec.len() {
            return Err(value_error(
                "subsample decrypt range exceeds plaintext size",
            ));
        }
        if enc_end > data.len() {
            return Err(value_error("subsample encrypted range exceeds sample size"));
        }
        if *enc_b > 0 {
            out.extend_from_slice(&full_dec[dec_off..dec_end]);
        }
        dec_off = dec_end;
        offset = enc_end;
    }
    if offset < data.len() {
        out.extend_from_slice(&data[offset..]);
    }
    if out.len() != data.len() {
        return Err(io_error(format!(
            "reassembled sample length mismatch: expected {}, got {}",
            data.len(),
            out.len()
        )));
    }
    Ok(out)
}

fn tcp_decrypt_reassemble(
    host: &str,
    port: u16,
    adam_id: &[u8],
    skd_uri: &[u8],
    items: Vec<BatchItem>,
) -> PyResult<Vec<Vec<u8>>> {
    if items.is_empty() {
        return Err(value_error(
            "wrapper-v2: ciphertext batch must not be empty",
        ));
    }

    let mut stream = TcpStream::connect((host, port))
        .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt connect failed: {e}")))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(600)))
        .map_err(|e| {
            io_error(format!(
                "wrapper-v2: TCP decrypt read timeout setup failed: {e}"
            ))
        })?;
    stream
        .set_write_timeout(Some(Duration::from_secs(600)))
        .map_err(|e| {
            io_error(format!(
                "wrapper-v2: TCP decrypt write timeout setup failed: {e}"
            ))
        })?;

    stream
        .write_all(&[adam_id.len() as u8])
        .and_then(|_| stream.write_all(adam_id))
        .and_then(|_| stream.write_all(&[skd_uri.len() as u8]))
        .and_then(|_| stream.write_all(skd_uri))
        .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt header write failed: {e}")))?;

    let mut out = Vec::with_capacity(items.len());
    for (idx, (data, aligned, tail, subsamples)) in items.into_iter().enumerate() {
        if aligned.is_empty() {
            return Err(value_error(format!(
                "wrapper-v2: ciphertext sample {idx} must not be empty"
            )));
        }
        if aligned.len() > u32::MAX as usize {
            return Err(value_error(format!(
                "wrapper-v2: ciphertext sample {idx} is too large"
            )));
        }

        stream
            .write_all(&(aligned.len() as u32).to_ne_bytes())
            .and_then(|_| stream.write_all(&aligned))
            .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt sample write failed: {e}")))?;

        let mut plain = vec![0u8; aligned.len()];
        stream
            .read_exact(&mut plain)
            .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt truncated plaintext: {e}")))?;
        out.push(reassemble_sample(&data, &plain, &tail, &subsamples)?);
    }

    stream.write_all(&0u32.to_ne_bytes()).map_err(|e| {
        io_error(format!(
            "wrapper-v2: TCP decrypt terminator write failed: {e}"
        ))
    })?;

    Ok(out)
}

#[pyfunction]
fn wrapper_decrypt_reassemble(
    py: Python<'_>,
    host: String,
    port: u16,
    adam_id: String,
    skd_uri: String,
    items: Vec<BatchItem>,
) -> PyResult<Vec<Vec<u8>>> {
    let adam_id = validate_label("adam_id", &adam_id)?;
    let skd_uri = validate_label("skd_uri", &skd_uri)?;
    py.allow_threads(move || tcp_decrypt_reassemble(&host, port, &adam_id, &skd_uri, items))
}

#[pymodule]
fn _amdecrypt(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(native_available, m)?)?;
    m.add_function(wrap_pyfunction!(wrapper_decrypt_reassemble, m)?)?;
    Ok(())
}
