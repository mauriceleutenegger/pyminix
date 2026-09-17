"""Unit and application configuration.

UnitConfig describes one controller: its model, voltage range and power
rating.

The controller reports a model on two strapping pins (§2.2), and for the
OEM models that gives the rating. resolve_unit() prefers it, uses the
configured rating as a cross-check, and takes the **lower** of the two if
they disagree. A non-OEM controller (type 3) does not distinguish 4 W from
10 W, so there the rating must be configured (§6.1.1); there is no
default.

Settings is the configuration file (see config/units.example.toml): one
entry per serial number, plus safety timings, polling rates and logging.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import tomli_w

from . import protocol as p

VALID_RATINGS_W = (4.0, 10.0)


@dataclass(frozen=True)
class DeviceModel:
    """A controller model, as reported by the strapping pins (§2.2)."""

    code: int
    controller: str
    tube: str
    watt_max_w: float | None        # None: the pins do not give the rating
    hv_factor: float | None = None  # None: derive from the serial number
    hv_min_kv: float | None = None
    hv_max_kv: float | None = None
    current_min_ua: float = 5.0
    current_max_ua: float = 200.0


# From the Mini-X API Programming Guide (2015) and its examples; the
# decoding was recovered from the vendor DLL. See docs/protocol.md §2.2.
DEVICE_MODELS = {
    0: DeviceModel(0, "MX70", "Mini-X-OEM 70kV 10W", 10.0, 20.0, 35.0, 70.0, 10.0, 143.0),
    1: DeviceModel(1, "MX50", "Mini-X-OEM 50kV 4W", 4.0, 12.5, 10.0, 50.0),
    2: DeviceModel(2, "MX50.10", "Mini-X-OEM 50kV 10W", 10.0, 12.5, 10.0, 50.0),
    3: DeviceModel(3, "Mini-X", "Mini-X 40kV/50kV (non-OEM)", None),
}
NSI_SERIAL_MIN = 10000          # serial > 9999 (§2.1)
HV50_SERIAL_MIN = 1118880

HV_MIN_KV = 10.0
HV_MAX_KV_40 = 40.0
HV_MAX_KV_50 = 50.0
CURRENT_MIN_UA = 5.0
CURRENT_MAX_UA = 200.0
SAFETY_MARGIN_W = 0.050


class ConfigError(ValueError):
    """The configuration is missing, malformed, or unsafe."""


class UnitNotConfigured(ConfigError):
    """No entry for this serial number, so its power rating is unknown."""

    def __init__(self, serial: str):
        super().__init__(f"Mini-X {serial} has no configured power rating")
        self.serial = serial


def serial_number_value(serial: str) -> int:
    """The number the vendor derives from the serial: leading digits of the first 8 characters."""
    digits = ""
    for ch in serial[:8]:
        if not ch.isdigit():
            break
        digits += ch
    if not digits:
        raise ConfigError(f"serial {serial!r} does not start with digits")
    return int(digits)


@dataclass(frozen=True)
class UnitConfig:
    serial: str
    watt_max_w: float
    safety_margin_w: float = SAFETY_MARGIN_W
    source: str = ""                    # where the rating came from
    device_type: int | None = None      # from the strapping pins, if read

    def __post_init__(self):
        if self.watt_max_w not in VALID_RATINGS_W:
            raise ConfigError(
                f"Mini-X {self.serial}: watt_max_w={self.watt_max_w} must be 4.0 or 10.0, "
                "from the unit's hardware documentation; it cannot be read from the device")
        if not 0.0 <= self.safety_margin_w < self.watt_max_w / 4:
            raise ConfigError(f"Mini-X {self.serial}: safety_margin_w={self.safety_margin_w} "
                              "is out of range")
        serial_number_value(self.serial)
        if self.hv_max_kv * self.current_min_ua >= self.safe_mw:
            raise ConfigError(f"Mini-X {self.serial}: the minimum current at maximum voltage "
                              "exceeds the power limit")

    @property
    def serial_value(self) -> int:
        return serial_number_value(self.serial)

    @property
    def is_nsi(self) -> bool:
        return self.serial_value >= NSI_SERIAL_MIN

    @property
    def is_50kv(self) -> bool:
        return self.is_nsi and self.serial_value >= HV50_SERIAL_MIN

    @property
    def model(self) -> DeviceModel | None:
        return DEVICE_MODELS.get(self.device_type)

    @property
    def hv_factor(self) -> float:
        """kV per DAC/ADC volt."""
        model = self.model
        if model is not None and model.hv_factor is not None:
            return model.hv_factor
        return p.HV_FACTOR_50KV if self.is_50kv else p.HV_FACTOR_40KV

    @property
    def current_factor(self) -> float:
        """uA per DAC/ADC volt."""
        return p.CURRENT_FACTOR

    @property
    def hv_min_kv(self) -> float:
        model = self.model
        return HV_MIN_KV if model is None or model.hv_min_kv is None else model.hv_min_kv

    @property
    def hv_max_kv(self) -> float:
        model = self.model
        if model is not None and model.hv_max_kv is not None:
            return model.hv_max_kv
        return HV_MAX_KV_50 if self.is_50kv else HV_MAX_KV_40

    @property
    def current_min_ua(self) -> float:
        return CURRENT_MIN_UA if self.model is None else self.model.current_min_ua

    @property
    def current_max_ua(self) -> float:
        return CURRENT_MAX_UA if self.model is None else self.model.current_max_ua

    @property
    def safe_mw(self) -> float:
        """Highest committable power (SafeWattageMW, §9.1). Derived on every access."""
        return (self.watt_max_w - self.safety_margin_w) * 1000.0

    @property
    def rated_mw(self) -> float:
        return self.watt_max_w * 1000.0

    def describe(self) -> str:
        model = self.model
        if model is not None and model.watt_max_w is not None:
            return (f"Mini-X {self.serial}: {model.controller}, {self.hv_max_kv:g} kV, "
                    f"{self.watt_max_w:g} W rating")
        board = f"{self.hv_max_kv:g} kV"
        family = "NSI" if self.is_nsi else "non-NSI"
        return f"Mini-X {self.serial}: {board}, {family}, {self.watt_max_w:g} W rating"


@dataclass(frozen=True)
class SafetyConfig:
    interlock_clear_s: float = 3.0      # §7.3: 3 cycles of the vendor's 1 s loop
    monx_timeout_s: float = 1.0         # §7.2: MONX must assert this soon after the ramp
    monx_warning_s: float = 1.0         # §7.2: warn when MONX stays low this long with HV on
    range_tolerance: float = 0.10       # §6.4
    hv_off_test_delay_s: float = 7.0    # §6.4: ErrTestDelay, 7 cycles of 1 s


@dataclass(frozen=True)
class PollingConfig:
    gpio_hz: float = 10.0
    adc_hz: float = 2.0
    temp_hz: float = 0.2
    display_hz: float = 1.0


DEFAULT_LOG_DIRECTORY = Path.home() / "minix_logs"


@dataclass(frozen=True)
class LoggingConfig:
    directory: Path = DEFAULT_LOG_DIRECTORY
    sample_hz: float = 1.0          # run-record rows per second


@dataclass(frozen=True)
class UnitEntry:
    watt_max_w: float
    source: str = ""


@dataclass(frozen=True)
class Settings:
    units: dict[str, UnitEntry] = field(default_factory=dict)
    safety_margin_w: float = SAFETY_MARGIN_W
    safety: SafetyConfig = SafetyConfig()
    polling: PollingConfig = PollingConfig()
    logging: LoggingConfig = LoggingConfig()
    path: Path | None = None

    def unit(self, serial: str, device_type: int | None = None) -> UnitConfig:
        """The unit's configuration, ignoring any warnings from resolve_unit()."""
        return self.resolve(serial, device_type)[0]

    def resolve(self, serial: str, device_type: int | None = None) -> tuple[UnitConfig,
                                                                           list[str]]:
        return resolve_unit(serial, device_type, self.units.get(serial), self.safety_margin_w)


