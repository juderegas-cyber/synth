"""
POLYPHONIC SYNTH - FIXED!
- Mixing happens ONCE when keys change (not during audio loop!)
- Better note layout (chromatic scale, no duplicates)
- I2C display on GP13/14
"""

from machine import Pin, I2S, ADC, I2C
import math
import time
import picokeypad

print("="*50)
print("  POLYPHONIC SYNTH v4.1 FIXED")
print("="*50)

# ============================================
# HARDWARE SETUP
# ============================================

keypad = picokeypad.PicoKeypad()
keypad.set_brightness(0.15)

enc_clk = Pin(12, Pin.IN, Pin.PULL_UP)
enc_dt = Pin(21, Pin.IN, Pin.PULL_UP)
enc_sw = Pin(22, Pin.IN, Pin.PULL_UP)
enc_last_clk = enc_clk.value()

vsys_adc = ADC(29)

# I2C for OLED - GP13=SCL, GP14=SDA (your existing wiring)
try:
    i2c = I2C(1, scl=Pin(13), sda=Pin(14), freq=400000)
    oled_connected = True
    print("✓ I2C initialized on GP13/14")
    
    try:
        from ssd1306 import SSD1306_I2C
        oled = SSD1306_I2C(128, 64, i2c)
        oled.fill(0)
        oled.text("POLYPHONIC", 15, 20)
        oled.text("SYNTH", 35, 35)
        oled.show()
        print("✓ OLED (SSD1306)")
    except:
        try:
            from sh1106 import SH1106_I2C
            oled = SH1106_I2C(128, 64, i2c)
            oled.fill(0)
            oled.text("POLYPHONIC", 15, 20)
            oled.text("SYNTH", 35, 35)
            oled.show()
            print("✓ OLED (SH1106)")
        except:
            print("⚠ OLED driver not found - upload ssd1306.py or sh1106.py to /lib/")
            oled_connected = False
except:
    print("⚠ No I2C OLED detected")
    oled_connected = False

# I2S
audio_out = I2S(
    0,
    sck=Pin(9),
    ws=Pin(10),
    sd=Pin(11),
    mode=I2S.TX,
    bits=16,
    format=I2S.STEREO,
    rate=16000,
    ibuf=2048
)

print("✓ I2S initialized")

# ============================================
# SETTINGS
# ============================================

sample_rate = 16000
samples_per_buffer = 512
MAX_POLYPHONY = 4

# FIXED NOTE LAYOUT - Chromatic scale (no duplicates!)
# Full chromatic octave across 16 keys
note_map = [
    60, 61, 62, 63,   # C, C#, D, D#
    64, 65, 66, 67,   # E, F, F#, G
    68, 69, 70, 71,   # G#, A, A#, B
    72, 73, 74, 75    # C (octave up), C#, D, D#
]

print(f"✓ Note map: 16 unique notes (chromatic scale)")

# Waveforms
WAVEFORM_SINE = 0
WAVEFORM_SQUARE = 1
WAVEFORM_TRIANGLE = 2
WAVEFORM_SAWTOOTH = 3

waveform_names = ['Sine', 'Square', 'Triangle', 'Sawtooth']

current_waveform = WAVEFORM_SINE
current_octave = 0

# ============================================
# WAVEFORM GENERATION
# ============================================

def generate_sine_buffer(freq_hz):
    buffer = bytearray(samples_per_buffer * 4)
    for i in range(0, len(buffer), 4):
        sample_num = i // 4
        t = sample_num / sample_rate
        sample_float = math.sin(2 * math.pi * freq_hz * t)
        sample_int = int(sample_float * 32767 * 0.35)  # Lower volume for mixing
        
        buffer[i] = sample_int & 0xFF
        buffer[i+1] = (sample_int >> 8) & 0xFF
        buffer[i+2] = sample_int & 0xFF
        buffer[i+3] = (sample_int >> 8) & 0xFF
    return buffer

def generate_square_buffer(freq_hz):
    buffer = bytearray(samples_per_buffer * 4)
    period = sample_rate / freq_hz
    
    for i in range(0, len(buffer), 4):
        sample_num = i // 4
        phase = (sample_num % period) / period
        sample_float = 1.0 if phase < 0.5 else -1.0
        sample_int = int(sample_float * 32767 * 0.35)
        
        buffer[i] = sample_int & 0xFF
        buffer[i+1] = (sample_int >> 8) & 0xFF
        buffer[i+2] = sample_int & 0xFF
        buffer[i+3] = (sample_int >> 8) & 0xFF
    return buffer

