//! Size-preserving ALAC END repair, inspired by zhaarey/apple-music-downloader's
//! alac-fix. Scan syntax, not PCM: no predictor reconstruction or re-encoding.
//! See docs/alac-repair.md for provenance, supported syntax and guardrails.

// Bound both file-backed scratch space and work on untrusted packets/configs.
pub(crate) const MAX_PACKET: usize = 1024 * 1024;
const MAX_SAMPLES: u32 = 65536;

#[derive(Clone, Copy, Debug)]
pub(crate) struct Config {
    frame_length: u32,
    depth: u32,
    history_mult: u32,
    initial_history: u32,
    rice_limit: u32,
    channels: u32,
}

impl Config {
    /// ALAC box payload, including its four version/flags bytes.
    pub(crate) fn parse(cookie: &[u8]) -> Option<Self> {
        if cookie.len() != 28 || cookie[..4] != [0; 4] || cookie[8] != 0 {
            return None;
        }
        let config = Self {
            frame_length: u32::from_be_bytes(cookie[4..8].try_into().ok()?),
            depth: cookie[9].into(),
            history_mult: cookie[10].into(),
            initial_history: cookie[11].into(),
            rice_limit: cookie[12].into(),
            channels: cookie[13].into(),
        };
        (config.frame_length > 0
            && config.frame_length <= MAX_SAMPLES
            && matches!(config.depth, 16 | 20 | 24 | 32)
            && config.rice_limit <= 31
            && (1..=8).contains(&config.channels))
        .then_some(config)
    }
}

#[derive(Clone)]
struct Bits<'a> {
    data: &'a [u8],
    pos: usize,
}

