use pyo3::exceptions::{PyIOError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

const DECRYPT_MAGIC: u32 = 0x57563244; // WV2D
const DECRYPT_VERSION: u16 = 1;
const DECRYPT_KIND_BATCH: u16 = 1;
const DECRYPT_KIND_OK: u16 = 2;
const DECRYPT_KIND_ERROR: u16 = 3;
const DECRYPT_KIND_CLOSE: u16 = 9;

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
    if bytes.len() > u16::MAX as usize {
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

fn build_decrypt_batch_payload(
    adam_id: &[u8],
    skd_uri: &[u8],
    items: &[BatchItem],
) -> PyResult<Vec<u8>> {
    if items.is_empty() {
        return Err(value_error(
            "wrapper-v2: ciphertext batch must not be empty",
        ));
    }
    if items.len() > u32::MAX as usize {
        return Err(value_error("wrapper-v2: ciphertext batch is too large"));
    }
    let mut size = 8usize
        .checked_add(
            items
                .len()
                .checked_mul(4)
                .ok_or_else(|| value_error("wrapper-v2: decrypt batch size overflow"))?,
        )
        .and_then(|n| n.checked_add(adam_id.len()))
        .and_then(|n| n.checked_add(skd_uri.len()))
        .ok_or_else(|| value_error("wrapper-v2: decrypt batch size overflow"))?;
    for (idx, (_, aligned, _, _)) in items.iter().enumerate() {
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
        size = size
            .checked_add(aligned.len())
            .ok_or_else(|| value_error("wrapper-v2: decrypt batch size overflow"))?;
    }

    let mut out = Vec::with_capacity(size);
    out.extend_from_slice(&(adam_id.len() as u16).to_be_bytes());
    out.extend_from_slice(&(skd_uri.len() as u16).to_be_bytes());
    out.extend_from_slice(&(items.len() as u32).to_be_bytes());
    for (_, aligned, _, _) in items {
        out.extend_from_slice(&(aligned.len() as u32).to_be_bytes());
    }
    out.extend_from_slice(adam_id);
    out.extend_from_slice(skd_uri);
    for (_, aligned, _, _) in items {
        out.extend_from_slice(aligned);
    }
    Ok(out)
}

fn read_decrypt_samples_payload(data: &[u8]) -> PyResult<Vec<Vec<u8>>> {
    if data.len() < 4 {
        return Err(io_error("wrapper-v2: decrypt response too short"));
    }
    let sample_count = u32::from_be_bytes([data[0], data[1], data[2], data[3]]) as usize;
    let table_end = 4usize
        .checked_add(
            sample_count
                .checked_mul(4)
                .ok_or_else(|| io_error("wrapper-v2: decrypt response overflow"))?,
        )
        .ok_or_else(|| io_error("wrapper-v2: decrypt response overflow"))?;
    if data.len() < table_end {
        return Err(io_error("wrapper-v2: truncated decrypt length table"));
    }
    let mut lengths = Vec::with_capacity(sample_count);
    for i in 0..sample_count {
        let off = 4 + i * 4;
        lengths.push(
            u32::from_be_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]]) as usize,
        );
    }
    let mut offset = table_end;
    let mut out = Vec::with_capacity(sample_count);
    for len in lengths {
        let end = offset
            .checked_add(len)
            .ok_or_else(|| io_error("wrapper-v2: decrypt response overflow"))?;
        if end > data.len() {
            return Err(io_error("wrapper-v2: truncated plaintext sample"));
        }
        out.push(data[offset..end].to_vec());
        offset = end;
    }
    if offset != data.len() {
        return Err(io_error("wrapper-v2: trailing decrypt response bytes"));
    }
    Ok(out)
}

