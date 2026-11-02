import numpy as np


# GroundLink Guardian - RF Monitoring
# BPSK : 4 echantillons par symbole

SAMPLES_PER_SYMBOL = 4
EPS = 1e-12


def analyze_iq(iq_bytes, samples_per_symbol=SAMPLES_PER_SYMBOL):
    """
    Analyse un bloc IQ complex64.

    Renvoie :
        samples
        symbols
        power_db_rel
        estimated_gain
        evm_percent
        residual_spectral_peak_db
    """

    # 1. Valider les donnees IQ
    if not isinstance(
        iq_bytes, (bytes, bytearray, memoryview)
    ):
        raise TypeError("IQ_BYTES_REQUIRED")

    if len(iq_bytes) == 0 or len(iq_bytes) % 8 != 0:
        raise ValueError("IQ_LENGTH_INVALID")

    if (
        type(samples_per_symbol) is not int
        or samples_per_symbol < 1
    ):
        raise ValueError("SAMPLES_PER_SYMBOL_INVALID")

    iq = np.frombuffer(
        iq_bytes, dtype=np.complex64
    ).astype(np.complex128)

    if iq.size % samples_per_symbol != 0:
        raise ValueError("IQ_SYMBOL_ALIGNMENT_INVALID")

    if not np.all(np.isfinite(iq)):
        raise ValueError("IQ_NON_FINITE")

    # 2. Puissance moyenne du signal
    power = float(np.mean(np.abs(iq) ** 2))

    if power < EPS:
        raise ValueError("IQ_POWER_TOO_LOW")

    power_db = float(10 * np.log10(power))

    # 3. Decisions BPSK provisoires
    symbol_means = iq.real.reshape(
        -1, samples_per_symbol
    ).mean(axis=1)

    decisions = np.where(
        symbol_means < 0, -1.0, 1.0
    )

    # Reference BPSK reconstruite
    reference = np.repeat(
        decisions, samples_per_symbol
    )

    # 4. Estimation du gain complexe
    gain = np.vdot(reference, iq) / iq.size

    fitted_signal = reference * gain

    residual = iq - fitted_signal

    # 5. Calcul de l'EVM
    fitted_power = float(
        np.mean(np.abs(fitted_signal) ** 2)
    )

    residual_power = float(
        np.mean(np.abs(residual) ** 2)
    )

    evm_percent = float(
        100 * np.sqrt(
            residual_power / max(fitted_power, EPS)
        )
    )

    # 6. Recherche d'un pic spectral
    # sur le residu (apres soustraction BPSK)
    if residual_power < EPS:

        spectral_peak_db = None

    else:

        window = np.hanning(iq.size)

        residual_centered = residual - residual.mean()

        spectrum = np.fft.fft(
            residual_centered * window
        )

        spectrum_power = np.abs(spectrum) ** 2

        median_power = float(
            np.median(spectrum_power)
        )

        peak_power = float(
            np.max(spectrum_power)
        )

        spectral_peak_db = float(
            10 * np.log10(
                peak_power / max(median_power, EPS)
            )
        )

    # 7. Retourner les caracteristiques
    return {
        "samples": int(iq.size),
        "symbols": int(
            iq.size // samples_per_symbol
        ),
        "power_db_rel": power_db,
        "estimated_gain": float(abs(gain)),
        "evm_percent": evm_percent,
        "residual_spectral_peak_db": spectral_peak_db,
    }


