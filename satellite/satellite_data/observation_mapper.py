"""Normalize NASA observations and write reproducible provenance artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from .nasa_power_loader import LoadResult, PowerObservation, load_nasa_power_csv

NASA_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=WS10M,WD10M&community=AG&longitude=11.0&latitude=36.0&start=20240101&end=20251231&format=CSV&time-standard=UTC"
FIELDNAMES = ["source_observation_id", "source_timestamp_utc", "latitude", "longitude", "wind_speed_ms", "wind_direction_deg"]


def source_observation_id(item: PowerObservation) -> str:
    basis = f"NASA_POWER|36.0|11.0|{item.timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    return "nasa-power-" + hashlib.sha256(basis.encode()).hexdigest()[:20]


def normalized_row(item: PowerObservation) -> dict[str, object]:
    return {"source_observation_id": source_observation_id(item),
            "source_timestamp_utc": item.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latitude": 36.0, "longitude": 11.0,
            "wind_speed_ms": item.wind_speed_ms, "wind_direction_deg": item.wind_direction_deg}


def preprocess(source: str | Path, output_dir: str | Path) -> LoadResult:
    source, output_dir = Path(source), Path(output_dir)
    result = load_nasa_power_csv(source)
    if result.metadata["latitude"] != 36.0 or result.metadata["longitude"] != 11.0:
        raise ValueError("NASA source coordinates do not match 36.0, 11.0")
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "wind_observations_public.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader(); writer.writerows(normalized_row(item) for item in result.observations)
    sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {"source": "NASA POWER hourly point API", "nasa_url": NASA_URL,
                "source_file": str(source), "source_sha256": sha256,
                "coordinates": {"latitude": 36.0, "longitude": 11.0},
                "units": {"wind_speed": "m/s", "wind_direction": "degrees"},
                "time_standard": "UTC", "source_period": {"start": "2024-01-01T00:00:00Z", "end": "2025-12-31T23:00:00Z"},
                "processing": {"missing_value": -999, "reject_non_finite": True, "reject_negative_speed": True,
                               "valid_direction_range_degrees": [0, 360], "sort": "source_timestamp_utc ascending",
                               "duplicates": "reject", "identifier": "SHA256(source, coordinates, timestamp), first 20 hex"}}
    report = {"total_rows": result.total_rows, "valid_rows": len(result.observations),
              "rejected_rows": result.rejected_rows, "rejection_reasons": result.rejection_reasons}
    for name, value in (("source_manifest.json", manifest), ("preprocessing_report.json", report)):
        (output_dir / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/raw/nasa_power_2024_2025.csv")
    parser.add_argument("--output-dir", default="data/normalized")
    args = parser.parse_args(); result = preprocess(args.source, args.output_dir)
    print(f"Processed {result.total_rows} rows: {len(result.observations)} valid, {result.rejected_rows} rejected")


if __name__ == "__main__":
    main()
