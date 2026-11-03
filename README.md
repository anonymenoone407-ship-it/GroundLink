# GroundLink Guardian

**GroundLink Guardian** is a software-emulated secure satellite-to-ground communication testbed developed for the **TSYP14 Technical Challenge — Ground Truth Under Attack: Securing the Earth Observation Chain (Track 1)**.

The testbed transmits prepared NASA POWER meteorological observations as digitally signed, BPSK-modulated complex I/Q samples through a configurable communication channel. The ground station processes received samples, checks integrity and authenticity, applies replay protection, records security decisions, and provides a monitoring dashboard.

> **Scope:** This is a software-emulated laboratory, not a live orbital satellite link or an SDR hardware deployment. The five-frame examples below are functional demonstrations, not statistical security-performance benchmarks.

## Architecture

| Virtual machine | Repository folder | Responsibility |
| --- | --- | --- |
| VM1 — Satellite | `satellite/` | Reads observations, signs data using Ed25519, generates and transmits BPSK I/Q samples |
| VM3 — Channel | `canal/` | Forwards or modifies I/Q samples and runs controlled attack scenarios |
| VM2 — Ground Station | `station/` | Receives and processes I/Q, performs CRC32/signature/replay checks, stores events in SQLite, provides a Streamlit dashboard |

**Data path:** `VM1 Satellite → VM3 Channel → VM2 Ground Station`

The laboratory uses VirtualBox internal networks `GroundLink-A` (VM1–VM3), `GroundLink-B` (VM3–VM2), and `GroundLink-Control` (management). Network addresses and adapter configuration are documented in the installation guide.

## Repository structure

```text
GroundLink/
├── satellite/    # Satellite transmitter and prepared data
├── canal/        # Communication channel and attack scripts
├── station/      # Receiver, event storage, and dashboard
├── .gitignore
└── README.md
```

The prepared dataset is committed under `satellite/data/normalized/`:

- `wind_observations_public.csv`
- `source_manifest.json`
- `preprocessing_report.json`

The original hourly NASA POWER series contains 17,544 validated records, using `WS10M` and `WD10M` at latitude 36° N and longitude 11° E for 2024–2025 (UTC). Dataset details and provenance are described in the technical documentation.

## Requirements

- Oracle VirtualBox with three Ubuntu 24.04.2 LTS virtual machines
- Python 3.12.3 and a virtual environment on each VM
- Git and the Python dependencies needed by each VM component
- An Ed25519 signing private key on VM1 and the matching trusted public key on VM2

**Important:** Configure network interfaces, install complete project dependencies, and provision keys using the **Installation, Operation & Reproducibility Guide** before running the commands below. The repository must not contain operational private keys.

## Obtain the code

Run on each VM:

```bash
git clone https://github.com/anonymenoone407-ship-it/GroundLink.git
cd GroundLink
git rev-parse HEAD
```

For repeatable experiments, use the validated commit specified by the release documentation.

Prepare the role-specific working directory on the corresponding VM:

**VM1 — Satellite**
```bash
mkdir -p ~/groundlink
cp -a ~/GroundLink/satellite/. ~/groundlink/
```

**VM2 — Ground Station**
```bash
mkdir -p ~/groundlink
cp -a ~/GroundLink/station/. ~/groundlink/
```

**VM3 — Channel**
```bash
mkdir -p ~/groundlink
cp -a ~/GroundLink/canal/. ~/groundlink/
```

Create a virtual environment in each `~/groundlink` directory:

```bash
cd ~/groundlink
python3 -m venv .venv
```

Install the **validated role-specific dependencies** and configure Ed25519 trust material as described in the installation guide. The dependencies and key-file paths must be checked against the released code; no generic key-generation command is assumed here.

## Baseline experiment

Start components **in this order: VM2 → VM3 → VM1**. Use a fresh log directory for each experiment and stop existing receiver/channel processes before restarting. The following are the documented baseline commands.

**VM2 — Receiver**
```bash
cd ~/groundlink
mkdir -p runs/ideal_001
GL_SCENARIO=ideal .venv/bin/python -u station_secure.py \
  2>&1 | tee runs/ideal_001/station.log
```

**VM3 — Ideal channel**
```bash
cd ~/groundlink
mkdir -p runs/ideal_001
.venv/bin/python -u channel_rf.py --mode ideal \
  2>&1 | tee runs/ideal_001/channel.log
```

**VM1 — Transmitter**
```bash
cd ~/groundlink
mkdir -p runs/ideal_001
.venv/bin/python -u satellite_secure.py --mode dataset --dataset-offset 0 \
  2>&1 | tee runs/ideal_001/satellite.log
```

For the documented five-observation ideal baseline, the receiver recorded five accepted frames without rejects. Actual results must be checked from each new execution; run IDs and event IDs vary.

## Controlled experiments

For each experiment, restart VM2 using the matching `GL_SCENARIO` value, run **exactly one** VM3 script, and start the normal VM1 dataset transmitter. Use a fresh log directory and preserve the receiver's replay-protection state.

| Scenario label on VM2 | VM3 command (run from `~/groundlink`) | Focus |
| --- | --- | --- |
| `ideal` | `.venv/bin/python -u channel_rf.py --mode ideal` | Reference transmission |
| `awgn_20db` | `.venv/bin/python -u channel_rf.py --mode awgn --snr-db 20 --seed 42` | Moderate Gaussian noise |
| `awgn_0db` | `.venv/bin/python -u channel_rf.py --mode awgn --snr-db 0 --seed 42` | Severe Gaussian noise |
| `interference` | `.venv/bin/python -u channel_rf.py --mode interference --tone-amplitude 0.8 --tone-frequency 0.125 --seed 42` | Tone interference and RF alerts |
| `replay` | `.venv/bin/python -u channel_replay.py` | Replay protection |
| `spoofing` | `.venv/bin/python -u channel_spoof.py` | Signature authentication |
| `tampering` | `.venv/bin/python -u channel_tamper.py` | Integrity and signature verification |

Scenario mechanisms, attack assumptions, observations, and limitations belong in the companion technical report. These scripts are intended for an isolated, authorized laboratory.

## Monitoring dashboard

On **VM2**, after events have been recorded and the dashboard dependencies have been installed:

```bash
cd ~/groundlink
.venv/bin/python -m streamlit run dashboard.py \
  --server.address 127.0.0.1 --server.port 8501
```

Open **http://localhost:8501** in the VM2 browser. The monitoring database is documented as `state/monitoring.sqlite3`. RF anomalies and cryptographic ACCEPT/REJECT decisions are distinct outputs.

## Documentation and reproducibility

The companion **Installation, Operation & Reproducibility Guide** provides exact VM setup, network addresses, dependency installation, key provisioning, dataset access, and experiment procedures. The separate **Technical Report** contains the threat model, attack methodology, experimental evidence, results, limitations, and future work.

Before publishing a release, record the tested Git commit, confirm all commands against that commit, and verify that example artifacts contain no private keys or sensitive data.

## Security notes

- Keep signing private keys on VM1; transfer only the corresponding public key to VM2.
- Never commit `.env`, private keys, live credentials, or local virtual environments.
- Retain attack scripts only for authorized laboratory experiments.
- Do not delete or reset persistent replay state as part of routine runs.
- Do not treat five-frame experiments as generalizable detection-rate measurements.

## Data source

[NASA POWER](https://power.larc.nasa.gov/) — hourly meteorological point observations.
