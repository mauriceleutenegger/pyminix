# minix

PySide6 control GUI for the Amptek Mini-X X-ray tube controller (FT2232C/D, MPSSE).

The protocol is documented in [docs/protocol.md](docs/protocol.md) (PDF build:
`docs/protocol.pdf`); the dated log of hardware runs behind it is
[docs/hardware-notes.md](docs/hardware-notes.md). Section references in the
source (e.g. "§9.2") point to protocol.md.

## Layout

| Path | Contents |
|---|---|
| `src/minix/` | The package. `transport.py`/`device.py` know MPSSE but not safety; `policy/` is pure functions with no I/O; `controller.py` owns the device thread; `ui/` knows nothing about the protocol. |
| `tools/` | Standalone hardware scripts. `device_check.py`, `ds1722_probe.py` and `probe.py` never enable HV. `adcsweep.py` is historical; its HV-on mode is superseded by `minix-gui` and needs an explicit extra flag. |
| `tests/` | pytest suite. Uses a fake transport and the simulator (`minix.sim`); never touches hardware. |
| `config/` | `units.example.toml`. Copy to `units.toml` (git-ignored) or `~/.config/minix/units.toml`. |
| `legacy/` | Earlier prototypes, git-ignored and kept only on disk. **Not trustworthy — do not import or run.** |

## Setup

    mamba env create -f environment.yml
    mamba activate minix
    pip install --no-deps --no-build-isolation -e .
    pytest

All dependencies, including libusb, come from conda-forge via
`environment.yml`. The pip step only installs this package in editable mode;
`--no-deps` keeps pip from resolving anything. Keep the dependency lists in
`environment.yml` and `pyproject.toml` in sync.

## Running

Operators: see the **[operator guide](docs/operator-guide.md)** (PDF:
`docs/operator-guide.pdf`) for safety notes, normal operation, and what
every indicator, warning and fault means.

    minix-gui --sim           # simulated controller, with a fault-injection panel
    minix-gui                 # real hardware
    minix-gui --config PATH   # a specific units.toml

`python -m minix` is equivalent. The configuration is read from
`~/.config/minix/units.toml`, then `config/units.toml`; see
`config/units.example.toml`. Run records and `minix.log` go to
`~/minix_logs/` unless `[logging] directory` says otherwise. In simulation
mode a rating entered in the dialog is kept in memory only.

## Continuous integration

GitHub Actions runs the test suite on every push and pull request
(`.github/workflows/tests.yml`), on Linux with Qt drawing offscreen.
Results are on the repository's **Actions** tab and as a tick or cross
beside each commit.

## Power rating

An OEM controller (MX50, MX50.10, MX70) states its model, and with it the
power rating and voltage range, on two strapping pins, which this software
reads at connect (docs/protocol.md §2.2). A non-OEM Mini-X controller does
not: there the rating must be configured, and on first connection the GUI
asks for it, defaulting to the safe 4 W and recording where the answer came
from. A configured rating is also a cross-check: if it disagrees with the
controller, the **lower** of the two is used and a warning is logged.

> **Caveat.** The pin decoding was inferred by disassembling Amptek's
> `MiniX.dll`, and the model-to-rating table comes from their 2015 API
> manual and examples. It has been checked on exactly one controller
> (sn `01300036`, which reads MX50.10: 50 kV, 10 W, matching its
> documentation). Confirm it with Amptek before relying on it for another
> unit, and keep a configured rating as a cross-check.
