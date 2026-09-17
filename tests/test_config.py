from pathlib import Path

import pytest

from minix import protocol as p
from minix.config import (
    ConfigError, PollingConfig, SafetyConfig, UnitConfig, UnitNotConfigured,
    load_settings, save_unit, serial_number_value,
)

EXAMPLE = Path(__file__).parent.parent / "config" / "units.example.toml"


@pytest.mark.parametrize("serial, value", [
    ("01300036", 1300036), ("00001234", 1234), ("0130003699", 1300036), ("123A4567", 123),
])
def test_serial_number_value(serial, value):
    assert serial_number_value(serial) == value


@pytest.mark.parametrize("serial", ["", "A1234567", "FT123456"])
def test_serial_number_without_digits(serial):
    with pytest.raises(ConfigError):
        serial_number_value(serial)


@pytest.mark.parametrize("serial, nsi, is50, factor, hv_max", [
    ("01300036", True, True, 12.5, 50.0),       # this project's unit
    ("01118880", True, True, 12.5, 50.0),       # 50 kV threshold
    ("01118879", True, False, 10.0, 40.0),
    ("00010000", True, False, 10.0, 40.0),      # NSI threshold (> 9999)
    ("00009999", False, False, 10.0, 40.0),     # non-NSI keeps the 40 kV table
    ("02000000", True, True, 12.5, 50.0),
])
def test_tables_from_serial(serial, nsi, is50, factor, hv_max):
    unit = UnitConfig(serial, 4.0)
    assert (unit.is_nsi, unit.is_50kv, unit.hv_factor, unit.hv_max_kv) == (nsi, is50, factor, hv_max)
    assert (unit.hv_min_kv, unit.current_min_ua, unit.current_max_ua) == (10.0, 5.0, 200.0)
    assert unit.current_factor == p.CURRENT_FACTOR


@pytest.mark.parametrize("watts, safe", [(4.0, 3950.0), (10.0, 9950.0)])
def test_power_limits(watts, safe):
    unit = UnitConfig("01300036", watts)
    assert unit.safe_mw == pytest.approx(safe)
    assert unit.rated_mw == watts * 1000


@pytest.mark.parametrize("watts", [0.0, 3.99, 5.0, 10.5, 100.0, float("nan")])
def test_rating_must_be_4_or_10(watts):
    with pytest.raises(ConfigError, match="4.0 or 10.0"):
        UnitConfig("01300036", watts)


@pytest.mark.parametrize("margin", [-0.01, 1.0, 5.0])
def test_safety_margin_is_bounded(margin):
    with pytest.raises(ConfigError, match="safety_margin_w"):
        UnitConfig("01300036", 4.0, safety_margin_w=margin)


def test_example_config_loads():
    settings = load_settings(EXAMPLE)
    unit = settings.unit("01300036")
    assert unit.watt_max_w == 10.0
    assert unit.safety_margin_w == 0.05
    assert "hardware documentation" in unit.source
    assert settings.safety == SafetyConfig()
    assert settings.polling == PollingConfig()
    assert settings.path == EXAMPLE


def test_unknown_unit_has_no_default_rating():
    settings = load_settings(EXAMPLE)
    with pytest.raises(UnitNotConfigured) as info:
        settings.unit("01234567")
    assert info.value.serial == "01234567"


def test_no_file_means_defaults_and_no_units(monkeypatch, tmp_path):
    monkeypatch.setattr("minix.config.default_paths", lambda: [tmp_path / "missing.toml"])
    settings = load_settings()
    assert settings.units == {} and settings.path is None


@pytest.mark.parametrize("text, match", [
    ('[units."01300036"]\nwatt_max_w = 5.0\n', "4.0 or 10.0"),
    ('[units."01300036"]\nsource = "x"\n', "no watt_max_w"),
    ('[units."01300036"]\nwatt_max_w = 4.0\nhv_max_kv = 60\n', "unknown keys"),
    ('[safety]\nrange_tolerence = 0.1\n', "range_tolerence"),
    ('[polling]\nadc_hz = 0\n', "positive"),
    ('[units\n', "cannot read"),
    ('[safety]\nsafety_margin_w = "a lot"\n', "could not convert"),
])
def test_bad_files_are_rejected(tmp_path, text, match):
    path = tmp_path / "units.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_settings(path)


def test_save_unit_round_trip(tmp_path):
    path = tmp_path / "sub" / "units.toml"
    save_unit(path, "01300036", 10.0, "label on the unit")
    save_unit(path, "01300037", 4.0, "")
    settings = load_settings(path)
    assert settings.unit("01300036").watt_max_w == 10.0
    assert settings.unit("01300037").source.startswith("entered by operator")


def test_save_unit_keeps_other_sections(tmp_path):
    path = tmp_path / "units.toml"
    path.write_text(EXAMPLE.read_text())
    save_unit(path, "01300037", 4.0, "test")
    settings = load_settings(path)
    assert set(settings.units) == {"01300036", "01300037"}
    assert settings.safety == SafetyConfig()


def test_save_unit_validates(tmp_path):
    with pytest.raises(ConfigError):
        save_unit(tmp_path / "units.toml", "01300036", 7.0, "")
    assert not (tmp_path / "units.toml").exists()
