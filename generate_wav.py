#!/usr/bin/env python3
"""
generate_wav.py — Simulates the synthesizer and writes audio/test_note.wav

Replicates the synthesis + ADSR logic from main.py in plain Python,
then outputs a WAV file you can open in any audio player.

Run:  python3 generate_wav.py
Then open audio/test_note.wav to hear the note.

No extra packages needed — only Python stdlib.
"""

import math
import wave
import struct
import os

# ── Constants (must match main.py) ────────────────────────────────────────────

SAMPLE_RATE = 16000
BUFFER_SIZE = 256
MAX_VOICES  = 6

SINE_TABLE = [int(math.sin(2.0 * math.pi * i / 256) * 32767) for i in range(256)]

BUF_MS      = 1000.0 * BUFFER_SIZE / SAMPLE_RATE   # 16 ms
SUSTAIN_INT = int(0.7 * 32767)
ATTACK_STEP  = max(1, int(32767 / max(1.0, 10.0  / BUF_MS)))
DECAY_STEP   = max(1, int((32767 - SUSTAIN_INT) / max(1.0, 100.0 / BUF_MS)))
RELEASE_STEP = max(1, int(SUSTAIN_INT / max(1.0, 200.0 / BUF_MS)))

ENV_IDLE = 0; ENV_ATTACK = 1; ENV_DECAY = 2; ENV_SUSTAIN = 3; ENV_RELEASE = 4

# ── Synthesis state ───────────────────────────────────────────────────────────

phases   = [0] * MAX_VOICES
incs     = [0] * MAX_VOICES
n_voices = 0
env_amp  = 0
env_state = ENV_IDLE


def midi_to_inc(midi_note):
    freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    return int(freq * 16777216.0 / SAMPLE_RATE)


def set_voices(notes):
    global n_voices
    for i, n in enumerate(notes[:MAX_VOICES]):
        incs[i] = midi_to_inc(n)
    n_voices = min(len(notes), MAX_VOICES)


def fill_buffer():
    """Synthesize one buffer of audio, advance ADSR — mirrors fill_audio()."""
    global env_amp, env_state

    if env_state == ENV_ATTACK:
        env_amp += ATTACK_STEP
        if env_amp >= 32767:
            env_amp = 32767; env_state = ENV_DECAY
    elif env_state == ENV_DECAY:
        env_amp -= DECAY_STEP
        if env_amp <= SUSTAIN_INT:
            env_amp = SUSTAIN_INT; env_state = ENV_SUSTAIN
    elif env_state == ENV_RELEASE:
        env_amp -= RELEASE_STEP
        if env_amp <= 0:
            env_amp = 0; env_state = ENV_IDLE

    if n_voices == 0 or env_state == ENV_IDLE:
        return bytes(BUFFER_SIZE * 2)   # silence

    scale = env_amp // n_voices
    samples = []

    for i in range(BUFFER_SIZE):
        cs = 0
        for v in range(n_voices):
            pb = (phases[v] >> 16) & 255
            cs += SINE_TABLE[pb]
            phases[v] = (phases[v] + incs[v]) & 0xFFFFFF
        cs = cs * scale >> 15
        cs = max(-32768, min(32767, cs))
        samples.append(cs)

    return struct.pack(f'<{BUFFER_SIZE}h', *samples)


# ── Simulate a held note then release ─────────────────────────────────────────

print("Simulating C Major chord held for 2 s then released...")

set_voices([60, 64, 67])     # C4 Major
env_amp   = 0
env_state = ENV_ATTACK

os.makedirs("audio", exist_ok=True)
out_path = "audio/test_note.wav"

with wave.open(out_path, "w") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)

    # 2 seconds held
    hold_buffers = int(2.0 * SAMPLE_RATE / BUFFER_SIZE)
    for _ in range(hold_buffers):
        wf.writeframes(fill_buffer())

    # Release
    env_state = ENV_RELEASE
    release_buffers = int(0.5 * SAMPLE_RATE / BUFFER_SIZE)
    for _ in range(release_buffers):
        wf.writeframes(fill_buffer())

print(f"Written → {out_path}")

# Open in default audio player on macOS
os.system(f'open "{out_path}"')
