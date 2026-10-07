"""Unit conversion. Everything is computed and stored in SI (m, m²); units apply at response time."""
from __future__ import annotations

from typing import Literal

AreaUnit = Literal["sqm", "sqkm", "ha", "acre", "sqft", "sqmi"]
LengthUnit = Literal["m", "km", "mi", "ft", "nmi"]

# factor = how many SI units in one of the named unit
_AREA_M2 = {
    "sqm": 1.0,
    "sqkm": 1_000_000.0,
    "ha": 10_000.0,
    "acre": 4046.8564224,
    "sqft": 0.09290304,
    "sqmi": 2_589_988.110336,
}
_LENGTH_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048, "nmi": 1852.0}


def area_from_m2(value: float | None, unit: str) -> float | None:
    return None if value is None else value / _AREA_M2[unit]


def length_from_m(value: float | None, unit: str) -> float | None:
    return None if value is None else value / _LENGTH_M[unit]
