#![no_std]
#![no_main]
#![feature(async_closure)]

use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::peripherals::{UART0, UART1};
use embassy_rp::uart::{Async, Config, Error, Instance, InterruptHandler, Uart, UartRx, UartTx};
use embassy_sync::blocking_mutex::raw::ThreadModeRawMutex;
use embassy_sync::channel::Channel;
use heapless::Vec;
use midi_parser::MidiMessage;
use panic_probe as _;

mod midi_parser;

static CHANNEL: Channel<ThreadModeRawMutex, Vec<u8, 3>, 10> = Channel::new();

#[embassy_executor::task]
async fn write_uart(mut usart: UartTx<'static, UART0, Async>) {
    defmt::info!("Write");
    loop {
        let message = CHANNEL.receive().await;
        usart.write(&message).await.unwrap();
    }
}

async fn read_from_uart(mut usart: UartRx<'static, impl Instance, Async>) {
    let mut buffer: [u8; 3] = [0x00; 3];
    let mut parser = midi_parser::MidiParser::default();
    loop {
        let result = usart.read(&mut buffer).await;
        match result {
            Ok(_) => {
                for byte in &buffer {
                    if let Some(message) = parser.feed_byte(byte) {
                        match message {
                            MidiMessage::SystemRealtime(data) => CHANNEL.send(data).await,
                            MidiMessage::Message(data) => CHANNEL.send(data).await,
                            MidiMessage::RunningStatus(data) => CHANNEL.send(data).await,
                        }
                    };
                }
            }
            Err(err) => match err {
                Error::Break => defmt::error!("Error: Break"),
                Error::Framing => defmt::error!("Error: Framing"),
                Error::Overrun => defmt::error!("Error: Overrun"),
                Error::Parity => defmt::error!("Error: Parity"),
                _ => defmt::error!("Other error"),
            },
        }
    }
}

#[embassy_executor::task]
async fn read_uart0(usart: UartRx<'static, UART0, Async>) {
    read_from_uart(usart).await
}

#[embassy_executor::task]
async fn read_uart1(usart: UartRx<'static, UART1, Async>) {
    read_from_uart(usart).await
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
