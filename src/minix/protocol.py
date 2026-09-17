"""Protocol constants: pin masks, MPSSE opcodes, DAC and ADC command words.

Constants and pure conversions only. Values are for board revision C0
(docs/protocol.md §3, §4, §6.2, §6.3, §8).
"""

# --- USB identity (§2) ------------------------------------------------------

USB_VID = 0x0403
USB_PID = 0xD058              # custom Amptek PID; pyftdi must be told about it
USB_DESCRIPTION = "MiniX Controller"
USB_INTERFACE = 1             # channel A
FT2232CD_VERSION = 0x0500     # bcdDevice of the FT2232C/D

LATENCY_MS = 4
READ_TIMEOUT_MS = 40
WRITE_TIMEOUT_MS = 40

# --- ADBUS, low byte (§3) ---------------------------------------------------

CLK = 0x01                    # SCLK
DATA_OUT = 0x02               # MOSI
DATA_IN = 0x04                # MISO (input)
ADCS = 0x08                   # ADC chip select, active low
DACS = 0x10                   # DAC chip select, active low
HV_EN_A = 0x20                # CTRL_HV_EN_A
HV_EN_B = 0x40                # CTRL_HV_EN_B
MONX = 0x80                   # MON MINIX RDY, input; 1 once the supply is up

HV_EN_BOTH = HV_EN_A | HV_EN_B

ADBUS_DIRECTION = 0x7B        # OUTPUTMODE == INPUTMODE on rev C0
# Initial state: all outputs high except the HV enables (§5 step 6).
ADBUS_INIT = 0xFB & ~HV_EN_BOTH

# --- ACBUS, high byte (§3) --------------------------------------------------

INTERLOCK = 0x01              # "!RESET" input: 1 = closed, 0 = open (§7.3)
ACBUS2_UNKNOWN = 0x04         # input, reads set, function unknown
TSCS = 0x08                   # DS1722 chip enable, ACTIVE HIGH

ACBUS_DIRECTION = 0x08        # only TSCS is an output
# The FT2232C/D has only ACBUS0-3 on channel A. Bits 4-7 of a read are not
# pins and have been observed set at random (0x40, 0x80).
ACBUS_PINS = 0x0F
ACBUS_INIT = 0x00             # TSCS deasserted

# --- MPSSE opcodes (§4) -----------------------------------------------------

SET_ADBUS = 0x80              # + state, direction
GET_ADBUS = 0x81              # -> 1 byte
SET_ACBUS = 0x82              # + state, direction
GET_ACBUS = 0x83              # -> 1 byte
SET_DIVISOR = 0x86            # + low, high
CLOCK_BYTES_OUT = 0x10        # + len-1 (2 bytes), data
CLOCK_BITS_OUT = 0x13         # + len-1 (1 byte), data
# Same as CLOCK_BITS_OUT on the other clock edge. The DS1722 address needs
# this one (§8.3); the ADC nibble needs 0x13.
CLOCK_BITS_OUT_TS = 0x12
CLOCK_BYTES_IN = 0x20         # + len-1 (2 bytes)

CLOCK_DIVISOR = (0x03, 0x00)  # ~1.5 MHz

# An invalid opcode makes MPSSE reply BAD_COMMAND_ECHO followed by the
# offending byte (§5.1).
SYNC_PROBE = 0xAB
BAD_COMMAND_ECHO = 0xFA

# --- AD5623R dual DAC (§6.2) ------------------------------------------------

DAC_HV = 0x18                 # write and update DAC A
DAC_CURRENT = 0x19            # write and update DAC B

# --- 12-bit two-channel ADC (§6.3) ------------------------------------------

# The document says "MAX186-family", but a MAX186 takes an 8-bit control
# byte. The 4-bit control word and the LSB-first repeat after the data word
# (see device.AdcReading.trailing_consistent) suggest an MCP3202/LTC1298-type
# part. Unconfirmed.
# Only the upper nibble is clocked: Start, SGL/DIF, ODD/SIGN, MSBF.
ADC_HV = 0xD0                 # 1101 -> channel 0
ADC_CURRENT = 0xF0            # 1111 -> channel 1
ADC_NIBBLE_BITS = 4
ADC_NULL_BIT = 0x1000         # must read 0 after the >> 3 unpack
ADC_DATA_MASK = 0x0FFF

# --- DS1722 temperature sensor (§8) -----------------------------------------

TS_CONFIG_READ = 0x00         # configuration register, read address
TS_CONFIG_WRITE = 0x80        # configuration register, write address
TS_TEMP_LSB = 0x01            # temperature LSB, read address (MSB follows)
TS_TEMP_MSB = 0x02
TS_CONFIG_FIXED = 0xE0        # top three config bits always read 1
# Config: fixed 111, 1SHOT=0, 12-bit resolution, SD=0 (continuous).
TS_CONFIG_12BIT_CONTINUOUS = 0xE8
TS_LSB_C = 0.0625
TS_MIN_C = -55.0              # DS1722 operating range
TS_MAX_C = 125.0

# --- Analog scaling (§6.1) --------------------------------------------------

VREF = 4.096
DAC_ADC_SCALE = 4096
COUNTS_MAX = 0x0FFF

# Engineering-unit factors. The HV factor depends on the board and is
# selected from the serial number in config; the current factor does not.
HV_FACTOR_40KV = 10.0         # kV per volt
HV_FACTOR_50KV = 12.5
CURRENT_FACTOR = 50.0         # uA per volt

# Nudge against binary float error before truncating (e.g. 10.1 kV is
# 807.9999999999999 counts). Same idiom as the reference's 1e-9 terms.
_COUNTS_EPSILON = 1e-9


def volts_to_counts(volts: float) -> int:
    """Convert a DAC voltage to counts, truncating toward zero (§6.2).

    Raises ValueError outside 0..COUNTS_MAX; clamping is policy's job, not
    this function's.
    """
    if volts < 0:
        raise ValueError(f"negative DAC voltage {volts} V")
    counts = int(volts / VREF * DAC_ADC_SCALE + _COUNTS_EPSILON)
    if counts > COUNTS_MAX:
        raise ValueError(f"{volts} V is {counts} counts, outside 0..{COUNTS_MAX}")
    return counts


def counts_to_volts(counts: int) -> float:
    return counts / DAC_ADC_SCALE * VREF
