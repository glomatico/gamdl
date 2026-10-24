//! Bounded, ordered, retryable HTTP fragment fetching for Python's Stream API.
//! Partial/failed responses are never sent downstream: retries always restart
//! an entire fragment, preventing duplicated bytes after an interrupted request.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::Duration;
use std::time::Instant;

use crate::decrypt::Mp4Decryptor;
use crate::wrapper::WrapperSession;
use futures_util::{StreamExt, stream};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use reqwest::header::{CONTENT_LENGTH, CONTENT_RANGE, RANGE};
use reqwest::{Client, StatusCode, Url, redirect};
use tokio::sync::{Mutex, mpsc};
use tokio::task::AbortHandle;

const MAX_PART_SIZE: usize = 64 * 1024 * 1024;

type Part = (String, Option<u64>, Option<u64>);
type DownloadResult = Result<Vec<u8>, &'static str>;

struct ProgressCounters {
    transferred: AtomicU64,
    emitted: AtomicU64,
    completed: AtomicUsize,
    total_parts: usize,
    total_bytes: Option<u64>,
    started: Instant,
}

fn valid_url(url: &Url) -> bool {
    if url.username() != "" || url.password().is_some() || url.fragment().is_some() {
        return false;
    }
    match url.scheme() {
        "https" => true,
        "http" => matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "::1")),
        _ => false,
    }
}

