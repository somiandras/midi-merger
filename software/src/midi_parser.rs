use defmt::{write, Format};
use embassy_time::{Duration, Instant};
use heapless::Vec;

/// Parser state machine states
///
/// The parser operates in one of three modes:
/// - `Reading`: Normal message parsing, accumulating status and data bytes
/// - `Resyncing`: Error recovery mode, hunting for the next valid status byte
/// - `InSysEx`: Inside a System Exclusive message, discarding all bytes until 0xF7
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ParserState {
    Reading,
    Resyncing,
    InSysEx,
}

/// A parsed MIDI message with its associated data bytes
///
/// MIDI messages are categorized into four types based on their status byte:
/// - Voice: Channel-specific messages (Note On/Off, Control Change, etc.) (0x80-0xEF)
/// - SystemCommon: System-wide messages (Song Position, Tune Request, etc.) (0xF1-0xF6)
/// - SystemRealtime: Timing and synchronization messages (Clock, Start/Stop, etc.) (0xF8-0xFF)
/// - RunningStatus: Data bytes without a status byte (reuses previous status)
///
/// Each variant contains a `Vec<u8, 3>` holding the complete message bytes.
#[derive(Debug)]
pub enum MidiMessage {
    SystemRealtime(Vec<u8, 3>),
    RunningStatus(Vec<u8, 3>),
    Voice(Vec<u8, 3>),
    SystemCommon(Vec<u8, 3>),
}

/// Errors that can occur during MIDI message parsing
#[derive(Debug, Clone)]
pub enum MidiMessageError {
    /// Received an invalid or undefined MIDI status byte
    UnknownStatus,
    /// Received a status byte while still processing a previous message
    DuplicateStatus,
    /// Received more data bytes than expected for the current message type
    UnexpectedDataByte,
    /// Received an undefined status byte (0xF4, 0xF5, 0xF9-0xFD)
    InvalidStatusByte,
}

impl MidiMessage {
    fn from_status_and_data(
        status_byte: &Vec<u8, 1>,
        data_bytes: &Vec<u8, 2>,
    ) -> Result<Self, MidiMessageError> {
        let mut data = heapless::Vec::from_slice(status_byte).unwrap();
        data.extend_from_slice(data_bytes).unwrap();

        let message: MidiMessage;

        if status_byte.is_empty() {
            message = MidiMessage::RunningStatus(data)
        } else if (0xF8..=0xFF).contains(&data[0]) {
            message = MidiMessage::SystemRealtime(data)
        } else if (0x80..=0xEF).contains(&data[0]) {
            message = MidiMessage::Voice(data)
        } else if (0xF1..=0xF3).contains(&data[0]) || data[0] == 0xF6 {
            message = MidiMessage::SystemCommon(data)
        } else {
            return Err(MidiMessageError::UnknownStatus);
        }

        Ok(message)
    }
}

impl Format for MidiMessage {
    fn format(&self, fmt: defmt::Formatter) {
        let data = match self {
            MidiMessage::Voice(d)
            | MidiMessage::RunningStatus(d)
            | MidiMessage::SystemCommon(d)
            | MidiMessage::SystemRealtime(d) => d,
        };
        for byte in data {
            write!(fmt, " {=u8:x}", byte)
        }
    }
}

/// Stateful MIDI 1.0 protocol parser
///
/// This parser implements the MIDI 1.0 specification, handling:
/// - Running status (omitted status bytes)
/// - System Realtime messages (can interrupt any message)
/// - System Exclusive (SysEx) messages (0xF0...0xF7)
/// - Variable-length messages (0-2 data bytes depending on status)
/// - Resynchronization after errors (hunting for valid status bytes)
///
/// Feed bytes one at a time using `feed_byte()`. The parser maintains internal
/// state and returns `Some(MidiMessage)` when a complete message is assembled.
///
/// After errors, the parser enters resync mode where it discards all bytes until
/// a valid status byte is found, allowing recovery from corrupted byte streams.
#[derive(Debug)]
pub struct MidiParser {
    status: Vec<u8, 1>,
    data: Vec<u8, 2>,
    expected_data_bytes: usize,
    state: ParserState,
    last_byte_time: Option<Instant>,
}

impl Default for MidiParser {
    fn default() -> Self {
        Self {
            status: Default::default(),
            data: Default::default(),
            expected_data_bytes: 2,
            state: ParserState::Reading,
            last_byte_time: None,
        }
    }
}

impl MidiParser {
    /// Maximum time between MIDI bytes before parser resets (in milliseconds)
    ///
    /// MIDI bytes at 31,250 baud arrive in ~0.32ms each. A complete 3-byte message
    /// (status + 2 data bytes) transmits in ~0.96ms at most. This generous 300ms
    /// timeout allows for device processing delays while protecting against stuck
    /// parser state from hardware glitches, cable disconnects, or electrical noise.
    const MIDI_BYTE_TIMEOUT_MS: u64 = 300;

    fn clear(&mut self) {
        *self = Self::default();
    }

    /// Reset the parser to its initial state and enter resynchronization mode
    ///
    /// This should be called after UART errors (Overrun, Framing, Break, Parity)
    /// or MIDI protocol errors to prevent corrupted parser state from affecting
    /// subsequent messages.
    ///
    /// Example scenario requiring reset:
    /// 1. Parser receives 0x90 (Note On status byte)
    /// 2. Expecting 2 data bytes next
    /// 3. UART Overrun error occurs (bytes lost)
    /// 4. Without reset, parser still expects data bytes
    /// 5. Next message's status byte would be misinterpreted as data!
    ///
    /// After calling reset(), the parser enters resync mode where it hunts for
    /// the next valid status byte, discarding any garbage bytes in the stream.
    /// This allows robust recovery from corrupted byte streams.
    pub fn reset(&mut self) {
        self.clear();
        self.state = ParserState::Resyncing;
    }

