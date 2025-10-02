# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Embedded Rust project for a 2-in, 4-out MIDI merge box running on Raspberry Pi Pico (RP2040). Uses Embassy async framework for concurrent UART handling.

## Build & Run Commands

```bash
# Build and flash to device (development mode with debug logging)
cd software
cargo run

# Build and flash release version with error-only logging
cd software
./build_release.sh
# Or manually:
DEFMT_LOG=error cargo run --release
```

The target is configured in `.cargo/config.toml` as `thumbv6m-none-eabi` with `probe-rs` as the runner.

## Architecture

### Core Components

- **main.rs**: Embassy executor setup with three async tasks:
  - `read_uart0` / `read_uart1`: Read from two MIDI inputs concurrently
  - `write_uart`: Merge and output messages from both inputs to UART0 TX

- **midi_parser.rs**: Stateful MIDI parser implementing MIDI 1.0 spec
  - Handles running status (messages without repeated status bytes)
  - Distinguishes Voice, SystemCommon, and SystemRealtime messages
  - Tracks expected data bytes per message type (0-2 bytes)

- **midi_uart.rs**: UART wrapper that feeds bytes into MidiParser
  - Wraps `UartRx` with a `MidiParser` instance
  - Tags messages with source `UartChannel` (Zero or One)

### Message Flow

1. Both UART inputs read bytes asynchronously
2. Each byte is fed to the input's `MidiParser`
3. Complete messages are wrapped in `UartMidiMessage` and sent to shared `Channel`
4. `write_uart` task receives messages and handles:
   - Running status validation across different input channels
   - Injecting status bytes when switching between channels
   - Direct passthrough of SystemRealtime and SystemCommon messages

### Running Status Handling

The `write_uart` task maintains per-channel status bytes (`uart_status.uart0`, `uart_status.uart1`) and tracks which channel last sent a message. When receiving a running status message from a different channel than the previous message, it automatically injects the appropriate status byte to maintain MIDI compliance on the merged output.

## Key Technical Details

- UART baudrate: 31250 (MIDI standard)
- Embassy channel capacity: 10 messages
- No heap allocation (`#![no_std]`)
- Uses `heapless::Vec` for fixed-size buffers
- Logging via `defmt` with RTT transport
