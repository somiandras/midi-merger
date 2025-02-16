use crate::midi_parser::{MidiMessage, MidiParser};
use defmt::Format;
use embassy_rp::uart::{Async, Error, Instance, UartRx};

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Format)]
pub enum UartChannel {
    #[default]
    Zero,
    One,
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
    pub async fn read(&mut self) -> Result<UartMidiMessage, Error> {
        'outer: loop {
            let read_result = self.usart.read(&mut self.buffer).await;
            match read_result {
                Ok(_) => {
                    for byte in &self.buffer {
                        if let Some(message) = self.parser.feed_byte(byte) {
                            match &message {
                                MidiMessage::SystemRealtime(_) | MidiMessage::SystemCommon(_) => {}
                                MidiMessage::RunningStatus(_) => {
                                    defmt::debug!(
                                        "Running Status {} on {}",
                                        message,
                                        self.uart_channel
                                    );
                                }
                                MidiMessage::Voice(_) => {
                                    defmt::debug!(
                                        "Voice Message {} on {}",
                                        message,
                                        self.uart_channel
                                    );
                                }
                            }
                            break 'outer Ok(UartMidiMessage {
                                message,
                                uart_channel: self.uart_channel,
                            });
                        };
                    }
                }
                Err(err) => break 'outer Err(err),
            }
        }
    }
}
