# Mini-X Controller — Operator Guide

This guide is for people operating the Amptek Mini-X X-ray source with the `minix-gui` program. It covers starting the program, switching X-rays on and off, reading the display, and what to do when something goes wrong.

---

## 1. Safety first

- **Follow your laboratory's radiation-safety procedures.** This program controls the X-ray tube; it is not a radiation-safety system.
- **The interlock is shorted in this installation.** The interlock lamp will always show *closed*. Nothing in the software stops X-rays when the enclosure is opened.
- **Emergency stop is a software command** sent over USB. If the program stops responding, or the controller stops answering, it may not work. Know how to remove power from the controller, and do so as your procedures require.
- **Treat the tube as on whenever the red X-RAYS ON banner is showing**, and whenever you are unsure. Confirm with a radiation monitor.
- **"Check the tube physically"** in a message means the software could not confirm that the high voltage is off. Do not assume it is. Check with a radiation monitor, and remove power from the controller if needed.

---

## 2. Starting and stopping the program

```
mamba activate minix
minix-gui
```

- **Closing the window with X-rays on:** the program asks first, then switches the high voltage off before it exits.
- **Simulation mode:** `minix-gui --sim` runs the same window against a simulated controller. Nothing is connected and no X-rays are produced, so it is a safe way to learn the program (§10).

---

## 3. The window

From top to bottom:

| Area | What it shows |
|---|---|
| **Controller** row | The controller found on USB, with **Refresh**, **Connect** and **Disconnect** buttons. |
| **Unit** line | Serial number, voltage range, and **power rating** of the connected unit, e.g. *Mini-X 01300036: 50 kV, NSI, 10 W rating*. The rating is also in the window title. On the right: the current state. |
| **Banners** | Red: a fault, or the program not responding (§8). |
| **X-ray indicator** | *X-rays off* (grey), or **X-RAYS ON** (blinking red) while the high voltage is on or switching. |
| **Setpoints** | The voltage and current you want, their allowed ranges, a power preview, the **Update** button, and what was last committed. |
| **Monitors** | Measured high voltage and emission current, the power bar, the limits, the board temperature, and whether the readings match the setpoints. |
| **Lamps** | Interlock, HV enables, tube ready (§6). |
| **Buttons** | **HV ON**, **HV OFF**, **EMERGENCY STOP**. |
| **Log** | Messages with times: what was done, adjusted, warned about, or went wrong (§7, §8). |
| **Status bar** | The file this session is being recorded to (§9). |

States shown on the right of the unit line:

| State | Meaning |
|---|---|
| disconnected | No controller is open. |
| HV off | Connected; high voltage off. |
| switching HV on | Ramping up; takes about 2 s. |
| HV on | X-rays are being produced. |
| changing setpoints | Moving to new values with HV on; about 2 s. |
| switching HV off | Ramping down; under 1 s. |
| fault | HV has been forced off because of a problem; see the red banner (§8). |

---

## 4. Normal operation

1. **Connect.** Choose the controller in the list (press **Refresh** if it is missing) and press **Connect**.
   - **First connection on this computer:** you are asked for the power rating (§5).
   - **Lamps:** the interlock lamp shows amber *Interlock restored* for about 3 seconds, then green.
   - **Monitors:** they show around 0.1 kV and up to about 1 µA. That is normal with HV off.
2. **Set the voltage and current** in the Setpoints boxes. The line below them previews the resulting power. If the power would exceed the limit, it says in orange how far the current will be reduced (§5.2).
3. **Press HV ON.** A confirmation shows the values that will be applied. Answer **Yes**. The X-RAYS ON banner starts blinking, and about 2 seconds later the state reads *HV on*.
4. **To change settings while on**, edit the boxes and press **Update**. The change takes about 2 seconds. With HV off, **Update (applies at HV on)** only stores the values.
5. **Press HV OFF** to stop. The high-voltage reading takes several seconds to fall to zero after switch-off: about 1 kV after 1 s, 0.5 kV after 3 s. This is normal discharge, not a fault.
6. **Press Disconnect** when finished, or close the window. If the high voltage is still on, Disconnect switches it off first.

**What normal looks like with HV on:**

- The monitors match the setpoints closely. Voltage reads about 0.4 % low (49.8 kV at 50 kV). Current reads within about 0.3 µA.
- The displayed values are running averages, so they move a little. Individual readings vary by about ±0.2 kV and ±0.8 µA.
- *Monitors in range* shows in green.
- The tube-ready lamp is green. At currents near 200 µA it also counts brief drops (§6); that is expected.

---

## 5. Power rating and limits

### 5.1 The power rating

The Mini-X is made in **4 W** and **10 W** versions: the most power (voltage × current) the tube may be run at.

