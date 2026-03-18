# Pico Synth — Developer Context Document

This document is intended to give a future Claude session full context to continue
development of this project. Read this before touching any code.

---

## Project Overview

A portable, battery-powered synthesizer built on a Raspberry Pi Pico W. It plays
chords and drum sounds triggered by a 4×4 RGB keypad. All synthesis is done in
real-time on the RP2040 — no samples, no SD card. The interface is a 128×64 OLED
display navigated with a rotary encoder. Three potentiometers control volume and
vibrato. The firmware is written in **C using the Pico SDK** (not MicroPython —
MicroPython was the original approach but was abandoned due to repeated-note bugs
and performance limits).

---

## Hardware

### Core

| Component | Part | Notes |
|---|---|---|
| MCU | Raspberry Pi Pico W (RP2040) | Dual-core Cortex-M0+ @ 133 MHz, 264 KB RAM |
| Keypad | Pimoroni Pico RGB Keypad Base | 4×4 grid, APA102 LEDs, plugs onto Pico header |
| Display | Inland IIC/SPI 1.3" 128×64 OLED V2.0 | SH1106 controller, I2C mode |
| DAC | Adafruit TLV320DAC3100 | I2S audio in, headphone/speaker amp out |
| Encoder | Inland KS0013 Keystudio Rotary Encoder | CLK/DT/SW, active-low with pull-ups |
| Pots | 3× potentiometers | 10kΩ recommended |
| Speaker | 4–8 Ω small speaker | Connected to DAC speaker output |
| Power | USB power bank | 5V USB, 150–250 mA typical draw |

### Pimoroni RGB Keypad Base — Critical Details

The keypad plugs directly onto the Pico's pin header. Its on-board ICs are:

- **Button IC**: TCA9555 16-bit GPIO expander at I2C address **0x20**
  - NOT an MCP23017 — this was a major bug in early firmware
  - Read buttons from registers **0x00** (port 0) and **0x01** (port 1)
  - Config registers 0x06/0x07 default to all-inputs; no pull-up config needed
  - Buttons are active-low (pressed = 0); firmware inverts with `~`
- **LEDs**: APA102 (DotStar) driven via SPI
  - Protocol: start frame (4× 0x00) + 16 LED frames (0xFF, B, G, R) + end frame
  - **SPI pins are fixed by the hardware header**: SCK = GP6, MOSI = GP3

---

## Pin Assignments (Verified Working)

```
I2S Audio (PIO + DMA):
  GP9   BCLK  (bit clock)
  GP10  WS    (word select / LRCLK) — must be BCLK+1
  GP11  DATA

SPI0 — APA102 LEDs:
  GP3   MOSI  ← Pimoroni hardware pin (not GP7 or GP19)
  GP6   SCK   ← Pimoroni hardware pin
  GP17  CS    (set high; APA102 ignores CS)

I2C0 (GP4/GP5) — shared bus, three devices:
  GP4   SDA
  GP5   SCL
  ├── TCA9555  @ 0x20  (keypad button IC)
  ├── TLV320DAC3100 @ 0x18  (audio DAC config)
  └── SH1106 OLED @ 0x3C  (display)
  NOTE: OLED and DAC must be wired to GP4/GP5, NOT GP20/GP21.
        The README wiring diagram says GP20/GP21 but that conflicts
        with the encoder DT pin (GP21). Correct wiring is GP4/GP5.

ADC:
  GP26  ADC0  Volume pot
  GP27  ADC1  Vibrato Rate pot
  GP28  ADC2  Vibrato Depth pot

Rotary Encoder (active-low, GPIO_PULL_UP):
  GP12  CLK
  GP21  DT    ← confirmed working in hardware
  GP22  SW
```

### I2C Bus Warning

