#![no_std]
#![no_main]

use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::peripherals::{UART0, UART1};
use embassy_rp::uart::{
    BufferedInterruptHandler, BufferedUart, BufferedUartRx, BufferedUartTx, Config, Instance,
};
use embassy_sync::blocking_mutex::raw::ThreadModeRawMutex;
use embassy_sync::channel::Channel;
use embedded_io_async::Write;
use midi_parser::{MidiMessage, MidiMessageError};
use midi_uart::{MidiUart, UartChannel, UartMidiError, UartMidiMessage};
use panic_probe as _;

mod midi_parser;
mod midi_uart;

// ============================================================================
// CONTROL MESSAGES
// ============================================================================

/// Control messages for managing running status state
///
/// Control messages flow through the same channel as MIDI messages to ensure
/// proper ordering. This prevents race conditions where a MIDI message could
/// be processed with stale running status after a parser error.
///
/// Flow example (parser error on UART0):
/// 1. read_uart0 detects error (UartError or MessageError)
/// 2. read_uart0 resets its parser state
/// 3. read_uart0 sends InvalidateRunningStatus(Zero) control message
/// 4. write_uart receives control message (ordered after any pending MIDI)
/// 5. write_uart clears cached status for UART0
/// 6. Next running status message from UART0 will be rejected (no cached status)
/// 7. UART0 must send a full status byte to re-establish running status
#[derive(Debug, Clone, Copy)]
pub enum ControlMessage {
    InvalidateRunningStatus(UartChannel),
}

/// Channel messages can be either MIDI data or control commands
///
/// Using a single channel type ensures control messages are processed in order
/// with MIDI messages, preventing race conditions in running status tracking.
#[derive(Debug)]
pub enum ChannelMessage {
    Midi(UartMidiMessage),
    Control(ControlMessage),
}

// ============================================================================
// STATIC BUFFERS
// ============================================================================

// Channel for passing MIDI messages from input tasks to output task
static CHANNEL: Channel<ThreadModeRawMutex, ChannelMessage, 64> = Channel::new();

// BufferedUart requires static buffers for background interrupt-driven I/O.
// These buffers allow the hardware to accumulate incoming bytes and queue
// outgoing bytes without CPU intervention, reducing interrupt overhead.
//
// Memory usage: 256 bytes × 3 buffers = 768 bytes total (0.3% of 264KB RAM)

// UART0 RX buffer: Receives MIDI from input 1
static mut UART0_RX_BUF: [u8; 256] = [0u8; 256];

// UART0 TX buffer: Sends merged MIDI output
static mut UART0_TX_BUF: [u8; 256] = [0u8; 256];

// UART1 RX buffer: Receives MIDI from input 2
static mut UART1_RX_BUF: [u8; 256] = [0u8; 256];

#[derive(Debug, Default)]
struct UartStatus {
    uart0: Option<u8>,
    uart1: Option<u8>,
    last_tx_from: Option<UartChannel>,
}

// ============================================================================
// WRITE TASK - Merges MIDI from both inputs to single output
// ============================================================================

