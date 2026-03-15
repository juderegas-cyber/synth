"""
main.py  -  PICO SYNTH v5.2
-----------------------------
Root-cause fixes:

CHOPPY / FREEZING
  Removed _thread entirely. MicroPython I2S + _thread on RP2040 has a
  known issue where I2S DMA and the Python thread scheduler fight over
  the same interrupt priorities, causing both glitches and hard freezes.
  Single-core is simpler and more reliable for this use case.

  ibuf = 8192 bytes = 128ms of audio buffered inside the I2S peripheral.
  audio_out.write() returns immediately as long as ibuf has space.
  The display SPI (~10ms) is absorbed by ibuf with 100ms to spare.
  Display is only redrawn every 300ms so it rarely blocks at all.

WRONG LED COLORS
  update_leds() is now called every single main loop iteration, not just
  on key changes. If the keypad library ever touches the LED buffer
  internally, we correct it within one loop cycle (~20ms).

CHOPPY SYNTHESIS
  Phase accumulators persist across loop iterations so there is never
  a discontinuity at the buffer boundary. Integer math throughout.
"""

from machine import Pin, I2S, ADC
from array import array
import math
import time
import picokeypad
from gui import (
    GridGUI, read_pots,
    CHORD_MAJOR, CHORD_MINOR, CHORD_DOM7, CHORD_MAJ7, CHORD_MIN7,
    CHORD_DIM, CHORD_AUG, CHORD_SUS2, CHORD_SUS4,
    WAVEFORM_SINE, WAVEFORM_SQUARE, WAVEFORM_TRIANGLE, WAVEFORM_SAWTOOTH,
)

print("=" * 50)
print("  PICO SYNTH v5.2")
print("=" * 50)

# ── Audio constants ───────────────────────────────────────────────────────────

SAMPLE_RATE  = 16000
BUFFER_SIZE  = 256            # 16 ms of audio per write
BUFFER_BYTES = BUFFER_SIZE * 4
MAX_VOICES   = 6
DRUM_SAMPLES = 2048

# ── Sine lookup table ─────────────────────────────────────────────────────────
SINE_TABLE = array('h', [int(math.sin(2.0 * math.pi * i / 256) * 32767)
                          for i in range(256)])

# ── Phase accumulators (persist across writes — no boundary clicks) ───────────
phases  = [0] * MAX_VOICES    # 24-bit position, 0-16777215
incs    = [0] * MAX_VOICES    # added to phase each sample
n_voices = 0                  # active voice count
chord_notes_display = []      # for the OLED info screen

current_waveform = WAVEFORM_SINE

# ── ADSR (integer, 0-32767) ───────────────────────────────────────────────────
BUF_MS        = 1000.0 * BUFFER_SIZE / SAMPLE_RATE   # 16 ms
SUSTAIN_INT   = int(0.7 * 32767)
ATTACK_STEP   = max(1, int(32767 / max(1.0,  10.0 / BUF_MS)))
DECAY_STEP    = max(1, int((32767 - SUSTAIN_INT) / max(1.0, 100.0 / BUF_MS)))
RELEASE_STEP  = max(1, int(SUSTAIN_INT / max(1.0, 200.0 / BUF_MS)))

env_amp   = 0        # 0-32767
env_state = 0        # 0=idle 1=attack 2=decay 3=sustain 4=release

ENV_IDLE    = 0
ENV_ATTACK  = 1
ENV_DECAY   = 2
ENV_SUSTAIN = 3
ENV_RELEASE = 4

# ── Drum state ────────────────────────────────────────────────────────────────
active_drum = None   # bytearray or None
drum_pos    = 0      # sample index 0-DRUM_SAMPLES

# ── Output buffers ────────────────────────────────────────────────────────────
out_buf = bytearray(BUFFER_BYTES)
silence = bytearray(BUFFER_BYTES)

