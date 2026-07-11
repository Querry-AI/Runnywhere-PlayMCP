import pytest
from pathlib import Path

from runart import data_integrity


def test_bundled_data_checksums_match():
    root = Path(__file__).resolve().parents[1] / "data"
    for filename in data_integrity.EXPECTED_SHA256:
        data_integrity.verify_data_file(root / filename)


def test_unregistered_or_tampered_data_is_rejected(tmp_path):
    path = tmp_path / "seoul_graph.pkl"
    path.write_bytes(b"not a trusted graph")
    with pytest.raises(RuntimeError, match="integrity"):
        data_integrity.verify_data_file(path)
