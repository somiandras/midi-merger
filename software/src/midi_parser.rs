use defmt::{write, Format};
use heapless::Vec;

#[derive(Debug)]
pub enum MidiMessage {
    SystemRealtime(Vec<u8, 3>),
    RunningStatus(Vec<u8, 3>),
    Voice(Vec<u8, 3>),
    SystemCommon(Vec<u8, 3>),
}

impl MidiMessage {
    fn from_status_and_data(status_byte: &Vec<u8, 1>, data_bytes: &Vec<u8, 2>) -> MidiMessage {
        let mut data = Vec::from_slice(status_byte).unwrap();
        data.extend_from_slice(data_bytes).unwrap();

        if status_byte.is_empty() {
            MidiMessage::RunningStatus(data)
        } else if (0xF8..=0xFF).contains(&data[0]) {
            MidiMessage::SystemRealtime(data)
        } else if (0x80..=0xEF).contains(&data[0]) {
            MidiMessage::Voice(data)
        } else if (0xF1..=0xF3).contains(&data[0])
            || data[0] == 0xF6
            || (0xF9..=0xFC).contains(&data[0])
        {
            MidiMessage::SystemCommon(data)
        } else {
            defmt::error!("Unknown status: {=u8}", data[0]);
            panic!("Unknown status")
        }
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

    pub fn feed_byte(&mut self, &byte: &u8) -> Option<MidiMessage> {
        if (0xF8..=0xFF).contains(&byte) {
            // SystemRealtime
            let status_byte = Vec::from_slice(&[byte]).unwrap();
            return Some(MidiMessage::from_status_and_data(&status_byte, &self.data));
        }

        if (byte & 0x80) == 0x80 {
            // status byte, will panic if we already have one
            self.status.push(byte).unwrap();

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
            match self.data.push(byte) {
                Ok(_) => {}
                Err(byte) => {
                    defmt::error!(
                        "new byte: {=u8:x}, status: {}, data: {}",
                        byte,
                        self.status.as_slice(),
                        self.data.as_slice()
                    )
                }
            }
        }

        if self.data.len() == self.expected_data_bytes {
            // we got all data bytes we expected, let's create a message and clear buffers
            let message = MidiMessage::from_status_and_data(&self.status, &self.data);
            self.clear();
            Some(message)
        } else {
            None
        }
    }
}