# ── I2S — large ibuf absorbs display/input delays ────────────────────────────
audio_out = I2S(
    0,
    sck=Pin(9), ws=Pin(10), sd=Pin(11),
    mode=I2S.TX, bits=16, format=I2S.STEREO,
    rate=SAMPLE_RATE,
    ibuf=8192,       # 128ms headroom — display SPI can't touch this
)
print("I2S ready  ibuf=8192")

# ── Synthesis ─────────────────────────────────────────────────────────────────

def midi_to_inc(midi_note):
    """Phase increment for a given MIDI note (integer, 24-bit scale)."""
    freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    return int(freq * 16777216.0 / SAMPLE_RATE)

def set_voices(notes):
    """Set phase increments for a list of MIDI notes. Does NOT reset phases."""
    global n_voices, chord_notes_display
    seen  = []
    added = set()
    for n in notes:
        if n not in added:
            added.add(n)
            seen.append(n)
        if len(seen) >= MAX_VOICES:
            break
    chord_notes_display = seen
    for i in range(len(seen)):
        incs[i] = midi_to_inc(seen[i])
    n_voices = len(seen)

def fill_audio():
    """
    Fill out_buf with one BUFFER_SIZE chunk of audio and write to I2S.
    Phase accumulators carry over from last call — no clicks at boundaries.
    Envelope applied as integer multiply-shift (no float division).
    """
    global env_amp, env_state, active_drum, drum_pos

    # ── Advance ADSR once per buffer ──────────────────────────────────────
    if env_state == ENV_ATTACK:
        env_amp += ATTACK_STEP
        if env_amp >= 32767:
            env_amp   = 32767
            env_state = ENV_DECAY
    elif env_state == ENV_DECAY:
        env_amp -= DECAY_STEP
        if env_amp <= SUSTAIN_INT:
            env_amp   = SUSTAIN_INT
            env_state = ENV_SUSTAIN
    elif env_state == ENV_RELEASE:
        env_amp -= RELEASE_STEP
        if env_amp <= 0:
            env_amp   = 0
            env_state = ENV_IDLE

    has_chord = n_voices > 0 and env_state != ENV_IDLE
    has_drum  = active_drum is not None

    if not has_chord and not has_drum:
        audio_out.write(silence)
        return

    # Amplitude scale: env_amp divided among voices to keep volume constant
    nv    = n_voices if has_chord else 1
    scale = env_amp // nv if has_chord else 0
    wf    = current_waveform

    # Cache locals for speed inside the tight loop
    ph = phases
    ic = incs
    st = SINE_TABLE

    for i in range(BUFFER_SIZE):

        # ── Chord synthesis (integer phase accumulator) ───────────────────
        cs = 0
        if scale > 0:
            for v in range(n_voices):
                pb = (ph[v] >> 16) & 255
                if wf == 0:    # SINE
                    cs += st[pb]
                elif wf == 1:  # SQUARE
                    cs += 32767 if pb < 128 else -32767
                elif wf == 2:  # TRIANGLE
                    cs += (pb * 516 - 32767) if pb < 128 else ((255 - pb) * 516 - 32767)
                else:          # SAWTOOTH
                    cs += pb * 256 - 32768
                ph[v] = (ph[v] + ic[v]) & 0xFFFFFF
            cs = cs * scale >> 15

        # ── Drum sample playback ──────────────────────────────────────────
        ds = 0
        if has_drum:
            di  = drum_pos * 4
            s16 = active_drum[di] | (active_drum[di + 1] << 8)
            if s16 >= 32768:
                s16 -= 65536
            ds       = s16
            drum_pos += 1
            if drum_pos >= DRUM_SAMPLES:
                active_drum = None
                has_drum    = False

        # ── Mix, clamp, write stereo ──────────────────────────────────────
        s = cs + ds
        if s >  32767: s =  32767
        if s < -32768: s = -32768
        if s < 0:      s += 65536

        j = i * 4
        out_buf[j]     = s & 0xFF
        out_buf[j + 1] = (s >> 8) & 0xFF
        out_buf[j + 2] = s & 0xFF
        out_buf[j + 3] = (s >> 8) & 0xFF

    audio_out.write(out_buf)

