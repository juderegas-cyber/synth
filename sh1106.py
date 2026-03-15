# MicroPython SH1106 OLED driver, I2C and SPI interfaces
# Based on https://github.com/robert-hh/SH1106

from micropython import const
import framebuf
import time


# SH1106 commands
SET_CONTRAST = const(0x81)
SET_NORM_INV = const(0xa6)
SET_DISP = const(0xae)
SET_SCAN_DIR = const(0xc0)
SET_SEG_REMAP = const(0xa0)
SET_COL_ADDR = const(0x00)
SET_PAGE_ADDR = const(0xb0)
SET_DISP_START_LINE = const(0x40)
SET_CHARGE_PUMP = const(0x8d)


class SH1106(framebuf.FrameBuffer):
    def __init__(self, width, height, external_vcc):
        self.width = width
        self.height = height
        self.external_vcc = external_vcc
        self.pages = self.height // 8
        self.buffer = bytearray(self.pages * self.width)
        super().__init__(self.buffer, self.width, self.height, framebuf.MONO_VLSB)
        self.init_display()

    def init_display(self):
        for cmd in (
            SET_DISP | 0x00,  # display off
            SET_DISP_START_LINE | 0x00,  # start line 0
            SET_SEG_REMAP | 0x01,  # column address 127 mapped to SEG0
            SET_SCAN_DIR | 0x08,  # scan from COM[N] to COM0
            SET_CONTRAST,
            0xff,  # maximum contrast
            0xa4,  # output follows RAM contents
            SET_NORM_INV,  # not inverted
            0xd5,
            0x80,  # set display clock divide ratio
            0xd9,
            0x22 if self.external_vcc else 0xf1,  # pre-charge period
            0xda,
            0x12,  # com pins configuration
            0xdb,
            0x40,  # vcom deselect level
            0x33,  # VPP = 9V
            SET_CHARGE_PUMP,
            0x10 if self.external_vcc else 0x14,  # charge pump setting
            SET_DISP | 0x01,  # display on
        ):
            self.write_cmd(cmd)
        self.fill(0)
        self.show()

    def poweroff(self):
        self.write_cmd(SET_DISP | 0x00)

    def poweron(self):
        self.write_cmd(SET_DISP | 0x01)
        
    def sleep(self, value):
        """Sleep mode (compatible with some libraries)"""
        if value:
            self.poweroff()
        else:
            self.poweron()

    def contrast(self, contrast):
        self.write_cmd(SET_CONTRAST)
        self.write_cmd(contrast)

    def invert(self, invert):
        self.write_cmd(SET_NORM_INV | (invert & 1))

    def show(self):
        # SH1106 has 132 columns but only 128 are visible
        # Column offset of 2 to center the 128 pixel display
        offset = 2
        
        for page in range(self.pages):
            self.write_cmd(SET_PAGE_ADDR | page)
            self.write_cmd(SET_COL_ADDR | offset)
            self.write_cmd(0x10 | (offset >> 4))
            self.write_data(self.buffer[page * self.width : (page + 1) * self.width])


class SH1106_I2C(SH1106):
    def __init__(self, width, height, i2c, addr=0x3c, external_vcc=False):
        self.i2c = i2c
        self.addr = addr
        self.temp = bytearray(2)
        self.write_list = [b"\x40", None]  # Co=0, D/C#=1
        super().__init__(width, height, external_vcc)

    def write_cmd(self, cmd):
        self.temp[0] = 0x80  # Co=1, D/C#=0
        self.temp[1] = cmd
        self.i2c.writeto(self.addr, self.temp)

    def write_data(self, buf):
        self.write_list[1] = buf
        self.i2c.writevto(self.addr, self.write_list)


class SH1106_SPI(SH1106):
    def __init__(self, width, height, spi, dc, res, cs, external_vcc=False):
        self.rate = 10 * 1024 * 1024
        dc.init(dc.OUT, value=0)
        res.init(res.OUT, value=0)
        cs.init(cs.OUT, value=1)
        self.spi = spi
        self.dc = dc
        self.res = res
        self.cs = cs
        
        self.res(1)
        time.sleep_ms(1)
        self.res(0)
        time.sleep_ms(10)
        self.res(1)
        super().__init__(width, height, external_vcc)

    def write_cmd(self, cmd):
        self.spi.init(baudrate=self.rate, polarity=0, phase=0)
        self.cs(1)
        self.dc(0)
        self.cs(0)
        self.spi.write(bytearray([cmd]))
        self.cs(1)

    def write_data(self, buf):
        self.spi.init(baudrate=self.rate, polarity=0, phase=0)
        self.cs(1)
        self.dc(1)
        self.cs(0)
        self.spi.write(buf)
        self.cs(1)
