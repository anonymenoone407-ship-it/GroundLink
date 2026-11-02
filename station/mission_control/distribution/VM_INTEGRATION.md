# VM1 and VM3 integration handoff

This archive is GroundLink agent contract version `1.0.0`. It is a reference
contract and test bundle, not an agent implementation or installer.

## Expected archive structure

```text
groundlink-agent-contract-1.0.0/
├── MANIFEST.sha256
├── README.md
├── mission_control/
│   ├── __init__.py
│   ├── agent_contract.py
│   ├── models.py
│   ├── run_id.py
│   ├── scenarios.py
│   ├── schemas/
│   │   ├── agent-contract-v1.schema.json
│   │   └── telemetry-v1.schema.json
│   └── tests/
│       ├── test_agent_contract.py
│       └── test_contract.py
└── docs/
    ├── AGENT_CONTRACT.md
    ├── DEPENDENCIES.md
    └── VM_INTEGRATION.md
```

`MANIFEST.sha256` covers every regular payload file except itself. The
separately distributed `.tar.gz.sha256` file authenticates the archive as a
whole once its value is obtained through a trusted channel.

## Install and verify on either VM

Transfer the archive and its `.sha256` sidecar through the approved management
path. On the destination, compare the sidecar value obtained from VM2 or the
operator, then run:

```bash
sha256sum -c groundlink-agent-contract-1.0.0.tar.gz.sha256
tar -tzf groundlink-agent-contract-1.0.0.tar.gz
tar -xzf groundlink-agent-contract-1.0.0.tar.gz
cd groundlink-agent-contract-1.0.0
sha256sum -c MANIFEST.sha256
python3 -m venv .venv-contract
.venv-contract/bin/python -m pip install 'jsonschema==4.26.0'
.venv-contract/bin/python -m unittest discover -s mission_control/tests -v
```

Review the listing before extraction. It must match the structure above and
must not contain absolute paths, `..`, keys, credentials, databases, logs,
environment files, caches, or virtual environments.

## Exact contract facts

- Supported agent-contract versions: only `1.0.0`.
- Telemetry schema version: only `1.0.0`.
- Identities: `vm1-satellite`/`SATELLITE`,
  `vm2-station`/`STATION`, and `vm3-channel`/`CHANNEL`.
- Telemetry event types: `satellite.telemetry_transmitted`,
  `channel.rf_observed`, and `station.frame_decided`.
- Scenarios: `unlabeled`, `baseline`, `awgn_20db`, `awgn_0db`,
  `replay`, `spoofing`, `jamming`, and `malformed`.
- Commands: exactly `PREPARE`, `START`, and `STOP`.
- Channel actions: `FORWARD`, `DROP`, or `ATTACK`; attacks are `AWGN`,
  `JAMMING`, `SPOOFING`, `REPLAY`, or `MALFORMED`.

## Compatibility and required future adapter

There is currently no FastAPI agent endpoint. Mission Control LIVE runs remain
`NOT_READY`, while the event database accepts events only for `ACTIVE` runs.
The broker accepts bare telemetry-v1 dictionaries, not agent ingestion
envelopes. An eventual authenticated Station ingress adapter must therefore:

1. authenticate the transport and construct `AuthenticatedAgent` only from
   trusted transport state, never request JSON;
2. parse an envelope with `ingestion_from_dict`, call `authorize_ingestion`,
   verify run/scenario/frame correlation and freshness, and deduplicate IDs;
3. unwrap only the validated telemetry event for `EventBroker.publish`;
4. map an authenticated Station principal to the broker's existing
   `EventSource(role="station_receiver", authenticated=True)` exclusively for
   `station.frame_decided`; and
5. preserve `frame_id` in a correlation store or a new explicitly versioned
   persistence contract, because telemetry v1 and the current broker payload
   do not contain it.

`ChannelActionEvent` is not telemetry v1: it has `message_type:
channel.action`, while the broker requires `event_type`. The current frontend
also dispatches only the three telemetry event types. Its
`explicitChannelAction()` helper is not wired to WebSocket input. A future
adapter must validate and persist/stream Channel actions on a separately
versioned action path, or introduce a new telemetry schema/event type and
update broker plus frontend together. It must not rename an action to
`channel.rf_observed`, inject an `action` field into telemetry v1, or infer a
Station decision. No such adapter is implemented in Phase 2B.2.

## Instructions for Codex on VM1 Satellite (10.10.3.10)

1. Verify both archive and manifest checksums, then run the supplied tests.
2. Import the copied package without editing it. Pin contract version `1.0.0`.
3. Assert only `AgentIdentity("vm1-satellite", "SATELLITE", VERSION)` and
   advertise only scenarios actually implemented, from the fixed vocabulary.
4. Build a dedicated unprivileged agent around the closed command models. Use
   an idempotent `IDLE -> PREPARED -> RUNNING -> STOPPING -> IDLE` state
   machine; reject unknown fields, concurrent runs, mismatched scenario/run,
   and START without its successful `preparation_id`.
5. Accept Station-generated run IDs and assigned frame IDs unchanged. Emit
   only `SatelliteEvent` (`satellite.telemetry_transmitted`) inside
   `IngestedTelemetry`. Never emit Channel or Station events or decisions.
6. Keep all existing satellite RF/crypto scripts untouched. Do not add shell
   execution, subprocess commands, arbitrary paths/configuration, or use the
   Mission Control browser token.
7. Leave the service disabled until mutual authentication, ingress endpoints,
   replay/freshness controls, and an operator-approved LIVE phase exist.

## Instructions for Codex on VM3 Channel (10.10.3.30)

1. Verify both archive and manifest checksums, then run the supplied tests.
2. Import the copied package without editing it. Pin contract version `1.0.0`.
3. Assert only `AgentIdentity("vm3-channel", "CHANNEL", VERSION)` and use the
   same safe, idempotent command state machine and rejection rules as VM1.
4. Correlate every frame with the Station run ID and assigned `frame_id`.
   Emit RF measurements only as `ChannelEvent` (`channel.rf_observed`) inside
   `IngestedTelemetry`.
5. Separately emit a validated `ChannelActionEvent` for each `FORWARD`, `DROP`,
   or `ATTACK`. Do not send that message to the existing telemetry broker or
   pretend it is telemetry v1; wait for the explicit Station adapter.
6. Never emit ACCEPT/REJECT, `station.frame_decided`, or authenticated
   satellite identity. Keep existing channel/RF scripts and algorithms
   unchanged; add no remote shell or arbitrary command facility.
7. Default to no run activity until authenticated PREPARE and matching START;
   leave any future service disabled until the Station integration is ready.
