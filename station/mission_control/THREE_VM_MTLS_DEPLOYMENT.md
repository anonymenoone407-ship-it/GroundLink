# GroundLink three-VM mTLS deployment preflight

Phase 2B.10A does not enable a listener, firewall rule, process, or LIVE state.

## Interoperability and certificate policy

Command transport is protocol `1.0`, embeds agent contract `1.0.0`, uses one
length-prefixed request/acknowledgement per TLS connection, and caps either JSON
document at 16,384 bytes. Station correlates request ID, command ID/type, agent
ID, run ID, scenario, and preparation ID. Retries reuse the request; agents
persist canonical command results and reject command-ID collisions.

| Connection | Exact server DNS SAN | Exact client DNS SAN | EKU |
|---|---|---|---|
| VM2 → VM1 | `vm1-satellite` | `vm2-station` | serverAuth / clientAuth |
| VM2 → VM3 | `vm3-channel` | `vm2-station` | serverAuth / clientAuth |

Each leaf must have exactly one DNS SAN, no wildcard, critical digitalSignature
key usage, and only its service EKU. Approved keys are ECDSA P-256 or stronger,
or RSA 3072 or stronger. Sign certificates and CRLs with SHA-256 or stronger.
CN is descriptive only and never an identity source.

Station now requires a PEM CRL and enables OpenSSL `VERIFY_CRL_CHECK_LEAF`.
Missing, malformed, expired, or revoking CRLs fail closed. After OpenSSL
chain/expiry/hostname/CRL checks, Station independently requires the exact
single SAN, serverAuth-only EKU, valid key usage, and approved key size before
sending command bytes. Remote servers must equivalently validate the Station
leaf and CRL. Source IP is defense in depth, not authentication.

## Private PKI and credential separation

Manage the CA offline/outside this repository. Its private key and writable CA
database never go to VM1, VM2, or VM3. Each VM generates its own private keys
and exports only CSRs. Track serial, service, owner, issue/expiry, and status in
the operator CA inventory.

Use separate keys/leaves for:

- VM2 command client: SAN `vm2-station`, clientAuth.
- VM1 command server: SAN `vm1-satellite`, serverAuth.
- VM3 command server: SAN `vm3-channel`, serverAuth.
- Future VM1 and VM3 telemetry clients: separate clientAuth keys/leaves with
  their respective SANs.
- Future VM2 telemetry ingestion server: separate serverAuth key/leaf with SAN
  `vm2-station`.

## Ubuntu filesystem layout

During an approved deployment window, run on **each VM**, substituting the real
dedicated service group (`groundlink-mc` on VM2 if applicable):

```bash
sudo install -d -o root -g root -m 0755 /etc/groundlink/pki/{ca,crl,certs}
sudo install -d -o root -g groundlink-agent -m 0750 /etc/groundlink/private
sudo install -d -o groundlink-agent -g groundlink-agent -m 0700 /var/lib/groundlink-command
```

CA, CRL, and leaf certificates: root-owned `0644`. Private keys: root-owned and
service-group-readable `0640`, or service-owned `0600`. Key directories must not
be group/world writable.

## Local key and CSR workflow

The following are future operator commands, not commands to run in this phase.

On **VM2 Station (10.10.3.20)**:

```bash
sudo openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out /etc/groundlink/private/station-command-client.key
sudo chmod 0640 /etc/groundlink/private/station-command-client.key
sudo openssl req -new -key /etc/groundlink/private/station-command-client.key \
  -subj '/CN=GroundLink Station Command Client' \
  -addext 'subjectAltName=DNS:vm2-station' \
  -addext 'keyUsage=critical,digitalSignature' \
  -addext 'extendedKeyUsage=clientAuth' -out /tmp/station-command-client.csr.pem
```

On **VM1 Satellite (10.10.3.10)**:

```bash
sudo openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out /etc/groundlink/private/satellite-command-server.key
sudo chmod 0640 /etc/groundlink/private/satellite-command-server.key
sudo openssl req -new -key /etc/groundlink/private/satellite-command-server.key \
  -subj '/CN=GroundLink Satellite Command Server' \
  -addext 'subjectAltName=DNS:vm1-satellite' \
  -addext 'keyUsage=critical,digitalSignature' \
  -addext 'extendedKeyUsage=serverAuth' -out /tmp/satellite-command-server.csr.pem
```

