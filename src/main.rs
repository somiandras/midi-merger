#![no_std]
#![no_main]
#![feature(iter_chain)]

use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::peripherals::{UART0, UART1};
use embassy_rp::uart::{Async, Config, Error, Instance, InterruptHandler, Uart, UartRx, UartTx};
use embassy_sync::blocking_mutex::raw::ThreadModeRawMutex;
use embassy_sync::channel::Channel;
use heapless::Vec;
use midi_uart::MidiUart;
use panic_probe as _;

mod midi_parser;
mod midi_uart;

static CHANNEL: Channel<ThreadModeRawMutex, Vec<u8, 3>, 10> = Channel::new();

#[embassy_executor::task]
async fn write_uart(mut usart: UartTx<'static, UART0, Async>) {
    defmt::info!("Write");
    loop {
        let message = CHANNEL.receive().await;
        usart.write(&message).await.unwrap()
    }
}

async fn read_from_uart(usart: UartRx<'static, impl Instance, Async>, channel: usize) {
    let mut midi_usart = MidiUart::new(usart, channel);
    loop {
        let message = midi_usart.read().await.unwrap();
    }
}

#[embassy_executor::task]
async fn read_uart0(usart: UartRx<'static, UART0, Async>) {
    read_from_uart(usart, 0).await
}

#[embassy_executor::task]
async fn read_uart1(usart: UartRx<'static, UART1, Async>) {
    read_from_uart(usart, 1).await
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
