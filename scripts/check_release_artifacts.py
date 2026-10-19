"""Check built sdist/wheel boundaries without installing or running either archive."""

from __future__ import annotations

import argparse
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_DIRECTORIES = {".git", ".venv", ".claude", ".agents", ".codex", "data", "__pycache__"}
PRIVATE_SUFFIXES = (".log", ".sqlite", ".sqlite3", ".db", ".bak", ".pem", ".key", ".pyc")


def check_names(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise AssertionError(f"Unsafe archive path: {name}")
        # Wheel data-files use <distribution>.data/data/<install-prefix>.
        # That standard installation prefix is not the gateway's runtime data/.
        private_parts = [
            part
            for index, part in enumerate(path.parts)
            if not (index == 1 and part == "data" and path.parts[0].endswith(".data"))
        ]
        if any(part in PRIVATE_DIRECTORIES for part in private_parts):
            raise AssertionError(f"Private/generated directory in archive: {name}")
        if any(part.startswith(".env") or part.upper().startswith("HANDOFF") for part in path.parts):
            raise AssertionError(f"Private local file in archive: {name}")
        if path.name.endswith(PRIVATE_SUFFIXES) or ".sqlite3-" in path.name:
            raise AssertionError(f"Runtime/credential file in archive: {name}")


def check(sdist: Path, wheel: Path) -> dict[str, int | str]:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        if any(not (item.isfile() or item.isdir()) for item in members):
            raise AssertionError("Source archive contains a link or special file")
        source_names = [item.name for item in members]
        check_names(source_names)
        if not any(name.endswith("/THIRD_PARTY_NOTICES.md") for name in source_names):
            raise AssertionError("Source archive is missing third-party notices")
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        check_names(wheel_names)
        notice = "/share/doc/monid-x402-xrpl/THIRD_PARTY_NOTICES.md"
        if not any(name.endswith(notice) for name in wheel_names):
            raise AssertionError("Wheel is missing third-party notices")
        if "gateway/main.py" not in wheel_names:
            raise AssertionError("Wheel is missing the gateway entrypoint")
    return {"sdist_members": len(source_names), "wheel_members": len(wheel_names), "result": "passed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdist", type=Path)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.sdist, args.wheel)))


if __name__ == "__main__":
    main()
