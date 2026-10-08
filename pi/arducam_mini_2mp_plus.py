"""arducam mini 2mp plus (OV2640) on a raspberry pi: spi talks to the arduchip frame fifo, i2c configures the sensor.

wiring (pi physical pin -> board pin): 24 CE0 -> CS, 19 MOSI -> MOSI, 21 MISO -> MISO, 23 SCLK -> SCK,
3 SDA -> SDA, 5 SCL -> SCL, 1 (3.3V) -> VCC, 6 GND -> GND. enable spi + i2c first (pi/setup.sh does).

  uv run python -m pi.arducam_mini_2mp_plus --selftest /tmp/arducam-test.jpg
"""
import argparse
import time

import smbus2
import spidev

from pi import ov2640_registers as registers

SPI_BUS = 0
SPI_CHIP_SELECT = 0
SPI_SPEED_HZ = 4_000_000
I2C_BUS = 1
SENSOR_I2C_ADDRESS = 0x30

# arduchip (cpld) registers, from arducam's ArduCAM.h
WRITE_FLAG = 0x80
TEST_REGISTER = 0x00
FIFO_CONTROL_REGISTER = 0x04
FIFO_CLEAR = 0x01
FIFO_START = 0x02
CPLD_RESET_REGISTER = 0x07
CPLD_RESET = 0x80
STATUS_REGISTER = 0x41
CAPTURE_DONE = 0x08
FIFO_LENGTH_REGISTERS = (0x42, 0x43, 0x44)  # low, middle, high byte
FIFO_LENGTH_HIGH_MASK = 0x7F
BURST_FIFO_READ = 0x3C
SPIDEV_BUFFER_SIZE_PATH = "/sys/module/spidev/parameters/bufsiz"  # pi/setup.sh raises it via cmdline.txt
MAX_FIFO_BYTES = 0x7FFFFF  # the plus has 8 MB; 0x5FFFF in arducam's pi example is the non-plus value

# ov2640 sensor registers
BANK_SELECT = 0xFF
SENSOR_BANK = 0x01
SENSOR_PRODUCT_ID_HIGH = 0x0A
SENSOR_PRODUCT_ID_LOW = 0x0B
SENSOR_COM7 = 0x12
SENSOR_SOFT_RESET = 0x80

JPEG_SIZE_TABLES = {
    "160x120": registers.OV2640_160x120_JPEG,
    "176x144": registers.OV2640_176x144_JPEG,
    "320x240": registers.OV2640_320x240_JPEG,
    "352x288": registers.OV2640_352x288_JPEG,
    "640x480": registers.OV2640_640x480_JPEG,
    "800x600": registers.OV2640_800x600_JPEG,
    "1024x768": registers.OV2640_1024x768_JPEG,
    "1280x1024": registers.OV2640_1280x1024_JPEG,
    "1600x1200": registers.OV2640_1600x1200_JPEG,
}

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class CorruptCapture(Exception):
    """one bad frame (empty fifo, missing jpeg markers); the next capture usually works."""


