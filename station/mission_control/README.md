# GroundLink Mission Control — Phase 1A

This directory defines the versioned telemetry boundary for the future Mission
Control dashboard. It intentionally contains no FastAPI server, WebSocket
transport, VM agent, database writer, cryptographic operation, or UI.

## Architecture

The v1 envelope correlates every event with the same opaque `run_id`, an
experiment-only allowlisted `scenario`, an `event_id`, and a timezone-aware
`event_time`. Three event families support the eventual vertical visualization:

- `satellite.telemetry_transmitted`: satellite lifecycle/transmission facts.
- `channel.rf_observed`: RF measurements, explicitly unauthenticated.
- `station.frame_decided`: the authoritative decision read from an existing
  `rx_events` row produced by the real Station receiver.

The source of truth is the current flow:

`station_secure.py` → CRC/Ed25519/anti-replay validation →
`MonitoringDB.record_event()` → `rx_events` → future read-only adapter →
Mission Control events.

No event model calls or duplicates the receiver's security mechanisms. Scenario
labels are presentation/correlation metadata and must never affect a decision.

## Integration contract

The future adapter must open `state/monitoring.sqlite3` read-only, preserve each
row's `run_id`, `event_time`, `scenario`, `id`, `status`, `reason`, RF fields,
and timing fields, and emit a `StationEvent`. Map `id` to `station_event_id`,
`status` to `decision`, and use `station_receiver` as the fixed
`decision_source`. Identity fields must remain null on rejected rows exactly as
enforced by `MonitoringDB`; accepted identity is copied, never inferred.

Satellite and Channel events cannot contain `decision`, `reason`, or
`decision_source`. They may animate the UI but may not drive ACCEPT/REJECT
display state. The future WebSocket layer must validate every message against
`schemas/telemetry-v1.schema.json` and must reject unknown fields and schema
versions.

`run_id.py` deliberately uses the same 32-character lowercase UUID hex format
as `MonitoringDB`. For Station data, the database value always wins; a future
orchestrator may call `new_run_id()` once and distribute that value to producers.

## Versioning

The wire version is `1.0.0`; its immutable contract is
`schemas/telemetry-v1.schema.json`. Additive compatible revisions should get a
new schema file/version rather than editing an already deployed contract.

## Tests

From the repository root:

```bash
python3 -m unittest discover -s mission_control/tests -v
```

The unit suite checks strict runtime validation and trust-boundary invariants.
The contract suite validates model output against the JSON Schema using
`jsonschema`.

## Phase 1B central backend

The backend is in `server/`. It persists only to
`mission_control/state/mission_control.sqlite3`; it never opens or changes the
Station monitoring and replay databases. Run mutations require a bearer token.
WebSockets require the same token as a query parameter and an exact Origin from
the configured allowlist.

Install dependencies only in the project virtual environment:

```bash
cd ~/groundlink
.venv/bin/python -m pip install -r mission_control/server/requirements.txt
```

Start locally (the loopback binding is intentional):

```bash
cd ~/groundlink
GL_MISSION_CONTROL_TOKEN='replace-with-at-least-16-random-characters' \
  .venv/bin/uvicorn mission_control.server.main:app --host 127.0.0.1 --port 8000
```

Optional variables are `GL_MISSION_CONTROL_DB` and the comma-separated
`GL_MISSION_CONTROL_ORIGINS`. Do not bind beyond loopback until transport
authentication and deployment controls are designed.

`POST /api/v1/runs` accepts `{"scenario":"baseline","mode":"MOCK"}` or
`LIVE`. MOCK creates two explicitly synthetic, decision-free Satellite/Channel
events for API/WebSocket testing. LIVE persists a `NOT_READY` run with zero
events because authenticated VM agents do not exist yet. No MOCK path can emit
a Station security decision.

Example local verification (replace `$TOKEN` with the startup token):

```bash
curl -s http://127.0.0.1:8000/api/v1/health
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"scenario":"baseline","mode":"MOCK"}' \
  http://127.0.0.1:8000/api/v1/runs
.venv/bin/python -c 'import os; from websockets.sync.client import connect; u="ws://127.0.0.1:8000/ws/v1/runs/{run}?token={token}".format(run=os.environ["RUN_ID"], token=os.environ["TOKEN"]); w=connect(u, origin="http://127.0.0.1:8000"); print(w.recv()); print(w.recv()); w.close()'
```

The final command replays the run's ordered MOCK events. Stop with
`POST /api/v1/runs/RUN_ID/stop` and the bearer header. The server contains no
command runner, subprocess launcher, SSH API, or VM agent.

## Phase 1C frontend

The buildless frontend is served at `http://127.0.0.1:8000/` from
`mission_control/frontend/`. It uses original inline SVG, CSS and vanilla ES
modules; no frontend dependency installation or build step is required.

The operator enters the control token once. The browser sends it to the
same-origin `/api/v1/browser-session` bridge and immediately clears the input.
The bridge returns a process-local, one-hour, HttpOnly, SameSite=Strict cookie.
Cookie-authorized mutations require an allowlisted Origin, and WebSockets
require both that session cookie and the existing origin check. The token is
never embedded in frontend files or stored in browser storage.

The v1 contract supports `baseline`, `awgn_20db`, `awgn_0db`, `jamming`,
`malformed`, `replay`, and `spoofing` controls. AWGN 10 and both fading controls
are displayed but disabled because no matching backend scenario IDs exist.
LIVE is disabled while `/health` reports `NOT_READY`.

Satellite and Channel animation is driven only by received events. Scenario
metadata may change ambient RF presentation, but cannot change the Station
verdict. ACCEPT or REJECT is rendered only for a `station.frame_decided` event
whose `decision_source` is `station_receiver`; otherwise the UI stays PENDING.

## Phase 2B.1 shared agent contract

The versioned, transport-neutral contract for future VM agents is documented
in [AGENT_CONTRACT.md](AGENT_CONTRACT.md). Its reference models and JSON Schema
are `agent_contract.py` and `schemas/agent-contract-v1.schema.json`. This phase
does not implement agents, add ingestion routes, or change LIVE readiness.
