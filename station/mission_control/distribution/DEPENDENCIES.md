# Contract dependencies

The shared contract runtime supports Python 3.10 or newer and uses only the
Python standard library. Do not install FastAPI, Uvicorn, cryptography, or RF
packages merely to import and use the contract models.

The supplied contract tests additionally require:

```text
jsonschema==4.26.0
```

Create an isolated virtual environment on each VM; do not install into the
system Python and do not copy VM2's `.venv`:

```bash
python3 -m venv .venv-contract
.venv-contract/bin/python -m pip install --upgrade pip
.venv-contract/bin/python -m pip install 'jsonschema==4.26.0'
```

Runtime code is the `mission_control` package containing `agent_contract.py`,
`models.py`, `run_id.py`, `scenarios.py`, and `__init__.py`. Both schema files
must remain together in `mission_control/schemas/` because the agent schema's
ingestion definition references `telemetry-v1.schema.json` by relative URI.

No credentials, certificates, keys, databases, or environment configuration
are supplied by this archive.
