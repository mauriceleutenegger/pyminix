import pytest

from minix import protocol as p


@pytest.mark.parametrize("volts, counts", [
    (15.0 / p.HV_FACTOR_50KV, 1200),   # the §10 setpoint: 15 kV
    (10.0 / p.CURRENT_FACTOR, 200),    # and 10 uA
    (10.1 / p.HV_FACTOR_50KV, 808),    # 807.9999999999999 before the epsilon
    (0.0, 0),
    (p.VREF * p.COUNTS_MAX / p.DAC_ADC_SCALE, p.COUNTS_MAX),
])
def test_volts_to_counts(volts, counts):
    assert p.volts_to_counts(volts) == counts


def test_volts_to_counts_truncates():
    assert p.volts_to_counts(p.counts_to_volts(100) + 0.0009) == 100


def test_counts_round_trip():
    for counts in range(p.COUNTS_MAX + 1):
        assert p.volts_to_counts(p.counts_to_volts(counts)) == counts


@pytest.mark.parametrize("volts", [-0.0001, -1.0, p.VREF, 5.0])
def test_volts_to_counts_rejects_out_of_range(volts):
    with pytest.raises(ValueError):
        p.volts_to_counts(volts)


def test_initial_adbus_state():
    # 0xFB with both HV enables cleared (§5 step 6); chip selects deasserted.
    assert p.ADBUS_INIT == 0x9B
    assert not p.ADBUS_INIT & p.HV_EN_BOTH
    assert p.ADBUS_INIT & p.ADCS and p.ADBUS_INIT & p.DACS