# ── Drum synthesis ────────────────────────────────────────────────────────────

_ns = 99991

def _noise():
    global _ns
    _ns = (_ns * 1664525 + 1013904223) & 0xFFFFFFFF
    return ((_ns >> 16) & 0xFFFF) / 32768.0 - 1.0

def _make_drum(t):
    buf = bytearray(DRUM_SAMPLES * 4)
    for i in range(DRUM_SAMPLES):
        ts = i / SAMPLE_RATE
        if t == 0:
            freq = max(50.0, 160.0 - ts * 1800.0)
            env  = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.7))
            s    = math.sin(2.0 * math.pi * freq * ts) * env * 0.95
        elif t == 1:
            en = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.35))
            et = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.15))
            s  = (_noise() * 0.75 * en
                  + math.sin(2.0 * math.pi * 200.0 * ts) * 0.35 * et) * 0.9
        elif t == 2:
            env = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.07))
            s   = _noise() * env * 0.7
        elif t == 3:
            b1  = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.06))
            off = max(0, i - int(DRUM_SAMPLES * 0.08))
            b2  = max(0.0, 1.0 - off / (DRUM_SAMPLES * 0.08))
            s   = _noise() * (b1 * 0.6 + b2 * 0.55)
        elif t == 4:
            freq = max(140.0, 200.0 - ts * 600.0)
            env  = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.55))
            s    = math.sin(2.0 * math.pi * freq * ts) * env * 0.85
        elif t == 5:
            freq = max(90.0, 130.0 - ts * 400.0)
            env  = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.6))
            s    = math.sin(2.0 * math.pi * freq * ts) * env * 0.85
        elif t == 6:
            freq = max(60.0, 90.0 - ts * 280.0)
            env  = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.7))
            s    = math.sin(2.0 * math.pi * freq * ts) * env * 0.85
        else:
            ef   = max(0.0, 1.0 - i / (DRUM_SAMPLES * 0.4))
            es   = max(0.0, 1.0 - i / DRUM_SAMPLES) ** 1.5
            shim = math.sin(2.0 * math.pi * 8000.0 * ts) * 0.15
            s    = _noise() * (ef * 0.5 + es * 0.35) + shim * es
            s   *= 0.65
        val = int(s * 32767)
        if val >  32767: val =  32767
        if val < -32768: val = -32768
        if val < 0:      val += 65536
        j = i * 4
        buf[j]     = val & 0xFF
        buf[j + 1] = (val >> 8) & 0xFF
        buf[j + 2] = val & 0xFF
        buf[j + 3] = (val >> 8) & 0xFF
    return buf

print("Building drum sounds...")
drum_buffers = [_make_drum(i) for i in range(8)]
print("Drums ready")

# ── Hardware ──────────────────────────────────────────────────────────────────

keypad = picokeypad.PicoKeypad()
keypad.set_brightness(0.15)

enc_clk        = Pin(12, Pin.IN, Pin.PULL_UP)
enc_dt         = Pin(21, Pin.IN, Pin.PULL_UP)
enc_sw         = Pin(22, Pin.IN, Pin.PULL_UP)
enc_last_clk   = enc_clk.value()
last_enc_press = 0

vsys_adc = ADC(29)

gui = GridGUI()
print("GUI ready")

# ── Key layout ────────────────────────────────────────────────────────────────

key_assignments = [
    ('drum', 0), ('drum', 1), ('drum', 2), ('drum', 3),
    ('chord', (60, CHORD_MAJOR)),   # C4 Maj  green
    ('chord', (62, CHORD_MAJOR)),   # D4 Maj  green
    ('chord', (64, CHORD_MAJOR)),   # E4 Maj  green
    ('chord', (65, CHORD_MAJOR)),   # F4 Maj  green
    ('chord', (48, CHORD_MINOR)),   # C3 Min  blue  (one octave lower = deep)
    ('chord', (50, CHORD_MINOR)),   # D3 Min  blue
    ('chord', (52, CHORD_MINOR)),   # E3 Min  blue
    ('chord', (53, CHORD_MINOR)),   # F3 Min  blue
    ('chord', (67, CHORD_MAJOR)),   # G4 Maj  green
    ('chord', (69, CHORD_MINOR)),   # A4 Min  blue
    ('chord', (71, CHORD_DIM)),     # B4 Dim  purple
    ('chord', (72, CHORD_MAJ7)),    # C5 Maj7 yellow
]