impl Bits<'_> {
    fn left(&self) -> usize {
        self.data.len() * 8 - self.pos
    }

    fn skip(&mut self, count: usize) -> Option<()> {
        if count > self.left() {
            return None;
        }
        self.pos += count;
        Some(())
    }

    fn read(&mut self, count: u32) -> Option<u32> {
        if count > 32 || count as usize > self.left() {
            return None;
        }
        let mut value = 0u32;
        let mut remaining = count as usize;
        while remaining > 0 {
            let take = remaining.min(8 - self.pos % 8);
            let shift = 8 - self.pos % 8 - take;
            value =
                (value << take) | ((self.data[self.pos / 8] as u32 >> shift) & ((1 << take) - 1));
            self.pos += take;
            remaining -= take;
        }
        Some(value)
    }

    fn peek(&self, count: u32) -> Option<u32> {
        self.clone().read(count)
    }

    fn scalar(&mut self, k: u32, width: u32) -> Option<u32> {
        if !(1..=31).contains(&k) || !(1..=32).contains(&width) {
            return None;
        }
        let mut unary = 0;
        while unary < 9 {
            if self.read(1)? == 0 {
                break;
            }
            unary += 1;
        }
        if unary == 9 {
            return self.read(width);
        }
        if k == 1 {
            return Some(unary);
        }
        let suffix = self.peek(k)?;
        let base = (unary as u64) * ((1u64 << k) - 1);
        let value = if suffix > 1 {
            self.skip(k as usize)?;
            base + suffix as u64 - 1
        } else {
            self.skip(k as usize - 1)?;
            base
        };
        u32::try_from(value).ok()
    }

    fn rice(&mut self, samples: u32, width: u32, multiplier: u32, config: Config) -> Option<()> {
        let mut history = config.initial_history;
        let mut sign = 0;
        let mut index = 0;
        while index < samples {
            let k = ((history >> 9) + 3).ilog2().min(config.rice_limit);
            let value = self.scalar(k, width)?.checked_add(sign)?;
            sign = 0;
            history = if value > 65535 {
                65535
            } else {
                history
                    .wrapping_add(value * multiplier)
                    .wrapping_sub(history.wrapping_mul(multiplier) >> 9)
            };
            index += 1;
            if history < 128 && index < samples {
                let log = if history == 0 { 0 } else { history.ilog2() };
                let k = (7 - log + ((history + 16) >> 6)).min(config.rice_limit);
                let run = self.scalar(k, 16)?;
                // A decoder may clamp an oversized run; a repair scanner must
                // not use a malformed audio body to guess a terminator offset.
                if run > samples - index {
                    return None;
                }
                index += run;
                sign = u32::from(run <= 65535);
                history = 0;
            }
        }
        Some(())
    }

    /// Scan a SCE/CPE/LFE body and return its PCM sample count.
    fn audio(&mut self, channels: u32, config: Config) -> Option<u32> {
        self.skip(4)?; // element instance
        if self.read(12)? != 0 {
            return None;
        }
        let partial = self.read(1)? != 0;
        let shifted = self.read(2)?;
        let escape = self.read(1)? != 0;
        if shifted == 3 {
            return None;
        }
        let samples = if partial {
            self.read(32)?
        } else {
            config.frame_length
        };
        if samples == 0 || samples > config.frame_length {
            return None;
        }
        if escape {
            // An escape element stores interleaved PCM at the cookie depth.
            if shifted != 0 {
                return None;
            }
            self.skip(samples as usize * channels as usize * config.depth as usize)?;
        } else {
            let width = config
                .depth
                .checked_sub(shifted * 8)?
                .checked_add(channels - 1)?;
            if width == 0 || width > 32 || config.rice_limit == 0 {
                return None;
            }
            let shift = self.read(8)?;
            let weight = self.read(8)?;
            if channels == 2 && weight != 0 && shift > 31 {
                return None;
            }
            let mut multipliers = [0; 2];
            for multiplier in &mut multipliers[..channels as usize] {
                let predictor = self.read(4)?;
                let quant = self.read(4)?;
                *multiplier = self.read(3)? * config.history_mult / 4;
                let order = self.read(5)?;
                if !matches!(predictor, 0 | 15) || quant == 0 || order >= config.frame_length {
                    return None;
                }
                self.skip(order as usize * 16)?;
            }
            self.skip(samples as usize * channels as usize * shifted as usize * 8)?;
            for multiplier in &multipliers[..channels as usize] {
                self.rice(samples, width, *multiplier, config)?;
            }
        }
        Some(samples)
    }

    /// Ancillary elements are legal ALAC syntax even if FFmpeg rejects them.
    /// Never replace a valid data/fill element with END just to appease FFmpeg.
    fn ancillary(&mut self, tag: u32) -> Option<()> {
        let bytes = match tag {
            4 => {
                self.skip(4)?;
                let align = self.read(1)? != 0;
                let mut size = self.read(8)?;
                if size == 255 {
                    size += self.read(8)?;
                }
                if align {
                    self.skip((8 - self.pos % 8) % 8)?;
                }
                size
            }
            6 => {
                let size = self.read(4)?;
                if size == 15 { 14 + self.read(8)? } else { size }
            }
            _ => return None,
        };
        self.skip(bytes as usize * 8)
    }
}

/// Locate a provably malformed trailer only after all configured channels.
fn repair_offset(packet: &[u8], config: Config) -> Option<usize> {
    if packet.len() > MAX_PACKET {
        return None;
    }
    let mut bits = Bits {
        data: packet,
        pos: 0,
    };
    let mut channels = 0;
    let mut samples = None;
    while channels < config.channels {
        let tag = bits.read(3)?;
        if matches!(tag, 4 | 6) {
            bits.ancillary(tag)?;
            continue;
        }
        let count = match tag {
            0 | 3 => 1,
            1 => 2,
            _ => return None,
        };
        channels += count;
        if channels > config.channels {
            return None;
        }
        let current = bits.audio(count, config)?;
        if samples.is_some_and(|previous| previous != current) {
            return None;
        }
        samples = Some(current);
    }
    // A valid END (including arbitrary subsequent padding) stays byte exact.
    // Preserve any well-formed ancillary elements preceding it as well.
    loop {
        let at = bits.pos;
        match bits.read(3)? {
            7 | 2 | 5 => return None, // END or unsupported, potentially valid syntax
            tag @ (4 | 6) => {
                if bits.ancillary(tag).is_none() {
                    return Some(at);
                }
            }
            _ => return Some(at), // extra audio channels cannot belong here
        }
    }
}

