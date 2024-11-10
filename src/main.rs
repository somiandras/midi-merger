#![no_std]
#![no_main]

use defmt_rtt as _;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::peripherals::{UART0, UART1};
use embassy_rp::uart::{Async, Config, Error, InterruptHandler, Uart, UartRx, UartTx};
use embassy_sync::blocking_mutex::raw::ThreadModeRawMutex;
use embassy_sync::channel::Channel;
use panic_probe as _;

static CHANNEL: Channel<ThreadModeRawMutex, [u8; 3], 10> = Channel::new();

#[embassy_executor::task]
async fn write_uart(mut usart: UartTx<'static, UART0, Async>) {
    defmt::info!("Write");
    loop {
        let data = CHANNEL.receive().await;
        defmt::info!("Data: {=[u8; 3]}", data);
        usart.write(&data).await.unwrap()
    }
}

#[embassy_executor::task]
async fn read_uart0(mut usart: UartRx<'static, UART0, Async>) {
    let mut buffer: [u8; 3] = [0x00; 3];
    loop {
        let value = usart.read(&mut buffer).await;
        match value {
            Ok(_) => CHANNEL.send(buffer).await,
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
async fn read_uart1(mut usart: UartRx<'static, UART1, Async>) {
    let mut buffer: [u8; 3] = [0x00; 3];
    loop {
        let value = usart.read(&mut buffer).await;
        match value {
            Ok(_) => CHANNEL.send(buffer).await,
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
        peripherals.PIN_9,
        Irqs,
        peripherals.DMA_CH2,
        uart_config,
    );

    defmt::info!("Initialized.");
    spawner.spawn(read_uart0(usart0_rx)).unwrap();
    spawner.spawn(read_uart1(usart1_rx)).unwrap();
    spawner.spawn(write_uart(usart0_tx)).unwrap();
}
