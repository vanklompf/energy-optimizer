"""Tests for the operator-run read-only Sigen Modbus diagnostic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "tools" / "sigen_raw_diagnostic.py"


def _load_module():
    assert SCRIPT.is_file(), "read-only diagnostic script has not been restored"
    spec = importlib.util.spec_from_file_location("sigen_raw_diagnostic", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Socket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, count: int) -> bytes:
        chunk, self.response = self.response[:count], self.response[count:]
        return chunk


def test_read_holding_uses_function_03_and_decodes_u32(monkeypatch: pytest.MonkeyPatch) -> None:
    """The diagnostic must issue exactly one read-only function-03 request."""
    module = _load_module()
    # Transaction=9, protocol=0, length=7, unit=247, function=03, byte-count=4,
    # followed by U32 8800 as two big-endian words.
    socket = _Socket(bytes.fromhex("000900000007f7030400002260"))
    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kw: socket)

    assert module.read_holding("192.0.2.10", 502, 247, 40032, 2) == [0, 8800]
    assert socket.sent == [bytes.fromhex("000900000006f7039c600002")]


def test_decode_report_has_only_the_six_expected_registers() -> None:
    """The supported diagnostic surface is intentionally narrow and read-only."""
    module = _load_module()
    assert [(r.name, r.address, r.count, r.scale) for r in module.REGISTERS] == [
        ("remote_ems_enable", 40029, 1, 1),
        ("remote_ems_mode", 40031, 1, 1),
        ("max_charge_kw", 40032, 2, 1000),
        ("max_discharge_kw", 40034, 2, 1000),
        ("charge_cutoff_pct", 40047, 1, 10),
        ("discharge_cutoff_pct", 40048, 1, 10),
    ]
    report = module.decode_registers(
        {
            40029: [0],
            40031: [1],
            40032: [0, 8800],
            40034: [0, 9600],
            40047: [1000],
            40048: [0],
        }
    )
    assert report == {
        "remote_ems_enable": {"address": 40029, "raw": 0, "value": 0.0, "words": [0]},
        "remote_ems_mode": {"address": 40031, "raw": 1, "value": 1.0, "words": [1]},
        "max_charge_kw": {"address": 40032, "raw": 8800, "value": 8.8, "words": [0, 8800]},
        "max_discharge_kw": {"address": 40034, "raw": 9600, "value": 9.6, "words": [0, 9600]},
        "charge_cutoff_pct": {"address": 40047, "raw": 1000, "value": 100.0, "words": [1000]},
        "discharge_cutoff_pct": {"address": 40048, "raw": 0, "value": 0.0, "words": [0]},
    }
