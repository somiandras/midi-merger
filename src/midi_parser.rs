use heapless::Vec;

pub enum MidiMessage {
    // Only differentiates between messages based on length and the status byte
    SystemRealtime(Vec<u8, 3>),
    Message(Vec<u8, 3>),
    RunningStatus(Vec<u8, 3>),
}

impl MidiMessage {
    fn from_status_and_data(status_byte: &Vec<u8, 1>, data_bytes: &Vec<u8, 2>) -> MidiMessage {
        let mut data = Vec::from_slice(status_byte).unwrap();
        data.extend_from_slice(data_bytes).unwrap();

        if status_byte.is_empty() {
            MidiMessage::RunningStatus(data)
        } else if (0xF8..=0xFF).contains(&data[0]) {
            MidiMessage::SystemRealtime(data)
        } else {
            MidiMessage::Message(data)
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
        self.status.clear();
        self.data.clear();
        self.expected_data_bytes = Default::default();
    }

    pub fn feed_byte(&mut self, &byte: &u8) -> Option<MidiMessage> {
        if (0xF8..=0xFF).contains(&byte) {
            // SystemRealtime
            return Some(MidiMessage::from_status_and_data(&self.status, &self.data));
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
            panic!("Unknown status byte");
        } else {
            // data byte, should panic if we already have 2 data bytes
            self.data.push(byte).unwrap();
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
