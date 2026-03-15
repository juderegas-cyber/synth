"""
gui.py  -  PICO SYNTH v5.2
----------------------------
OLED wiring (SH1106 SPI0):
  CLK  -> GP18   MOSI -> GP19
  RES  -> GP20   DC   -> GP17   CS -> GP15

Potentiometers:
  GP26 -> ignored (volume fixed in main.py)
  GP27 -> Vibrato rate  (info screen display only)
  GP28 -> Vibrato depth (info screen display only)
"""

from machine import Pin, SPI, ADC
import time

WAVEFORM_SINE     = 0
WAVEFORM_SQUARE   = 1
WAVEFORM_TRIANGLE = 2
WAVEFORM_SAWTOOTH = 3
WAVEFORM_NAMES    = ['Sine', 'Sqr', 'Tri', 'Saw']

DISPLAY_WIDTH  = 128
DISPLAY_HEIGHT = 64

NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']

CHORD_MAJOR = (0, 4, 7)
CHORD_MINOR = (0, 3, 7)
CHORD_DOM7  = (0, 4, 7, 10)
CHORD_MAJ7  = (0, 4, 7, 11)
CHORD_MIN7  = (0, 3, 7, 10)
CHORD_DIM   = (0, 3, 6)
CHORD_AUG   = (0, 4, 8)
CHORD_SUS2  = (0, 2, 7)
CHORD_SUS4  = (0, 5, 7)

DRUM_NAMES   = ['Kick','Snar','HHat','Clap','Tom1','Tom2','Tom3','Cymb']
MENU_TYPES   = ['Drum', 'Chord']
DRUM_OPTIONS = [(i, DRUM_NAMES[i]) for i in range(len(DRUM_NAMES))]

CHORD_ROOTS = [
    (60,'C4'), (61,'C#4'), (62,'D4'), (63,'D#4'),
    (64,'E4'), (65,'F4'), (66,'F#4'), (67,'G4'),
    (68,'G#4'), (69,'A4'), (70,'A#4'), (71,'B4'),
    (48,'C3'), (50,'D3'), (52,'E3'), (53,'F3'),
    (55,'G3'), (57,'A3'), (72,'C5'), (74,'D5'),
]

CHORD_TYPES = [
    (CHORD_MAJOR,'Major'), (CHORD_MINOR,'Minor'), (CHORD_DOM7,'Dom 7'),
    (CHORD_MAJ7,'Maj 7'), (CHORD_MIN7,'Min 7'), (CHORD_DIM,'Dim  '),
    (CHORD_AUG,'Aug  '), (CHORD_SUS2,'Sus 2'), (CHORD_SUS4,'Sus 4'),
]

# ── Pot reader ────────────────────────────────────────────────────────────────

class PotReader:
    def __init__(self, pin_num, out_min=0, out_max=100, samples=4):
        self.adc     = ADC(Pin(pin_num))
        self.out_min = out_min
        self.out_max = out_max
        self.samples = samples

    def read(self):
        total = 0
        for _ in range(self.samples):
            total += self.adc.read_u16()
        raw  = total // self.samples
        span = self.out_max - self.out_min
        val  = self.out_min + int((raw / 65535) * span)
        return max(self.out_min, min(self.out_max, val))

pot_volume    = PotReader(26, 0, 100)
pot_vib_rate  = PotReader(27, 2, 8)
pot_vib_depth = PotReader(28, 0, 50)

def read_pots():
    return pot_volume.read(), pot_vib_rate.read(), pot_vib_depth.read()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _pad2(n):
    """Zero-pad an integer to 2 digits without zfill."""
    s = str(n)
    return "0" + s if len(s) < 2 else s

def _chord_label(ivs):
    """Return 3-char chord suffix from interval tuple."""
    n  = len(ivs)
    i1 = ivs[1] if n > 1 else -1
    i2 = ivs[2] if n > 2 else -1
    i3 = ivs[3] if n > 3 else -1
    if i1 == 4 and i2 == 7 and i3 == -1: return "Maj"
    if i1 == 3 and i2 == 7 and i3 == -1: return "Min"
    if i1 == 3 and i2 == 6:              return "Dim"
    if i1 == 4 and i2 == 8:              return "Aug"
    if i1 == 2 and i2 == 7:              return "Su2"
    if i1 == 5 and i2 == 7:              return "Su4"
    if i3 == 11:                          return "M7"
    if i3 == 10 and i1 == 4:             return "7"
    if i3 == 10:                          return "m7"
    return "?"

