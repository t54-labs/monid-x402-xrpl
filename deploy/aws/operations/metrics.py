#!/usr/bin/env python3
"""Publish fixed-cardinality health metrics, without request or secret contents."""

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


def measurements():
    healthy = 0
    try:
        with urllib.request.urlopen("http://127.0.0.1/health", timeout=5) as response:
            health = json.load(response)
        healthy = int(
            health.get("ok") is True
            and health.get("tools", 0) > 0
            and (Path("/etc/monid/standby").exists() or health.get("paymentsReady") is True)
        )
    except Exception:
        pass
    disk = shutil.disk_usage("/srv/monid/data")
    try:
        age = max(0, (time.time() - float(Path("/srv/monid/backups/last-success").read_text())))
    except (OSError, ValueError):
        age = 1000000000
    return [
        ("HealthOK", healthy, "Count"),
        ("DiskUsedPercent", disk.used / disk.total * 100, "Percent"),
        ("BackupAgeSeconds", age, "Seconds"),
    ]


def main():
    data = [
        {
            "MetricName": name,
            "Value": value,
            "Unit": unit,
            "Dimensions": [{"Name": "InstanceId", "Value": os.environ["INSTANCE_ID"]}],
        }
        for name, value, unit in measurements()
    ]
    subprocess.run(
        [
            "aws",
            "cloudwatch",
            "put-metric-data",
            "--namespace",
            "Monid/Gateway",
            "--metric-data",
            json.dumps(data),
            "--region",
            os.environ["AWS_REGION"],
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    print("Operational metrics published.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Operational metric publication failed.") from None