# ── LED colors ────────────────────────────────────────────────────────────────
# Compare individual semitone integers — never relies on tuple object identity.

def chord_color(ivs):
    n  = len(ivs)
    i1 = ivs[1] if n > 1 else -1
    i2 = ivs[2] if n > 2 else -1
    i3 = ivs[3] if n > 3 else -1
    if i1 == 4 and i2 == 7 and i3 == -1: return (0,  32,  0)   # Major   green
    if i1 == 3 and i2 == 7 and i3 == -1: return (0,   0, 32)   # Minor   blue
    if i1 == 3 and i2 == 6:              return (20,  0, 32)   # Dim     purple
    if i1 == 4 and i2 == 8:              return (32, 12,  0)   # Aug     orange
    if i1 == 2 and i2 == 7:              return (0,  28, 32)   # Sus2    cyan
    if i1 == 5 and i2 == 7:              return (0,  28, 32)   # Sus4    cyan
    if i3 == 11:                          return (32, 28,  0)   # Maj7    yellow
    if i3 == 10:                          return (32, 28,  0)   # Dom7/m7 yellow
    return (16, 16, 16)

def update_leds(pressed):
    for i in range(16):
        if i in pressed:
            r, g, b = 64, 64, 64        # bright white = held
        else:
            a = key_assignments[i]
            if a[0] == 'drum':
                r, g, b = 22, 22, 22    # dim white
            elif a[0] == 'chord':
                r, g, b = chord_color(a[1][1])
            else:
                r, g, b = 10, 10, 10
        keypad.illuminate(i, r, g, b)
    keypad.update()

# ── Encoder ───────────────────────────────────────────────────────────────────

def read_encoder():
    global enc_last_clk
    clk = enc_clk.value()
    rot = 0
    if clk != enc_last_clk:
        if clk == 0:
            rot = 1 if enc_dt.value() == 1 else -1
        enc_last_clk = clk
    return rot

# ── Startup ───────────────────────────────────────────────────────────────────

update_leds(set())
gui.update(key_assignments, force=True)
print("=" * 50)
print("  READY — press a key!")
print("=" * 50)

# ── Main loop ─────────────────────────────────────────────────────────────────

pressed_keys         = set()
chord_playing        = False
last_display         = time.ticks_ms()
last_batt            = time.ticks_ms()
_release_time        = {}    # key -> ticks_ms when first seen released
RELEASE_DEBOUNCE_MS  = 30    # ms a key must be absent before counted as released
prev_chord_notes     = []    # notes from last envelope trigger (restart guard)