#[embassy_executor::task]
async fn write_uart(mut usart: BufferedUartTx<'static, UART0>) {
    let mut uart_status = UartStatus::default();
    loop {
        let channel_message = CHANNEL.receive().await;
        match channel_message {
            ChannelMessage::Control(ControlMessage::InvalidateRunningStatus(channel)) => {
                // Parser reset on error - invalidate cached running status
                //
                // When a parser error occurs on an input channel, we must clear the
                // cached running status for that channel. Otherwise, we could inject
                // a stale status byte that doesn't match the current parser state.
                //
                // Example scenario without invalidation:
                //   1. UART0 sends 0x90 0x3C 0x64 (Note On) → cache uart0=0x90
                //   2. UART0 has protocol error, parser reset
                //   3. UART1 sends running status message
                //   4. We'd inject stale 0x90 from UART0 → WRONG NOTE!
                //
                // With invalidation:
                //   1. UART0 sends 0x90 0x3C 0x64 → cache uart0=0x90
                //   2. UART0 has error, parser reset, InvalidateRunningStatus(Zero) sent
                //   3. We clear uart0=None
                //   4. UART1 running status uses correct UART1 status → CORRECT
                match channel {
                    UartChannel::Zero => uart_status.uart0 = None,
                    UartChannel::One => uart_status.uart1 = None,
                }
                // If this was the last TX channel, clear that too
                if uart_status.last_tx_from == Some(channel) {
                    uart_status.last_tx_from = None;
                }
                defmt::debug!("Invalidated running status for {:?}", channel);
            }
            ChannelMessage::Midi(message) => {
                match message.message {
                    MidiMessage::Voice(data) => {
                        match message.uart_channel {
                            // Set the current status for the corresponding channel
                            UartChannel::Zero => uart_status.uart0 = Some(data[0]),
                            UartChannel::One => uart_status.uart1 = Some(data[0]),
                        }
                        if usart.write(&data).await.is_err() {
                            defmt::error!("Failed to write Voice message");
                            continue;
                        }
                    }
                    MidiMessage::SystemCommon(data) | MidiMessage::SystemRealtime(data) => {
                        // Nothing to do, immediately send
                        if usart.write(&data).await.is_err() {
                            defmt::error!("Failed to write System message");
                            continue;
                        }
                    }
                    MidiMessage::RunningStatus(data) => {
                        defmt::debug!("Running status: {:?}", data);

                        // Determine if we need to prepend status byte
                        let need_status = uart_status
                            .last_tx_from
                            .map(|prev| prev != message.uart_channel)
                            .unwrap_or(true); // First message ever, need status

                        if need_status {
                            // Get the appropriate status byte for this channel
                            let status_byte = match message.uart_channel {
                                UartChannel::Zero => uart_status.uart0,
                                UartChannel::One => uart_status.uart1,
                            };

                            match status_byte {
                                Some(status) => {
                                    defmt::debug!("Need to add previous status");
                                    if usart.write(&[status]).await.is_err() {
                                        defmt::error!("Failed to write status byte");
                                        continue;
                                    }
                                }
                                None => {
                                    // Running status without prior voice message - protocol violation
                                    defmt::error!(
                                        "Running status without previous voice message on {:?}",
                                        message.uart_channel
                                    );
                                    continue;
                                }
                            }
                        }

                        if usart.write(&data).await.is_err() {
                            defmt::error!("Failed to write running status data");
                            continue;
                        }
                    }
                }
                uart_status.last_tx_from = Some(message.uart_channel)
            }
        }
    }
}

// ============================================================================
// READ TASK - Receives MIDI from one input and sends to channel
// ============================================================================

async fn read_from_uart(usart: BufferedUartRx<'static, impl Instance>, uart_channel: UartChannel) {
    let mut midi_uart = MidiUart::new(usart, uart_channel);
    loop {
        let result = midi_uart.read().await;
        match result {
            Ok(message) => {
                match message.message {
                    MidiMessage::SystemRealtime(_) => {}
                    _ => {
                        defmt::info!(
                            "Received message: {:?} on channel {:?}",
                            message.message,
                            uart_channel
                        );
                    }
                }

                CHANNEL.send(ChannelMessage::Midi(message)).await;
            }
            Err(error) => {
                // Handle error
                match error {
                    UartMidiError::UartError(uart_error) => {
                        // UART hardware errors can leave the parser in an inconsistent state
                        // (e.g., expecting data bytes that will never arrive due to lost bytes).
                        // Reset the parser to ensure clean recovery.
                        match uart_error {
                            embassy_rp::uart::Error::Overrun => {
                                defmt::error!("Uart Overrun error");
                            }
                            embassy_rp::uart::Error::Framing => {
                                defmt::error!("Uart Framing error");
                            }
                            embassy_rp::uart::Error::Break => {
                                defmt::error!("Uart Break error");
                            }
                            embassy_rp::uart::Error::Parity => {
                                defmt::error!("Uart Parity error");
                            }
                            _ => {
                                defmt::error!("Unknown Uart error");
                            }
                        }
                        // Reset parser after any UART error to prevent state corruption
                        midi_uart.reset_parser();

                        // Invalidate running status tracking for this channel
                        // Send control message to write_uart task to clear cached status.
                        // This ensures the output task won't inject stale status bytes
                        // after the parser has been reset.
                        CHANNEL
                            .send(ChannelMessage::Control(
                                ControlMessage::InvalidateRunningStatus(uart_channel),
                            ))
                            .await;
                    }
                    UartMidiError::MessageError(err) => {
                        // MIDI protocol errors require parser reset and running status invalidation
                        match err {
                            MidiMessageError::DuplicateStatus => {
                                defmt::error!("Duplicate status byte");
                            }
                            MidiMessageError::UnexpectedDataByte => {
                                defmt::error!("Unexpected data byte");
                            }
                            MidiMessageError::UnknownStatus => {
                                defmt::error!("Unknown status byte");
                            }
                            MidiMessageError::InvalidStatusByte => {
                                defmt::error!("Invalid/undefined status byte");
                            }
                        }
                        // Reset parser after any message error to prevent state corruption
                        midi_uart.reset_parser();

                        // Invalidate running status tracking for this channel
                        // Send control message to write_uart task to clear cached status.
                        // This ensures the output task won't inject stale status bytes
                        // after the parser has been reset.
                        CHANNEL
                            .send(ChannelMessage::Control(
                                ControlMessage::InvalidateRunningStatus(uart_channel),
                            ))
                            .await;
                    }
                }
            }
        }
    }
}

