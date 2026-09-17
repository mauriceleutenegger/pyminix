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

The unit's power rating (4 W or 10 W) **cannot be read from the device** (§2.1).
It must come from the unit's label or documentation. On first connection to
an unknown unit the GUI asks for it, defaulting to the safe 4 W, and stores it
in the configuration file with its source; the controller refuses to open a
unit without a rating.
