# GroundLink command transport protocol v1

Phase 2B.8 defines the Station-side command client and wire protocol. It does
not provide a production agent listener, start a service, or change LIVE from
`NOT_READY`.

## Wire format

The protocol version is `1.0`; embedded commands use the immutable shared agent
contract `1.0.0`. Each TLS connection carries one request and one response,
framed by a four-byte unsigned network-order length. Either JSON document is
limited to 16,384 bytes.

A request contains exactly:

```json
{
  "protocol_version": "1.0",
  "request_id": "station-generated-unique-id",
  "sent_at": "2026-11-01T12:00:00+00:00",
  "deadline_at": "2026-11-01T12:00:03+00:00",
  "target_agent_id": "vm1-satellite",
  "command": {
    "contract_version": "1.0.0",
    "command_type": "PREPARE",
    "command_id": "station-generated-command-id",
    "run_id": "0123456789abcdef0123456789abcdef",
    "scenario": "baseline",
    "issued_at": "2026-11-01T12:00:00+00:00"
  }
}
```

The only command types are PREPARE, START, and STOP. START additionally carries
`preparation_id`; STOP carries `reason`. Unknown fields and any executable,
shell, path, argv, environment, or script input are rejected.

Acknowledgements contain exactly protocol/request identity, authenticated agent
ID, command ID/type, run ID, scenario, accepted flag, resulting state,
preparation ID, reason, and observation timestamp. Station validates every
correlation field. An acknowledgement is not process readiness: the coordinator
still waits for an independently authenticated readiness observation.

The machine-readable definition is
`schemas/command-transport-v1.schema.json`. It is separate from—and does not
modify—the distributed agent-contract archive.

## TLS and network deployment

The proposed dedicated command port is TCP 7443 on both agent management
addresses:

| Agent | Endpoint | Required server DNS SAN |
|---|---|---|
| Satellite | `10.10.3.10:7443` | `vm1-satellite` |
| Channel | `10.10.3.30:7443` | `vm3-channel` |

The Station connects from `10.10.3.20` and presents a dedicated client leaf
whose sole DNS identity is `vm2-station`. Agents must accept that identity only.
Station connects to the management IP but supplies the expected DNS SAN for TLS
hostname verification. TLS 1.3, private-CA validation, expiry validation, and
client certificates are mandatory; plaintext fallback is forbidden.

No firewall change is performed by this phase. Future rules should permit the
command port only from `10.10.3.20`, while agent listeners bind only their
management interface and run as dedicated unprivileged accounts.

## Agent responsibilities

Each production agent command server must:

1. Validate the Station certificate and exact `vm2-station` SAN.
2. Enforce the 16 KiB framing limit, protocol version, deadline, schema, target,
   run ID, scenario, and finite-state transition before any action.
3. Persist `(command_id, canonical command, result)` before returning. An
   identical duplicate returns the stored result in an acknowledgement bound to
   the current request ID. Reusing a command ID for different command content is
   rejected and audited.
4. Persist preparation/run state safely across restart. START accepts only the
   preparation ID returned by PREPARE for that run and scenario.
5. Serialize state-changing commands and reject unsafe concurrency.
6. Make STOP idempotent and bounded. Failure or timeout must default to a safe
   non-running state where possible.
7. Report actual process readiness separately over authenticated readiness
   transport. TLS connection or command acceptance is not readiness.
8. Expose no generic RPC, shell, SSH command API, executable path, or arbitrary
   process arguments.

VM1 implements Satellite preparation and starts transmission only after START.
VM3 implements Channel preparation and starts forwarding/attack behavior only
after START. Both must remain inert before a valid prepared run.

## Test versus production

`tests/tls_command_server.py` is an in-memory simulator that binds exclusively
to `127.0.0.1`, generates no credentials itself, and launches no process. It is
not a production server and must not be installed or imported by agent services.
Tests create certificates only in temporary directories.

Production remains blocked on agent-side persistent command servers, PKI and
revocation deployment, OS service identities, firewall approval, persistent
coordination recovery, and three-VM negative testing.
