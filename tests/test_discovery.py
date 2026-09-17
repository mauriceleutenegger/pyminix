"""Enumeration and opening, with pyftdi stubbed out.

A controller that is unplugged and plugged back in may appear at another
USB address. pyftdi caches the old one, and opening it then fails with
"no such device" (as seen on 2026-09-17), so the cache must be cleared
before enumerating or opening.
"""

import pytest

from minix import discovery, protocol as p, transport
from minix.discovery import ControllerInfo, list_controllers
from minix.transport import FtdiTransport, TransportError


class FakeDescriptor:
    def __init__(self, serial, vid=p.USB_VID, pid=p.USB_PID):
        self.sn = serial
        self.vid = vid
        self.pid = pid
        self.description = "MiniX Controller"
        self.bus = 2
        self.address = 3


@pytest.fixture
def calls(monkeypatch):
    order = []
    monkeypatch.setattr(discovery.UsbTools, "flush_cache",
                        classmethod(lambda cls: order.append("flush")))
    monkeypatch.setattr(transport.UsbTools, "flush_cache",
                        classmethod(lambda cls: order.append("flush")))
    return order


def test_listing_flushes_the_cache_first(monkeypatch, calls):
    def list_devices():
        calls.append("list")
        return [(FakeDescriptor("01300036"), 2), (FakeDescriptor("00000001", pid=0x6001), 1)]

    monkeypatch.setattr(discovery.Ftdi, "list_devices", staticmethod(list_devices))
    found = list_controllers()
    assert calls == ["flush", "list"]
    assert found == [ControllerInfo("01300036", "MiniX Controller", 2, 3)]   # other PIDs ignored


def test_enumeration_failure(monkeypatch, calls):
    monkeypatch.setattr(discovery.Ftdi, "list_devices",
                        staticmethod(lambda: (_ for _ in ()).throw(OSError("usb is unhappy"))))
    with pytest.raises(OSError, match="USB enumeration failed"):
        list_controllers()


class FakeFtdi:
    """Enough of pyftdi's Ftdi for FtdiTransport.open()."""

    class BitMode:
        MPSSE = 0x02

    opened = []
    fail = None
    version = p.FT2232CD_VERSION

    def __init__(self):
        self.closed = False
        self.timeouts = None
        self.device_version = FakeFtdi.version

    def open(self, vendor, product, serial=None, interface=1):
        if self.fail:
            raise self.fail
        FakeFtdi.opened.append(serial)

    def set_latency_timer(self, ms):
        pass

    def purge_buffers(self):
        pass

    def set_bitmode(self, mask, mode):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_ftdi(monkeypatch):
    FakeFtdi.opened, FakeFtdi.fail = [], None
    FakeFtdi.version = p.FT2232CD_VERSION
    monkeypatch.setattr(transport, "Ftdi", FakeFtdi)
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)
    return FakeFtdi


def test_open_flushes_the_cache_first(fake_ftdi, calls):
    link = FtdiTransport.open("01300036")
    assert calls == ["flush"]
    assert fake_ftdi.opened == ["01300036"]
    assert link.serial == "01300036"


def test_open_reports_a_missing_device(fake_ftdi, calls):
    FakeFtdi.fail = OSError("No such device (it may have been disconnected)")
    with pytest.raises(TransportError, match="No such device"):
        FtdiTransport.open("01300036")


def test_open_checks_the_chip(fake_ftdi, monkeypatch, calls):
    monkeypatch.setattr(FakeFtdi, "version", 0x0700)      # an FT2232H, not our chip
    with pytest.raises(TransportError, match="expected 0x0500"):
        FtdiTransport.open("01300036")
