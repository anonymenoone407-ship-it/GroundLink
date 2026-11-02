# VM integration requirements

## Fixed identities

- VM1: certificate/server identity `vm1-satellite`, role `SATELLITE`.
- VM3: certificate/server identity `vm3-channel`, role `CHANNEL`.
- VM2 command client: certificate identity `vm2-station`, role `STATION`.

An agent must derive Station identity from a verified TLS 1.3 client certificate,
not JSON or headers. Station verifies the agent's exact DNS SAN while connecting
to its management IP. The proposed command endpoint is TCP 7443 on each agent;
this package does not open it.

## Parsing and framing

Use `command_request_from_dict` before state changes. The outer request and
response are four-byte unsigned network-order lengths followed by UTF-8 JSON.
Reject lengths outside `1..16384`, unknown fields, unknown versions, expired
deadlines, wrong target identity, and trailing/multiple messages.

Only PREPARE, START, and STOP exist. There is no executable, shell, path, argv,
environment, script, generic RPC, or SSH command field.

## State and idempotency

Persist the canonical command and its result keyed by `command_id` before
acknowledging. An identical duplicate returns the stored result in an
acknowledgement bound to the current `request_id`. A different command using an
existing command ID is a collision and must fail closed.

PREPARE binds one run ID and scenario and returns a unique `preparation_id`.
START is accepted only for that exact run, scenario, and preparation ID. STOP is
bounded and idempotent for its correlated run. Serialize state transitions and
persist sufficient state to recover safely after restart.

Acknowledgement acceptance is not process readiness. Report actual readiness
through the separately authenticated readiness channel.

## VM1 Satellite

Remain inert until valid START. PREPARE validates resources without transmitting.
START begins only the preconfigured Satellite operation for the correlated run.
STOP safely ends it. Emit no Station decisions.

## VM3 Channel

Remain inert until valid START. PREPARE validates the fixed scenario. START
enables only the preconfigured Channel behavior for the correlated run. Preserve
FORWARD, DROP, and ATTACK as Channel actions; never infer Station reception or a
security decision.