GP20 and GP21 are I2C0 pins on the RP2040 (alternate mapping). GP4 and GP5 are
also I2C0. You **cannot** run i2c0 on both pin pairs simultaneously. The original
README described OLED and DAC on GP20/21, which conflicts with the encoder on
GP21. The correct solution (already in firmware) is to put all I2C devices on
GP4/GP5 and physically rewire the OLED and DAC to those pins.

---

## Software Architecture

### Files

```
synth/
├── main.c          — entire firmware (single-file C)
├── i2s.pio         — PIO program for I2S audio output
├── CMakeLists.txt  — Pico SDK CMake build
├── README.md       — original project spec and user-facing docs
└── SYNTH_CONTEXT.md — this file
```

### Build

```bash
# First time:
mkdir build && cd build
cmake ..
make -j4

# Subsequent builds:
cd build && make -j4

# Clean rebuild:
cd build && make clean && make -j4

# Full clean (reset CMake):
rm -rf build && mkdir build && cd build && cmake .. && make -j4
```

**Toolchain**: arm-none-eabi-gcc (`brew install --cask gcc-arm-embedded`),
CMake (`brew install cmake`), Pico SDK at `~/pico-sdk`
(`export PICO_SDK_PATH=~/pico-sdk` — add to `~/.zshrc`).

**Flash**: Hold BOOTSEL, plug USB, copy `build/pico_synth.uf2` to `RPI-RP2` drive.

**Serial debug**: `screen /dev/tty.usbmodem* 115200` — USB serial is enabled.

### Audio Engine

- **PIO I2S**: `i2s.pio` implements a 16-bit stereo I2S master.
  - 64 PIO cycles per stereo sample
  - Clock divider = sys_clk / (sample_rate × 64)
  - Word format: bits 31–16 = left channel, bits 15–0 = right channel
- **DMA**: Ping-pong double buffer. DMA plays one buffer while CPU fills the other.
  - `audio_buf[2][BUFFER_SIZE]` — `uint32_t`, each word = packed stereo sample
  - IRQ handler swaps buffers and sets `need_fill = true`
  - Main loop calls `fill_audio_buffer()` when `need_fill` is set
- **Sample rate**: 16,000 Hz, 16-bit stereo
- **Buffer size**: 256 samples (~16 ms per buffer)

### Synthesis

- **Polyphony**: 4 simultaneous voices (`MAX_VOICES = 4`)
- **Waveforms**: Sine (lookup table), Square, Triangle, Sawtooth
  - Selected globally via encoder rotation
- **Dual oscillator**: Each voice has two oscillators with configurable
  coarse/fine detune and mix ratio
- **ADSR envelope** (integer, 0–32767):
  - Attack: 10 ms (single-buffer clamp)
  - Decay: 100 ms to sustain level (0.7 × 32767)
  - Release: 200 ms fade
- **LFO / Vibrato**: Sine LFO modulates pitch. Rate (2–8 Hz) and depth
  (0–50 cents) set by pots.
- **Drum synthesis**: 8 pre-computed drum types (Kick, Snare, Hi-Hat, Clap,
  Tom×3, Cymbal). Drum buffers (`drum_buf[8][2048]`) generated at startup
  using noise + sine synthesis. Only one drum plays at a time (voice slot
  separate from chord voices).

### Key Assignment System

Each of the 16 keys has a `KeyAssign` struct:

```c
typedef struct {
    KeyType type;        // KEY_DRUM or KEY_CHORD
    int     root;        // drum index (0-7) or MIDI root note
    int     intervals[4]; // semitone offsets from root (e.g. {0,4,7} for major)
    int     n_intervals; // number of notes in chord (2-4)
} KeyAssign;
```

**Chord types** (interval arrays):
| Name | Intervals | n |
|------|-----------|---|
| Major | 0, 4, 7 | 3 |
| Minor | 0, 3, 7 | 3 |
| Dom7 | 0, 4, 7, 10 | 4 |
| Maj7 | 0, 4, 7, 11 | 4 |
| Min7 | 0, 3, 7, 10 | 4 |
| Dim | 0, 3, 6 | 3 |
| Aug | 0, 4, 8 | 3 |
| Sus2 | 0, 2, 7 | 3 |
| Sus4 | 0, 5, 7 | 3 |