- **Most controllers state their model**, and with it the rating and voltage range. The program reads it when connecting and shows it beside the rating, e.g. *rating 10 W (controller reports MX50.10)*. Nothing needs configuring.
- **Some controllers do not state a rating** (the older non-OEM Mini-X). The program then asks the first time that serial number is connected on this computer.
  - **Take the rating from the unit's label or documentation.** If you are unsure, choose **4 W**. A 4 W setting on a 10 W unit only limits the output; a 10 W setting on a 4 W unit would let the tube be over-driven.
  - **Say where it came from** (e.g. "label on the unit"), tick the confirmation box, and press **Save**. The answer goes into `~/.config/minix/units.toml` and is used from then on.
- **If a stored rating disagrees with the controller**, the program uses the **lower** of the two and logs a warning. Find out which is right before running near the limit.
- **To change a stored rating**, edit that file (look for the unit's serial number) and restart the program.
- **In simulation mode** an entered rating is not stored, so you are asked each time.
- **How the rating is known** was worked out from Amptek's own software and checked on one unit, so treat a surprising rating as a question for Amptek rather than as fact.

### 5.2 Limits

| | 50 kV unit | 40 kV unit |
|---|---|---|
| Voltage | 10 – 50 kV | 10 – 40 kV |
| Current | 5 – 200 µA | 5 – 200 µA |
| Power (committed setpoints) | 9.95 W (10 W unit), 3.95 W (4 W unit) | same |

The boxes do not accept values outside the voltage and current ranges. If the voltage × current you ask for would exceed the power limit, **the current is reduced; the voltage is never changed.** The preview says so before you commit, and the log records it afterwards, e.g.:

> current reduced to stay under the power limit: 200 → 198.95 µA

### 5.3 The power bar

The bar shows the measured power (averaged) against the rating, with the rating and limit written below it.

| Colour | Band | Range (10 W unit) | Range (4 W unit) |
|---|---|---|---|
| white | idle | below 10 mW | below 10 mW |
| green | normal | up to 9.9 W | up to 3.9 W |
| yellow | caution | 9.9 – 10 W | 3.9 – 4 W |
| red | danger | 10 W and above | 4 W and above |

- **At the highest setting, yellow is expected.** The bar may show green for a second or two first.
- **Red means the measured power has reached the rating.** Lower the setpoints. It should not happen at committed settings; if it persists, switch HV off and report it.

---

## 6. Lamps

| Lamp | Shows | Meaning |
|---|---|---|
| Interlock | green *Interlock closed* | Normal. **In this installation it is always closed** (§1). |
| | amber *Interlock restored* | For about 3 s after connecting or clearing a fault. HV ON is unavailable meanwhile. |
| | red *Interlock OPEN* | The interlock input reads open, and HV is switched off. Not expected here. |
| HV enables | red *HV enabled* | The controller's high-voltage enable signals are on. |
| | grey *HV enables off* | They are off. |
| Tube ready | green *Tube ready* | The controller reports the tube ready. |
| | green *Tube ready (N brief drops)* | With HV on, the ready signal dropped briefly N times since switch-on. Common at 190–200 µA; the output is not affected. A summary goes to the log once a minute. |
| | amber *Tube not ready* | With HV on, the ready signal has stayed off for over a second (§7). |
| | grey *Tube not ready* | HV is off. |

Under the monitors: green *Monitors in range* means the readings match the setpoints (within 10 % + 1). Amber **OUT OF RANGE: HV / current / power** means they don't; it is an indicator only (§7). After HV off, the check starts 7 seconds later, once the voltage has discharged.

---

## 7. Messages and warnings

These appear in the log. None of them switches the high voltage off by itself.

| Message | Meaning | What to do |
|---|---|---|
| *current reduced to stay under the power limit: A → B µA* (and similar *raised to the minimum*, *lowered to the maximum*) | Your request was adjusted (§5.2). | Nothing, unless the adjusted value doesn't suit you. |
| *setpoints … will apply when HV is switched on* | Stored while HV is off. | — |
| *MONX dropped N times in the last 60 s with HV on* | Brief drops of the tube-ready signal. | Nothing; expected near 200 µA. |
| *the tube has not reported ready (MONX) for 1 s with HV on* | The ready signal has stayed off. | Watch the monitors. If they no longer match the setpoints, switch HV off. *the tube reports ready (MONX) again* follows when it recovers. |
| *HV did not reach … within 7.5 s; continuing* (or *current did not reach …*) | A setting took longer than expected to be reached. | Check the monitors. If they stay wrong, switch HV off and report it. |
| **OUT OF RANGE** indicator | Readings don't match the setpoints. | Same as above. |
| *… reconfiguring the temperature sensor* | The temperature sensor lost its settings, e.g. after the controller lost power. | Nothing, unless it repeats. |
| *ADC null bit set …* / *ADC trailing bits do not repeat …* | One garbled reading was discarded. | Nothing, unless it repeats; three in a row cause a fault. |
| *the interlock opened after HV on was requested; request again* | The request was refused because the interlock opened while you were confirming. | Press HV ON again. |
| *cannot switch HV on while …* / *the interlock is open or was just restored* | The request was refused in the current state. | Wait for the state or lamp to allow it. |
| *… request cancelled by a later HV off or emergency stop* | A queued HV ON or Update was dropped because you pressed HV OFF or EMERGENCY STOP. | Intended. |
| *emergency stop: HV off* | EMERGENCY STOP worked: the enables are off and both setpoints are at zero. | Press HV ON again when ready. |

---

## 8. Faults

A **fault** switches the high voltage off at once, shows a red banner, and waits. To continue:

1. **Read the banner** and deal with the cause.
2. **Press Clear fault.** If the enable signals still read on, the fault stays, and you must check the tube physically.
3. **Wait for the lamps**, which show amber for about 3 seconds, then continue as normal.

| Banner text contains | Meaning |
|---|---|
| *the tube did not report ready (MONX) within 1 s* | After ramping up, the controller never reported the tube ready. |
| *HV enable did not read back* | The enable signals did not switch on as commanded. |
| *HV enable bits still set after disable … check the tube physically* | The enable signals did not switch **off**. **Treat the tube as on** (§1). |
| *only one HV enable bit reads set* / *HV enable bits read set while HV is off* / *HV enable bits dropped while HV is on* | The enable signals changed without being commanded. |
| *3 ADC framing errors in a row* | The voltage and current readings could not be trusted. |
| *connection failed* / *controller lost* | USB communication failed. After an unplug the program may take about 10 s to release the device, during which the "no update from the controller" banner (below) can appear. The program closes the controller, and **Clear fault** returns to *disconnected*; then reconnect. If the message also says *could not be confirmed clear: check the tube physically*, **treat the tube as on**. |
| *internal error* | A software problem. HV was switched off. Please report it with the log file (§9). |

**Program-level banners** (not faults):

- **"No update from the controller for over 3 s …"** The program's worker has stopped responding. HV ON and Update are unavailable. Use EMERGENCY STOP if the tube may be on, and check the tube. If it doesn't clear within a few seconds, close the program and check the tube.
- **"The controller has stopped …"** The worker has ended. Check the tube, then restart the program.

---

## 9. Records

Everything is recorded in `~/minix_logs/` (or the directory set in the configuration file):

- **`minix.log`** is the program log: every message, plus technical detail. Include it when reporting a problem.
- **`<date>-<time>_<serial>.csv`** is one file per connection, with one row per second plus a row at every state change. Its columns include:
  - time, state, lamps, setpoints;
  - measured and averaged voltage and current;
  - power (single reading and averaged), power band, in-range flag;
  - temperature, the count of tube-ready drops, and any fault message.
- **`<date>-<time>_<serial>_events.csv`** has every log message of that connection.

The CSV files begin with comment lines (`#`) giving the serial number, the power rating and its source, and the software version. In Python, read one with `pandas.read_csv(path, comment="#")`. The status bar shows the current file, or a message if recording has failed (the controller keeps working either way).

---

## 10. Practising in simulation mode

`minix-gui --sim` adds a **Simulator** panel with switches that apply at once, and also to the next connection:

| Switch | Simulates |
|---|---|
| Interlock closed | Untick to open the interlock. |
| Tube never ready | The ready signal never comes on: switching on ends in a fault. |
| Supply stuck at 0 | The voltage and current don't follow the setpoints. |
| Enable readback stuck on | The enable signals appear stuck on: switching off ends in a fault. |
| USB failure | The controller stops answering. |
| MONX flicker near 200 µA | The brief ready-signal drops seen on the real unit (on by default). |

The simulated readings, ramp times and discharge follow measurements from the real unit. Temperature behaviour is approximate.

---

## 11. Troubleshooting

| Problem | Try |
|---|---|
| *no controller found* | Check the USB cable and the controller's power, then press **Refresh**. Close any other program using the controller, such as the Amptek software. |
| Connect ends in a *connection failed* fault | Clear the fault and connect again. If it keeps failing, unplug and replug the USB cable. |
| USB was unplugged with X-rays on | The controller switches the high voltage off by itself, and a radiation monitor confirms it. The program reports a lost controller and says to check the tube, because it can no longer confirm anything. Plug the cable back in, press **Clear fault**, then **Connect**; no restart is needed. |
| Asked for the power rating every time | Normal in simulation mode (§5.1). On the real unit, check that `~/.config/minix/units.toml` exists and is writable. |
| The program won't start: *Configuration error* | The message names the problem in the configuration file. `config/units.example.toml` in the project shows the valid settings. |
| Temperature shows — | It appears about 2 s after connecting. If it stays blank, see the log. |

---

## 12. Configuration file

`~/.config/minix/units.toml` is read at start-up (`config/units.toml` in the working directory is used if the first doesn't exist). `minix-gui --config PATH` selects another file. The project's `config/units.example.toml` documents every setting.

- **`[units."<serial>"]`** holds each unit's power rating and its source. The program writes this entry the first time you connect (§5.1).
- **`[safety]`** holds the timings and the safety margin. Change these only with a reason.
- **`[polling]`** sets how often the controller is read.
- **`[logging]`** sets where the records go.
