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
        loop {
            // Read bytes from UART
            // NOTE: Currently using single-byte reads. Multi-byte buffering would improve
            // performance but requires read_until_idle() which is not available in
            // embassy-rp 0.2.0. Consider upgrading Embassy to enable this optimization.
            match self.usart.read(&mut self.buffer).await {
                Ok(_) => {
                    // Feed bytes to the parser one at a time
                    for byte in &self.buffer {
                        match self.parser.feed_byte(byte) {
                            Ok(Some(message)) => {
                                return Ok(UartMidiMessage {
                                    message,
                                    uart_channel: self.uart_channel,
                                });
                            }
                            Ok(None) => continue,
                            Err(err) => return Err(UartMidiError::MessageError(err)),
                        }
                    }
                }
                Err(embassy_rp::uart::Error::Overrun) => {
                    defmt::warn!("UART overrun on {:?}", self.uart_channel);
                    continue;
                }
                Err(e) => return Err(UartMidiError::UartError(e)),
            }
        }
    }
}
