"""Integrity checks for trusted build-time data artifacts.

The graph and public-data snapshots are pickle files for fast startup. Pickle
must never be loaded from an unverified source, so production accepts only the
checksums committed with this code. ETL developers may explicitly opt out while
regenerating artifacts with RUNART_ALLOW_UNVERIFIED_DATA=1.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

EXPECTED_SHA256 = {
    "seoul_graph.pkl": "5958a323fe270e64e7e123225b1f0247353bb4d0897b41acb7d5769a6cea5f85",
    "facilities.pkl": "a577f9e39012d2d70827e8ade0f969bf7d12794a83555d7db62eef328f4b2e7f",
    "infra_points.pkl": "cedaed2e22a3860f6d1d29239eb428cb7382ec782861041d29a56921e638f453",
    "animal_station_presets.json.gz": "71a22c5a443d4e9089d5deb63a21d83ae3fefc089d627602a4ae7a9b172c8bab",
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_data_file(path: Path) -> None:
    if os.environ.get("RUNART_ALLOW_UNVERIFIED_DATA") == "1":
        return
    expected = EXPECTED_SHA256.get(path.name)
    if expected is None:
        raise RuntimeError(f"No trusted checksum registered for {path.name}")
    actual = _file_sha256(path)
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError(f"Data integrity check failed for {path.name}")