**Default layout:**
```
Row 0 (drums, white LEDs):     Kick, Snare, Hi-Hat, Clap
Row 1 (major chords, green):   C, D, E, F Major
Row 2 (minor chords, blue):    C, D, E, F Minor (octave lower)
Row 3 (mixed):                 G Major, A Minor, B Dim, C+ Maj7
```

### Display (OLED)

Driver: custom SH1106 I2C driver in `main.c`.

- **Framebuffer**: `oled_buf[128 × 8]` = 1024 bytes (one byte per 8-pixel column strip)
- **Font**: 5×7 monospace, stored as 5-byte column bitmaps (bit 0 = topmost row)
  Covers ASCII 0x20 (space) through 0x5A ('Z').
- **Page addressing**: SH1106 uses page-mode only (8 pages × 128 columns).
  Column offset = 2 (SH1106 has 132-column internal buffer, 128 visible).
- **Current GUI**: `draw_grid()` renders the 4×4 key assignment grid.
  - Each cell: 32×16 px, 4-char uppercase label centered
  - Held keys shown as inverted (white block, dark text)
  - Refreshed at ~10 Hz from main loop

### LED Control (APA102)

`update_leds()` sends the full APA102 frame over SPI0:
- Start frame: 4× `0x00`
- 16 LED frames: `0xFF`, blue, green, red (APA102 byte order)
- End frame: 4× `0xFF`
- Brightness set in the `0xFF` frame byte (bits 0–4, 0–31)

**LED color scheme:**
- White (dimmed): drums
- Green: major chords
- Blue: minor chords
- Yellow: 7th chords
- Purple: diminished
- Orange: augmented
- Cyan: suspended
- Bright white (full): key currently held

### Button Debounce

Press: key must appear in `PRESS_SCANS = 3` consecutive scans before registering.
Release: key must be absent for `RELEASE_SCANS = 5` consecutive scans before releasing.
State tracked in `press_count[16]` and `release_count[16]`.

### Encoder

- CLK edge detection (falling edge = movement)
- Direction: DT high when CLK falls = clockwise (+1), DT low = counter-clockwise (-1)
- Button: 300 ms debounce timer, active-low
- Currently: rotation changes `current_waveform` (0–3 cycling through sine/square/triangle/saw)

### Pot Reading

ADC multiplexed via `adc_select_input()`:
- Volume: maps 0–4095 → 0–100 (percent)
- Vibrato rate: maps 0–4095 → 2–8 Hz
- Vibrato depth: maps 0–4095 → 0–50 cents

---

## TLV320DAC3100 Init

The DAC is configured via I2C at startup (`tlv_init()`). It is set to:
- 16-bit I2S slave mode (Pico generates clocks)
- Internal charge pump
- Speaker output enabled

The DAC's I2C address is 0x18. It must be wired to GP4/GP5 (same I2C0 bus as
TCA9555 and OLED).

---

## Development History and Bug Fixes

