//! Sparse MP4 metadata at original file offsets, without mapping media payloads.
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::ops::Range;

/// Both byte APIs and file APIs use the same parsers and offset validation.
pub(crate) trait Input {
    fn len(&self) -> usize;
    fn slice(&self, range: Range<usize>) -> Option<&[u8]>;
    fn byte(&self, at: usize) -> Option<&u8> {
        self.slice(at..at.checked_add(1)?)?.first()
    }
}

impl Input for [u8] {
    fn len(&self) -> usize {
        self.len()
    }
    fn slice(&self, range: Range<usize>) -> Option<&[u8]> {
        self.get(range)
    }
}

impl Input for Vec<u8> {
    fn len(&self) -> usize {
        self.len()
    }
    fn slice(&self, range: Range<usize>) -> Option<&[u8]> {
        self.as_slice().get(range)
    }
}

pub(crate) struct Metadata {
    len: usize,
    blocks: Vec<(usize, Vec<u8>)>,
}

impl Metadata {
    pub(crate) fn read(file: &mut File) -> io::Result<Self> {
        const MAX_BOX: usize = 64 * 1024 * 1024;
        const MAX_TOTAL: usize = 256 * 1024 * 1024;
        let invalid = || {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "Malformed or oversized MP4 metadata",
            )
        };
        let len = usize::try_from(file.metadata()?.len()).map_err(|_| invalid())?;
        let mut blocks = Vec::new();
        let mut at = 0usize;
        let mut total = 0usize;
        while at < len {
            if len - at < 8 {
                return Err(invalid());
            }
            file.seek(SeekFrom::Start(at as u64))?;
            let mut header = [0u8; 16];
            file.read_exact(&mut header[..8])?;
            let short = u32::from_be_bytes(header[..4].try_into().unwrap());
            let (size, header_len) = match short {
                0 => (len - at, 8),
                1 => {
                    if len - at < 16 {
                        return Err(invalid());
                    }
                    file.read_exact(&mut header[8..])?;
                    (
                        usize::try_from(u64::from_be_bytes(header[8..].try_into().unwrap()))
                            .map_err(|_| invalid())?,
                        16,
                    )
                }
                size => (size as usize, 8),
            };
            if size < header_len || size > len - at {
                return Err(invalid());
            }
            // All other top-level boxes, including mdat/free, need only a
            // header so the shared parser can skip to the next original offset.
            let keep = if matches!(&header[4..8], b"moov" | b"moof") {
                if size > MAX_BOX {
                    return Err(invalid());
                }
                size
            } else {
                header_len
            };
            let cost = keep + std::mem::size_of::<(usize, Vec<u8>)>();
            total = total
                .checked_add(cost)
                .filter(|n| *n <= MAX_TOTAL)
                .ok_or_else(invalid)?;
            let mut bytes = vec![0; keep];
            bytes[..header_len].copy_from_slice(&header[..header_len]);
            file.read_exact(&mut bytes[header_len..])?;
            blocks.push((at, bytes));
            at += size;
        }
        Ok(Self { len, blocks })
    }
}

impl Input for Metadata {
    fn len(&self) -> usize {
        self.len
    }
    fn slice(&self, range: Range<usize>) -> Option<&[u8]> {
        let index = self
            .blocks
            .partition_point(|(start, _)| *start <= range.start)
            .checked_sub(1)?;
        let (start, bytes) = &self.blocks[index];
        bytes.get(range.start.checked_sub(*start)?..range.end.checked_sub(*start)?)
    }
}

pub(crate) fn read_metadata(file: &mut File) -> pyo3::PyResult<Metadata> {
    Metadata::read(file).map_err(|error| {
        if matches!(
            error.kind(),
            io::ErrorKind::InvalidData | io::ErrorKind::UnexpectedEof
        ) {
            pyo3::exceptions::PyValueError::new_err(error.to_string())
        } else {
            pyo3::exceptions::PyOSError::new_err(error)
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn sparse_payload_is_not_loaded() {
        // Sparse temporary file; no repository fixture needed.
        let path = std::env::temp_dir().join(format!("gamdl-metadata-{}", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        file.write_all(&[0, 0, 0, 1, b'm', b'd', b'a', b't'])
            .unwrap();
        let size = 512u64 * 1024 * 1024;
        file.write_all(&size.to_be_bytes()).unwrap();
        file.set_len(size).unwrap();
        let metadata = Metadata::read(&mut file).unwrap();
        assert_eq!(metadata.len(), size as usize);
        assert_eq!(metadata.blocks[0].1.len(), 16);
        assert!(metadata.slice(16..17).is_none());
        assert_eq!(metadata.slice(4..8), Some(b"mdat".as_slice()));
        drop(file);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn sparse_boxes_preserve_absolute_offsets() {
        let path =
            std::env::temp_dir().join(format!("gamdl-metadata-boxes-{}", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        file.write_all(&[0, 0, 0, 8, b'f', b't', b'y', b'p'])
            .unwrap();
        file.write_all(&[0, 0, 0, 12, b'm', b'o', b'o', b'v', 1, 2, 3, 4])
            .unwrap();
        file.write_all(&[0, 0, 0, 1, b'm', b'd', b'a', b't'])
            .unwrap();
        let payload_size = 1024 * 1024u64;
        file.write_all(&(payload_size + 16).to_be_bytes()).unwrap();
        file.seek(SeekFrom::Current(payload_size as i64)).unwrap();
        let fragment_start = 20 + 16 + payload_size as usize;
        file.write_all(&[0, 0, 0, 12, b'm', b'o', b'o', b'f', 5, 6, 7, 8])
            .unwrap();

        let metadata = Metadata::read(&mut file).unwrap();
        assert_eq!(metadata.len(), fragment_start + 12);
        assert_eq!(metadata.blocks.len(), 4);
        assert_eq!(
            metadata.slice(8..20),
            Some(&[0, 0, 0, 12, b'm', b'o', b'o', b'v', 1, 2, 3, 4][..])
        );
        assert_eq!(metadata.slice(24..28), Some(b"mdat".as_slice()));
        assert!(metadata.slice(36..37).is_none());
        assert_eq!(
            metadata.slice(fragment_start + 8..fragment_start + 12),
            Some(&[5, 6, 7, 8][..])
        );
        drop(file);
        std::fs::remove_file(path).unwrap();
    }
}
