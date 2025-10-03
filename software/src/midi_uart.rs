use crate::midi_parser::{MidiMessage, MidiMessageError, MidiParser};
use defmt::Format;
use embassy_rp::uart::{BufferedUartRx, Instance};
use embedded_io_async::BufRead;

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

/// MIDI UART wrapper that combines buffered UART reception with MIDI parsing
///
/// This struct wraps a BufferedUartRx and feeds incoming bytes to a MidiParser,
/// returning complete MIDI messages when they've been fully received.
///
/// The BufferedUart advantage:
/// - Interrupt handler fills buffer in background (no CPU busy-waiting)
/// - We can read however many bytes are available (1 to N)
/// - Reduces risk of buffer overruns during burst MIDI traffic
pub struct MidiUart<'a, T: Instance> {
    pub usart: BufferedUartRx<'a, T>,
    pub uart_channel: UartChannel,
    parser: MidiParser,
}

impl<'a, T: Instance> MidiUart<'a, T> {
    /// Create a new MIDI UART reader
    ///
    /// # Arguments
    /// * `usart` - BufferedUartRx instance (interrupt-driven receiver)
    /// * `uart_channel` - Identifies which physical UART this is (Zero or One)
    pub fn new(usart: BufferedUartRx<'static, T>, uart_channel: UartChannel) -> Self {
        let parser = MidiParser::default();

        Self {
            usart,
            uart_channel,
            parser,
        }
    }

    /// Reset the MIDI parser to clean state
    ///
    /// Call this after UART errors (Overrun, Framing, Break, Parity) to prevent
    /// corrupted parser state from affecting subsequent messages.
    pub fn reset_parser(&mut self) {
        self.parser.reset();
    }
    /// Read the next complete MIDI message from the UART
    ///
    /// This method uses BufferedUartRx's fill_buf() which leverages the
    /// interrupt-driven background buffering for efficient I/O.
    ///
    /// How it works:
    /// 1. fill_buf() returns a slice of bytes already in the buffer
    ///    - If buffer is empty, it waits for interrupts to fill it
    ///    - If buffer has data, it returns immediately (no waiting!)
    /// 2. We feed bytes one-by-one to the MIDI parser
    /// 3. When parser returns a complete message, we return it
    /// 4. consume() tells the buffer how many bytes we've processed
    ///
    /// Performance characteristics:
    /// - No busy-waiting for individual bytes
    /// - Can process multiple bytes per call when available
    /// - Interrupt handler fills buffer in background
    /// - Approximately 30x fewer context switches than DMA single-byte reads
    ///
    /// # Returns
    /// * `Ok(UartMidiMessage)` - A complete MIDI message with channel info
    /// * `Err(UartMidiError)` - UART error or invalid MIDI data
    pub async fn read(&mut self) -> Result<UartMidiMessage, UartMidiError> {
        loop {
            // Get a view into the buffered data without consuming it
            // This is the key to BufferedUart efficiency: we peek at available
            // data rather than blocking for a specific number of bytes
            let buf = self
                .usart
                .fill_buf()
                .await
                .map_err(UartMidiError::UartError)?;

            // If buffer is empty, the UART is idle. Loop and wait for more data.
            if buf.is_empty() {
                continue;
            }

            // Track how many bytes we process from the buffer
            let mut consumed = 0;

            // Feed available bytes to the MIDI parser one at a time
            // We stop as soon as we get a complete message
            for byte in buf {
                consumed += 1;

                match self.parser.feed_byte(*byte) {
                    Ok(Some(message)) => {
                        // Got a complete MIDI message!
                        // Mark these bytes as consumed so buffer can reuse the space
                        self.usart.consume(consumed);

                        return Ok(UartMidiMessage {
                            message,
                            uart_channel: self.uart_channel,
                        });
                    }
                    Ok(None) => {
                        // Parser needs more bytes to complete the message
                        // Continue to next byte
                    }
                    Err(err) => {
                        // Invalid MIDI data (protocol violation)
                        // Mark bytes as consumed and return error
                        self.usart.consume(consumed);
                        return Err(UartMidiError::MessageError(err));
                    }
                }
            }

            // We've processed all available bytes but no complete message yet
            // Mark bytes as consumed and loop to wait for more
            self.usart.consume(consumed);
        }
    }
}
