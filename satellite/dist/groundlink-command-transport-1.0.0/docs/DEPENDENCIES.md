# Dependencies and package closure

Runtime requires Python 3.10 or newer and only standard-library modules:
`dataclasses`, `datetime`, `enum`, `math`, `re`, `typing`, and `uuid`.

`groundlink_command_transport.command_protocol` depends on the bundled:

- `agent_contract.py`
- `models.py`
- `run_id.py`
- `scenarios.py`

These are copied from Station's approved agent contract 1.0.0 implementation so
the command module is not installed in isolation. No Station server, FastAPI,
Uvicorn, SQLite, TLS key, RF, cryptographic, or dashboard module is included.

The portable compatibility tests also use only `unittest`, `json`, and
`pathlib`. JSON Schema validation tooling is optional; the authoritative runtime
parsers remain strict even when `jsonschema` is not installed.

