# GroundLink Satellite agent (offline core)

This isolated package implements the contract 1.0.0 Satellite state machine.
It opens no sockets, exposes no API, starts no process, and never reads keys.

The optional secure command implementation is documented in
`SECURE_SERVER.md`. It is disabled by default and has no launcher or service.

Commands are validated first against the distributed JSON Schema and then its
Python reference model. The fixed identity is `vm1-satellite` / `SATELLITE`.
Only confirmed transmitter handoffs can become
`satellite.telemetry_transmitted` events in an `IngestedTelemetry` envelope.

## Future transmitter integration

Keep `satellite_secure.py`'s signing, BPSK, and telemetry construction intact.
A controlled runner must add a callback immediately after successful return
from its existing `socket.send(iq_samples.tobytes())`. That callback supplies
the real `SAT-001`, session ID, and sequence number to
`SatelliteSecureIntegrationAdapter.confirm_handoff`; Mission Control supplies
and retains the correlated run/scenario/frame IDs. Process start and ZeroMQ
connect are not proof of transmission. Successful send proves local ZeroMQ
handoff only; end-to-end reception needs a later authenticated Channel/Station
observation.

LIVE execution additionally requires authenticated encrypted transport,
certificate identity binding, replay/freshness protection, authorization,
rate/body limits, audit logs, an unprivileged service account, fixed executable
configuration, safe process supervision, credential provisioning outside the
repository, firewall/interface binding, and recovery/state persistence.

## Offline verification

Use the contract distribution's self-contained environment (the project's
existing `.venv` is intentionally untouched):

```bash
PYTHONPATH=dist/groundlink-agent-contract-1.0.0 \
  dist/groundlink-agent-contract-1.0.0/.venv-contract/bin/python \
  -m unittest discover -s dist/groundlink-agent-contract-1.0.0/mission_control/tests -v
PYTHONPATH=. dist/groundlink-agent-contract-1.0.0/.venv-contract/bin/python \
  -m unittest discover -s satellite_agent/tests -v
```