# ── Grid GUI ──────────────────────────────────────────────────────────────────

class GridGUI:

    def __init__(self):
        self.display_available = False
        self.oled = None
        self._init_display()

        self.mode         = 'grid'
        self.selected_key = 0
        self.last_update  = 0
        self.info_timeout = 0

        self.menu_step      = 0
        self.menu_selection = 0
        self.menu_type      = None
        self.menu_root      = None

        self.current_waveform = WAVEFORM_SINE
        self.playing_notes    = []
        self.volume           = 100
        self.vibrato_rate     = 2
        self.vibrato_depth    = 0

        if self.display_available:
            self._show_splash()

    def _init_display(self):
        try:
            from sh1106 import SH1106_SPI
            spi = SPI(0, baudrate=10_000_000, sck=Pin(18), mosi=Pin(19))
            self.oled = SH1106_SPI(
                DISPLAY_WIDTH, DISPLAY_HEIGHT, spi,
                dc=Pin(17, Pin.OUT), res=Pin(20, Pin.OUT), cs=Pin(15, Pin.OUT),
                external_vcc=False,
            )
            self.display_available = True
            print("SH1106 ready on SPI0")
        except ImportError:
            print("sh1106.py missing - upload to /lib/")
        except Exception as e:
            print("Display init error: " + str(e))

    def _show_splash(self):
        try:
            self.oled.fill(0)
            self.oled.text("PICO SYNTH", 19, 20, 1)
            self.oled.text("v5.2", 46, 35, 1)
            self.oled.show()
            time.sleep(1)
        except Exception as e:
            print("Splash error: " + str(e))

    # ── Main refresh ──────────────────────────────────────────────────────────

    def update(self, key_assignments, force=False):
        """
        Call every loop. Info timeout is checked BEFORE the rate limiter
        so the mode always switches back at exactly 3 s.
        """
        if not self.display_available:
            return

        now = time.ticks_ms()

        # ── Info timeout: ALWAYS checked, even when rate-limited ──────────
        if self.mode == 'info' and self.info_timeout > 0:
            if time.ticks_diff(now, self.info_timeout) > 3000:
                self.mode     = 'grid'
                self.info_timeout = 0
                force         = True    # redraw grid immediately

        if not force and time.ticks_diff(now, self.last_update) < 100:
            return
        self.last_update = now

        try:
            if   self.mode == 'grid':       self._draw_grid(key_assignments)
            elif self.mode == 'assignment': self._draw_menu()
            elif self.mode == 'info':       self._draw_info()
            self.oled.show()
        except Exception as e:
            print("Display error: " + str(e))

    # ── Screen drawing ────────────────────────────────────────────────────────

    def _draw_grid(self, key_assignments):
        self.oled.fill(0)
        self.oled.text("LAYOUT", 0, 0, 1)
        self.oled.text("K:" + _pad2(self.selected_key), 92, 0, 1)
        for row in range(4):
            for col in range(4):
                k = row * 4 + col
                x = col * 32
                y = row * 13 + 12
                label = self._label(key_assignments[k] if k < len(key_assignments) else None)
                if k == self.selected_key:
                    self.oled.fill_rect(x, y, 31, 12, 1)
                    self.oled.text(label, x + 1, y + 2, 0)
                else:
                    self.oled.text(label, x + 1, y + 2, 1)

    def _draw_menu(self):
        self.oled.fill(0)
        if self.menu_step == 0:
            self.oled.text("KEY " + _pad2(self.selected_key) + " ASSIGN", 10, 0, 1)
            for i in range(len(MENU_TYPES)):
                m = ">" if i == self.menu_selection else " "
                self.oled.text(m + " " + MENU_TYPES[i], 20, 24 + i * 16, 1)
        elif self.menu_step == 1:
            if self.menu_type == 'Drum':
                self.oled.text("SELECT DRUM", 18, 0, 1)
                self._scroll_list(DRUM_OPTIONS, self.menu_selection, 14, key=lambda x: x[1])
            else:
                self.oled.text("ROOT NOTE", 26, 0, 1)
                self._scroll_list(CHORD_ROOTS, self.menu_selection, 14, key=lambda x: x[1])
        elif self.menu_step == 2:
            self.oled.text("CHORD TYPE", 22, 0, 1)
            self._scroll_list(CHORD_TYPES, self.menu_selection, 14, key=lambda x: x[1])

    def _scroll_list(self, items, selected, y_start, key=None):
        visible = 4
        start   = max(0, min(selected - 1, len(items) - visible))
        for i in range(visible):
            idx = start + i
            if idx >= len(items):
                break
            label  = key(items[idx]) if key else str(items[idx])
            marker = ">" if idx == selected else " "
            self.oled.text(marker + label[:9], 8, y_start + i * 12, 1)

    def _draw_info(self):
        self.oled.fill(0)
        self.oled.text("WF: " + WAVEFORM_NAMES[self.current_waveform], 0, 0, 1)
        self.oled.text("< rotate to change >", 0, 10, 1)

        # Active notes — plain for loop, no list comprehension
        label = ""
        for note in self.playing_notes[:4]:
            if label:
                label = label + " "
            label = label + NOTE_NAMES[note % 12] + str((note // 12) - 1)
        self.oled.text(label if label else "---", 0, 22, 1)

        # Volume bar (always full — volume is fixed at 100%)
        self.oled.text("Vol", 0, 36, 1)
        self.oled.rect(24, 36, 94, 8, 1)
        self.oled.fill_rect(25, 37, 93, 6, 1)

        # Vibrato
        vr = str(self.vibrato_rate)
        vd = str(self.vibrato_depth)
        self.oled.text("Vib " + vr + "Hz " + vd + "ct", 0, 52, 1)

    # ── Label helper ──────────────────────────────────────────────────────────

    def _label(self, assignment):
        if assignment is None:
            return "----"
        if assignment[0] == 'drum':
            idx = assignment[1]
            return (DRUM_NAMES[idx] if idx < len(DRUM_NAMES) else "Drum")[:4]
        if assignment[0] == 'chord':
            root, ivs = assignment[1]
            return (NOTE_NAMES[root % 12] + _chord_label(ivs))[:4]
        if assignment[0] == 'note':
            m = assignment[1]
            return (NOTE_NAMES[m % 12] + str((m // 12) - 1))[:4]
        return "????"

    # ── Encoder ───────────────────────────────────────────────────────────────

    def rotate_encoder(self, direction):
        if self.mode == 'grid':
            self.selected_key = (self.selected_key + direction) % 16
        elif self.mode == 'assignment':
            if self.menu_step == 0:
                n = len(MENU_TYPES)
            elif self.menu_step == 1:
                n = len(DRUM_OPTIONS) if self.menu_type == 'Drum' else len(CHORD_ROOTS)
            else:
                n = len(CHORD_TYPES)
            self.menu_selection = (self.menu_selection + direction) % n
        elif self.mode == 'info':
            self.current_waveform = (self.current_waveform + direction) % 4

    def press_encoder(self):
        if self.mode == 'grid':
            self.mode           = 'assignment'
            self.menu_step      = 0
            self.menu_selection = 0
            self.menu_type      = None
            self.menu_root      = None
            return None
        elif self.mode == 'assignment':
            if self.menu_step == 0:
                self.menu_type      = MENU_TYPES[self.menu_selection]
                self.menu_step      = 1
                self.menu_selection = 0
            elif self.menu_step == 1:
                if self.menu_type == 'Drum':
                    drum_idx  = DRUM_OPTIONS[self.menu_selection][0]
                    self.mode = 'grid'
                    return ('complete', ('drum', drum_idx))
                else:
                    self.menu_root      = CHORD_ROOTS[self.menu_selection][0]
                    self.menu_step      = 2
                    self.menu_selection = 0
            elif self.menu_step == 2:
                intervals = CHORD_TYPES[self.menu_selection][0]
                self.mode = 'grid'
                return ('complete', ('chord', (self.menu_root, intervals)))
        elif self.mode == 'info':
            self.mode = 'grid'
        return None

    # ── State setters ─────────────────────────────────────────────────────────

    def update_controls(self, volume, vibrato_rate, vibrato_depth):
        self.volume        = volume
        self.vibrato_rate  = vibrato_rate
        self.vibrato_depth = vibrato_depth

    def update_playing_notes(self, notes):
        self.playing_notes = list(notes)

    def enter_info_mode(self):
        self.mode         = 'info'
        self.info_timeout = time.ticks_ms()

    def cleanup(self):
        if self.display_available:
            try:
                self.oled.fill(0)
                self.oled.show()
                self.oled.poweroff()
            except:
                pass
