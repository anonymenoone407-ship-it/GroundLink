"""Strict loader for NASA POWER hourly point CSV exports."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_COLUMNS = {"YEAR", "MO", "DY", "HR", "WS10M", "WD10M"}
MISSING_VALUE = -999.0


@dataclass(frozen=True)
class PowerObservation:
    timestamp: datetime
    wind_speed_ms: float
    wind_direction_deg: float


@dataclass(frozen=True)
class LoadResult:
    observations: tuple[PowerObservation, ...]
    metadata: dict[str, object]
    total_rows: int
    rejected_rows: int
    rejection_reasons: dict[str, int]


def _metadata(lines: list[str]) -> tuple[dict[str, object], int]:
    try:
        end = lines.index("-END HEADER-")
    except ValueError as exc:
        raise ValueError("NASA metadata header is missing -END HEADER-") from exc
    header = lines[: end + 1]
    joined = "\n".join(header)
    dates = re.search(r"Dates \(month/day/year\): (\d{2}/\d{2}/\d{4}) through (\d{2}/\d{2}/\d{4}) in (\w+)", joined)
    location = re.search(r"Location: Latitude\s+([-+\d.]+)\s+Longitude\s+([-+\d.]+)", joined)
    speed = re.search(r"WS10M\s+.*\((m/s)\)", joined)
    direction = re.search(r"WD10M\s+.*\((Degrees)\)", joined)
    missing = re.search(r"missing source data.*:\s*([-+\d.]+)", joined)
    if not all((dates, location, speed, direction, missing)):
        raise ValueError("NASA metadata is incomplete or has unsupported units")
    if dates.group(3) != "UTC" or speed.group(1) != "m/s" or direction.group(1) != "Degrees":
        raise ValueError("NASA time standard or wind units are unsupported")
    return {
        "start_date": dates.group(1), "end_date": dates.group(2),
        "time_standard": dates.group(3), "latitude": float(location.group(1)),
        "longitude": float(location.group(2)), "wind_speed_unit": speed.group(1),
        "wind_direction_unit": direction.group(1), "missing_value": float(missing.group(1)),
    }, end + 1


def load_nasa_power_csv(path: str | Path) -> LoadResult:
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    metadata, csv_line = _metadata(lines)
    if metadata["missing_value"] != MISSING_VALUE:
        raise ValueError("Unexpected NASA missing-value sentinel")
    reader = csv.DictReader(lines[csv_line:])
    if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
        missing = sorted(REQUIRED_COLUMNS.difference(reader.fieldnames or ()))
        raise ValueError(f"Missing required NASA columns: {', '.join(missing)}")
    valid: list[PowerObservation] = []
    reasons: dict[str, int] = {}
    total = 0
    for row in reader:
        total += 1
        try:
            values = {name: float(row[name]) for name in ("WS10M", "WD10M")}
            if any(not math.isfinite(v) for v in values.values()):
                raise ValueError("non_finite")
            if any(v == MISSING_VALUE for v in values.values()):
                raise ValueError("missing_value")
            if values["WS10M"] < 0:
                raise ValueError("negative_wind_speed")
            if not 0 <= values["WD10M"] <= 360:
                raise ValueError("invalid_wind_direction")
            timestamp = datetime(int(row["YEAR"]), int(row["MO"]), int(row["DY"]), int(row["HR"]), tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            reason = str(exc) if str(exc) in {"non_finite", "missing_value", "negative_wind_speed", "invalid_wind_direction"} else "invalid_datetime_or_number"
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        valid.append(PowerObservation(timestamp, values["WS10M"], values["WD10M"]))
    valid.sort(key=lambda item: item.timestamp)
    timestamps = [item.timestamp for item in valid]
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("Duplicate NASA observation timestamp")
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError("NASA timestamps are not strictly increasing")
    return LoadResult(tuple(valid), metadata, total, total - len(valid), reasons)