#[embassy_executor::task]
async fn read_uart0(usart: BufferedUartRx<'static, UART0>) {
    read_from_uart(usart, UartChannel::Zero).await
}

#[embassy_executor::task]
async fn read_uart1(usart: BufferedUartRx<'static, UART1>) {
    read_from_uart(usart, UartChannel::One).await
}

// ============================================================================
// MAIN - System initialization and task spawning
// ============================================================================

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    defmt::info!("Initializing...");

    let peripherals = embassy_rp::init(Default::default());

    // Bind UART interrupts to handlers
    // BufferedUart uses interrupts (not DMA) to transfer data between hardware
    // and software buffers, which is more efficient for byte-by-byte protocols
    // like MIDI where data arrives in small bursts.
    bind_interrupts!(struct Irqs {
        UART0_IRQ => BufferedInterruptHandler<UART0>;
        UART1_IRQ => BufferedInterruptHandler<UART1>;
    });

    // MIDI standard baud rate: 31,250 bits/sec
    // This unusual rate was chosen in 1983 to work with available clock crystals
    let mut uart_config = Config::default();
    uart_config.baudrate = 31250;

    // UART0: Bidirectional (receives input 1, transmits merged output)
    // Uses BufferedUart for efficient interrupt-driven I/O with background buffering
    //
    // How BufferedUart works:
    // 1. Hardware UART receives bytes and triggers interrupt
    // 2. Interrupt handler copies bytes from hardware FIFO to software buffer (UART0_RX_BUF)
    // 3. Application reads from software buffer asynchronously (no waiting for hardware)
    // 4. This decouples hardware timing from application logic
    //
    // Safety: We use unsafe to pass static mut buffers. This is safe because:
    // - Each buffer is used by only one UART instance
    // - BufferedUart takes ownership and manages exclusive access
    let usart0 = BufferedUart::new(
        peripherals.UART0,  // Hardware peripheral
        Irqs,               // Interrupt bindings
        peripherals.PIN_12, // TX pin (output to MIDI OUT)
        peripherals.PIN_13, // RX pin (input from MIDI IN 1)
        // Safe: Each static buffer is used by only one UART instance
        // Using addr_of_mut!() to avoid direct mutable static reference
        unsafe { &mut *core::ptr::addr_of_mut!(UART0_TX_BUF) }, // TX buffer for outgoing data
        unsafe { &mut *core::ptr::addr_of_mut!(UART0_RX_BUF) }, // RX buffer for incoming data
        uart_config,
    );

    // Split UART0 into separate TX and RX handles
    // This allows independent operation: one task writes, another reads
    let (usart0_tx, usart0_rx) = usart0.split();

    // UART1: Receive-only (input 2)
    // We only need RX for this input, so we create a BufferedUartRx directly
    // instead of creating a full BufferedUart and splitting it
    let usart1_rx = BufferedUartRx::new(
        peripherals.UART1, // Hardware peripheral
        Irqs,              // Interrupt bindings
        peripherals.PIN_5, // RX pin (input from MIDI IN 2)
        // Safe: Each static buffer is used by only one UART instance
        // Using addr_of_mut!() to avoid direct mutable static reference
        unsafe { &mut *core::ptr::addr_of_mut!(UART1_RX_BUF) }, // RX buffer for incoming data
        uart_config,
    );

    defmt::info!("Initialized.");

    // Spawn async tasks
    // Each task runs concurrently, scheduled by the Embassy executor
    spawner
        .spawn(read_uart0(usart0_rx))
        .expect("Failed to spawn read_uart0 task");
    spawner
        .spawn(read_uart1(usart1_rx))
        .expect("Failed to spawn read_uart1 task");
    spawner
        .spawn(write_uart(usart0_tx))
        .expect("Failed to spawn write_uart task");
}