class ArducamMini2MPPlus:
    def __init__(self, resolution):
        if resolution not in JPEG_SIZE_TABLES:
            raise ValueError(f"resolution must be one of {sorted(JPEG_SIZE_TABLES)}, got {resolution}")
        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_CHIP_SELECT)
        self.spi.max_speed_hz = SPI_SPEED_HZ
        self.spi.mode = 0
        with open(SPIDEV_BUFFER_SIZE_PATH) as buffer_size_file:
            self.spi_buffer_size = int(buffer_size_file.read())
        self.i2c = smbus2.SMBus(I2C_BUS)
        self.reset_cpld()
        self.check_spi()
        self.check_sensor()
        self.configure_jpeg(resolution)

    # arduchip over spi
    def write_register(self, address, value):
        self.spi.xfer2([address | WRITE_FLAG, value])

    def read_register(self, address):
        return self.spi.xfer2([address & ~WRITE_FLAG, 0x00])[1]

    def reset_cpld(self):
        self.write_register(CPLD_RESET_REGISTER, CPLD_RESET)
        time.sleep(0.1)
        self.write_register(CPLD_RESET_REGISTER, 0x00)
        time.sleep(0.1)

    def check_spi(self):
        self.write_register(TEST_REGISTER, 0x55)
        echoed = self.read_register(TEST_REGISTER)
        if echoed != 0x55:
            raise RuntimeError(f"spi test failed: wrote 0x55, read {echoed:#04x} (check CS/MOSI/MISO/SCK wiring and that spi is enabled)")

    # ov2640 over i2c
    def write_sensor_register(self, register, value):
        self.i2c.write_byte_data(SENSOR_I2C_ADDRESS, register, value)
        time.sleep(0.001)  # arducam paces every sensor write by 1 ms

    def read_sensor_register(self, register):
        return self.i2c.read_byte_data(SENSOR_I2C_ADDRESS, register)

    def write_sensor_table(self, table):
        # like arducam's python port, the {0xff, 0xff} terminator is not sent (their c writers send it, which
        # flips the bank select to the sensor bank and misroutes the first writes of OV2640_JPEG)
        for register, value in table:
            self.write_sensor_register(register, value)

    def check_sensor(self):
        self.write_sensor_register(BANK_SELECT, SENSOR_BANK)
        product_id = (self.read_sensor_register(SENSOR_PRODUCT_ID_HIGH), self.read_sensor_register(SENSOR_PRODUCT_ID_LOW))
        if product_id[0] != 0x26 or product_id[1] not in (0x40, 0x41, 0x42):
            raise RuntimeError(f"not an OV2640: product id {product_id[0]:#04x} {product_id[1]:#04x} (check SDA/SCL wiring and that i2c is enabled)")

    def configure_jpeg(self, resolution):
        self.write_sensor_register(BANK_SELECT, SENSOR_BANK)
        self.write_sensor_register(SENSOR_COM7, SENSOR_SOFT_RESET)
        time.sleep(0.1)
        self.write_sensor_table(registers.OV2640_JPEG_INIT)
        self.write_sensor_table(registers.OV2640_YUV422)
        self.write_sensor_table(registers.OV2640_JPEG)
        self.write_sensor_register(BANK_SELECT, SENSOR_BANK)
        self.write_sensor_register(0x15, 0x00)
        self.write_sensor_table(registers.OV2640_320x240_JPEG)  # arducam's InitCAM always lands here first
        self.write_sensor_table(JPEG_SIZE_TABLES[resolution])
        time.sleep(1)  # let auto exposure settle before the first frame

    # capture
    def fifo_length(self):
        low, middle, high = (self.read_register(address) for address in FIFO_LENGTH_REGISTERS)
        return ((high & FIFO_LENGTH_HIGH_MASK) << 16) | (middle << 8) | low

    def read_fifo(self, length):
        # the whole frame in one chip-select-low transaction: burst command, then clock out `length` bytes.
        # spidev caps a transaction at bufsiz (4096 by default), so frames need the raised limit from pi/setup.sh
        if length + 1 > self.spi_buffer_size:
            raise RuntimeError(f"frame is {length} bytes but spidev.bufsiz is {self.spi_buffer_size}; run pi/setup.sh (adds spidev.bufsiz to cmdline.txt) and reboot")
        response = self.spi.xfer2(bytes([BURST_FIFO_READ]) + bytes(length))
        return bytes(response[1:])

    def capture_jpeg(self, timeout_seconds=3.0):
        self.write_register(FIFO_CONTROL_REGISTER, FIFO_CLEAR)  # flush
        self.write_register(FIFO_CONTROL_REGISTER, FIFO_CLEAR)  # clear the capture-done flag (same bit, arducam writes it twice)
        self.write_register(FIFO_CONTROL_REGISTER, FIFO_START)
        deadline = time.monotonic() + timeout_seconds
        while not self.read_register(STATUS_REGISTER) & CAPTURE_DONE:
            if time.monotonic() > deadline:
                raise RuntimeError(f"capture did not finish within {timeout_seconds}s")
            time.sleep(0.005)
        length = self.fifo_length()
        if length == 0 or length > MAX_FIFO_BYTES:
            raise CorruptCapture(f"fifo length {length} out of range")
        data = self.read_fifo(length)
        self.write_register(FIFO_CONTROL_REGISTER, FIFO_CLEAR)
        # no fixed dummy byte on the plus, and the fifo pads past the end marker: cut from FFD8 to the next FFD9
        start = data.find(JPEG_START)
        if start < 0 and (marker_tail := data.find(b"\xd8\xff", 0, 8)) >= 0:
            # some 2mp modules return 00 instead of the FF before D8 (ArduCAM/Arduino issue 402)
            data = JPEG_START + data[marker_tail + 1 :]
            start = 0
        end = data.find(JPEG_END, start + 2)
        if start < 0 or end < 0:
            raise CorruptCapture(f"no complete jpeg in {length} fifo bytes (start marker at {start}, end marker at {end})")
        return data[start : end + 2]


def selftest(output_path, resolution):
    camera = ArducamMini2MPPlus(resolution)
    print("spi ok (test register echoed 0x55), sensor ok (OV2640 product id)")
    started = time.monotonic()
    jpeg = camera.capture_jpeg()
    print(f"captured {len(jpeg)} byte jpeg at {resolution} in {time.monotonic() - started:.2f}s")
    with open(output_path, "wb") as output_file:
        output_file.write(jpeg)
    import cv2
    import numpy

    frame = cv2.imdecode(numpy.frombuffer(jpeg, numpy.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("captured bytes are not a decodable jpeg")
    print(f"decoded {frame.shape[1]}x{frame.shape[0]} -> {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", metavar="OUTPUT_JPEG", required=True, help="check spi + sensor, capture one frame to this path")
    parser.add_argument("--resolution", default="640x480", choices=sorted(JPEG_SIZE_TABLES))
    arguments = parser.parse_args()
    selftest(arguments.selftest, arguments.resolution)
