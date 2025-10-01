use defmt::{write, Format};
use heapless::Vec;

#[derive(Debug)]
pub enum MidiMessage {
    SystemRealtime(Vec<u8, 3>),
    RunningStatus(Vec<u8, 3>),
    Voice(Vec<u8, 3>),
    SystemCommon(Vec<u8, 3>),
}
#[derive(Debug, Clone)]
pub enum MidiMessageError {
    UnknownStatus,
    DuplicateStatus,
    UnexpectedDataByte,
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
        } else if (0xF1..=0xF3).contains(&data[0])
            || data[0] == 0xF6
            || (0xF9..=0xFC).contains(&data[0])
        {
            message = MidiMessage::SystemCommon(data)
        } else {
            return Err(MidiMessageError::UnknownStatus);
        }

        Ok(message)
    }
}

impl Format for MidiMessage {
    fn format(&self, fmt: defmt::Formatter) {
        match self {
            MidiMessage::RunningStatus(message) => {
                for byte in message {
                    write!(fmt, " {=u8:x}", byte)
                }
            }
            MidiMessage::SystemCommon(message) => {
                for byte in message {
                    write!(fmt, " {=u8:x}", byte)
                }
            }
            MidiMessage::SystemRealtime(message) => {
                for byte in message {
                    write!(fmt, " {=u8:x}", byte)
                }
            }
            MidiMessage::Voice(message) => {
                for byte in message {
                    write!(fmt, " {=u8:x}", byte)
                }
            }
        }
    }
}

#[derive(Debug)]
pub struct MidiParser {
    status: Vec<u8, 1>,
    data: Vec<u8, 2>,
    expected_data_bytes: usize,
}

impl Default for MidiParser {
    fn default() -> Self {
        Self {
            status: Default::default(),
            data: Default::default(),
            expected_data_bytes: 2,
        }
    }
}

impl MidiParser {
    fn clear(&mut self) {
        *self = Self::default();
    }

    pub fn feed_byte(&mut self, &byte: &u8) -> Result<Option<MidiMessage>, MidiMessageError> {
        if (0xF8..=0xFF).contains(&byte) {
            // SystemRealtime
            let status_byte = Vec::from_slice(&[byte]).unwrap();
            let empty_data: Vec<u8, 2> = Vec::new();
            let message = MidiMessage::from_status_and_data(&status_byte, &empty_data)?;
            return Ok(Some(message));
        }

        if (byte & 0x80) == 0x80 {
            // status byte
            if self.status.push(byte).is_err() {
                // We already have an active status, raise error
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
            // data byte
            if self.data.push(byte).is_err() {
                // We got more data bytes than expected, raise error
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
