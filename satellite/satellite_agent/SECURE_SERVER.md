# Satellite secure command server

The implementation is intentionally disabled by default. `ServerConfig()` has
`enabled=False`; no executable entry point, daemon configuration, or service
unit is provided. Tests opt in only with `host=127.0.0.1` and an ephemeral port.

## TLS and identity

`build_server_ssl_context` pins TLS 1.3, requires a client certificate, loads a
private CA and mandatory CRL, and enables full-chain CRL checking. OpenSSL
performs chain, validity-period, signature, and revocation validation. After
the handshake, the application requires the complete client SAN set to be
exactly `DNS:vm2-station`; extra or non-DNS SANs fail closed. It preflights the
server certificate in the same way for exactly `DNS:vm1-satellite`. JSON
identity assertions are not part of the closed wire schema and are rejected.

Each connection accepts one four-byte network-order length and one JSON request
of at most 16,384 bytes. Reads, writes, and handshakes have time limits.
Connection concurrency and per-address connection/request rates are bounded.
Audit output contains only event class, peer address, bounded IDs, command type,
state, and exception class—never commands, certificates, or credentials.

## Persistence and recovery

`DurableCommandProcessor` uses a separate SQLite database with WAL and
`synchronous=FULL`. It stores canonical command JSON and the accepted or
rejected result in the same transaction as logical state, before creating the
acknowledgement. Identical command retries replay the stored outcome with the
new request ID. Changed content under an existing command ID is rejected and
cannot overwrite the original.

PREPARED state is recoverable. Because no transmitter supervisor exists,
RUNNING, STOPPING, and ERROR recover conservatively to IDLE and the interrupted
run cannot be reused. SQLite cannot by itself make a future external process
side effect atomic; real integration requires a supervisor with a durable
operation token and verified PID/process identity reconciliation.

## Readiness and deployment gate

`ReadinessProvider` accepts only the authenticated peer identity
`vm2-station`. It distinguishes agent-process online, PREPARED, logical state,
and transmitter-process readiness. Transmitter readiness and contract
readiness remain false. The command protocol contains no readiness request, so
the provider is not exposed through an invented wire message; Station needs an
authenticated contract-defined telemetry transport in a later phase.

Deployment still requires production CA/leaf/CRL provisioning, protected key
permissions, CRL refresh/fail-closed policy, a dedicated unprivileged account,
approved firewall rules from `10.10.3.20`, audit destination/rotation,
database ownership/backups, resource limits, monitoring, and three-VM negative
tests. Only then should an explicit launcher set `enabled=True` and bind
`10.10.3.10:7443`.
