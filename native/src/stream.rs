//! Bounded HLS fragment fetching and progressive HTTP transfers for Streamer.
//! Partial/failed responses are never sent downstream: retries always restart
//! an entire fragment, preventing duplicated bytes after an interrupted request.

use std::ops::Range;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
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
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore, mpsc};
use tokio::task::AbortHandle;

const MAX_PART_SIZE: usize = 64 * 1024 * 1024;
// Process-wide payload capacity across concurrent HLS streams. Unknown-size
// parts reserve their maximum before admission; completed parts release slack.
const MAX_BUFFERED_BYTES: usize = 256 * 1024 * 1024;
static PAYLOAD_BUDGET: OnceLock<Arc<Semaphore>> = OnceLock::new();
const UNKNOWN_TOTAL: u64 = u64::MAX;

type Part = (String, Option<u64>, Option<u64>);
type DownloadResult = Result<Chunk, &'static str>;

struct Fragment {
    bytes: Vec<u8>,
    // Retain the reservation until Python has copied the final queued slice.
    _reservation: Option<OwnedSemaphorePermit>,
}

struct Chunk {
    fragment: Arc<Fragment>,
    range: Range<usize>,
}

impl Chunk {
    fn owned(bytes: Vec<u8>) -> Self {
        let range = 0..bytes.len();
        Self {
            fragment: Arc::new(Fragment {
                bytes,
                _reservation: None,
            }),
            range,
        }
    }

    fn as_slice(&self) -> &[u8] {
        &self.fragment.bytes[self.range.clone()]
    }
}

fn reservation_size(part: &Part) -> usize {
    match (part.1, part.2) {
        (Some(start), Some(end)) => (end - start + 1) as usize,
        _ => MAX_PART_SIZE,
    }
}

fn reserve_body(body: &mut Vec<u8>, required: usize, limit: usize) -> Result<(), &'static str> {
    if required > limit {
        return Err("Streaming fragment exceeds size limit");
    }
    if required > body.capacity() {
        let target = required.max(body.capacity().saturating_mul(2)).min(limit);
        body.try_reserve_exact(target - body.len())
            .map_err(|_| "Unable to allocate streaming fragment")?;
    }
    Ok(())
}

