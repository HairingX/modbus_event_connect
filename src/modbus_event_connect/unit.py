"""Units of measurement, by world standards."""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class Unit(StrEnum):
    """Units of measurement. The value is the symbol to show, `code` the UCUM code.

    Symbols follow the SI's writing rules, and UCUM's where the SI has none (week, month, year).
    """
    CELSIUS = "°C"
    FAHRENHEIT = "°F"
    KELVIN = "K"
    PERCENT = "%"
    PPM = "ppm"
    MILLISECONDS = "ms"
    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"
    DAYS = "d"
    WEEKS = "wk"
    MONTHS = "mo"
    YEARS = "a"
    WATT = "W"
    KILOWATT = "kW"
    WATT_HOUR = "W·h"
    KILOWATT_HOUR = "kW·h"
    VOLT = "V"
    AMPERE = "A"
    HERTZ = "Hz"
    RPM = "/min"
    PASCAL = "Pa"
    BAR = "bar"
    CUBIC_METERS_PER_HOUR = "m³/h"
    LITERS_PER_MINUTE = "L/min"

    @property
    def code(self) -> str:
        """The UCUM code: unambiguous, case-sensitive ASCII, for exchange with other systems."""
        return _UCUM_CODES[self]


_UCUM_CODES: Mapping[Unit, str] = MappingProxyType({
    Unit.CELSIUS: "Cel",
    Unit.FAHRENHEIT: "[degF]",
    Unit.KELVIN: "K",
    Unit.PERCENT: "%",
    Unit.PPM: "[ppm]",
    Unit.MILLISECONDS: "ms",
    Unit.SECONDS: "s",
    Unit.MINUTES: "min",
    Unit.HOURS: "h",
    Unit.DAYS: "d",
    Unit.WEEKS: "wk",
    Unit.MONTHS: "mo",
    Unit.YEARS: "a",
    Unit.WATT: "W",
    Unit.KILOWATT: "kW",
    Unit.WATT_HOUR: "W.h",
    Unit.KILOWATT_HOUR: "kW.h",
    Unit.VOLT: "V",
    Unit.AMPERE: "A",
    Unit.HERTZ: "Hz",
    Unit.RPM: "/min",
    Unit.PASCAL: "Pa",
    Unit.BAR: "bar",
    Unit.CUBIC_METERS_PER_HOUR: "m3/h",
    Unit.LITERS_PER_MINUTE: "L/min",
})
"""Every unit's UCUM code, written out even where it equals the symbol."""
