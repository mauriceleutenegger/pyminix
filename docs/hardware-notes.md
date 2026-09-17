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

## 2026-09-17: first energized session through the GUI

`minix-gui`, run record `20260917-113354_01300036.csv`. Three HV-on periods
at 15 kV, with 15 µA and 10 µA, including setpoint changes while on. X-ray
production was confirmed with a radiation monitor. Every switch-on and
switch-off completed without a fault or warning.

- **Ramp.** At the first settle check, 0.5 s after the DAC write, HV read
  14.58 kV (97 %) and the current was also within range. A full switch-on
  takes 1.8 s: the NSI plan's fixed pauses plus one settle check per
  channel.
- **MONX** was already asserted at the first recorded sample after the
  enables were set (about 1 s later). The 1 s check after the ramp never
  failed. Whether MONX asserts with zero setpoints is still unknown.
- **Readback with HV on** (1 Hz samples, steady state):

  | setpoint | kV mean | kV σ | µA mean | µA σ |
  |---|---|---|---|---|
  | 15 kV, 10 µA (n=10) | 14.90 | 0.17 (14 counts) | 10.32 | 0.82 (16 counts) |
  | 15 kV, 15 µA (n=36) | 14.97 | 0.18 (14 counts) | 15.07 | 0.77 (15 counts) |

  Single readings are noisy. With the supply on, the noise is about four
  times the idle noise; the 7-sample average steadies the display. Every
  sample was inside the ±10 % + 1 band.
- **Idle, before any HV:** mean 8.7 counts (HV) and 8.5 counts (current),
  maxima 24 and 23 counts.
- **HV after switch-off** (from 15 kV): 1.1 kV after 1 s, 0.85 after 2 s,
  0.46 after 3 s, about 0.1 after 6 s. A fast fall followed by a slow
  discharge tail. The reference's 7 s wait before testing HV-off readings
  covers it.
- **Board temperature:** 27.2 °C falling to 26.9 °C; no heating visible at
  0.15–0.23 W.

The simulator was calibrated to these results: ramp time constant 0.15 s,
noise 15 counts with the supply on, and a two-part discharge (7 % of the
voltage decaying with a 2.5 s time constant).

## 2026-09-17: full power

`minix-gui`, run record `20260917-114531_01300036.csv`. Short runs up to
50 kV / 198.95 µA (9.95 W, reduced from a 200 µA request by the power
limit), and at 20 kV with 50–200 µA. Every sequence completed without a
fault.

| setpoint | power | MONX high (1 Hz samples) | kV | µA |
|---|---|---|---|---|
| 15 kV / 15 µA | 0.23 W | 16/16 | 14.90 ± 0.18 | 15.26 ± 0.72 |
| 50 kV / 15 µA | 0.75 W | 71/71 | 49.79 ± 0.17 | 15.01 ± 0.74 |
| 20 kV / 50 µA | 1.0 W | 19/19 | 19.97 ± 0.18 | 50.05 ± 0.74 |
| 20 kV / 100 µA | 2.0 W | 18/18 | 19.97 ± 0.17 | 99.84 ± 0.70 |
| 20 kV / 190 µA | 3.8 W | 7/13 | 19.95 ± 0.24 | 189.85 ± 0.98 |
| 20 kV / 200 µA | 4.0 W | 14/15 | 19.91 ± 0.18 | 199.90 ± 0.97 |
| 50 kV / 198.95 µA | 9.9 W | 80/95 | 49.79 ± 0.18 | 198.67 ± 0.73 |

- **MONX flickers at high emission current.** It dropped briefly and often
  (173 warnings in about 3 minutes, sometimes several per second) at
  190–200 µA, at both 20 kV and 50 kV. It never dropped at 50 kV with
  15 µA or at up to 100 µA. During the drops the HV and current readings
  stayed on target with ordinary noise, and every sample was in range, so
  the drops did not coincide with any visible change in output. What MONX
  signals is unknown: the only name for it is "MON MINIX RDY" in the
  source, and the vendor application only displays it. It may be a
  regulation or compliance flag near the current limit; a question for
  Amptek.
- **The power indicator flickers at full power.** Measured power at
  50 kV / 198.95 µA was 9892 mW on average (committed 9948 mW), with noise
  of about 50 mW. Single readings crossed the caution (9900 mW) and danger
  (10000 mW, max 10007 mW) thresholds, so the band switched between
  normal, caution and danger. The out-of-range power check (< 10050 mW)
  never tripped.
- **Readback at full scale** is as good as at low settings: HV about
  0.2 kV low at 50 kV, current within 0.3 µA at 199 µA.
- **Board temperature** stayed between 27.0 and 28.0 °C. The runs were
  short, and the sensor is on the controller board, not the tube.

**Changes made in response** (same day):

- **MONX:** brief drops are now counted rather than reported one by one.
  The count is in the status and the run record (`monx_drops`), with a
  once-a-minute summary. A warning appears only when MONX stays low for
  `monx_warning_s` (1 s). The GUI lamp shows the count and turns amber
  only for a sustained drop.
- **Power band:** now computed from the averaged power with 100 mW
  hysteresis. The run record has `power_mw` (single reading) and
  `power_average_mw`.
- **Simulator:**
  - MONX reads low on 20 % of reads above 185 µA, switchable in the panel.
  - Readback uses the measured gains: HV 0.4 % low; current 0.3 % low plus
    6 counts.
  - Board heating now has a 10-minute time constant, a guess consistent
    with no visible heating in these short runs.
