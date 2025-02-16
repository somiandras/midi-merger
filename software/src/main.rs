#![no_std]
#![no_main]

use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::peripherals::{UART0, UART1};
use embassy_rp::uart::{Async, Config, Instance, InterruptHandler, Uart, UartRx, UartTx};
use embassy_sync::blocking_mutex::raw::ThreadModeRawMutex;
use embassy_sync::channel::Channel;
use midi_parser::MidiMessage;
use midi_uart::{MidiUart, UartChannel, UartMidiMessage};
use panic_probe as _;

mod midi_parser;
mod midi_uart;

static CHANNEL: Channel<ThreadModeRawMutex, UartMidiMessage, 10> = Channel::new();

#[derive(Debug, Default)]
struct UartStatus {
    uart0: Option<u8>,
    uart1: Option<u8>,
    last_tx_from: Option<UartChannel>,
}

#[embassy_executor::task]
async fn write_uart(mut usart: UartTx<'static, UART0, Async>) {
    let mut uart_status = UartStatus::default();
    loop {
        let message = CHANNEL.receive().await;
        match message.message {
            MidiMessage::Voice(data) => {
                match message.uart_channel {
                    // Set the current status for the corresponding channel
                    UartChannel::Zero => uart_status.uart0 = Some(data[0]),
                    UartChannel::One => uart_status.uart1 = Some(data[0]),
                }
                usart.write(&data).await.unwrap();
            }
            MidiMessage::SystemCommon(data) | MidiMessage::SystemRealtime(data) => {
                // Nothing to do, immediately send
                usart.write(&data).await.unwrap();
            }
            MidiMessage::RunningStatus(data) => {
                if let Some(prev_channel) = uart_status.last_tx_from {
                    if prev_channel != message.uart_channel {
                        // we already got another message from the other channel so
                        // running status is not valid anymore,
                        // send the proper status first
                        match message.uart_channel {
                            UartChannel::Zero => {
                                usart.write(&[uart_status.uart0.unwrap()]).await.unwrap()
                            }
                            UartChannel::One => {
                                usart.write(&[uart_status.uart1.unwrap()]).await.unwrap()
                            }
                        }
                    }
                }

                usart.write(&data).await.unwrap()
            }
        }
        uart_status.last_tx_from = Some(message.uart_channel)
    }
}

async fn read_from_uart(usart: UartRx<'static, impl Instance, Async>, channel: UartChannel) {
    let mut midi_usart = MidiUart::new(usart, channel);
    loop {
        let message = midi_usart.read().await.unwrap();
        CHANNEL.send(message).await;
    }
}

#[embassy_executor::task]
async fn read_uart0(usart: UartRx<'static, UART0, Async>) {
    read_from_uart(usart, UartChannel::Zero).await
}

#[embassy_executor::task]
async fn read_uart1(usart: UartRx<'static, UART1, Async>) {
    read_from_uart(usart, UartChannel::One).await
}

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    defmt::info!("Initializing...");

    let peripherals = embassy_rp::init(Default::default());

    bind_interrupts!(struct Irqs {
        UART0_IRQ => InterruptHandler<UART0>;
        UART1_IRQ => InterruptHandler<UART1>;
    });

    let mut uart_config = Config::default();
    uart_config.baudrate = 31250;

    let usart0 = Uart::new(
        peripherals.UART0,
        peripherals.PIN_12,
        peripherals.PIN_13,
        Irqs,
        peripherals.DMA_CH0,
        peripherals.DMA_CH1,
        uart_config,
    );

    let (usart0_tx, usart0_rx) = usart0.split();

    let usart1_rx = UartRx::new(
        peripherals.UART1,
        peripherals.PIN_5,
        Irqs,
        peripherals.DMA_CH2,
        uart_config,
    );

    defmt::info!("Initialized.");
    spawner.spawn(read_uart0(usart0_rx)).unwrap();
    spawner.spawn(read_uart1(usart1_rx)).unwrap();
    spawner.spawn(write_uart(usart0_tx)).unwrap();
}
