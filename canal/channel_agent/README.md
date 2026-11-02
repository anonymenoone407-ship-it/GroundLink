# GroundLink VM3 Channel agent core (offline)

This package implements contract `1.0.0` command state and event construction only. It opens no
socket, launches no process, exposes no API, and does not enable LIVE.

## Verified script interface and mapping

| Contract scenario | Fixed program and argv | Confirmed behavior |
|---|---|---|
| `unlabeled` | `channel_rf.py --mode ideal` | forwards valid non-empty IQ blocks unchanged |
| `baseline` | `channel_rf.py --mode ideal` | forwards valid non-empty IQ blocks unchanged |
| `awgn_20db` | `channel_rf.py --mode awgn --snr-db 20` | adds seeded complex AWGN to every forwarded block |
| `awgn_0db` | `channel_rf.py --mode awgn --snr-db 0` | adds seeded complex AWGN to every forwarded block |
| `jamming` | `channel_rf.py --mode interference` | adds the script's default continuous tone |
| `replay` | `channel_replay.py` | captures block 1, substitutes it for block 3, exits after 5 blocks |
| `spoofing` | `channel_spoof.py` | block 3 gets forged SAT-001 data signed by a generated attacker key; exits after 5 blocks |
| `malformed` | `channel_tamper.py` | sequence 3 wind is changed to 99.9, original signature retained and CRC rebuilt; other frames pass intact |

`malformed` is a contract label, not a claim that all malformed behavior equals tampering. The
adapter emits `ATTACK/MALFORMED` only for the script's explicit changed-frame observation.
Contract v1 does not expose RF `drop`, `fading`, or AWGN 10 dB even though `channel_rf.py` can accept
some of those CLI choices. They are deliberately absent from the registry.

`channel_rf.py` also accepts fixed CLI options `--tone-amplitude`, `--tone-frequency`,
`--fading-gain`, `--drop-every`, and `--seed`; none is remotely configurable here. It runs until
SIGINT/error. Replay and spoofing have five-block loops. Tamper runs until SIGINT. All scripts bind
`tcp://10.10.1.1:5555` and connect to `tcp://10.10.2.10:5556`; this package does not change them.

## Event boundary

`TelemetryAdapter` consumes a confirmed, correlated execution observation and creates:

1. an `IngestedTelemetry(ChannelEvent)` for the existing telemetry-v1 path; and
2. a separate `ChannelActionEvent` for the agent-contract path.

No ACCEPT, REJECT, Station event, or decision field exists in this API. Both products retain the
same `run_id` and `frame_id`. An unconfirmed observation is rejected.

`ScriptOutputAdapter` recognizes only the existing scripts' successful TX lines and explicit RF
DROP line. Informational attack lines, RX lines, errors, and scenario selection alone emit nothing.
Because the legacy output has no frame ID or run ID, a future supervisor must correlate each receive
ordinal with Mission Control's assigned frame ID and supply that context; the adapter never invents it.

## Future process supervision requirements

A LIVE implementation must use the immutable registry as an absolute-path executable/argv
allowlist; never accept executable, path, argv, environment, or shell text from a command. Spawn an
unprivileged child in a new process group, capture stdout/stderr as structured evidence, and map an
input frame ID to the corresponding receive ordinal before emitting anything. Require a positive
bind/readiness signal before START succeeds. Apply startup, per-frame, five-frame completion, STOP,
and kill timeouts. On STOP or failure send SIGTERM to the group, wait, escalate to SIGKILL, reap it,
close pipes, and verify port cleanup. Treat early exit, malformed output, correlation gaps, send
timeouts, and partial five-frame runs as errors; never infer an action from the selected scenario.

Before authenticated LIVE control, provision TLS 1.3 mutual authentication (or the contract's
strict signed-bearer alternative), bind the exact `vm3-channel`/`CHANNEL` identity, restrict the
management interface/firewall, validate schema and freshness/nonces, rate/body-limit requests,
protect keys outside the repository, run unprivileged, and audit failures without secrets.

## Disabled secure command server

`ChannelCommandServer` implements command transport protocol `1.0` with the distributed transport
parser and its 16 KiB, one-request/one-response framing. It is disabled unless `enabled=True`, and
this phase additionally refuses every bind address except `127.0.0.1`; it therefore cannot bind the
production management address. There is no plaintext path.

The TLS context permits TLS 1.3 only, requires a private-CA client certificate, enables leaf CRL
checking, requires the server certificate's sole DNS SAN to be `vm3-channel`, and accepts only a
verified client whose sole DNS SAN is `vm2-station`. OpenSSL performs chain, validity-period, key
usage and revocation verification during the handshake. Requests have bounded framing, socket
timeouts, deadline validation and per-source rate limiting. JSON-lines audit records contain only
bounded identifiers, outcome/reason and a truncated certificate fingerprint—never commands, keys,
certificate bodies or payloads.

`DurableCommandProcessor` stores canonical commands, acknowledgements and current run/preparation
state in SQLite using WAL and FULL synchronous durability before returning a response. Identical
command retries return the stored result with the new request ID; a different command using the same
ID is rejected. PREPARED state is recoverable. A restart from RUNNING or STOPPING becomes `ERROR`,
retains its run association, refuses START, and requires a correlated STOP. This prevents a retry
from launching a second process. The current implementation contains no process launcher, so a
RUNNING command acknowledgement is state-machine acceptance, never RF readiness.

Production enablement still requires reviewed certificate/CRL provisioning and renewal, protected
SQLite/audit paths, a dedicated OS account, firewall approval for Station management only, process
supervision and authenticated readiness/event delivery, boot recovery tests, and full three-VM
positive and negative testing. The loopback-only guard must be replaced deliberately at that phase;
do not merely change the default host.
