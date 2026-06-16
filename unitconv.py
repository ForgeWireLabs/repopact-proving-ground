"""unitconv — a deliberately small unit converter.

This program exists only to give the RepoPact proving ground something real to
govern: genuine behavior, genuine tests, and a place to introduce a genuine bug.
It is not meant to be useful.
"""

from __future__ import annotations

import argparse
import sys

# Temperature conversions go through Celsius as the pivot unit.
_TO_CELSIUS = {
    "C": lambda t: t,
    "F": lambda t: (t - 32.0) * 5.0 / 9.0,
    "K": lambda t: t - 273.15,
}
_FROM_CELSIUS = {
    "C": lambda c: c,
    "F": lambda c: c * 9.0 / 5.0 + 32.0,
    "K": lambda c: c + 273.15,
}

# Length conversions go through metres as the pivot unit.
_METRES_PER = {"m": 1.0, "km": 1000.0, "ft": 0.3048, "mi": 1609.344}


def convert_temperature(value: float, frm: str, to: str) -> float:
    if frm not in _TO_CELSIUS or to not in _FROM_CELSIUS:
        raise ValueError(f"unknown temperature unit: {frm!r} or {to!r}")
    return _FROM_CELSIUS[to](_TO_CELSIUS[frm](value))


def convert_length(value: float, frm: str, to: str) -> float:
    if frm not in _METRES_PER or to not in _METRES_PER:
        raise ValueError(f"unknown length unit: {frm!r} or {to!r}")
    return value * _METRES_PER[frm] / _METRES_PER[to]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unitconv", description="Convert between units.")
    parser.add_argument("kind", choices=["temp", "length"])
    parser.add_argument("value", type=float)
    parser.add_argument("frm")
    parser.add_argument("to")
    args = parser.parse_args(argv)

    try:
        if args.kind == "temp":
            result = convert_temperature(args.value, args.frm, args.to)
        else:
            result = convert_length(args.value, args.frm, args.to)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{result:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
