from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_release_artifacts import check


def archives(tmp_path, fault=None):
    root = Path(__file__).resolve().parents[1]
    license_text = (root / "LICENSE").read_bytes()
    notices = (root / "THIRD_PARTY_NOTICES.md").read_bytes()
    source = {"example/LICENSE": license_text, "example/THIRD_PARTY_NOTICES.md": notices}
    prefix = "example-0.1.dist-info/"
    wheel = {
        "gateway/main.py": b"",
        "example-0.1.data/data/share/doc/monid-x402-xrpl/THIRD_PARTY_NOTICES.md": notices,
        prefix + "licenses/LICENSE": license_text,
        prefix + "licenses/THIRD_PARTY_NOTICES.md": notices,
        prefix + "METADATA": b"Metadata-Version: 2.4\nLicense-Expression: MIT\n"
        b"License-File: LICENSE\nLicense-File: THIRD_PARTY_NOTICES.md\n",
    }
    if fault == "missing_source_license":
        del source["example/LICENSE"]
    elif fault == "missing_wheel_license":
        del wheel[prefix + "licenses/LICENSE"]
    elif fault == "wrong_expression":
        wheel[prefix + "METADATA"] = wheel[prefix + "METADATA"].replace(b"MIT", b"UNKNOWN")
    elif fault == "changed_license":
        wheel[prefix + "licenses/LICENSE"] = b"incomplete grant"
    elif fault == "private_file":
        source["example/data/e2e-wallets.json"] = b"synthetic canary"
    sdist_path, wheel_path = tmp_path / "example.tar.gz", tmp_path / "example.whl"
    with tarfile.open(sdist_path, "w:gz") as archive:
        for name, contents in source.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, contents in wheel.items():
            archive.writestr(name, contents)
    return sdist_path, wheel_path


def test_release_artifacts_include_exact_mit_and_notice(tmp_path):
    assert check(*archives(tmp_path))["license_expression"] == "MIT"


@pytest.mark.parametrize("fault", [
    "missing_source_license", "missing_wheel_license", "wrong_expression",
    "changed_license", "private_file",
])
def test_release_artifacts_reject_incomplete_licensing_or_private_files(tmp_path, fault):
    with pytest.raises(AssertionError):
        check(*archives(tmp_path, fault))