/// Restore END and padding only when the audio body was completely parsed.
/// No size changes: fewer than three remaining bits cannot safely be repaired.
pub(crate) fn repair(packet: &mut [u8], config: Config) -> bool {
    let Some(end) = repair_offset(packet, config) else {
        return false;
    };
    for bit in end..end + 3 {
        packet[bit / 8] |= 1 << (7 - bit % 8);
    }
    let pad = end + 3;
    let byte = pad / 8;
    if pad % 8 != 0 {
        packet[byte] &= 0xff << (8 - pad % 8);
        packet[byte + 1..].fill(0);
    } else {
        packet[byte..].fill(0);
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(depth: u32, channels: u32) -> Config {
        Config {
            frame_length: 4,
            depth,
            history_mult: 40,
            initial_history: 10,
            rice_limit: 14,
            channels,
        }
    }

    #[derive(Default)]
    struct Writer(Vec<bool>);
    impl Writer {
        fn put(&mut self, value: u32, count: u32) {
            for bit in (0..count).rev() {
                self.0.push(value & (1 << bit) != 0);
            }
        }
        fn packet(&self) -> Vec<u8> {
            let mut bytes = vec![0; self.0.len().div_ceil(8)];
            for (bit, set) in self.0.iter().enumerate() {
                if *set {
                    bytes[bit / 8] |= 1 << (7 - bit % 8);
                }
            }
            bytes
        }
        fn audio(&mut self, depth: u32, channels: u32, partial: bool, compressed: bool) {
            self.put(if channels == 2 { 1 } else { 0 }, 3);
            self.put(0, 16); // instance + reserved
            self.put(u32::from(partial), 1);
            self.put(0, 2); // no shifted low bytes
            self.put(u32::from(!compressed), 1);
            if partial {
                self.put(4, 32);
            }
            if compressed {
                self.put(0, 16); // no decorrelation
                for _ in 0..channels {
                    self.put(0, 4); // predictor type
                    self.put(1, 4); // quantization
                    self.put(0, 3); // zero history multiplier
                    self.put(0, 5); // predictor order
                }
                for _ in 0..channels {
                    // History 10 => k=1, scalar 0. Then a run of 3 zeros:
                    // k=4, unary 0, suffix (run+1)=4, completing four samples.
                    self.put(0, 1);
                    self.put(0, 1);
                    self.put(4, 4);
                }
            } else {
                for n in 0..4 * channels {
                    self.put(n * 123, depth);
                }
            }
        }
    }

    #[test]
    fn repairs_only_trailer_for_escape_and_rice_across_depths_and_channels() {
        for depth in [16, 20, 24, 32] {
            for channels in [1, 2, 6] {
                for partial in [false, true] {
                    for compressed in [false, true] {
                        if compressed && depth == 32 && channels > 1 {
                            continue;
                        }
                        let cfg = config(depth, channels);
                        let mut body = Writer::default();
                        for _ in 0..channels.div_ceil(2) {
                            body.audio(depth, channels.min(2), partial, compressed);
                        }
                        let end = body.0.len();
                        let mut good = Writer(body.0.clone());
                        good.put(7, 3);
                        let expected = good.packet();
                        let mut healthy = expected.clone();
                        assert!(!repair(&mut healthy, cfg));
                        assert_eq!(healthy, expected);
                        body.put(0, 3);
                        let mut bad = body.packet();
                        assert_eq!(repair_offset(&bad, cfg), Some(end));
                        assert!(repair(&mut bad, cfg));
                        assert_eq!(bad, expected);
                        assert!(!repair(&mut bad, cfg));
                    }
                }
            }
        }
    }

    #[test]
    fn truncated_dse_matches_the_observed_stereo_escape_failure() {
        let cfg = config(16, 2);
        let mut body = Writer::default();
        body.audio(16, 2, false, false);
        let end = body.0.len();
        body.put(4, 3);
        let mut packet = body.packet();
        assert_eq!(repair_offset(&packet, cfg), Some(end));
        let original = packet.clone();
        assert!(repair(&mut packet, cfg));
        assert_eq!(packet.len(), original.len());
        assert_eq!(&packet[..packet.len() - 1], &original[..original.len() - 1]);
        assert_eq!(packet.last(), Some(&0xc0));
    }

    #[test]
    fn preserves_valid_ancillary_elements_and_end_padding() {
        for tag in [4, 6] {
            let cfg = config(16, 2);
            let mut body = Writer::default();
            body.audio(16, 2, false, false);
            body.put(tag, 3);
            if tag == 4 {
                body.put(0, 5); // instance + alignment
                body.put(1, 8); // one data byte
            } else {
                body.put(1, 4); // one fill byte
            }
            body.put(0xa5, 8);
            body.put(7, 3);
            body.put(0xff, 8); // legal opaque padding after END
            let mut packet = body.packet();
            let original = packet.clone();
            assert!(!repair(&mut packet, cfg));
            assert_eq!(packet, original);
        }
    }

    #[test]
    fn does_not_guess_on_incomplete_or_unsupported_audio() {
        let cfg = config(16, 2);
        let mut body = Writer::default();
        body.audio(16, 2, false, false);
        body.put(0, 3);
        let packet = body.packet();
        for length in 0..packet.len() - 1 {
            let mut truncated = packet[..length].to_vec();
            let original = truncated.clone();
            assert!(!repair(&mut truncated, cfg));
            assert_eq!(truncated, original);
        }
        let mut unsupported = packet.clone();
        unsupported[0] |= 0x40; // CPE -> LFE, only one of two channels
        let original = unsupported.clone();
        assert!(!repair(&mut unsupported, cfg));
        assert_eq!(unsupported, original);
        let mut reserved = packet.clone();
        reserved[1] = 1;
        assert!(!repair(&mut reserved, cfg));
        let mut oversized = vec![0; MAX_PACKET + 1];
        assert!(!repair(&mut oversized, cfg));
    }

    #[test]
    fn config_and_rice_bounds_are_checked() {
        let mut cookie = [0u8; 28];
        cookie[4..8].copy_from_slice(&4096u32.to_be_bytes());
        cookie[9] = 16;
        cookie[12] = 14;
        cookie[13] = 2;
        assert!(Config::parse(&cookie).is_some());
        for length in 0..28 {
            assert!(Config::parse(&cookie[..length]).is_none());
        }
        cookie[12] = 255;
        assert!(Config::parse(&cookie).is_none());
        let mut bits = Bits {
            data: &[0xff; 8],
            pos: 0,
        };
        assert!(bits.rice(4, 17, 40, config(16, 2)).is_none());
        assert!(bits.read(33).is_none());
    }

    #[test]
    fn never_appends_bytes_when_terminator_does_not_fit() {
        let cfg = config(16, 2);
        let mut body = Writer::default();
        body.audio(16, 2, false, false);
        // Only one alignment bit remains after this complete audio body.
        let mut packet = body.packet();
        assert_eq!(packet.len() * 8 - body.0.len(), 1);
        let original = packet.clone();
        assert!(!repair(&mut packet, cfg));
        assert_eq!(packet, original);
    }

    #[test]
    fn rejects_zero_run_exceeding_remaining_samples() {
        let mut data = Writer::default();
        data.put(0, 1); // zero residual, history 10
        data.put(0, 1); // run unary
        data.put(5, 4); // run of four, but only three samples remain
        let packet = data.packet();
        let mut bits = Bits {
            data: &packet,
            pos: 0,
        };
        assert!(bits.rice(4, 16, 0, config(16, 1)).is_none());
    }
}
