# Secure readiness and LIVE coordination preparation

Phase 2B.7 is a local model only. It neither activates a LIVE database run nor
connects to an agent control service. Mission Control continues to report LIVE
as `NOT_READY`.

## Readiness

`ReadinessRegistry` combines four independent facts:

1. A transport-authenticated fixed agent identity (or trusted local provenance
   for the Station receiver).
2. Exact agent-contract and control-protocol compatibility.
3. A fresh, non-replayed heartbeat whose state agrees with readiness.
4. Explicit process readiness, distinct from connection/agent readiness.

Satellite, Channel, and Station must all be fresh, ready, process-ready, IDLE,
and unbound to a run before coordination may begin. A connection by itself
never enables LIVE.

## Local state machine

The only command models are contract `PREPARE`, `START`, and `STOP`. Injected
`AgentControl` implementations are called in this order:

```text
PREPARE: Station → Channel → Satellite
START:   Station → Channel → Satellite
STOP:    Satellite → Channel → Station
```

Every PREPARE acknowledgement must return a preparation ID. START carries that
exact ID and waits for an explicit RUNNING observation. Rejection, mismatch, or
timeout causes reverse-order STOP rollback. STOP is safe and idempotent at the
coordinator. No command contains an executable, argument vector, environment,
path, shell fragment, or arbitrary operation.

## Missing secure command transport

Telemetry TLS is intentionally one-way agent-to-Station and cannot carry
outbound control commands. A future command client on Station and command
listener on each agent must provide:

- TLS 1.3 mutual authentication using the private CA;
- server SAN verification for `vm1-satellite` or `vm3-channel`, and client SAN
  verification for `vm2-station`;
- strict `command_from_dict` validation and the fixed command allowlist;
- Station-generated request IDs, timestamps, persistent replay protection, and
  idempotent command-ID result caching on each agent;
- acknowledgements bound to command ID, run ID, scenario, preparation ID, and
  authenticated agent identity;
- bounded request size, connection/rate limits, deadlines, and fail-closed
  cancellation;
- no HTTP certificate headers, generic RPC, remote shell, SSH command API, or
  unauthenticated management listener.

This control service must remain disabled until its independent threat review,
certificate deployment, three-VM negative testing, and operator approval are
complete.
