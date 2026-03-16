"""
main.py  -  PICO SYNTH v6.0
-----------------------------
v6.0 new features:
  - Per-note 4-voice polyphony: note_on / note_off with round-robin steal
  - Dual oscillators per voice: OSC1 + detuned OSC2
  - Per-voice ADSR envelopes (no shared global envelope)
  - LFO triangle wave wired to vib_rate / vib_depth pots
  - key_notes dict for correct polyphonic overlap tracking

Previous fixes retained:
  - I2S single-core, ibuf=8192 (128 ms headroom)
  - UI throttle at 100 Hz
  - Scan-count release debounce (loop-speed independent)
  - update_leds() before get_button_states() (correct LED colors)
  - No n_voices=0 on release (envelope fade plays through)
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
print("  PICO SYNTH v6.0")
print("=" * 50)

# ── Audio constants ────────────────────────────────────────────────────────────

SAMPLE_RATE  = 16000
BUFFER_SIZE  = 256            # 16 ms of audio per write
BUFFER_BYTES = BUFFER_SIZE * 4
MAX_VOICES   = 4              # tetraphonic (4 simultaneous notes)
DRUM_SAMPLES = 2048

# ── Sine lookup table ──────────────────────────────────────────────────────────

SINE_TABLE = array('h', [int(math.sin(2.0 * math.pi * i / 256) * 32767)
                          for i in range(256)])

# ── ADSR constants (integer, 0-32767) ─────────────────────────────────────────

BUF_MS        = 1000.0 * BUFFER_SIZE / SAMPLE_RATE   # 16 ms
SUSTAIN_INT   = int(0.7 * 32767)
ATTACK_STEP   = max(1, int(32767 / max(1.0,  10.0 / BUF_MS)))
DECAY_STEP    = max(1, int((32767 - SUSTAIN_INT) / max(1.0, 100.0 / BUF_MS)))
RELEASE_STEP  = max(1, int(SUSTAIN_INT / max(1.0, 200.0 / BUF_MS)))

ENV_IDLE    = 0
ENV_ATTACK  = 1
ENV_DECAY   = 2
ENV_SUSTAIN = 3
ENV_RELEASE = 4

# ── Per-voice state arrays ─────────────────────────────────────────────────────

v_pitch  = [60]    * MAX_VOICES   # MIDI note number
v_gate   = [False] * MAX_VOICES   # True = key still held
v_phase1 = [0]     * MAX_VOICES   # OSC1 24-bit phase accumulator
v_phase2 = [0]     * MAX_VOICES   # OSC2 24-bit phase accumulator
v_inc1   = [0]     * MAX_VOICES   # OSC1 phase increment per sample
v_inc2   = [0]     * MAX_VOICES   # OSC2 phase increment per sample (detuned)
v_env    = [0]     * MAX_VOICES   # current envelope amplitude 0-32767
v_state  = [0]     * MAX_VOICES   # ENV_* state per voice
voice_rr = 0                      # round-robin steal pointer

# Pre-allocated LFO scratch (avoids GC allocation in fill_audio)
_inc1_mod = [0] * MAX_VOICES
_inc2_mod = [0] * MAX_VOICES

# ── Dual oscillator parameters ─────────────────────────────────────────────────

osc2_coarse = 0    # semitone offset for OSC2 (0 = unison)
osc2_fine   = 7    # fine detune; 1 unit ≈ 1 cent (range 0-50)
osc1_mix    = 48   # OSC1 mix weight (out of 64 total)
osc2_mix    = 16   # OSC2 mix weight

# ── LFO state ──────────────────────────────────────────────────────────────────

lfo_phase = 0
lfo_inc   = int(4 * 16777216 // SAMPLE_RATE)   # default 4 Hz
lfo_depth = 0                                   # 0-50 (cents-like units)
_lfo_val  = 0                                   # current LFO sample (-32256..+32256)

# ── Drum state ─────────────────────────────────────────────────────────────────

active_drum = None
drum_pos    = 0

current_waveform = WAVEFORM_SINE

# ── Output buffers ─────────────────────────────────────────────────────────────

out_buf = bytearray(BUFFER_BYTES)
silence = bytearray(BUFFER_BYTES)

# ── I2S — large ibuf absorbs display/input delays ─────────────────────────────

audio_out = I2S(
    0,
    sck=Pin(9), ws=Pin(10), sd=Pin(11),
    mode=I2S.TX, bits=16, format=I2S.STEREO,
    rate=SAMPLE_RATE,
    ibuf=8192,       # 128 ms headroom
)
print("I2S ready  ibuf=8192")

# ── Synthesis helpers ──────────────────────────────────────────────────────────

def midi_to_inc(midi_note):
    """Phase increment for a given MIDI note (24-bit scale)."""
    freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    return int(freq * 16777216.0 / SAMPLE_RATE)

def _inc2_for(midi_note):
    """OSC2 increment: coarse semitone offset + fine detune (≈ cents/1024)."""
    base = midi_to_inc(midi_note + osc2_coarse)
    return base + (base * osc2_fine >> 10)

# ── Voice management ───────────────────────────────────────────────────────────

def _voice_start(v, midi_note):
    """Initialise voice v to play midi_note from the start of the attack."""
    v_pitch[v]  = midi_note
    v_gate[v]   = True
    v_phase1[v] = 0
    v_phase2[v] = 0
    v_inc1[v]   = midi_to_inc(midi_note)
    v_inc2[v]   = _inc2_for(midi_note)
    v_env[v]    = 0
    v_state[v]  = ENV_ATTACK

def note_on(midi_note):
    """Trigger midi_note: retrigger existing, grab idle voice, or steal oldest."""
    global voice_rr
    # If this note is already sustaining, leave the envelope alone.
    # Only restart from zero if it was idle or releasing (genuine re-press).
    for v in range(MAX_VOICES):
        if v_gate[v] and v_pitch[v] == midi_note:
            if v_state[v] in (ENV_IDLE, ENV_RELEASE):
                v_env[v]   = 0
                v_state[v] = ENV_ATTACK
            return   # already held — do nothing further
    # Use idle voice first
    for v in range(MAX_VOICES):
        if v_state[v] == ENV_IDLE:
            _voice_start(v, midi_note)
            return
    # Round-robin steal
    _voice_start(voice_rr, midi_note)
    voice_rr = (voice_rr + 1) % MAX_VOICES

def note_off(midi_note):
    """Release midi_note (move its voice to RELEASE phase)."""
    for v in range(MAX_VOICES):
        if v_gate[v] and v_pitch[v] == midi_note:
            v_gate[v]  = False
            if v_state[v] != ENV_IDLE:
                v_state[v] = ENV_RELEASE

# ── Key-to-note tracking ───────────────────────────────────────────────────────

key_notes = {}   # key_index -> list[midi_note]  (only chord/note keys)

def _key_press(k):
    """Trigger all notes associated with key k."""
    a = key_assignments[k]
    if a[0] == 'chord':
        root, ivs = a[1]
        notes = [root + sv for sv in ivs]
    elif a[0] == 'note':
        notes = [a[1]]
    else:
        return
    key_notes[k] = notes
    for n in notes:
        note_on(n)

def _key_release(k):
    """Release notes from key k that are not held by another key."""
    freed = key_notes.pop(k, [])
    still_held = set()
    for other in key_notes.values():
        still_held.update(other)
    for n in freed:
        if n not in still_held:
            note_off(n)

def _active_notes():
    """Return list of pitches from non-idle voices (used by OLED display)."""
    result = []
    for v in range(MAX_VOICES):
        p = v_pitch[v]
        if v_state[v] != ENV_IDLE and p not in result:
            result.append(p)
    return result

# ── Audio fill ─────────────────────────────────────────────────────────────────

def fill_audio():
    """
    Synthesize one BUFFER_SIZE chunk: advance LFO, advance per-voice ADSR,
    mix OSC1+OSC2 for each active voice, mix in drum, write to I2S.
    All integer arithmetic — no float in the hot path.
    """
    global lfo_phase, _lfo_val, active_drum, drum_pos

    # ── Advance LFO (triangle wave, 4 segments of 64 steps) ───────────────
    lfo_phase = (lfo_phase + lfo_inc) & 0xFFFFFF
    lp = (lfo_phase >> 16) & 255
    if lp < 64:
        _lfo_val = lp * 512              # 0 → +32256
    elif lp < 128:
        _lfo_val = (127 - lp) * 512     # +32256 → 0
    elif lp < 192:
        _lfo_val = -(lp - 128) * 512    # 0 → -32256
    else:
        _lfo_val = -(255 - lp) * 512    # -32256 → 0

    # ── Advance per-voice ADSR once per buffer ─────────────────────────────
    for v in range(MAX_VOICES):
        st = v_state[v]
        if st == ENV_ATTACK:
            v_env[v] += ATTACK_STEP
            if v_env[v] >= 32767:
                v_env[v]  = 32767
                v_state[v] = ENV_DECAY
        elif st == ENV_DECAY:
            v_env[v] -= DECAY_STEP
            if v_env[v] <= SUSTAIN_INT:
                v_env[v]  = SUSTAIN_INT
                v_state[v] = ENV_SUSTAIN
        elif st == ENV_RELEASE:
            v_env[v] -= RELEASE_STEP
            if v_env[v] <= 0:
                v_env[v]  = 0
                v_state[v] = ENV_IDLE

    # ── Check for anything to render ───────────────────────────────────────
    has_drum  = active_drum is not None
    any_voice = False
    for v in range(MAX_VOICES):
        if v_state[v] != ENV_IDLE:
            any_voice = True
            break

    if not any_voice and not has_drum:
        audio_out.write(silence)
        return

    # ── Apply LFO modulation to phase increments (pre-buffer) ─────────────
    lv = _lfo_val
    ld = lfo_depth
    for v in range(MAX_VOICES):
        if v_state[v] != ENV_IDLE and ld > 0:
            mod1 = ((v_inc1[v] * lv) >> 20) * ld >> 6
            _inc1_mod[v] = v_inc1[v] + mod1
            mod2 = ((v_inc2[v] * lv) >> 20) * ld >> 6
            _inc2_mod[v] = v_inc2[v] + mod2
        else:
            _inc1_mod[v] = v_inc1[v]
            _inc2_mod[v] = v_inc2[v]

    # ── Cache locals for the tight inner loop ──────────────────────────────
    ph1  = v_phase1
    ph2  = v_phase2
    im1  = _inc1_mod
    im2  = _inc2_mod
    env  = v_env
    sta  = v_state
    tbl  = SINE_TABLE
    o1m  = osc1_mix
    o2m  = osc2_mix
    wf   = current_waveform
    MV   = MAX_VOICES
    IDLE = ENV_IDLE
    buf  = out_buf

    for i in range(BUFFER_SIZE):

        # ── Sum all active voices ─────────────────────────────────────────
        cs = 0
        for v in range(MV):
            if sta[v] == IDLE:
                continue

            ea = env[v]

            # OSC1
            pb1 = (ph1[v] >> 16) & 255
            if wf == 0:    # SINE
                s1 = tbl[pb1]
            elif wf == 1:  # SQUARE
                s1 = 32767 if pb1 < 128 else -32767
            elif wf == 2:  # TRIANGLE
                s1 = (pb1 * 516 - 32767) if pb1 < 128 else ((255 - pb1) * 516 - 32767)
            else:          # SAWTOOTH
                s1 = pb1 * 256 - 32768
            ph1[v] = (ph1[v] + im1[v]) & 0xFFFFFF

            # OSC2
            pb2 = (ph2[v] >> 16) & 255
            if wf == 0:
                s2 = tbl[pb2]
            elif wf == 1:
                s2 = 32767 if pb2 < 128 else -32767
            elif wf == 2:
                s2 = (pb2 * 516 - 32767) if pb2 < 128 else ((255 - pb2) * 516 - 32767)
            else:
                s2 = pb2 * 256 - 32768
            ph2[v] = (ph2[v] + im2[v]) & 0xFFFFFF

            # Mix both oscillators, apply envelope.
            # >> 23 = >> 6 (mix /64) + >> 17 (env /32767 /4voices headroom)
            cs += (s1 * o1m + s2 * o2m) * ea >> 23

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
        buf[j]     = s & 0xFF
        buf[j + 1] = (s >> 8) & 0xFF
        buf[j + 2] = s & 0xFF
        buf[j + 3] = (s >> 8) & 0xFF

    audio_out.write(buf)

# ── Drum synthesis ─────────────────────────────────────────────────────────────

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

# ── Hardware ───────────────────────────────────────────────────────────────────

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

# ── Key layout ─────────────────────────────────────────────────────────────────

key_assignments = [
    ('drum', 0), ('drum', 1), ('drum', 2), ('drum', 3),
    ('chord', (60, CHORD_MAJOR)),   # C4 Maj  green
    ('chord', (62, CHORD_MAJOR)),   # D4 Maj  green
    ('chord', (64, CHORD_MAJOR)),   # E4 Maj  green
    ('chord', (65, CHORD_MAJOR)),   # F4 Maj  green
    ('chord', (48, CHORD_MINOR)),   # C3 Min  blue
    ('chord', (50, CHORD_MINOR)),   # D3 Min  blue
    ('chord', (52, CHORD_MINOR)),   # E3 Min  blue
    ('chord', (53, CHORD_MINOR)),   # F3 Min  blue
    ('chord', (67, CHORD_MAJOR)),   # G4 Maj  green
    ('chord', (69, CHORD_MINOR)),   # A4 Min  blue
    ('chord', (71, CHORD_DIM)),     # B4 Dim  purple
    ('chord', (72, CHORD_MAJ7)),    # C5 Maj7 yellow
]

# ── LED colors ─────────────────────────────────────────────────────────────────

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

# ── Encoder ────────────────────────────────────────────────────────────────────

def read_encoder():
    global enc_last_clk
    clk = enc_clk.value()
    rot = 0
    if clk != enc_last_clk:
        if clk == 0:
            rot = 1 if enc_dt.value() == 1 else -1
        enc_last_clk = clk
    return rot

# ── Startup ────────────────────────────────────────────────────────────────────

update_leds(set())
gui.update(key_assignments, force=True)
print("=" * 50)
print("  READY — press a key!")
print("=" * 50)

# ── Main loop ──────────────────────────────────────────────────────────────────

pressed_keys   = set()
last_display   = time.ticks_ms()
last_batt      = time.ticks_ms()
last_ui        = time.ticks_ms()
UI_INTERVAL_MS = 10    # UI work capped at 100 Hz; audio runs every iteration
_release_count = {}    # key -> consecutive absent-scan count
RELEASE_SCANS  = 5     # scans absent before a key is considered released
_press_count   = {}    # key -> consecutive present-scan count
PRESS_SCANS    = 3     # scans present before a key is considered pressed

try:
    while True:

        # ── Audio first — keeps ibuf topped up ────────────────────────────
        fill_audio()

        # ── UI throttle — cap non-audio work at 100 Hz ────────────────────
        now = time.ticks_ms()
        if time.ticks_diff(now, last_ui) < UI_INTERVAL_MS:
            continue
        last_ui = now

        # ── Encoder rotate ────────────────────────────────────────────────
        rot = read_encoder()
        if rot != 0:
            prev_wf = gui.current_waveform
            gui.rotate_encoder(rot)
            if gui.current_waveform != prev_wf:
                current_waveform = gui.current_waveform

        # ── Encoder button ────────────────────────────────────────────────
        if enc_sw.value() == 0 and time.ticks_diff(now, last_enc_press) > 300:
            last_enc_press = now
            result = gui.press_encoder()
            if result and result[0] == 'complete':
                old_a = key_assignments[gui.selected_key]
                key_assignments[gui.selected_key] = result[1]
                # Re-trigger if this key is currently held
                k = gui.selected_key
                if k in pressed_keys and old_a[0] in ('chord', 'note'):
                    _key_release(k)
                    _key_press(k)
                gui.update(key_assignments, force=True)

        # ── Read pots → update LFO and display ────────────────────────────
        _, vib_rate, vib_depth = read_pots()
        lfo_inc   = int(vib_rate * 16777216 // SAMPLE_RATE)
        lfo_depth = vib_depth
        gui.update_controls(100, vib_rate, vib_depth)

        # ── LEDs first (also refreshes keypad hardware state) ─────────────
        update_leds(pressed_keys)

        # ── Scan keypad (debounced both press and release) ─────────────────
        # Press:   key must appear PRESS_SCANS times before registering
        # Release: key must be absent RELEASE_SCANS times before registering
        # This prevents a single physical press from bouncing into multiple
        # note_on() calls, which would reset the envelope repeatedly.
        states = keypad.get_button_states()
        raw    = set(i for i in range(16) if states & (1 << i))

        # Update press counters
        for k in raw:
            _press_count[k] = _press_count.get(k, 0) + 1
        for k in list(_press_count):
            if k not in raw:
                del _press_count[k]

        # Update release counters
        for k in raw:
            _release_count.pop(k, None)
        for k in pressed_keys - raw:
            _release_count[k] = _release_count.get(k, 0) + 1

        # A key is "confirmed pressed" once it has PRESS_SCANS consecutive reads
        confirmed = set(k for k, c in _press_count.items() if c >= PRESS_SCANS)

        # Hold keys that are in release debounce window
        new_pressed = set(confirmed)
        for k in list(_release_count):
            if _release_count[k] < RELEASE_SCANS:
                new_pressed.add(k)      # still within release window, keep held
            else:
                del _release_count[k]  # truly released

        # ── Key changes ───────────────────────────────────────────────────
        if new_pressed != pressed_keys:
            just_pressed  = new_pressed - pressed_keys
            just_released = pressed_keys - new_pressed
            pressed_keys  = new_pressed

            for k in just_released:
                if key_assignments[k][0] in ('chord', 'note'):
                    _key_release(k)

            for k in just_pressed:
                a = key_assignments[k]
                if a[0] == 'drum':
                    active_drum = drum_buffers[a[1]]
                    drum_pos    = 0
                elif a[0] in ('chord', 'note'):
                    _key_press(k)
                    gui.enter_info_mode()

            gui.update_playing_notes(_active_notes())

        # ── Display — throttled to every 300 ms ───────────────────────────
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

# ── Cleanup ────────────────────────────────────────────────────────────────────
audio_out.write(silence)
audio_out.deinit()
for i in range(16):
    keypad.illuminate(i, 0, 0, 0)
keypad.update()
gui.cleanup()
print("Done")
