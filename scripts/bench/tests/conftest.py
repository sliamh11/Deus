"""Redirect DEUS_BENCH_DB to a tmp file for every test."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolate_bench_db(tmp_path, monkeypatch):
    db = tmp_path / "test_runs.db"
    monkeypatch.setenv("DEUS_BENCH_DB", str(db))
    yield db