Understanding these bugs prevents re-introducing them:

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Notes repeating on single press (MicroPython) | No press debounce; bouncy key reset envelope | Added `_press_count` dict + retrigger guard in `note_on()` |
| Switched to C | MicroPython performance/stability limits | Full rewrite using Pico SDK |
| `pico_generate_pio_header` CMake error | Called before `add_executable` | Swap order in CMakeLists.txt |
| `ADC_VOL` undeclared | Name clashed with Pico SDK register macros | Renamed to `POT_CH_VOL` etc. |
| Buttons not registering | I2C on wrong pins (GP20/21 instead of GP4/5) | Changed `PIN_I2C_SDA/SCL` to 4/5 |
| Encoder not working | Wrong pins (GP13/14 instead of GP21/22) | Changed `PIN_ENC_DT/SW` to 21/22 |
| `BTN raw=0x8888` (constant, no button response) | Wrong chip type — code used MCP23017 registers (0x12/0x13) but hardware has TCA9555 (reads from 0x00/0x01) | Changed all button register reads to TCA9555 map |
| LEDs not working | Wrong SPI MOSI pin: code used GP7/GP19, hardware uses GP3 | Changed `PIN_APA_MOSI` to 3 |
| OLED not working | Not implemented in C yet + wrong wiring (GP20/21 conflicts with encoder DT on GP21) | Added SH1106 driver; rewire OLED to GP4/GP5 |

---

## Current Status (as of last session)

| Feature | Status |
|---------|--------|
| Audio synthesis (chords) | ✅ Working |
| Audio synthesis (drums) | ✅ Working |
| Buttons (TCA9555) | ✅ Working |
| Rotary encoder | ✅ Working |
| Waveform switching | ✅ Working (encoder rotation) |
| Pot reading (volume/vibrato) | ✅ Working |
| APA102 LEDs | ⚠️ Pin fixed (GP3), needs hardware verification after flash |
| OLED display | ⚠️ Driver written, needs hardware rewire (OLED SDA→GP4, SCL→GP5) |
| Key assignment GUI | 🔲 Not implemented (shows grid, no edit menu yet) |
| Preset save/load (flash) | 🔲 Not implemented |
| 6-voice polyphony | 🔲 Currently 4 voices (`MAX_VOICES = 4`) |

---

## Objectives / Planned Features

These are the original goals not yet completed:

1. **Key assignment menu** — Press encoder to enter edit mode for the selected key.
   Navigate type (drum/chord), root note, chord type. Auto-save to flash.

2. **Flash persistence** — Save `key_assign[16]` to Pico's onboard flash using
   Pico SDK's `hardware/flash.h` so layout survives power-off.

3. **Increase polyphony to 6 voices** — Currently `MAX_VOICES = 4`. The RP2040
   should handle 6 at 16 kHz. Test CPU load before increasing further.

4. **Delay effect** — Simple feedback delay (~44 KB RAM for a 500 ms buffer at
   16 kHz). Check RAM budget before implementing.

5. **Info display mode** — Show waveform name, currently playing note, volume bar,
   and vibrato bars on OLED during playback. Auto-return to grid after 3 seconds.

6. **USB MIDI output** — Send MIDI note-on/off messages over USB so the synth
   can drive external software. Pico SDK's `tinyusb` handles this but requires
   CMakeLists changes to enable the USB MIDI class.

7. **Startup animation** — Brief splash screen on OLED at boot.

---

## Key Constants to Know

```c
#define SAMPLE_RATE    16000     // Hz
#define BUFFER_SIZE    256       // samples per DMA buffer (~16 ms)
#define MAX_VOICES     4         // polyphonic voices
#define DRUM_SAMPLES   2048      // pre-computed drum buffer length
#define PRESS_SCANS    3         // debounce: scans before press confirmed
#define RELEASE_SCANS  5         // debounce: scans before release confirmed
#define MCP_ADDR       0x20      // TCA9555 (named MCP for historical reasons)
#define TLV_ADDR       0x18      // TLV320DAC3100 DAC
#define OLED_ADDR      0x3C      // SH1106 OLED
```

---

## Hardware Purchase Links (from README)

- Keypad: https://www.microcenter.com/product/633763/pimoroni-pico-rgb-keypad-base
- DAC: https://www.adafruit.com/product/6309
- OLED: https://www.microcenter.com/product/643965/inland-iic-spi-13-128x64-oled-v20-graphic-display-module-for-arduino-uno-r3
- Encoder: https://www.microcenter.com/product/618904/inland-ks0013-keystudio-rotary-encoder-module