def generate_triangle_buffer(freq_hz):
    buffer = bytearray(samples_per_buffer * 4)
    period = sample_rate / freq_hz
    
    for i in range(0, len(buffer), 4):
        sample_num = i // 4
        phase = (sample_num % period) / period
        
        if phase < 0.5:
            sample_float = -1.0 + (4.0 * phase)
        else:
            sample_float = 3.0 - (4.0 * phase)
        
        sample_int = int(sample_float * 32767 * 0.35)
        
        buffer[i] = sample_int & 0xFF
        buffer[i+1] = (sample_int >> 8) & 0xFF
        buffer[i+2] = sample_int & 0xFF
        buffer[i+3] = (sample_int >> 8) & 0xFF
    return buffer

def generate_sawtooth_buffer(freq_hz):
    buffer = bytearray(samples_per_buffer * 4)
    period = sample_rate / freq_hz
    
    for i in range(0, len(buffer), 4):
        sample_num = i // 4
        phase = (sample_num % period) / period
        sample_float = -1.0 + (2.0 * phase)
        sample_int = int(sample_float * 32767 * 0.35)
        
        buffer[i] = sample_int & 0xFF
        buffer[i+1] = (sample_int >> 8) & 0xFF
        buffer[i+2] = sample_int & 0xFF
        buffer[i+3] = (sample_int >> 8) & 0xFF
    return buffer

def generate_all_buffers(waveform):
    print(f"Generating {waveform_names[waveform]}...")
    buffers = {}
    
    # Generate for all unique notes in note_map
    for midi_note in set(note_map):
        freq_hz = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
        
        if waveform == WAVEFORM_SINE:
            buffers[midi_note] = generate_sine_buffer(freq_hz)
        elif waveform == WAVEFORM_SQUARE:
            buffers[midi_note] = generate_square_buffer(freq_hz)
        elif waveform == WAVEFORM_TRIANGLE:
            buffers[midi_note] = generate_triangle_buffer(freq_hz)
        elif waveform == WAVEFORM_SAWTOOTH:
            buffers[midi_note] = generate_sawtooth_buffer(freq_hz)
    
    return buffers

note_buffers = generate_all_buffers(current_waveform)
silence = bytearray(samples_per_buffer * 4)

# CRITICAL: Pre-allocate buffer for mixing
mixed_buffer = bytearray(samples_per_buffer * 4)

print(f"✓ Buffers ready")

# ============================================
# MIXING FUNCTION - CRITICAL FIX!
# ============================================

def mix_buffers_into_target(buffer_list, target_buffer):
    """
    Mix multiple buffers into target_buffer
    CRITICAL: This happens ONCE when keys change, NOT in audio loop!
    """
    if len(buffer_list) == 0:
        # Silence
        for i in range(len(target_buffer)):
            target_buffer[i] = 0
        return
    
    if len(buffer_list) == 1:
        # Single buffer - just copy
        for i in range(len(target_buffer)):
            target_buffer[i] = buffer_list[0][i]
        return
    
    # Mix multiple buffers
    for i in range(0, len(target_buffer), 4):
        mixed_sample = 0
        
        # Add samples from each buffer
        for buf in buffer_list:
            # Read 16-bit signed sample
            sample = buf[i] | (buf[i+1] << 8)
            if sample >= 32768:
                sample -= 65536
            mixed_sample += sample
        
        # Average to prevent clipping
        mixed_sample = mixed_sample // len(buffer_list)
        
        # Clamp
        if mixed_sample > 32767:
            mixed_sample = 32767
        elif mixed_sample < -32768:
            mixed_sample = -32768
        
        # Convert back
        if mixed_sample < 0:
            mixed_sample += 65536
        
        # Write stereo to target buffer
        target_buffer[i] = mixed_sample & 0xFF
        target_buffer[i+1] = (mixed_sample >> 8) & 0xFF
        target_buffer[i+2] = mixed_sample & 0xFF
        target_buffer[i+3] = (mixed_sample >> 8) & 0xFF