On **VM3 Channel (10.10.3.30)**:

```bash
sudo openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out /etc/groundlink/private/channel-command-server.key
sudo chmod 0640 /etc/groundlink/private/channel-command-server.key
sudo openssl req -new -key /etc/groundlink/private/channel-command-server.key \
  -subj '/CN=GroundLink Channel Command Server' \
  -addext 'subjectAltName=DNS:vm3-channel' \
  -addext 'keyUsage=critical,digitalSignature' \
  -addext 'extendedKeyUsage=serverAuth' -out /tmp/channel-command-server.csr.pem
```

Inspect locally with `openssl req -in CSR -noout -text -verify`. Transfer only
CSRs to the offline CA through the approved authenticated/offline process.

## Offline signing, CRL, rotation, and revocation

On the **offline CA host only**, validate every CSR and sign through an OpenSSL
CA database. Service-specific extension sections must override CSR extensions;
do not use `copy_extensions = copy`.

```bash
openssl req -in incoming.csr.pem -noout -text -verify
openssl ca -config /srv/groundlink-ca/openssl.cnf -extensions command_server -days 90 -md sha256 -in incoming.csr.pem -out issued-server.pem
openssl ca -config /srv/groundlink-ca/openssl.cnf -extensions command_client -days 90 -md sha256 -in incoming.csr.pem -out issued-client.pem
openssl ca -config /srv/groundlink-ca/openssl.cnf -gencrl -out /srv/groundlink-ca/export/groundlink-lab-ca.crl.pem
```

Set CRL `nextUpdate` to a bounded operational interval, such as seven days.
Publish only the CA certificate, issued leaf, and signed CRL. For compromise:

```bash
openssl ca -config /srv/groundlink-ca/openssl.cnf -revoke compromised.pem -crl_reason keyCompromise
openssl ca -config /srv/groundlink-ca/openssl.cnf -gencrl -out /srv/groundlink-ca/export/groundlink-lab-ca.crl.pem
```

Distribute the CRL atomically to every verifier and reload services only after
validation. For rotation, create a new local key/CSR and install versioned files;
retain the previous valid, unrevoked pair for rollback until verification ends.

## Public-material installation and validation

On **each VM**, verify operator-published SHA-256 values out of band, then run:

```bash
sha256sum groundlink-lab-ca.pem groundlink-lab-ca.crl.pem issued-leaf.pem
openssl verify -crl_check -CAfile groundlink-lab-ca.pem -CRLfile groundlink-lab-ca.crl.pem issued-leaf.pem
openssl crl -in groundlink-lab-ca.crl.pem -noout -issuer -lastupdate -nextupdate
openssl x509 -in issued-leaf.pem -noout -subject -issuer -serial -dates -ext subjectAltName -ext extendedKeyUsage -ext keyUsage
```

Install public files with `sudo install -o root -g root -m 0644`. Compare public
keys without displaying private material:

```bash
openssl x509 -in issued-leaf.pem -pubkey -noout | sha256sum
openssl pkey -in /etc/groundlink/private/SERVICE.key -pubout | sha256sum
```

The hashes must match.

## Station and agent configuration

On **VM2**, configure:

```python
CommandTLSConfig(
    ca_certificate=Path('/etc/groundlink/pki/ca/groundlink-lab-ca.pem'),
    certificate_revocation_list=Path('/etc/groundlink/pki/crl/groundlink-lab-ca.crl.pem'),
    station_certificate=Path('/etc/groundlink/pki/certs/station-command-client.pem'),
    station_private_key=Path('/etc/groundlink/private/station-command-client.key'),
    connect_timeout=3.0, io_timeout=3.0, retries=1,
)
```

Create clients for `10.10.3.10:7443` expecting `vm1-satellite` and
`10.10.3.30:7443` expecting `vm3-channel`. Never use an IP as `server_hostname`.
The provided example remains `"enabled": false`.

On **VM1**, future approved configuration binds exactly `10.10.3.10:7443`; on
**VM3**, exactly `10.10.3.30:7443`. Each server trusts the CA/current CRL,
requires a client certificate, and accepts only exact SAN `vm2-station` with
clientAuth. Never bind `0.0.0.0`, an IQ address, or a NAT interface.

## Read-only preflight

Run on **each VM** before approval:

