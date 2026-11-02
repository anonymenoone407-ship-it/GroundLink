import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from groundlink_secure_common import bpsk_demodulate, bpsk_modulate, create_signed_frame, verify_signed_frame
from satellite_data.nasa_power_loader import load_nasa_power_csv
from satellite_secure import build_observation, select_dataset_rows

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/nasa_power_2024_2025.csv"
NORMALIZED = ROOT / "data/normalized/wind_observations_public.csv"


class NasaDatasetTests(unittest.TestCase):
    def test_imported_dataset_and_metadata(self):
        result = load_nasa_power_csv(RAW)
        self.assertEqual((result.total_rows, len(result.observations), result.rejected_rows), (17544, 17544, 0))
        self.assertEqual((result.metadata["time_standard"], result.metadata["wind_speed_unit"], result.metadata["wind_direction_unit"]), ("UTC", "m/s", "Degrees"))
        self.assertEqual((result.metadata["latitude"], result.metadata["longitude"]), (36.0, 11.0))

    def test_provenance(self):
        manifest = json.loads((ROOT / "data/normalized/source_manifest.json").read_text())
        self.assertEqual(manifest["source_sha256"], "c7a2e10417084dc6adc4ed74b7663c9f386fc1da794656aac59dbab6305de82a")
        self.assertIn("power.larc.nasa.gov", manifest["nasa_url"])

    def test_deterministic_five_row_selection(self):
        one = select_dataset_rows(NORMALIZED, 0)
        two = select_dataset_rows(NORMALIZED, 0)
        self.assertEqual(one, two)
        self.assertEqual([r["source_timestamp_utc"] for r in one], [f"2024-01-01T0{h}:00:00Z" for h in range(5)])

    def test_missing_column_and_invalid_values(self):
        base = """-BEGIN HEADER-\nNASA/POWER Source Native Resolution Hourly Data\nDates (month/day/year): 01/01/2024 through 12/31/2025 in UTC\nLocation: Latitude  36.0   Longitude 11.0\nThe value for missing source data that cannot be computed or is outside of the sources availability range: -999\nParameter(s):\nWS10M Wind (m/s)\nWD10M Direction (Degrees)\n-END HEADER-\n"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text(base + "YEAR,MO,DY,HR,WS10M,WD10M\n2024,1,1,0,-999,20\n2024,1,1,1,-1,20\n2024,1,1,2,nan,20\n2024,1,1,3,2,361\n2024,13,1,4,2,20\n")
            result = load_nasa_power_csv(path)
            self.assertEqual((result.total_rows, result.rejected_rows), (5, 5))
            path.write_text(base + "YEAR,MO,DY,HR,WS10M\n2024,1,1,0,2\n")
            with self.assertRaisesRegex(ValueError, "WD10M"):
                load_nasa_power_csv(path)

    def test_payload_signing_bpsk_and_freshness_fields(self):
        row = select_dataset_rows(NORMALIZED, 0, 1)[0]
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        payload = build_observation(row, "a" * 32, 1, now)
        self.assertEqual(set(payload), {"satellite_id", "session_id", "observation_id", "sequence_number", "measurement_type", "data_origin", "latitude", "longitude", "wind_speed_ms", "wind_direction_deg", "timestamp"})
        self.assertEqual(payload["timestamp"], "2026-11-02T12:00:00Z")
        self.assertEqual(payload["sequence_number"], 1)
        frame = create_signed_frame(payload, ROOT / "keys/satellite_private.pem")
        self.assertEqual(verify_signed_frame(frame, ROOT / "keys/satellite_public.pem"), payload)
        iq = bpsk_modulate(frame)
        self.assertEqual(iq.dtype, np.complex64)
        self.assertEqual(len(iq), len(frame) * 8 * 4)
        self.assertEqual(bpsk_demodulate(iq.tobytes()), frame)

    def test_offsets_and_sequence_are_replay_safe(self):
        rows = select_dataset_rows(NORMALIZED, 10)
        first = [build_observation(row, "session-one", seq) for seq, row in enumerate(rows, 1)]
        second = [build_observation(row, "session-two", seq) for seq, row in enumerate(rows, 1)]
        self.assertEqual([x["sequence_number"] for x in first], [1, 2, 3, 4, 5])
        self.assertNotEqual(first[0]["session_id"], second[0]["session_id"])
        self.assertEqual(len({x["observation_id"] for x in first}), 5)


if __name__ == "__main__":
    unittest.main()