fn read_frame(stream: &mut TcpStream) -> PyResult<(u16, u32, Vec<u8>)> {
    let mut h = [0u8; 16];
    stream.read_exact(&mut h).map_err(|e| {
        io_error(format!(
            "wrapper-v2: TCP decrypt truncated frame header: {e}"
        ))
    })?;
    let magic = u32::from_be_bytes([h[0], h[1], h[2], h[3]]);
    let version = u16::from_be_bytes([h[4], h[5]]);
    if magic != DECRYPT_MAGIC {
        return Err(io_error("wrapper-v2: bad decrypt response magic"));
    }
    if version != DECRYPT_VERSION {
        return Err(io_error("wrapper-v2: bad decrypt response version"));
    }
    let kind = u16::from_be_bytes([h[6], h[7]]);
    let request_id = u32::from_be_bytes([h[8], h[9], h[10], h[11]]);
    let payload_len = u32::from_be_bytes([h[12], h[13], h[14], h[15]]) as usize;
    let mut payload = vec![0u8; payload_len];
    stream.read_exact(&mut payload).map_err(|e| {
        io_error(format!(
            "wrapper-v2: TCP decrypt truncated frame payload: {e}"
        ))
    })?;
    Ok((kind, request_id, payload))
}

fn write_frame(stream: &mut TcpStream, kind: u16, request_id: u32, payload: &[u8]) -> PyResult<()> {
    if payload.len() > u32::MAX as usize {
        return Err(value_error("wrapper-v2: decrypt frame is too large"));
    }
    stream
        .write_all(&DECRYPT_MAGIC.to_be_bytes())
        .and_then(|_| stream.write_all(&DECRYPT_VERSION.to_be_bytes()))
        .and_then(|_| stream.write_all(&kind.to_be_bytes()))
        .and_then(|_| stream.write_all(&request_id.to_be_bytes()))
        .and_then(|_| stream.write_all(&(payload.len() as u32).to_be_bytes()))
        .and_then(|_| stream.write_all(payload))
        .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt frame write failed: {e}")))
}

#[pyclass]
pub struct WrapperDecryptSession {
    stream: Option<TcpStream>,
    next_request_id: u32,
}

#[pymethods]
impl WrapperDecryptSession {
    #[new]
    fn new(host: String, port: u16) -> PyResult<Self> {
        let stream = TcpStream::connect((host.as_str(), port))
            .map_err(|e| io_error(format!("wrapper-v2: TCP decrypt connect failed: {e}")))?;
        stream
            .set_nodelay(true)
            .map_err(|e| io_error(format!("wrapper-v2: TCP_NODELAY setup failed: {e}")))?;
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
        Ok(Self {
            stream: Some(stream),
            next_request_id: 1,
        })
    }

    fn decrypt_reassemble(
        &mut self,
        py: Python<'_>,
        adam_id: String,
        skd_uri: String,
        items: Vec<BatchItem>,
    ) -> PyResult<Vec<Vec<u8>>> {
        let adam_id = validate_label("adam_id", &adam_id)?;
        let skd_uri = validate_label("skd_uri", &skd_uri)?;
        let request_id = self.next_request_id;
        self.next_request_id = self.next_request_id.wrapping_add(1).max(1);
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("wrapper-v2: decrypt session is closed"))?;
        py.detach(move || {
            let payload = build_decrypt_batch_payload(&adam_id, &skd_uri, &items)?;
            write_frame(stream, DECRYPT_KIND_BATCH, request_id, &payload)?;
            let (kind, response_id, response_payload) = read_frame(stream)?;
            if response_id != request_id {
                return Err(io_error("wrapper-v2: mismatched decrypt response id"));
            }
            if kind == DECRYPT_KIND_ERROR {
                return Err(io_error(format!(
                    "wrapper-v2: decrypt failed: {}",
                    String::from_utf8_lossy(&response_payload)
                )));
            }
            if kind != DECRYPT_KIND_OK {
                return Err(io_error("wrapper-v2: unexpected decrypt response kind"));
            }
            let plains = read_decrypt_samples_payload(&response_payload)?;
            if plains.len() != items.len() {
                return Err(io_error(format!(
                    "wrapper-v2: expected {} plaintexts, got {}",
                    items.len(),
                    plains.len()
                )));
            }
            let mut out = Vec::with_capacity(items.len());
            for ((data, _, tail, subsamples), plain) in items.into_iter().zip(plains) {
                out.push(reassemble_sample(&data, &plain, &tail, &subsamples)?);
            }
            Ok(out)
        })
    }

    fn close(&mut self) -> PyResult<()> {
        if let Some(mut stream) = self.stream.take() {
            let _ = write_frame(&mut stream, DECRYPT_KIND_CLOSE, 0, &[]);
        }
        Ok(())
    }
}

impl Drop for WrapperDecryptSession {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
