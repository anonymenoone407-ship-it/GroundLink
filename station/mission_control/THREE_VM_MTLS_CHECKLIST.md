# Three-VM mTLS operator checklist

This checklist does not authorize LIVE. Keep boxes unchecked until verified.

## PKI

- [ ] CA host is separate from VM1, VM2, and VM3; its private key was never copied to them.
- [ ] Every service key was generated on and remains on its owning VM.
- [ ] Every CSR and leaf has exactly one approved DNS SAN and no wildcard.
- [ ] Command server leaves have serverAuth only; the Station command leaf has clientAuth only.
- [ ] Key usage is critical digitalSignature; algorithms and sizes meet policy.
- [ ] CA certificate and CRL fingerprints were verified out of band.
- [ ] CRL signature and `nextUpdate` cover the test window.
- [ ] Rotation and emergency-revocation owners are assigned.

## VM1 Satellite

- [ ] Command SAN is `vm1-satellite`; private-key permissions are least privilege.
- [ ] CA and current CRL are installed read-only.
- [ ] Configuration binds only `10.10.3.10:7443`.
- [ ] Listener remains disabled until Stage 3 approval.

## VM2 Station

- [ ] Command client SAN is `vm2-station`; EKU is clientAuth only.
- [ ] `CommandTLSConfig` references CA, CRL, leaf, and private key.
- [ ] Satellite expectation is `vm1-satellite`; Channel is `vm3-channel`.
- [ ] CRL, wrong-SAN, wrong-EKU, expiry, and downgrade tests pass.
- [ ] Mission Control LIVE remains `NOT_READY`.

## VM3 Channel

- [ ] Command SAN is `vm3-channel`; private-key permissions are least privilege.
- [ ] CA and current CRL are installed read-only.
- [ ] Configuration binds only `10.10.3.30:7443`.
- [ ] Listener remains disabled until Stage 3 approval.

## Network and closure

- [ ] Proposed rules permit TCP 7443 only from `10.10.3.20`.
- [ ] No listener binds NAT, IQ, wildcard, or public addresses.
- [ ] SSH, NAT Internet, and IQ ZeroMQ paths are unchanged.
- [ ] IPv4 and IPv6 forwarding remain disabled.
- [ ] Tests use no real RF or process execution.
- [ ] Listeners are stopped and `ss` shows port 7443 closed after testing.
- [ ] Any separately approved firewall changes are rolled back or recorded.
