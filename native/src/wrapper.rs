//! Native asynchronous wrapper-v2 sample delivery.

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{Duration, timeout};

const MAGIC: u32 = 0x5756_3244;
const VERSION: u16 = 1;
const BATCH: u16 = 1;
const OK: u16 = 2;
const ERROR: u16 = 3;
const CLOSE: u16 = 9;
pub(crate) const BATCH_SIZE: usize = 128;
const MAX_BATCH_BYTES: usize = 64 * 1024 * 1024;

pub(crate) struct WrapperSession {
    socket: TcpStream,
    next_id: u32,
}

impl WrapperSession {
    pub(crate) async fn connect(host: &str, port: u16) -> Result<Self, &'static str> {
        let socket = timeout(Duration::from_secs(15), TcpStream::connect((host, port)))
            .await
            .map_err(|_| "Wrapper connection timed out")?
            .map_err(|_| "Wrapper connection failed")?;
        Ok(Self { socket, next_id: 1 })
    }

    pub(crate) async fn decrypt_batch(
        &mut self,
        media_id: &str,
        fairplay_key: &str,
        ciphertexts: &[Vec<u8>],
    ) -> Result<Vec<Vec<u8>>, &'static str> {
        if ciphertexts.is_empty()
            || ciphertexts.len() > BATCH_SIZE
            || media_id.is_empty()
            || fairplay_key.is_empty()
            || media_id.len() > u16::MAX as usize
            || fairplay_key.len() > u16::MAX as usize
        {
            return Err("Invalid wrapper sample batch");
        }
        let payload_size = ciphertexts
            .iter()
            .try_fold(
                8usize + 4 * ciphertexts.len() + media_id.len() + fairplay_key.len(),
                |total, data| total.checked_add(data.len()),
            )
            .ok_or("Wrapper sample batch size overflow")?;
        if payload_size > MAX_BATCH_BYTES {
            return Err("Wrapper sample batch exceeds size limit");
        }
        let mut payload = Vec::new();
        payload.extend_from_slice(&(media_id.len() as u16).to_be_bytes());
        payload.extend_from_slice(&(fairplay_key.len() as u16).to_be_bytes());
        payload.extend_from_slice(&(ciphertexts.len() as u32).to_be_bytes());
        for data in ciphertexts {
            payload.extend_from_slice(&(data.len() as u32).to_be_bytes());
        }
        payload.extend_from_slice(media_id.as_bytes());
        payload.extend_from_slice(fairplay_key.as_bytes());
        for data in ciphertexts {
            payload.extend_from_slice(data);
        }

        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1).max(1);
        let mut header = Vec::with_capacity(16);
        header.extend_from_slice(&MAGIC.to_be_bytes());
        header.extend_from_slice(&VERSION.to_be_bytes());
        header.extend_from_slice(&BATCH.to_be_bytes());
        header.extend_from_slice(&id.to_be_bytes());
        header.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        timeout(Duration::from_secs(120), async {
            self.socket.write_all(&header).await?;
            self.socket.write_all(&payload).await?;
            self.socket.flush().await
        })
        .await
        .map_err(|_| "Wrapper delivery timed out")?
        .map_err(|_| "Wrapper delivery failed")?;
        let mut response = [0u8; 16];
        timeout(
            Duration::from_secs(120),
            self.socket.read_exact(&mut response),
        )
        .await
        .map_err(|_| "Wrapper reply timed out")?
        .map_err(|_| "Wrapper reply truncated")?;
        if u32::from_be_bytes(response[0..4].try_into().unwrap()) != MAGIC
            || u16::from_be_bytes(response[4..6].try_into().unwrap()) != VERSION
            || u32::from_be_bytes(response[8..12].try_into().unwrap()) != id
        {
            return Err("Invalid wrapper response frame");
        }
        let len = u32::from_be_bytes(response[12..16].try_into().unwrap()) as usize;
        if len > MAX_BATCH_BYTES {
            return Err("Wrapper response too large");
        }
        let mut body = vec![0u8; len];
        timeout(Duration::from_secs(120), self.socket.read_exact(&mut body))
            .await
            .map_err(|_| "Wrapper response timed out")?
            .map_err(|_| "Wrapper response incomplete")?;
        let kind = u16::from_be_bytes(response[6..8].try_into().unwrap());
        if kind == ERROR {
            return Err("Wrapper rejected sample batch");
        }
        if kind != OK || body.len() < 4 {
            return Err("Invalid wrapper decrypt response");
        }
        let count = u32::from_be_bytes(body[0..4].try_into().unwrap()) as usize;
        if count != ciphertexts.len() || body.len() < 4 + 4 * count {
            return Err("Wrapper reply count mismatch");
        }
        let mut off = 4 + 4 * count;
        let mut result = Vec::new();
        for (i, ciphertext) in ciphertexts.iter().enumerate() {
            let size = u32::from_be_bytes(body[4 + 4 * i..8 + 4 * i].try_into().unwrap()) as usize;
            if size != ciphertext.len() || off + size > body.len() {
                return Err("Wrapper decrypted sample size mismatch");
            }
            result.push(body[off..off + size].to_vec());
            off += size;
        }
        if off != body.len() {
            return Err("Wrapper response trailing bytes");
        }
        Ok(result)
    }

    pub(crate) async fn finish(&mut self) {
        let mut frame = Vec::with_capacity(16);
        frame.extend_from_slice(&MAGIC.to_be_bytes());
        frame.extend_from_slice(&VERSION.to_be_bytes());
        frame.extend_from_slice(&CLOSE.to_be_bytes());
        frame.extend_from_slice(&0u32.to_be_bytes());
        frame.extend_from_slice(&0u32.to_be_bytes());
        let _ = self.socket.write_all(&frame).await;
    }
}
