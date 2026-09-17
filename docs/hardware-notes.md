# Hardware notes

Findings from running this project's code against the Mini-X controller
(serial 01300036, 50 kV, 10 W), in date order. Findings up to 2026-09-17
are incorporated in [protocol.md](protocol.md); this file is the log behind
them.

## 2026-09-17: read-only checks

`tools/device_check.py`, HV off throughout.

**Confirmed.**

- The controller identifies as an FT2232C/D (bcdDevice `0x0500`).
- The MPSSE sync check works.
- First GPIO read after init: ADBUS `00011111`, ACBUS `00000101`, as in §7.1.
- Enable bits read back clear; MONX reads 0; the chip selects are idle
  between transactions.
- Monitors with HV off: 0–20 counts, including exact zeros. §10.2 saw
  8–23. A zero reading is normal and says nothing about the read path.

**ADBUS bits 0–1 are not status.** After the first transaction they read
back whatever clock and data levels the previous transaction left:
`…1110` after an ADC read, `…1100` after a DS1722 read. This follows from
the reference's shadow-state handling and is expected.

**ACBUS bits 4–7 are not pins.** Channel A of the FT2232C/D has only
ACBUS0–3. Reads showed bit 6 or bit 7 set at random (`0x45`, `0x85`).
`device.py` masks the byte to `0x0F`.

**The ADC trailing bits are data, not padding.** §6.3 describes the reply
as 1 null bit + 12 data bits + 3 trailing bits. The trailing bits always
equal data bits B1, B2, B3: the converter continues LSB-first after the
LSB. This held for every reading: the §10.1 pair, 10 readings from the
first run, and 400 from a 200-poll run (0 mismatches). `device.py` now
rejects a reply whose trailing bits don't match, which catches shifted
frames the null bit misses. An all-zero reply passes both checks, so a
dead data line is still undetectable from a single reading.

**The ADC is probably not a MAX186.** A MAX186 takes an 8-bit control byte
and sends zeros after the data. The 4-bit Start/SGL/ODD/MSBF control word
and the LSB-first continuation look more like an MCP3202/LTC1298-type
part. The part number is unconfirmed; this doesn't change the
implementation.

## 2026-09-17: DS1722 temperature sensor

`tools/ds1722_probe.py`, 1.5 MHz clock. The §8 read transaction (clock low
at TSCS, clock out with `0x10`, clock in with `0x20`) does not work.

- **Reads.** At 1.5 MHz, reading 3 bytes from address `0x00` works with
  the clock low at TSCS plus `0x24`, or the clock high plus `0x20`. Both
  return `e3 00 19`: config `0xE3`, temperature 25.0 °C. The other two
  read combinations return the same bytes shifted one bit
  (`71 80 0c`, `f1 80 0c`). A shifted frame can still decode to a
  plausible temperature (12.5 °C), so a plausibility check alone cannot
  validate a read; reading back the config register can.
- **Addressing.** No combination sent both `0x01` and `0x02` correctly.
  With the reads framed correctly:

  | clk at TSCS / out opcode | `0x01` read as | `0x02` read as |
  |---|---|---|
  | low / `0x10` | `0x00` | `0x00` |
  | low / `0x11` | `0x01` | `0x00` |
  | high / `0x10` | `0x01` | `0x00` |
  | high / `0x11` | `0x00` | `0x00` |

  A plain shift would affect both addresses alike. This pattern points to
  bit-level timing races, possibly from delay on the board's clock or data
  lines, which is comparable to the 333 ns half-period at 1.5 MHz.
  Unconfirmed.
- **Writes.** No config write took effect in any combination.
- **Config `0xE3`.** If the bit layout is 111, 1SHOT, R2–R0, SD, this
  means 9-bit resolution with shutdown set, so the temperature register
  may be stale. Check against the DS1722 datasheet.

A second run confirmed the address table above.

## 2026-09-17: vendor source for the DS1722

The vendor's DLL source is in `reference/Mini-X DLL src/`. `MiniXDlg.cpp`
(`ReadTemperature`, `SetTempSensor`) has the transactions that §8 lacked,
and they differ from everything probed so far:

- TSCS rises with the clock **low**. The low state is rewritten twice
  more, then the clock goes **high** for the transfer. The source comment
  confirms that the board inverts the clock ("take TS clock low - this
  makes clock high").
- The read address is sent with `0x12`, 8 bits in bit mode, and the reply
  is read with `0x20`, LSB then MSB.
- The config write sends `[0x80, 0xE8]` with `0x10`, using the same
  low-then-high clock sequence.
- The source calls register `0x00` "Status". It is the config register's
  read address.

`tools/ds1722_probe.py` now tries this sequence (mode `vendor`) alongside
the earlier modes.

## 2026-09-17: DS1722 probe with the vendor sequence

`tools/ds1722_probe.py`, read-only phase, at 1.5 MHz and 150 kHz.

- **Vendor mode reads and addresses correctly** with address opcode `0x10`
  or `0x12` and read opcode `0x20` or `0x24`: `@00 = e3 00 19`,
  `@01 = 00 19`, `@02 = 19`. With `0x11` or `0x13`, address `0x01` reads
  as `0x00`.
- **Low and high modes** gave the same results as before.
- **Both rates gave identical tables.** The earlier timing-race guess was
  wrong: the clock sequence at select is what matters.
- **Config** is still `0xE3`, so the temperature register may be stale.

`device.py` now uses the vendor read (`0x12` address, `0x20`, three bytes
from `00h`) and checks the config byte on every reading.

## 2026-09-17: DS1722 config write

`tools/ds1722_probe.py --khz 1500 --write`, then
`tools/device_check.py --temp`.

- **Writes work in vendor mode** with `0x10` (the vendor's opcode) or
  `0x12`: writing `0xE4` then `0xE8` reads back exactly, whichever working
  read combination is used. `0x11` and `0x13` do not write.
- **After the write,** config reads `0xE8` (12-bit, converting), and the
  temperature read 25.8750 °C 1.5 s later.
- **`device_check.py --temp` through `device.py`** read 27.3125 °C on three
  polls. There were 0 ADC framing errors in 6 readings, and ADBUS read
  `00011101`: clock high and data low, as the temperature read leaves them.
- **Not yet independent:** the sensor was already configured by the probe
  when `device_check.py` ran, so `device.py`'s own write has not been
  tested on its own. Its bytes match the probe's. The config is volatile, so
  the next run after a power-up will test it.
