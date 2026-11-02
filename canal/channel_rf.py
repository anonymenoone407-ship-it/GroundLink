#!/usr/bin/env python3
"""GroundLink Guardian — Jalon 4 : canal RF simulé sur échantillons IQ.

VM3 (Canal) : Satellite -> 10.10.1.1:5555 -> modifications IQ
             -> Station 10.10.2.10:5556.
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import zmq


def arguments():
    p = argparse.ArgumentParser(description="GroundLink Guardian - canal RF simulé")
    p.add_argument("--mode", choices=("ideal", "awgn", "interference", "fading", "drop"),
                   default="awgn")
    p.add_argument("--snr-db", type=float, default=20.0,
                   help="SNR nominal par échantillon (AWGN, fading), en dB")
    p.add_argument("--tone-amplitude", type=float, default=2.0,
                   help="Amplitude de la tonalité perturbatrice")
    p.add_argument("--tone-frequency", type=float, default=0.05,
                   help="Fréquence de la tonalité, cycles/échantillon (0 à 0.5)")
    p.add_argument("--fading-gain", type=float, default=0.2,
                   help="Gain réel positif du canal affaibli (0 à 1)")
    p.add_argument("--drop-every", type=int, default=2,
                   help="Une trame sur N est supprimée (mode drop)")
    p.add_argument("--seed", type=int, default=42,
                   help="Graine aléatoire pour répéter les tests")
    args = p.parse_args()
    if args.drop_every < 1:
        p.error("--drop-every doit être >= 1")
    if not (0.0 <= args.tone_frequency <= 0.5):
        p.error("--tone-frequency doit être compris entre 0 et 0.5")
    if not (0.0 < args.fading_gain <= 1.0):
        p.error("--fading-gain doit être compris dans ]0, 1]")
    if args.tone_amplitude < 0:
        p.error("--tone-amplitude doit être >= 0")
    return args


def init_log():
    log_dir = Path.home() / "groundlink" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("groundlink.channel_rf")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (logging.StreamHandler(), logging.FileHandler(log_dir / "channel_rf.log")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log


def awgn(iq, snr_db, rng, reference_power=None):
    """Ajoute un bruit IQ circulaire complexe de puissance P_ref / SNR_lin."""
    p_ref = float(np.mean(np.abs(iq) ** 2)) if reference_power is None else reference_power
    p_noise = p_ref / (10.0 ** (snr_db / 10.0))
    sigma = np.sqrt(p_noise / 2.0)
    noise = sigma * (rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size))
    return (iq + noise).astype(np.complex64)


def main():
    args = arguments()
    log = init_log()
    rng = np.random.default_rng(args.seed)
    ctx = zmq.Context()
    receiver = ctx.socket(zmq.PULL)
    sender = ctx.socket(zmq.PUSH)
    receiver.setsockopt(zmq.RCVHWM, 20)
    sender.setsockopt(zmq.SNDHWM, 20)
    sender.setsockopt(zmq.IMMEDIATE, 1)
    sender.setsockopt(zmq.SNDTIMEO, 3000)

    try:
        receiver.bind("tcp://10.10.1.1:5555")
        sender.connect("tcp://10.10.2.10:5556")
        log.info("Démarrage | mode=%s | SNR=%.1f dB | seed=%d",
                 args.mode, args.snr_db, args.seed)
        log.info("Écoute 10.10.1.1:5555 -> Station 10.10.2.10:5556")
        count_rx = count_tx = count_drop = 0
        sample_offset = 0

        while True:
            data = receiver.recv()
            count_rx += 1
            if len(data) % np.dtype(np.complex64).itemsize:
                log.error("RX #%d rejeté : taille IQ invalide (%d octets)", count_rx, len(data))
                continue
            iq = np.frombuffer(data, dtype=np.complex64).copy()
            if iq.size == 0:
                log.warning("RX #%d vide : ignoré", count_rx)
                continue
            log.info("RX #%d : %d échantillons IQ (%d octets)", count_rx, iq.size, len(data))

            if args.mode == "drop" and count_rx % args.drop_every == 0:
                count_drop += 1
                log.warning("DROP #%d : bloc volontairement supprimé", count_rx)
                sample_offset += iq.size
                continue

            if args.mode == "awgn":
                iq = awgn(iq, args.snr_db, rng)
            elif args.mode == "interference":
                # Tonalité continue sur l'ensemble des blocs (pas de remise à zéro de phase).
                phase = 2 * np.pi * args.tone_frequency * (
                    np.arange(iq.size, dtype=np.float64) + sample_offset
                )
                iq += (args.tone_amplitude * np.exp(1j * phase)).astype(np.complex64)
            elif args.mode == "fading":
                # Fade simplifié : affaiblissement d'amplitude + bruit référencé au signal initial.
                p_before = float(np.mean(np.abs(iq) ** 2))
                iq = awgn(iq * args.fading_gain, args.snr_db, rng, reference_power=p_before)

            sample_offset += iq.size
            try:
                sender.send(iq.tobytes())
                count_tx += 1
                log.info("TX #%d accepté par ZeroMQ | %d octets | mode=%s",
                         count_rx, iq.nbytes, args.mode)
            except zmq.Again:
                count_drop += 1
                log.error("TIMEOUT TX #%d : Station non connectée sur TCP 5556", count_rx)

    except KeyboardInterrupt:
        log.info("Arrêt manuel")
    except Exception:
        log.exception("Erreur inattendue dans le Canal")
        raise
    finally:
        if 'count_rx' in locals():
            log.info("BILAN Canal : reçus=%d, transmis_ZeroMQ=%d, supprimés/timeout=%d",
                     count_rx, count_tx, count_drop)
        receiver.close(linger=0)
        sender.close(linger=0)
        ctx.term()


if __name__ == "__main__":
    main()

