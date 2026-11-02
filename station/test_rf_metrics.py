import numpy as np

from rf_metrics import analyze_iq


print("========== GROUNDLINK RF TEST ==========")

rng = np.random.default_rng(42)

# 1. Signal BPSK ideal
data = rng.integers(0, 256, 378, dtype=np.uint8)

bits = np.unpackbits(data)

symbols = 1.0 - 2.0 * bits.astype(np.float32)

ideal = np.repeat(symbols, 4).astype(np.complex64)

# 2. Bruit AWGN 20 dB
noise20 = (
    rng.standard_normal(ideal.size)
    + 1j * rng.standard_normal(ideal.size)
) * np.sqrt(0.01 / 2)

awgn20 = (ideal + noise20).astype(np.complex64)

# 3. Bruit AWGN 0 dB
noise0 = (
    rng.standard_normal(ideal.size)
    + 1j * rng.standard_normal(ideal.size)
) * np.sqrt(1.0 / 2)

awgn0 = (ideal + noise0).astype(np.complex64)

# 4. Attenuation
fading = (0.2 * ideal).astype(np.complex64)

# 5. Interference tonale
n = np.arange(ideal.size)

tone = (
    ideal + 2.0 * np.exp(2j * np.pi * 0.05 * n)
).astype(np.complex64)

# Regrouper les scenarios
tests = {
    "IDEAL": ideal,
    "AWGN_20dB": awgn20,
    "AWGN_0dB": awgn0,
    "FADING": fading,
    "INTERFERENCE": tone
}

# Analyse des signaux
for name, signal in tests.items():

    metrics = analyze_iq(signal.tobytes())

    power = metrics["power_db_rel"]
    evm = metrics["evm_percent"]
    gain = metrics["estimated_gain"]
    peak = metrics["residual_spectral_peak_db"]

    if peak is None:
        peak_text = "N/A"
    else:
        peak_text = f"{peak:.2f} dB"

    print(
        f"[{name}] "
        f"Power={power:.2f} dB | "
        f"EVM={evm:.2f}% | "
        f"Gain={gain:.3f} | "
        f"SpectralPeak={peak_text}"
    )

print("========================================")
print("[SUCCESS] RF metrics test completed")
