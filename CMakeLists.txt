cmake_minimum_required(VERSION 3.13)

# Pull in Pico SDK (PICO_SDK_PATH must be set or pico-sdk must be a sibling dir)
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(pico_synth C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(pico_synth main.c)

# Generate C header from i2s.pio (must be after add_executable)
pico_generate_pio_header(pico_synth ${CMAKE_CURRENT_LIST_DIR}/i2s.pio)

target_link_libraries(pico_synth
    pico_stdlib
    hardware_spi
    hardware_i2c
    hardware_adc
    hardware_dma
    hardware_pio
    hardware_clocks
    m          # math library (sinf, powf used at startup for drum tables)
)

# Produce .uf2 file (drag-and-drop onto Pico in BOOTSEL mode)
pico_add_extra_outputs(pico_synth)

# USB serial for debug prints (optional — can set to 0 to save flash)
pico_enable_stdio_usb(pico_synth 1)
pico_enable_stdio_uart(pico_synth 0)