```bash
ip -br address
ip route
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
ss -lntp
stat -c '%U %G %a %n' /etc/groundlink/pki/ca/* /etc/groundlink/pki/crl/* /etc/groundlink/pki/certs/* /etc/groundlink/private/*
openssl crl -in /etc/groundlink/pki/crl/groundlink-lab-ca.crl.pem -noout -issuer -lastupdate -nextupdate
```

Forwarding must be `0`. Before Stage 3, no TCP 7443 listener may exist. On VM2,
run the Mission Control and isolated command TLS suites. Confirm LIVE is still
`NOT_READY`.

## Minimum network policy proposal

No firewall rule is changed here. After separate approval:

- VM1 permits TCP destination `10.10.3.10:7443` only from `10.10.3.20` on its
  management interface and denies other sources to that port.
- VM3 permits TCP destination `10.10.3.30:7443` only from `10.10.3.20` on its
  management interface and denies other sources.
- VM2 needs no inbound command port.

Preserve SSH, NAT Internet, and IQ ZeroMQ rules and default policies. Capture
`sudo ufw status numbered` and `sudo nft list ruleset` first. Proposed commands,
**not to run during preflight**, are:

```bash
# VM1 only, after identifying MANAGEMENT_IF
sudo ufw allow in on MANAGEMENT_IF proto tcp from 10.10.3.20 to 10.10.3.10 port 7443
# VM3 only, after identifying MANAGEMENT_IF
sudo ufw allow in on MANAGEMENT_IF proto tcp from 10.10.3.20 to 10.10.3.30 port 7443
```

Rollback uses the exact matching `sudo ufw delete allow ...` command or restores
the operator-captured nftables configuration. Never flush all rules.

## Staged test and shutdown

1. Validate chains, CRLs, SAN/EKU/key usage, key matching, permissions, clocks,
   versions, management addresses, forwarding, and closed ports.
2. On VM2 run local protocol, mTLS, revocation, SAN/EKU, downgrade, replay,
   timeout, and shutdown tests.
3. Only with explicit approval, apply narrow rules and start test-mode listeners
   on VM1/VM3 management addresses. Test adapters must launch no real process.
4. Test VM2→VM1 mTLS and a harmless test-mode PREPARE/STOP.
5. Test VM2→VM3 independently.
6. Exercise logical PREPARE/START/STOP with no-op process adapters. Logical
   RUNNING is not transmitter, receiver, IQ, RF, or security-decision readiness.
7. Test revoked/expired/untrusted certificates, wrong/ambiguous identities,
   missing/expired CRL, stale command, replay/collision, wrong run/scenario/
   preparation, timeout, concurrency, rollback, and disconnect.
8. Stop listeners and verify `ss -lntp` has no 7443 entry. Remove only temporary
   approved rules; recheck SSH/NAT/IQ and forwarding. Preserve secret-free logs.

Dashboard LIVE remains disabled throughout.

The proposed test-only systemd unit names are
`groundlink-satellite-command-test.service` on VM1 and
`groundlink-channel-command-test.service` on VM3. They must be reviewed to
confirm test/no-op process adapters and exact management binds before use. Only
after Stage 3 approval:

```bash
# VM1 only
sudo systemctl start groundlink-satellite-command-test.service
sudo systemctl --no-pager --full status groundlink-satellite-command-test.service
ss -lntp '( sport = :7443 )'

# VM3 only
sudo systemctl start groundlink-channel-command-test.service
sudo systemctl --no-pager --full status groundlink-channel-command-test.service
ss -lntp '( sport = :7443 )'
```

Shutdown is always explicit:

```bash
# VM1 only
sudo systemctl stop groundlink-satellite-command-test.service
sudo systemctl disable groundlink-satellite-command-test.service

# VM3 only
sudo systemctl stop groundlink-channel-command-test.service
sudo systemctl disable groundlink-channel-command-test.service

# Verify on each agent
ss -lntp '( sport = :7443 )'
```

If these reviewed unit names do not exist, stop and create/review units in a
separate implementation phase; do not substitute an unreviewed command line.

## Secure rollback

Stop the affected listener first. Restore only a prior verified, current,
unrevoked leaf/key and a current CRL; never restore a revoked certificate or an
older CRL that removes revocations. Revert only the specifically added firewall
rule. Verify port closure and that existing SSH, NAT, and IQ paths remain intact.
