# GroundLink command transport 1.0.0

This portable bundle contains command transport protocol `1.0` implemented
against GroundLink agent contract `1.0.0`. It is intended for VM1 Satellite,
VM3 Channel, and compatibility verification against VM2 Station.

The bundle is protocol code and documentation only. It contains no TLS keys,
certificates, credentials, network listener, process launcher, or LIVE control.

Run the portable tests from the extracted archive root:

```bash
python3 -m unittest discover -s tests -v
sha256sum -c MANIFEST.sha256
```

Python 3.10 or newer is required. Runtime code uses only the Python standard
library.