try:
    while True:

        # ── Audio first — keeps ibuf topped up ───────────────────────────
        # write() returns immediately if ibuf has space (128ms headroom).
        # If ibuf is full it blocks briefly — that's fine, audio is safe.
        fill_audio()

        # ── Encoder rotate ────────────────────────────────────────────────
        rot = read_encoder()
        if rot != 0:
            prev_wf = gui.current_waveform
            gui.rotate_encoder(rot)
            if gui.current_waveform != prev_wf:
                current_waveform = gui.current_waveform

        # ── Encoder button ────────────────────────────────────────────────
        now = time.ticks_ms()
        if enc_sw.value() == 0 and time.ticks_diff(now, last_enc_press) > 300:
            last_enc_press = now
            result = gui.press_encoder()
            if result and result[0] == 'complete':
                key_assignments[gui.selected_key] = result[1]
                if gui.selected_key in pressed_keys:
                    notes = []
                    for k in pressed_keys:
                        if key_assignments[k][0] == 'chord':
                            root, ivs = key_assignments[k][1]
                            notes += [root + sv for sv in ivs]
                    set_voices(notes)
                gui.update(key_assignments, force=True)

        # ── Read pots ─────────────────────────────────────────────────────
        _, vib_rate, vib_depth = read_pots()
        gui.update_controls(100, vib_rate, vib_depth)

        # ── Scan keypad (debounced releases) ──────────────────────────────
        # Presses register immediately.  Releases must be absent for
        # RELEASE_DEBOUNCE_MS ms before they are accepted — this prevents
        # button bounce from triggering spurious release+re-press cycles
        # that would reset the envelope mid-note and cause audible jitter.
        states = keypad.get_button_states()
        raw    = set(i for i in range(16) if states & (1 << i))

        for k in raw:                         # key active → clear release timer
            _release_time.pop(k, None)
        for k in pressed_keys - raw:          # newly absent → start release timer
            if k not in _release_time:
                _release_time[k] = now

        new_pressed = set(raw)
        for k in list(_release_time):
            if time.ticks_diff(now, _release_time[k]) < RELEASE_DEBOUNCE_MS:
                new_pressed.add(k)            # still within debounce window
            else:
                del _release_time[k]          # truly released

        # ── Key changes ───────────────────────────────────────────────────
        if new_pressed != pressed_keys:
            just_pressed = new_pressed - pressed_keys
            pressed_keys = new_pressed

            # Drums: trigger on fresh press
            for k in just_pressed:
                if key_assignments[k][0] == 'drum':
                    active_drum = drum_buffers[key_assignments[k][1]]
                    drum_pos    = 0

            # Chords: collect all notes from held chord keys
            notes = []
            for k in pressed_keys:
                if key_assignments[k][0] == 'chord':
                    root, ivs = key_assignments[k][1]
                    notes += [root + sv for sv in ivs]

            new_chord = any(key_assignments[k][0] == 'chord' for k in just_pressed)
            any_chord = len(notes) > 0

            if new_chord:
                set_voices(notes)
                # Only restart the envelope from zero when the notes actually
                # changed or when we were idle/releasing.  If the same chord
                # is detected again (e.g. from residual bounce that slipped
                # through the debounce window), leave the sustain phase alone.
                if sorted(notes) != sorted(prev_chord_notes) or env_state in (ENV_IDLE, ENV_RELEASE):
                    env_amp   = 0
                    env_state = ENV_ATTACK
                prev_chord_notes = list(notes)
                chord_playing = True
                gui.enter_info_mode()
            elif any_chord:
                set_voices(notes)
                chord_playing = True
            else:
                if chord_playing:
                    env_state = ENV_RELEASE
                chord_playing = False
                n_voices = 0

            gui.update_playing_notes(chord_notes_display)

        # ── LEDs — refreshed every loop so colors are always correct ──────
        # Cost: ~1ms. Benefit: any library-internal LED reset is corrected
        # within one loop cycle.
        update_leds(pressed_keys)

        # ── Display — throttled to every 300ms ────────────────────────────
        # SPI display takes ~10ms. With ibuf=8192 we have 128ms headroom,
        # so this never causes audio glitches.
        if time.ticks_diff(now, last_display) >= 300:
            gui.update(key_assignments)
            last_display = now

        # ── Battery check every 60 s ──────────────────────────────────────
        if time.ticks_diff(now, last_batt) > 60000:
            v = (vsys_adc.read_u16() / 65535) * 3.3 * 3
            if v < 3.2:
                print("Low battery: " + str(v) + "V")
            last_batt = now

except KeyboardInterrupt:
    pass

# ── Cleanup ───────────────────────────────────────────────────────────────────
audio_out.write(silence)
audio_out.deinit()
for i in range(16):
    keypad.illuminate(i, 0, 0, 0)
keypad.update()
gui.cleanup()
print("Done")