# ============================================
# DISPLAY
# ============================================

def update_display(active_notes):
    if not oled_connected:
        return
    
    try:
        oled.fill(0)
        oled.text("POLYPHONIC", 15, 0)
        
        # Waveform and octave
        oled.text(waveform_names[current_waveform][:8], 0, 12)
        oled.text(f"Oct:{current_octave:+d}", 70, 12)
        
        # Battery
        voltage = get_battery_voltage()
        oled.text(f"{voltage:.1f}V", 0, 24)
        
        # Active notes
        if len(active_notes) > 0:
            oled.text(f"Notes: {len(active_notes)}", 0, 36)
            
            note_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
            y = 48
            for i, midi_note in enumerate(active_notes[:3]):
                note_name = note_names[midi_note % 12]
                octave_num = (midi_note // 12) - 1
                oled.text(f"{note_name}{octave_num}", i * 35, y)
        else:
            oled.text("Ready", 0, 48)
        
        oled.show()
    except:
        pass

# ============================================
# LED & BATTERY
# ============================================

def set_led_colors(pressed_keys):
    colors = {
        WAVEFORM_SINE: (0, 32, 0),
        WAVEFORM_SQUARE: (32, 32, 0),
        WAVEFORM_TRIANGLE: (0, 32, 32),
        WAVEFORM_SAWTOOTH: (32, 0, 32)
    }
    
    base_color = colors.get(current_waveform, (16, 16, 16))
    
    for i in range(16):
        if i in pressed_keys:
            keypad.illuminate(i, 64, 64, 64)
        else:
            keypad.illuminate(i, base_color[0], base_color[1], base_color[2])
    keypad.update()

def get_battery_voltage():
    reading = vsys_adc.read_u16()
    return (reading / 65535) * 3.3 * 3

def check_battery():
    voltage = get_battery_voltage()
    if voltage < 3.2:
        for _ in range(2):
            for i in range(16):
                keypad.illuminate(i, 64, 0, 0)
            keypad.update()
            time.sleep(0.1)
            set_led_colors([])
        print(f"⚠️ LOW BATTERY: {voltage:.2f}V")
    return voltage

# ============================================
# ENCODER
# ============================================

def read_encoder():
    global enc_last_clk
    clk = enc_clk.value()
    rotation = 0
    
    if clk != enc_last_clk:
        if clk == 0:
            rotation = 1 if enc_dt.value() == 1 else -1
        enc_last_clk = clk
    
    return rotation

def check_encoder_button():
    return enc_sw.value() == 0

# ============================================
# MAIN LOOP - FIXED!
# ============================================

set_led_colors([])

print("\n" + "="*50)
print("  READY - PLAY CHORDS!")
print("="*50)
print(f"Waveform: {waveform_names[current_waveform]}")
print(f"Octave: {current_octave:+d}")
print(f"Note layout: Chromatic (16 unique notes)")
if oled_connected:
    print("Display: I2C on GP13/14")
print("="*50 + "\n")

if oled_connected:
    update_display([])

# CRITICAL: Current buffer to play (like original diagnostic approach!)
current_play_buffer = silence

pressed_keys = set()
last_battery_check = time.ticks_ms()
last_encoder_press = 0

try:
    while True:
        # Encoder rotation
        rotation = read_encoder()
        if rotation != 0:
            current_waveform = (current_waveform + rotation) % 4
            note_buffers = generate_all_buffers(current_waveform)
            
            # Regenerate current mixed buffer with new waveform
            if len(pressed_keys) > 0:
                buffers_to_mix = []
                for key in list(pressed_keys)[:MAX_POLYPHONY]:
                    base_note = note_map[key]
                    midi_note = max(21, min(108, base_note + (current_octave * 12)))
                    if midi_note in note_buffers:
                        buffers_to_mix.append(note_buffers[midi_note])
                mix_buffers_into_target(buffers_to_mix, mixed_buffer)
                current_play_buffer = mixed_buffer
            
            set_led_colors(pressed_keys)
            print(f"Waveform: {waveform_names[current_waveform]}")
            if oled_connected:
                active_notes = [note_map[k] + (current_octave * 12) for k in pressed_keys]
                update_display(active_notes)
        
        # Encoder button
        if check_encoder_button():
            now = time.ticks_ms()
            if time.ticks_diff(now, last_encoder_press) > 300:
                current_octave = (current_octave + 1) % 5 - 2
                print(f"Octave: {current_octave:+d}")
                last_encoder_press = now
                
                # Regenerate current buffer with new octave
                if len(pressed_keys) > 0:
                    buffers_to_mix = []
                    for key in list(pressed_keys)[:MAX_POLYPHONY]:
                        base_note = note_map[key]
                        midi_note = max(21, min(108, base_note + (current_octave * 12)))
                        
                        # Might need to generate on-the-fly
                        if midi_note in note_buffers:
                            buffers_to_mix.append(note_buffers[midi_note])
                        else:
                            freq_hz = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
                            if current_waveform == WAVEFORM_SINE:
                                buffers_to_mix.append(generate_sine_buffer(freq_hz))
                            elif current_waveform == WAVEFORM_SQUARE:
                                buffers_to_mix.append(generate_square_buffer(freq_hz))
                            elif current_waveform == WAVEFORM_TRIANGLE:
                                buffers_to_mix.append(generate_triangle_buffer(freq_hz))
                            else:
                                buffers_to_mix.append(generate_sawtooth_buffer(freq_hz))
                    
                    mix_buffers_into_target(buffers_to_mix, mixed_buffer)
                    current_play_buffer = mixed_buffer
                
                if oled_connected:
                    active_notes = [note_map[k] + (current_octave * 12) for k in pressed_keys]
                    update_display(active_notes)
        
        # Scan keypad
        button_states = keypad.get_button_states()
        new_pressed_keys = set()
        
        for i in range(16):
            if button_states & (1 << i):
                new_pressed_keys.add(i)
        
        # CRITICAL: Only remix if keys changed!
        if new_pressed_keys != pressed_keys:
            pressed_keys = new_pressed_keys
            set_led_colors(pressed_keys)
            
            # Mix buffers ONCE when keys change
            if len(pressed_keys) == 0:
                # No keys - silence
                current_play_buffer = silence
            else:
                # Get buffers for pressed keys
                buffers_to_mix = []
                
                for key in list(pressed_keys)[:MAX_POLYPHONY]:
                    base_note = note_map[key]
                    midi_note = max(21, min(108, base_note + (current_octave * 12)))
                    
                    if midi_note in note_buffers:
                        buffers_to_mix.append(note_buffers[midi_note])
                    else:
                        # Generate on-the-fly
                        freq_hz = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
                        if current_waveform == WAVEFORM_SINE:
                            buffers_to_mix.append(generate_sine_buffer(freq_hz))
                        elif current_waveform == WAVEFORM_SQUARE:
                            buffers_to_mix.append(generate_square_buffer(freq_hz))
                        elif current_waveform == WAVEFORM_TRIANGLE:
                            buffers_to_mix.append(generate_triangle_buffer(freq_hz))
                        else:
                            buffers_to_mix.append(generate_sawtooth_buffer(freq_hz))
                
                # Mix into mixed_buffer
                mix_buffers_into_target(buffers_to_mix, mixed_buffer)
                current_play_buffer = mixed_buffer
                
                # Print what's playing
                if len(pressed_keys) > 0:
                    notes = [note_map[k] + (current_octave * 12) for k in pressed_keys]
                    print(f"Playing: {notes[:MAX_POLYPHONY]}")
            
            if oled_connected:
                active_notes = [note_map[k] + (current_octave * 12) for k in pressed_keys]
                update_display(active_notes)
        
        # Battery check
        now = time.ticks_ms()
        if time.ticks_diff(now, last_battery_check) > 60000:
            check_battery()
            last_battery_check = now
        
        # CRITICAL: Write current buffer (tight loop like diagnostic!)
        audio_out.write(current_play_buffer)

except KeyboardInterrupt:
    print("\n\nStopping...")

# Cleanup
audio_out.write(silence)
audio_out.deinit()

for i in range(16):
    keypad.illuminate(i, 0, 0, 0)
keypad.update()

if oled_connected:
    oled.fill(0)
    oled.text("STOPPED", 30, 28)
    oled.show()

print("Stopped")
print(f"Battery: {get_battery_voltage():.2f}V")
