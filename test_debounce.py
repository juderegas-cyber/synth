#!/usr/bin/env python3
"""
test_debounce.py — Tests the release-debounce and envelope-restart-guard
logic from main.py using plain Python. No hardware or extra packages needed.

Run:  python3 test_debounce.py
"""

# ── Replicate state from main.py ──────────────────────────────────────────────

RELEASE_DEBOUNCE_MS = 30

_release_time    = {}
pressed_keys     = set()
prev_chord_notes = []

ENV_IDLE    = 0
ENV_ATTACK  = 1
ENV_DECAY   = 2
ENV_SUSTAIN = 3
ENV_RELEASE = 4

env_state = ENV_IDLE
env_amp   = 0

SUSTAIN_INT = int(0.7 * 32767)


def scan_keys_debounced(raw_set, now):
    """Debounce logic copied verbatim from main.py."""
    global pressed_keys

    for k in raw_set:
        _release_time.pop(k, None)
    for k in pressed_keys - raw_set:
        if k not in _release_time:
            _release_time[k] = now

    new_pressed = set(raw_set)
    for k in list(_release_time):
        if now - _release_time[k] < RELEASE_DEBOUNCE_MS:
            new_pressed.add(k)
        else:
            del _release_time[k]

    pressed_keys = new_pressed
    return new_pressed


def handle_chord_press(notes, is_new_chord):
    """Envelope-restart guard copied verbatim from main.py."""
    global env_state, env_amp, prev_chord_notes

    if is_new_chord:
        if sorted(notes) != sorted(prev_chord_notes) or env_state in (ENV_IDLE, ENV_RELEASE):
            env_amp   = 0
            env_state = ENV_ATTACK
        prev_chord_notes[:] = notes
    elif not notes:
        if env_state not in (ENV_IDLE,):
            env_state = ENV_RELEASE


# ── Test helpers ──────────────────────────────────────────────────────────────

_passed = 0
_failed = 0


def test(name, condition):
    global _passed, _failed
    if condition:
        print(f"  PASS  {name}")
        _passed += 1
    else:
        print(f"  FAIL  {name}")
        _failed += 1


# ── Debounce tests ────────────────────────────────────────────────────────────

print("\n── Debounce tests ──────────────────────────────────────────────────────────")

_release_time.clear()
pressed_keys.clear()

t = 0
new = scan_keys_debounced({0}, t)
test("Key registers immediately on press", 0 in new)

t = 5   # bounce: key absent for only 5 ms
new = scan_keys_debounced(set(), t)
test("Key stays pressed during 5 ms bounce", 0 in new)

t = 15  # still absent, still within debounce window
new = scan_keys_debounced(set(), t)
test("Key stays pressed during 15 ms bounce", 0 in new)

t = 20  # key comes back before debounce window expires
new = scan_keys_debounced({0}, t)
test("Key remains pressed when it comes back before window", 0 in new)

t = 200  # key absent long enough — truly released
new = scan_keys_debounced(set(), t)
test("Key still held inside debounce window after 200 ms scan", 0 in new)

t = 240  # past 30 ms window from t=200
new = scan_keys_debounced(set(), t)
test("Key released after debounce window expires", 0 not in new)

# Second key press works normally after release
t = 300
new = scan_keys_debounced({0}, t)
test("Key can be pressed again after proper release", 0 in new)


# ── Envelope restart guard tests ──────────────────────────────────────────────

print("\n── Envelope restart guard tests ────────────────────────────────────────────")

env_state = ENV_IDLE
env_amp   = 0
prev_chord_notes.clear()

# Initial press: envelope must start attack
handle_chord_press([60, 64, 67], is_new_chord=True)
test("Initial press starts attack from zero", env_state == ENV_ATTACK and env_amp == 0)

# Advance to sustain (simulates ADSR running for a few buffers)
env_state = ENV_SUSTAIN
env_amp   = SUSTAIN_INT

# Same notes detected as "newly pressed" (bounce slipped through) — must NOT restart
handle_chord_press([60, 64, 67], is_new_chord=True)
test("Same notes in sustain do NOT restart envelope", env_state == ENV_SUSTAIN)
test("env_amp unchanged during spurious re-press", env_amp == SUSTAIN_INT)

# Different chord pressed while sustaining — MUST restart
handle_chord_press([62, 65, 69], is_new_chord=True)
test("Different notes DO restart envelope from zero", env_state == ENV_ATTACK and env_amp == 0)

# Release, then press same chord again — must restart (we were idle/releasing)
env_state = ENV_RELEASE
env_amp   = SUSTAIN_INT // 2
prev_chord_notes[:] = [62, 65, 69]

handle_chord_press([62, 65, 69], is_new_chord=True)
test("Same notes restart envelope when coming from RELEASE", env_state == ENV_ATTACK and env_amp == 0)


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'─' * 50}")
print(f"  {_passed} passed, {_failed} failed")
if _failed:
    print("  Some tests failed — check the logic above.")
else:
    print("  All good!")
print()
