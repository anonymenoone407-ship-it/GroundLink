
import math
import sqlite3

from collections import Counter, defaultdict
from pathlib import Path


# Seuils provisoires : calibration a venir
EVM_DEGRADED = 20.0
EVM_ANOMALY = 60.0
SPECTRAL_PEAK_ANOMALY = 30.0
POWER_LOW = -7.0
GAIN_LOW = 0.4


def detect_rf_anomaly(metrics):
    """
    Classification RF independante de CRC32,
    Ed25519, Anti-Replay et scenario.
    """

    required = (
        "power_db_rel",
        "evm_percent",
        "estimated_gain",
    )

    if not isinstance(metrics, dict):
        return {
            "level": "UNKNOWN",
            "reasons": ["RF_METRICS_UNAVAILABLE"],
        }

    for name in required:

        value = metrics.get(name)

        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
        ):
            return {
                "level": "UNKNOWN",
                "reasons": [f"INVALID_{name.upper()}"],
            }

    power = metrics["power_db_rel"]
    evm = metrics["evm_percent"]
    gain = metrics["estimated_gain"]
    peak = metrics.get("residual_spectral_peak_db")

    if evm < 0 or gain < 0:
        return {
            "level": "UNKNOWN",
            "reasons": ["INVALID_RF_RANGE"],
        }

    if peak is not None and (
        type(peak) not in (int, float)
        or not math.isfinite(peak)
    ):
        return {
            "level": "UNKNOWN",
            "reasons": ["INVALID_SPECTRAL_PEAK"],
        }

    critical = []
    warnings = []

    # Forte distorsion
    if evm >= EVM_ANOMALY:
        critical.append("EVM_CRITICAL")

    elif evm >= EVM_DEGRADED:
        warnings.append("EVM_ELEVATED")

    # Composante spectrale anormalement forte
    if (
        peak is not None
        and peak >= SPECTRAL_PEAK_ANOMALY
    ):
        critical.append("SPECTRAL_PEAK_HIGH")

    # Attenuation : pas necessairement une attaque
    if power <= POWER_LOW:
        warnings.append("LOW_POWER")

    if gain <= GAIN_LOW:
        warnings.append("LOW_GAIN")

    if critical:
        level = "ANOMALY"

    elif warnings:
        level = "DEGRADED"

    else:
        level = "NORMAL"

    return {
        "level": level,
        "reasons": critical + warnings or ["RF_OK"],
    }


def main():

    db_path = (
        Path.home()
        / "groundlink"
        / "state"
        / "monitoring.sqlite3"
    )

    if not db_path.is_file():
        raise SystemExit(
            f"Monitoring DB introuvable : {db_path}"
        )

    summary = defaultdict(Counter)

    with sqlite3.connect(db_path) as db:

        rows = db.execute("""
            SELECT
                scenario,
                power_db_rel,
                evm_percent,
                estimated_gain,
                residual_spectral_peak_db
            FROM rx_events
            ORDER BY id
        """)

        for scenario, power, evm, gain, peak in rows:

            metrics = {
                "power_db_rel": power,
                "evm_percent": evm,
                "estimated_gain": gain,
                "residual_spectral_peak_db": peak,
            }

            result = detect_rf_anomaly(metrics)

            summary[scenario][result["level"]] += 1
            summary[scenario]["TOTAL"] += 1

    print("\n=== GROUNDLINK RF ANOMALY DETECTOR ===")

    print(
        f"{'SCENARIO':<18}"
        f"{'TOTAL':>7}"
        f"{'NORMAL':>9}"
        f"{'DEGRADED':>11}"
        f"{'ANOMALY':>10}"
        f"{'UNKNOWN':>9}"
    )

    for scenario in sorted(summary):

        counts = summary[scenario]

        print(
            f"{scenario:<18}"
            f"{counts['TOTAL']:>7}"
            f"{counts['NORMAL']:>9}"
            f"{counts['DEGRADED']:>11}"
            f"{counts['ANOMALY']:>10}"
            f"{counts['UNKNOWN']:>9}"
        )

    print("====================================\n")


if __name__ == "__main__":
    main()