def resolve_unit(serial: str, device_type: int | None, entry: UnitEntry | None,
                 safety_margin_w: float = SAFETY_MARGIN_W) -> tuple[UnitConfig, list[str]]:
    """Work out a unit's configuration from the controller and the settings.

    The strapping pins win where they give a rating; a configured rating is
    a cross-check, and the lower of the two is used if they disagree.
    Raises UnitNotConfigured when neither gives a rating.
    """
    model = DEVICE_MODELS.get(device_type)
    from_pins = model.watt_max_w if model is not None else None
    configured = entry.watt_max_w if entry is not None else None
    warnings: list[str] = []
    if from_pins is not None and configured is not None and from_pins != configured:
        watt = min(from_pins, configured)
        warnings.append(
            f"the controller reports {model.controller} ({from_pins:g} W) but the "
            f"configuration says {configured:g} W; using the lower, {watt:g} W. "
            "Check which is right before running near the limit.")
        source = (f"lower of controller ({from_pins:g} W) and configuration "
                  f"({configured:g} W)")
    elif from_pins is not None:
        watt = from_pins
        source = f"controller reports {model.controller}"
        if configured is not None:
            source += "; configuration agrees"
    elif configured is not None:
        watt = configured
        source = entry.source or "configuration"
    else:
        raise UnitNotConfigured(serial)
    return UnitConfig(serial=serial, watt_max_w=watt, safety_margin_w=safety_margin_w,
                      source=source, device_type=device_type), warnings


