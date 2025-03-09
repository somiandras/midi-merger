use crate::midi_parser::{MidiMessage, MidiMessageError, MidiParser};
use defmt::Format;
use embassy_rp::uart::{Async, Instance, UartRx};

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Format)]
pub enum UartChannel {
    #[default]
    Zero,
    One,
}

pub enum UartMidiError {
    UartError(embassy_rp::uart::Error),
    MessageError(MidiMessageError),
}

pub struct UartMidiMessage {
    // Wraps MidiMessage to record the UART channel where the message comes from
    pub message: MidiMessage,
    pub uart_channel: UartChannel,
}

pub struct MidiUart<'a, T: Instance> {
    pub usart: UartRx<'a, T, Async>,
    pub uart_channel: UartChannel,
    buffer: [u8; 1],
    parser: MidiParser,
}

impl<'a, T: Instance> MidiUart<'a, T> {
    pub fn new(usart: UartRx<'static, T, Async>, uart_channel: UartChannel) -> Self {
        let buffer: [u8; 1] = [0x00];
        let parser = MidiParser::default();

        Self {
            usart,
            uart_channel,
            buffer,
            parser,
        }
    }
    pub async fn read(&mut self) -> Result<UartMidiMessage, UartMidiError> {
        'outer: loop {
            // NOTE: The buffer is of size 1, so we can only read one byte at a time. Maybe read in
            // more bytes at once to improve performance? They will be fed to the parser one by one
            // anyway though...
            // We also have to forward SystemRealTime messages immediately, as they don't have any
            // data bytes to wait for.
            let read_result = self.usart.read(&mut self.buffer).await;
            match read_result {
                Ok(_) => {
                    // Got some bytes, let's feed them to the parser
                    for byte in &self.buffer {
                        match self.parser.feed_byte(byte) {
                            Ok(result) => {
                                // Feeding a byte might result in a message, or not. If it does, we break
                                // the loop and return the message. If it doesn't, we continue reading
                                // bytes until we get a message.
                                if let Some(message) = result {
                                    break 'outer Ok(UartMidiMessage {
                                        message,
                                        uart_channel: self.uart_channel,
                                    });
                                }
                            }
                            Err(err) => break 'outer Err(UartMidiError::MessageError(err)),
                        };
                    }
                }
                Err(err) => break 'outer Err(UartMidiError::UartError(err)),
            }
        }
    }
}
