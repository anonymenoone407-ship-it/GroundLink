# GroundLink shared agent contract v1

Phase 2B.1 defines the communication boundary for future VM1 Satellite and VM3
Channel agents. It does not implement an agent, open a listener, start a
service, or make LIVE mode ready. The immutable wire version is `1.0.0`.

The Python reference types are in `agent_contract.py`; the machine-readable
wire definition is `schemas/agent-contract-v1.schema.json`. Existing Mission
Control telemetry remains version `1.0.0` in `telemetry-v1.schema.json`.

## Identities and network placement

| Management address | Agent ID | Role |
|---|---|---|
| `10.10.3.10` | `vm1-satellite` | `SATELLITE` |
| `10.10.3.20` | `vm2-station` | `STATION` |
| `10.10.3.30` | `vm3-channel` | `CHANNEL` |

The source IP is a deployment allowlist, not an identity credential. The
authenticated transport identity must be bound to the exact agent ID and role.
An asserted JSON identity is never sufficient authentication.

## Protocol rules

- Negotiate exactly a supported contract version before preparation. Unknown
  major/minor versions fail closed.
- Capabilities contain only fixed scenario IDs and the three commands
  `PREPARE`, `START`, and `STOP`. All agents in a run must advertise the chosen
  scenario. Full compatibility requires the current eight-ID allowlist.
- `PREPARE` fixes `run_id` and scenario. `START` must reference the successful
  `preparation_id`; an agent must reject mismatched or repeated run context.
  `STOP` is idempotent and scoped to one run. There is no shell, executable,
  argv, script, path, environment, or free-form configuration command field.
- A Station-created 32-lowercase-hex `run_id` is distributed unchanged. Each
  physical/logical frame gets an opaque `frame_id`, preserved in Satellite,
  Channel, and Station ingestion envelopes. `event_id` remains the idempotency
  key for one producer event.
- Heartbeats report `IDLE`, `PREPARED`, `RUNNING`, `STOPPING`, or `ERROR`.
  Active states require the correlated `run_id`. Health does not imply
  readiness; readiness also includes compatible capabilities.
- Channel handling is explicit: `FORWARD`, `DROP`, or `ATTACK`. `ATTACK`
  requires one of `AWGN`, `JAMMING`, `SPOOFING`, `REPLAY`, or `MALFORMED`.
  Channel events are observations and can never contain ACCEPT/REJECT.
- Scenario labels and Channel actions never determine a security result.
  Only a validated `station.frame_decided` event accepted over an authenticated
  `vm2-station`/`STATION` transport may update LIVE ACCEPT/REJECT.

## Security prerequisites

Before any LIVE endpoint is enabled, deploy a mutually authenticated encrypted
transport. Prefer a private CA and TLS 1.3 client certificates with separate
leaf identities for the three agents. A short-lived, audience-bound signed
bearer scheme is contract-compatible only if TLS, issuer/audience checks,
expiry, unique token IDs, key rotation, and exact agent/role binding are also
enforced. Never use the browser control token as an agent credential.

On Station, allow inbound agent traffic only from `10.10.3.10` and
`10.10.3.30`; bind the service to the management interface; use a dedicated
unprivileged account; keep credentials outside the repository; restrict key
permissions; cap body size and rate; require fresh timestamps/nonces; log auth
failures without credentials; and reject unknown fields, versions, identities,
roles, scenarios, run IDs, frame IDs, and event types. Certificate/private-key
provisioning is deliberately outside this repository and no private key is
read by this phase.

## Future VM1 implementation

Implement a dedicated unprivileged Satellite agent that presents the
`vm1-satellite` credential, advertises the exact allowlist it truly supports,
validates the JSON Schema before state changes, and implements a finite state
machine: `IDLE -> PREPARED -> RUNNING -> STOPPING -> IDLE`. Emit only
`satellite.telemetry_transmitted` envelopes with the assigned run/frame IDs.
Do not add remote execution or copy Station decision fields.

## Future VM3 implementation

Implement a dedicated unprivileged Channel agent with the `vm3-channel`
credential and the same command state machine. For every correlated frame,
emit the ordinary RF observation plus a `channel.action` record identifying
`FORWARD`, `DROP`, or the specific attack. Preserve the selected scenario and
frame ID. Do not infer or emit ACCEPT/REJECT and do not alter existing RF,
cryptographic, or anti-replay code.

Both agents should treat duplicate command/event IDs idempotently, reject a
second concurrent run, stop safely after restart, and default to no forwarding
or attack until a matching authenticated PREPARE/START sequence succeeds.

## Verification

From `~/groundlink`:

```bash
.venv/bin/python -m unittest discover -s mission_control/tests -v
```

The current backend deliberately continues to report `live_agents` as
`NOT_READY`. No Phase 2B.1 route consumes these messages yet.
