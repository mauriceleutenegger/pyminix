# minix

PySide6 control GUI for the Amptek Mini-X X-ray tube controller (FT2232C/D, MPSSE).

The protocol is documented in [docs/protocol.md](docs/protocol.md). Section
references in the source (e.g. "§9.2") point there.

## Layout

| Path | Contents |
|---|---|
| `src/minix/` | The package. `transport.py`/`device.py` know MPSSE but not safety; `policy/` is pure functions with no I/O; `controller.py` owns the device thread; `ui/` knows nothing about the protocol. |
| `tools/` | Standalone hardware scripts. `probe.py` is read-only; `adcsweep.py --enable-hv` **energizes the tube**. |
| `tests/` | pytest suite. Runs against the simulator; never touches hardware. |
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

## Power rating

The unit's power rating (4 W or 10 W) **cannot be read from the device** (§2.1).
It must be entered in the unit config from the hardware documentation. The
software refuses to open a unit whose rating is not configured.