struct ProgressCounters {
    transferred: AtomicU64,
    emitted: AtomicU64,
    completed: AtomicUsize,
    total_parts: usize,
    total_bytes: AtomicU64,
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

fn http_client(progressive: bool) -> PyResult<Client> {
    let builder = Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .read_timeout(Duration::from_secs(40))
        .redirect(redirect::Policy::custom(|attempt| {
            if attempt.previous().len() < 8 && valid_url(attempt.url()) {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }));
    // HLS fragments have a bounded lifetime; a progressive file may transfer
    // for hours, but must still fail if the server stops sending data.
    let builder = if progressive {
        builder
    } else {
        builder.timeout(Duration::from_secs(40))
    };
    builder
        .build()
        .map_err(|_| PyOSError::new_err("Unable to initialize streaming HTTP client"))
}

async fn fetch_progressive(
    client: Client,
    url: String,
    retries: u32,
    chunk_size: usize,
    progress: Arc<ProgressCounters>,
    sender: mpsc::Sender<DownloadResult>,
) {
    let mut attempt = 0;
    loop {
        let response = match client.get(&url).send().await {
            Ok(response) => response,
            Err(_) if attempt < retries => {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            Err(_) => {
                let _ = sender
                    .send(Err("Streaming request failed after retries"))
                    .await;
                return;
            }
        };
        if response.status() != StatusCode::OK {
            if (response.status() == StatusCode::TOO_MANY_REQUESTS
                || response.status().is_server_error()
                || response.status() == StatusCode::REQUEST_TIMEOUT)
                && attempt < retries
            {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            let _ = sender
                .send(Err("Streaming server returned an unexpected HTTP status"))
                .await;
            return;
        }

        let expected = response.content_length();
        if let Some(length) = expected {
            progress.total_bytes.store(length, Ordering::Relaxed);
        }
        let mut response = response;
        let mut pending = Vec::with_capacity(chunk_size);
        let mut received = 0u64;
        let mut emitted = false;
        let mut failed = false;
        loop {
            match response.chunk().await {
                Ok(Some(bytes)) => {
                    received = received.saturating_add(bytes.len() as u64);
                    progress
                        .transferred
                        .fetch_add(bytes.len() as u64, Ordering::Relaxed);
                    if expected.is_some_and(|length| received > length) {
                        failed = true;
                        break;
                    }
                    let mut remaining = bytes.as_ref();
                    while !remaining.is_empty() {
                        let take = (chunk_size - pending.len()).min(remaining.len());
                        pending.extend_from_slice(&remaining[..take]);
                        remaining = &remaining[take..];
                        if pending.len() == chunk_size {
                            emitted = true;
                            if sender
                                .send(Ok(Chunk::owned(std::mem::take(&mut pending))))
                                .await
                                .is_err()
                            {
                                return;
                            }
                        }
                    }
                }
                Ok(None) => break,
                Err(_) => {
                    failed = true;
                    break;
                }
            }
        }
        if failed || expected.is_some_and(|length| length != received) || received == 0 {
            // Never retry after publishing bytes: that would duplicate the
            // beginning of a progressive response or silently corrupt output.
            if !emitted && attempt < retries {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            let _ = sender
                .send(Err("Progressive streaming response was incomplete"))
                .await;
            return;
        }
        if !pending.is_empty() && sender.send(Ok(Chunk::owned(pending))).await.is_err() {
            return;
        }
        progress.completed.fetch_add(1, Ordering::Relaxed);
        return;
    }
}

async fn fetch_part(
    client: Client,
    part: Part,
    retries: u32,
    progress: Arc<ProgressCounters>,
) -> Result<Vec<u8>, &'static str> {
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
        let limit = range.map_or(MAX_PART_SIZE, |(a, b)| (b - a + 1) as usize);
        let expected = range.map(|(a, b)| b - a + 1).or(response.content_length());
        let mut body = Vec::new();
        if let Some(size) = expected {
            let size =
                usize::try_from(size).map_err(|_| "Streaming fragment exceeds size limit")?;
            reserve_body(&mut body, size, limit)?;
        }
        let mut reply = response;
        let mut failed = false;
        loop {
            match reply.chunk().await {
                Ok(Some(bytes)) => {
                    progress
                        .transferred
                        .fetch_add(bytes.len() as u64, Ordering::Relaxed);
                    if bytes.len() > limit.saturating_sub(body.len()) {
                        return Err("Streaming fragment exceeds size limit");
                    }
                    let required = body.len() + bytes.len();
                    reserve_body(&mut body, required, limit)?;
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
            // Do not hold a partially received allocation during backoff.
            drop(body);
            if attempt < retries {
                tokio::time::sleep(Duration::from_millis(150 * (1 << attempt))).await;
                attempt += 1;
                continue;
            }
            return Err("Streaming response interrupted after retries");
        }
        if let Some((a, b)) = range {
            if body.len() as u64 != b - a + 1 {
                drop(body);
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
    #[pyo3(signature = (parts, concurrency=8, chunk_size=262144, retries=3, decryption_key=None, key_id=None, enhanced_hls_song=false, wrapper=None, asset_song_cbcs=false, music_video_cbcs=false))]
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
        music_video_cbcs: bool,
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
            music_video_cbcs,
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
        let client = http_client(false)?;
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
            total_bytes: AtomicU64::new(total_bytes.unwrap_or(UNKNOWN_TOTAL)),
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
            let budget = PAYLOAD_BUDGET
                .get_or_init(|| Arc::new(Semaphore::new(MAX_BUFFERED_BYTES)))
                .clone();
            // Reserve in manifest order, *before* placing a fetch into the
            // ordered buffer. Acquiring inside fetches could let later parts
            // exhaust the budget and deadlock an earlier part awaiting space.
            let admitted = stream::iter(parts).then(move |part| {
                let budget = budget.clone();
                async move {
                    let permit = budget
                        .acquire_many_owned(reservation_size(&part) as u32)
                        .await
                        .expect("payload budget is never closed");
                    (part, permit)
                }
            });
            let tasks = admitted.map(|(part, mut permit)| {
                let client = client.clone();
                let progress = Arc::clone(&worker_progress);
                async move {
                    let bytes = fetch_part(client, part, retries, progress).await?;
                    let spare = permit
                        .num_permits()
                        .checked_sub(bytes.capacity())
                        .ok_or("Streaming allocation exceeds reservation")?;
                    drop(permit.split(spare));
                    Ok::<_, &'static str>((bytes, permit))
                }
            });
            let ready = tasks.buffered(concurrency);
            futures_util::pin_mut!(ready);
            while let Some(result) = ready.next().await {
                match result {
                    Ok((mut bytes, permit)) => {
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
                        let fragment = Arc::new(Fragment {
                            bytes,
                            _reservation: Some(permit),
                        });
                        for start in (0..fragment.bytes.len()).step_by(chunk_size) {
                            let chunk = Chunk {
                                fragment: Arc::clone(&fragment),
                                range: start..(start + chunk_size).min(fragment.bytes.len()),
                            };
                            if sender.send(Ok(chunk)).await.is_err() {
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

    /// Stream a single progressive file without the 64-MiB HLS part limit.
    /// No range splitting or MP4 decryption: it is an ordered raw HTTP body.
    #[staticmethod]
    #[pyo3(signature = (url, chunk_size=262144, retries=3))]
    fn progressive(url: String, chunk_size: usize, retries: u32) -> PyResult<Self> {
        let parsed = Url::parse(&url)
            .map_err(|_| PyValueError::new_err("Invalid streaming resource URL"))?;
        if !valid_url(&parsed) {
            return Err(PyValueError::new_err("Streaming resources must use HTTPS"));
        }
        if !(1024..=4 * 1024 * 1024).contains(&chunk_size) {
            return Err(PyValueError::new_err(
                "chunk_size must be between 1024 and 4194304",
            ));
        }
        if retries > 8 {
            return Err(PyValueError::new_err("retries must not exceed 8"));
        }
        let client = http_client(true)?;
        let progress = Arc::new(ProgressCounters {
            transferred: AtomicU64::new(0),
            emitted: AtomicU64::new(0),
            completed: AtomicUsize::new(0),
            total_parts: 1,
            total_bytes: AtomicU64::new(UNKNOWN_TOTAL),
            started: Instant::now(),
        });
        let (sender, receiver) = mpsc::channel(2);
        let job = pyo3_async_runtimes::tokio::get_runtime().spawn(fetch_progressive(
            client,
            url,
            retries,
            chunk_size,
            Arc::clone(&progress),
            sender,
        ));
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
                        .fetch_add(bytes.as_slice().len() as u64, Ordering::Relaxed);
                    Python::attach(|py| Ok(PyBytes::new(py, bytes.as_slice()).into_any().unbind()))
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
            match self.progress.total_bytes.load(Ordering::Relaxed) {
                UNKNOWN_TOTAL => None,
                count => Some(count),
            },
            self.progress.started.elapsed().as_secs_f64(),
        )
    }
}

impl Drop for NativeStream {
    fn drop(&mut self) {
        self.abort_handle.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn growth_never_exceeds_the_reserved_fragment_limit() {
        let mut body = Vec::new();
        for size in [100, 1000, 1900, 4000, 4096] {
            reserve_body(&mut body, size, 4096).unwrap();
            body.resize(size, 7);
            assert!(body.capacity() <= 4096);
        }
        assert!(reserve_body(&mut body, 4097, 4096).is_err());
        assert_eq!(body, vec![7; 4096]);
    }

    #[test]
    fn queued_slices_hold_the_payload_budget_until_the_last_copy() {
        let budget = Arc::new(Semaphore::new(8));
        let reservation = budget.clone().try_acquire_many_owned(8).unwrap();
        let fragment = Arc::new(Fragment {
            bytes: b"abcdefgh".to_vec(),
            _reservation: Some(reservation),
        });
        let first = Chunk {
            fragment: fragment.clone(),
            range: 0..4,
        };
        let last = Chunk {
            fragment: fragment.clone(),
            range: 4..8,
        };
        drop(fragment);
        assert_eq!(first.as_slice(), b"abcd");
        drop(first);
        assert_eq!(budget.available_permits(), 0);
        assert_eq!(last.as_slice(), b"efgh");
        drop(last);
        assert_eq!(budget.available_permits(), 8);
    }
}
