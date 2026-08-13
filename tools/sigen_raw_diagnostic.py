#!/usr/bin/env python3
"""Read-only Sigen Modbus/TCP diagnostic for the PvOpti commissioning gates.

This tool sends Modbus function 0x03 requests only.  It intentionally has no
write implementation and it never enables Remote EMS.  The narrow register list
is the recorded safe-baseline surface: 40029, 40031, 40032, 40034, 40047, 40048.

Optionally pass --ha-url and provide HA_TOKEN in the environment to record the
state/absence of the six HA control entities alongside the independent raw
Modbus results.  HTTP calls are GET-only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Register:
    """One explicitly approved holding-register diagnostic read."""

    name: str
    address: int
    count: int
    scale: int


REGISTERS = (
    Register("remote_ems_enable", 40029, 1, 1),
    Register("remote_ems_mode", 40031, 1, 1),
    Register("max_charge_kw", 40032, 2, 1000),
    Register("max_discharge_kw", 40034, 2, 1000),
    Register("charge_cutoff_pct", 40047, 1, 10),
    Register("discharge_cutoff_pct", 40048, 1, 10),
)

HA_CONTROL_ENTITIES = (
    "switch.sigen_plant_remote_ems_controlled_by_home_assistant",
    "select.sigen_plant_remote_ems_control_mode",
    "number.sigen_plant_ess_max_charging_limit",
    "number.sigen_plant_ess_max_discharging_limit",
    "number.sigen_plant_ess_charge_cut_off_state_of_charge",
    "number.sigen_plant_ess_discharge_cut_off_state_of_charge",
)


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise RuntimeError("Modbus peer closed the connection before the complete response")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_holding(host: str, port: int, unit: int, address: int, count: int) -> list[int]:
    """Perform one Modbus/TCP function-03 holding-register read, and nothing else."""
    transaction_id = 9
    request = struct.pack(">HHHBBHH", transaction_id, 0, 6, unit, 0x03, address, count)
    with socket.create_connection((host, port), timeout=8) as sock:
        sock.sendall(request)
        header = _recv_exact(sock, 7)
        returned_tid, protocol_id, length, returned_unit = struct.unpack(">HHHB", header)
        pdu = _recv_exact(sock, length - 1)

    if (returned_tid, protocol_id, returned_unit) != (transaction_id, 0, unit):
        raise RuntimeError("unexpected Modbus/TCP response header")
    if not pdu:
        raise RuntimeError("empty Modbus PDU")
    if pdu[0] & 0x80:
        code = pdu[1] if len(pdu) > 1 else None
        raise RuntimeError(f"Modbus exception response {code} for register {address}")
    if len(pdu) < 2 or pdu[0] != 0x03 or pdu[1] != count * 2:
        raise RuntimeError(f"unexpected function-03 payload for register {address}")
    return list(struct.unpack(">" + "H" * count, pdu[2:]))


def decode_registers(raw: dict[int, list[int]]) -> dict[str, dict[str, Any]]:
    """Decode approved raw register words without inferring actuator state."""
    result: dict[str, dict[str, Any]] = {}
    for register in REGISTERS:
        words = raw[register.address]
        integer = words[0] if register.count == 1 else (words[0] << 16) | words[1]
        result[register.name] = {
            "address": register.address,
            "raw": integer,
            "value": integer / register.scale,
            "words": words,
        }
    return result


def read_ha_entities(ha_url: str, token: str) -> dict[str, dict[str, Any]]:
    """GET HA state only; missing entities are expected when controls are disabled."""
    base = ha_url.rstrip("/") + "/api/states/"
    headers = {"Authorization": f"Bearer {token}"}
    results: dict[str, dict[str, Any]] = {}
    for entity_id in HA_CONTROL_ENTITIES:
        request = urllib.request.Request(base + entity_id, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.load(response)
            results[entity_id] = {
                "state": payload["state"],
                "last_updated": payload["last_updated"],
            }
        except urllib.error.HTTPError as error:
            if error.code == 404:
                results[entity_id] = {"state": "absent_or_disabled", "http_status": 404}
            else:
                results[entity_id] = {"state": "http_error", "http_status": error.code}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Sigen Modbus/TCP host")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=247)
    parser.add_argument("--ha-url", help="optional HA base URL; requires HA_TOKEN")
    args = parser.parse_args()

    raw = {
        register.address: read_holding(
            args.host, args.port, args.unit, register.address, register.count
        )
        for register in REGISTERS
    }
    report: dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "modbus": {"host": args.host, "port": args.port, "unit": args.unit, "function": "0x03"},
        "raw_function_03": decode_registers(raw),
    }
    if args.ha_url:
        token = os.environ.get("HA_TOKEN")
        if not token:
            parser.error("HA_TOKEN must be set when --ha-url is used")
        report["ha_control_entities"] = read_ha_entities(args.ha_url, token)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