async fn fetch_part(
    client: Client,
    part: Part,
    retries: u32,
    progress: Arc<ProgressCounters>,
) -> DownloadResult {
    let (url, start, end) = part;
    let range = match (start, end) {
        (Some(a), Some(b)) => Some((a, b)),
        _ => None,
    };
    let mut attempt = 0;
    loop {
        let mut req = client.get(&url);
        if let Some((a, b)) = range {
            req = req.header(RANGE, format!("bytes={a}-{b}"));
        }
        let response = match req.send().await {
            Ok(reply) => reply,
            Err(_) if attempt < retries => {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            Err(_) => return Err("Streaming request failed after retries"),
        };
        let expected_status = if range.is_some() {
            StatusCode::PARTIAL_CONTENT
        } else {
            StatusCode::OK
        };
        if response.status() != expected_status {
            if (response.status() == StatusCode::TOO_MANY_REQUESTS
                || response.status().is_server_error()
                || response.status() == StatusCode::REQUEST_TIMEOUT)
                && attempt < retries
            {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            return Err("Streaming server returned an unexpected HTTP status");
        }
        if let Some((a, b)) = range {
            let valid = response
                .headers()
                .get(CONTENT_RANGE)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.strip_prefix("bytes "))
                .and_then(|v| v.split_once('/'))
                .and_then(|(actual, _)| actual.split_once('-'))
                .is_some_and(|(lo, hi)| {
                    lo.parse::<u64>().ok() == Some(a) && hi.parse::<u64>().ok() == Some(b)
                });
            if !valid {
                return Err("Streaming server returned an invalid Content-Range");
            }
        }
        if response
            .headers()
            .get(CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u64>().ok())
            .is_some_and(|n| n > MAX_PART_SIZE as u64)
        {
            return Err("Streaming fragment exceeds size limit");
        }
        let mut body = Vec::new();
        let mut reply = response;
        let mut failed = false;
        loop {
            match reply.chunk().await {
                Ok(Some(bytes)) => {
                    progress
                        .transferred
                        .fetch_add(bytes.len() as u64, Ordering::Relaxed);
                    if bytes.len() > MAX_PART_SIZE.saturating_sub(body.len()) {
                        return Err("Streaming fragment exceeds size limit");
                    }
                    body.extend_from_slice(&bytes);
                }
                Ok(None) => break,
                Err(_) => {
                    failed = true;
                    break;
                }
            }
        }
        if failed {
            if attempt < retries {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            return Err("Streaming response interrupted after retries");
        }
        if let Some((a, b)) = range {
            if body.len() as u64 != b - a + 1 {
                if attempt < retries {
                    tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                    attempt += 1;
                    continue;
                }
                return Err("Streaming byte range was incomplete");
            }
        }
        if body.is_empty() {
            return Err("Streaming fragment was empty");
        }
        return Ok(body);
    }
}

#[pyclass]
pub(crate) struct NativeStream {
    receiver: Arc<Mutex<mpsc::Receiver<DownloadResult>>>,
    abort_handle: AbortHandle,
    progress: Arc<ProgressCounters>,
}

#[pymethods]
impl NativeStream {
    #[new]
    #[pyo3(signature = (parts, concurrency=8, chunk_size=262144, retries=3, decryption_key=None, key_id=None, enhanced_hls_song=false, wrapper=None, asset_song_cbcs=false))]
    fn new(
        parts: Vec<Part>,
        concurrency: usize,
        chunk_size: usize,
        retries: u32,
        decryption_key: Option<Vec<u8>>,
        key_id: Option<Vec<u8>>,
        enhanced_hls_song: bool,
        wrapper: Option<(String, u16, String, String)>,
        asset_song_cbcs: bool,
    ) -> PyResult<Self> {
        if wrapper.is_some() && (decryption_key.is_some() || key_id.is_some()) {
            return Err(PyValueError::new_err(
                "Wrapper and AES key modes are mutually exclusive",
            ));
        }
        if let Some((ref host, port, ref media_id, ref skd)) = wrapper {
            if host.trim().is_empty()
                || port == 0
                || media_id.is_empty()
                || media_id.len() > u16::MAX as usize
                || !skd.starts_with("skd://")
                || skd.len() <= 6
                || skd.len() > u16::MAX as usize
            {
                return Err(PyValueError::new_err(
                    "Invalid wrapper sample delivery configuration",
                ));
            }
        }
        let decryptor = Mp4Decryptor::new(
            decryption_key,
            key_id,
            enhanced_hls_song,
            wrapper.is_some(),
            asset_song_cbcs,
        )
        .map_err(PyValueError::new_err)?;
        if parts.is_empty() || parts.len() > 100_000 {
            return Err(PyValueError::new_err(
                "Expected between 1 and 100000 streaming parts",
            ));
        }
        if !(1..=32).contains(&concurrency) {
            return Err(PyValueError::new_err(
                "concurrency must be between 1 and 32",
            ));
        }
        if !(1024..=4 * 1024 * 1024).contains(&chunk_size) {
            return Err(PyValueError::new_err(
                "chunk_size must be between 1024 and 4194304",
            ));
        }
        if retries > 8 {
            return Err(PyValueError::new_err("retries must not exceed 8"));
        }
        for (url, start, end) in &parts {
            let parsed = Url::parse(url)
                .map_err(|_| PyValueError::new_err("Invalid streaming resource URL"))?;
            if !valid_url(&parsed) {
                return Err(PyValueError::new_err("Streaming resources must use HTTPS"));
            }
            if start.is_some() != end.is_some() || start > end {
                return Err(PyValueError::new_err("Invalid streaming byte range"));
            }
            if start.is_some_and(|a| end.is_some_and(|b| b - a >= MAX_PART_SIZE as u64)) {
                return Err(PyValueError::new_err(
                    "Streaming byte range exceeds size limit",
                ));
            }
        }
        let client = Client::builder()
            .timeout(Duration::from_secs(40))
            .redirect(redirect::Policy::custom(|attempt| {
                if attempt.previous().len() < 8 && valid_url(attempt.url()) {
                    attempt.follow()
                } else {
                    attempt.stop()
                }
            }))
            .build()
            .map_err(|_| PyOSError::new_err("Unable to initialize streaming HTTP client"))?;
        let total_bytes = parts.iter().try_fold(0u64, |sum, (_, start, end)| {
            let (Some(start), Some(end)) = (start, end) else {
                return None;
            };
            sum.checked_add(end - start + 1)
        });
        let progress = Arc::new(ProgressCounters {
            transferred: AtomicU64::new(0),
            emitted: AtomicU64::new(0),
            completed: AtomicUsize::new(0),
            total_parts: parts.len(),
            total_bytes,
            started: Instant::now(),
        });
        let (sender, receiver) = mpsc::channel(2);
        let worker_progress = Arc::clone(&progress);
        let job = pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            let mut decryptor = decryptor;
            let mut wrapper_session = if let Some((ref host, port, _, _)) = wrapper {
                match WrapperSession::connect(host, port).await {
                    Ok(session) => Some(session),
                    Err(error) => {
                        let _ = sender.send(Err(error)).await;
                        return;
                    }
                }
            } else {
                None
            };
            let tasks = stream::iter(parts).map(|part| {
                let client = client.clone();
                let progress = Arc::clone(&worker_progress);
                async move { fetch_part(client, part, retries, progress).await }
            });
            let mut ready = tasks.buffered(concurrency);
            while let Some(result) = ready.next().await {
                match result {
                    Ok(mut bytes) => {
                        if let Some(ref mut handler) = decryptor {
                            let media_id = wrapper.as_ref().map(|w| w.2.as_str()).unwrap_or("");
                            let fairplay_key = wrapper.as_ref().map(|w| w.3.as_str()).unwrap_or("");
                            if let Err(error) = handler
                                .decrypt_part_with_wrapper(
                                    &mut bytes,
                                    wrapper_session.as_mut(),
                                    media_id,
                                    fairplay_key,
                                )
                                .await
                            {
                                let _ = sender.send(Err(error)).await;
                                return;
                            }
                        }
                        for chunk in bytes.chunks(chunk_size) {
                            if sender.send(Ok(chunk.to_vec())).await.is_err() {
                                return;
                            }
                        }
                        worker_progress.completed.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(error) => {
                        let _ = sender.send(Err(error)).await;
                        return;
                    }
                }
            }
            if let Some(ref mut session) = wrapper_session {
                session.finish().await;
            }
        });
        Ok(Self {
            receiver: Arc::new(Mutex::new(receiver)),
            abort_handle: job.abort_handle(),
            progress,
        })
    }

    fn next_chunk<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let receiver = self.receiver.clone();
        let progress = Arc::clone(&self.progress);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = receiver.lock().await.recv().await;
            match result {
                Some(Ok(bytes)) => {
                    progress
                        .emitted
                        .fetch_add(bytes.len() as u64, Ordering::Relaxed);
                    Python::attach(|py| Ok(PyBytes::new(py, &bytes).into_any().unbind()))
                }
                Some(Err(msg)) => Err(PyOSError::new_err(msg)),
                None => Python::attach(|py| Ok(py.None())),
            }
        })
    }

    fn close(&self) {
        self.abort_handle.abort();
    }

    fn progress(&self) -> (u64, u64, usize, usize, Option<u64>, f64) {
        (
            self.progress.transferred.load(Ordering::Relaxed),
            self.progress.emitted.load(Ordering::Relaxed),
            self.progress.completed.load(Ordering::Relaxed),
            self.progress.total_parts,
            self.progress.total_bytes,
            self.progress.started.elapsed().as_secs_f64(),
        )
    }
}

impl Drop for NativeStream {
    fn drop(&mut self) {
        self.abort_handle.abort();
    }
}
