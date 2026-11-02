"""Build the secret-free GroundLink command transport distribution."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
import shutil
import tarfile
import tempfile


VERSION = "1.0.0"
ROOT_NAME = f"groundlink-command-transport-{VERSION}"
REPOSITORY = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent / "command_transport"
DIST = REPOSITORY / "dist"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def build() -> tuple[Path, Path]:
    DIST.mkdir(mode=0o755, exist_ok=True)
    archive = DIST / f"{ROOT_NAME}.tar.gz"
    detached = DIST / f"{ROOT_NAME}.tar.gz.sha256"
    with tempfile.TemporaryDirectory(prefix="groundlink-command-package-") as temporary:
        package_root = Path(temporary) / ROOT_NAME
        runtime = package_root / "groundlink_command_transport"
        runtime.mkdir(parents=True)
        for name in ("command_protocol.py", "agent_contract.py", "models.py", "run_id.py", "scenarios.py"):
            shutil.copyfile(REPOSITORY / "mission_control" / name, runtime / name)
        (runtime / "__init__.py").write_text(
            '"""GroundLink command transport protocol 1.0 / contract 1.0.0."""\n'
            '__version__ = "1.0.0"\n', encoding="utf-8"
        )
        shutil.copytree(ASSETS / "tests", package_root / "tests")
        shutil.copytree(ASSETS / "vectors", package_root / "vectors")
        shutil.copyfile(ASSETS / "README.md", package_root / "README.md")
        docs = package_root / "docs"
        docs.mkdir()
        shutil.copyfile(ASSETS / "DEPENDENCIES.md", docs / "DEPENDENCIES.md")
        shutil.copyfile(ASSETS / "INTEGRATION.md", docs / "INTEGRATION.md")
        shutil.copyfile(REPOSITORY / "mission_control" / "COMMAND_TRANSPORT.md", docs / "COMMAND_TRANSPORT.md")
        schemas = package_root / "schemas"
        schemas.mkdir()
        shutil.copyfile(
            REPOSITORY / "mission_control" / "schemas" / "command-transport-v1.schema.json",
            schemas / "command-transport-v1.schema.json",
        )

        files = sorted(path for path in package_root.rglob("*") if path.is_file())
        manifest = package_root / "MANIFEST.sha256"
        manifest.write_text(
            "".join(f"{digest(path)}  {path.relative_to(package_root).as_posix()}\n" for path in files),
            encoding="utf-8",
        )

        with archive.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for path in sorted([package_root, *package_root.rglob("*")]):
                        info = tar.gettarinfo(str(path), arcname=str(path.relative_to(package_root.parent)))
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        info.mtime = 0
                        if path.is_file():
                            with path.open("rb") as source:
                                tar.addfile(info, source)
                        else:
                            tar.addfile(info)

    detached.write_text(f"{digest(archive)}  {archive.name}\n", encoding="utf-8")
    return archive, detached


if __name__ == "__main__":
    for result in build():
        print(result)