    pub fn feed_byte(&mut self, byte: u8) -> Result<Option<MidiMessage>, MidiMessageError> {
        // SystemRealtime messages (0xF8-0xFF) can interrupt any message without affecting
        // parser state. They are processed immediately even during resync mode, and do NOT
        // update the timestamp, ensuring the timeout only measures time between actual
        // message bytes (status/data).
        if (0xF8..=0xFF).contains(&byte) {
            // Validate it's a defined SystemRealtime byte (not 0xF9 or 0xFD)
            if byte == 0xF9 || byte == 0xFD {
                self.clear();
                self.state = ParserState::Resyncing;
                return Err(MidiMessageError::InvalidStatusByte);
            }

            let status_byte = Vec::from_slice(&[byte]).unwrap();
            let empty_data: Vec<u8, 2> = Vec::new();
            let message = MidiMessage::from_status_and_data(&status_byte, &empty_data)?;
            return Ok(Some(message));
        }

        // Check if too much time elapsed since last byte (message timeout)
        // On the first byte after startup/reset, last_byte_time is None, so no timeout
        // is checked (correct behavior - we need at least one byte to start timing).
        if let Some(last_time) = self.last_byte_time {
            if last_time.elapsed() > Duration::from_millis(Self::MIDI_BYTE_TIMEOUT_MS) {
                self.clear();
                self.state = ParserState::Resyncing;
                defmt::warn!("MIDI message timeout - entering resync mode");
            }
        }

        // State machine: handle different parser modes
        match self.state {
            ParserState::Resyncing => {
                // Resynchronization mode: hunt for a valid status byte
                //
                // After errors, the byte stream may be corrupted. Instead of blindly processing
                // bytes based on corrupt expectations, actively search for the next valid status
                // byte (synchronization point) and discard any garbage in between.
                //
                // This prevents cascading errors where:
                // - Data bytes are misinterpreted as status bytes
                // - Status bytes are misinterpreted as data bytes
                // - Phantom messages are created from garbage bytes
                if (byte & 0x80) == 0x80 {
                    // Found a status byte - validate it's in legal range
                    if byte == 0xF4 || byte == 0xF5 || (0xF9..=0xFD).contains(&byte) {
                        // Invalid/undefined status byte, keep hunting
                        defmt::debug!("Resync: discarding invalid status byte {:#x}", byte);
                        return Ok(None);
                    }

                    // Valid status byte found - exit resync mode and process normally
                    defmt::info!("Resync complete on status byte {:#x}", byte);
                    self.state = ParserState::Reading;
                    // Fall through to Reading state processing below
                } else {
                    // Still hunting for status byte, discard this data byte
                    defmt::debug!("Resync: discarding data byte {:#x}", byte);
                    return Ok(None);
                }
            }
            ParserState::InSysEx => {
                // Inside SysEx - ignore all bytes until 0xF7
                // Note: SysEx start (0xF0) and end (0xF7) are handled below
                return Ok(None);
            }
            ParserState::Reading => {
                // Normal parsing mode - continue below
            }
        }

        // Update timestamp for this byte
        self.last_byte_time = Some(Instant::now());

        // Handle SysEx start (0xF0)
        if byte == 0xF0 {
            // Reset parser state including timestamp - intentional, as we're discarding
            // any partial message and entering SysEx mode
            self.clear();
            self.state = ParserState::InSysEx;
            return Ok(None); // Ignore SysEx, don't forward
        }

        // Handle SysEx end (0xF7)
        if byte == 0xF7 {
            // Reset parser state including timestamp - ready for next normal message
            self.clear();
            self.state = ParserState::Reading;
            return Ok(None); // Ignore SysEx, don't forward
        }

        if (byte & 0x80) == 0x80 {
            // status byte - validate it's in legal range
            // Undefined status bytes: 0xF4, 0xF5, 0xF9-0xFD
            if byte == 0xF4 || byte == 0xF5 || (0xF9..=0xFD).contains(&byte) {
                self.clear();
                self.state = ParserState::Resyncing;
                return Err(MidiMessageError::InvalidStatusByte);
            }

            if self.status.push(byte).is_err() {
                // We already have an active status, raise error
                self.clear();
                self.state = ParserState::Resyncing;
                return Err(MidiMessageError::DuplicateStatus);
            };

            // we need to set how many data bytes we expect
            if byte & 0xF0 == 0xC0 || byte & 0xF0 == 0xD0 || byte == 0xF1 || byte == 0xF3 {
                // 0xCx: Program change
                // 0xDx: Channel Pressure
                // 0xF1: MTC Quarter Frame Message
                // 0xF3: Song Select
                self.expected_data_bytes = 1;
            } else if byte == 0xF6 {
                // 0xF6: Tune Request
                self.expected_data_bytes = 0;
            } else {
                // everything else has two databytes
                self.expected_data_bytes = 2;
            }
        } else {
            // data byte - bit 7 is guaranteed to be 0 by the if/else structure
            if self.data.push(byte).is_err() {
                // We got more data bytes than expected, raise error
                self.clear();
                self.state = ParserState::Resyncing;
                return Err(MidiMessageError::UnexpectedDataByte);
            }
        }

        if self.data.len() == self.expected_data_bytes {
            // we got all data bytes we expected, let's create a message and clear buffers
            let message = MidiMessage::from_status_and_data(&self.status, &self.data)?;
            self.clear();
            Ok(Some(message))
        } else {
            Ok(None)
        }
    }
}