def default_paths() -> list[Path]:
    """Where the configuration is looked for, in order."""
    return [Path.home() / ".config" / "minix" / "units.toml",
            Path.cwd() / "config" / "units.toml"]


def find_settings_file() -> Path | None:
    return next((path for path in default_paths() if path.is_file()), None)


def load_settings(path: Path | None = None) -> Settings:
    """Load the given file, else the first default path that exists, else defaults."""
    path = path or find_settings_file()
    if path is None:
        return Settings()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        units = {}
        for serial, entry in data.get("units", {}).items():
            unknown = set(entry) - {"watt_max_w", "source"}
            if unknown:
                raise ConfigError(f"{path}: unit {serial}: unknown keys {sorted(unknown)}")
            if "watt_max_w" not in entry:
                raise ConfigError(f"{path}: unit {serial} has no watt_max_w")
            units[str(serial)] = UnitEntry(float(entry["watt_max_w"]), str(entry.get("source", "")))
        safety = dict(data.get("safety", {}))
        margin = float(safety.pop("safety_margin_w", SAFETY_MARGIN_W))
        settings = Settings(
            units=units,
            safety_margin_w=margin,
            safety=SafetyConfig(**{k: float(v) for k, v in safety.items()}),
            polling=PollingConfig(**{k: float(v) for k, v in data.get("polling", {}).items()}),
            logging=_logging_config(data.get("logging", {}), path),
            path=path,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"{path}: {exc}") from exc
    for serial in units:
        settings.unit(serial)       # validate every entry now, not at connect time
    for name, value in [*vars(settings.polling).items(),
                        ("sample_hz", settings.logging.sample_hz)]:
        if not (math.isfinite(value) and value > 0):
            raise ConfigError(f"{path}: {name} must be positive")
    return settings


def _logging_config(section: dict, config_path: Path) -> LoggingConfig:
    unknown = set(section) - {"directory", "sample_hz"}
    if unknown:
        raise ConfigError(f"{config_path}: logging: unknown keys {sorted(unknown)}")
    directory = LoggingConfig.directory
    if "directory" in section:
        directory = Path(str(section["directory"])).expanduser()
        if not directory.is_absolute():
            directory = config_path.parent / directory
    return LoggingConfig(directory=directory,
                         sample_hz=float(section.get("sample_hz", LoggingConfig.sample_hz)))


def save_unit(path: Path, serial: str, watt_max_w: float, source: str) -> None:
    """Add or replace one unit's entry, keeping the rest of the file.

    Comments in the file are not preserved.
    """
    UnitConfig(serial=serial, watt_max_w=watt_max_w, source=source)   # validate
    data = {}
    if path.is_file():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    data.setdefault("units", {})[serial] = {
        "watt_max_w": watt_max_w,
        "source": source or f"entered by operator {date.today().isoformat()}",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
