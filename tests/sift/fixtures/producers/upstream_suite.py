"""Intentionally failing fixture producer. Not part of any test suite: run only to regenerate fixtures."""

import json
import logging

import pytest


@pytest.mark.parametrize("case", range(12))
def test_checkout(case):
    for attempt in range(25):
        print(json.dumps({"event": "cache_probe", "case": case, "attempt": attempt, "ok": True}))
    if case in (3, 9):
        logging.error("checksum mismatch for café/注文/%s: expected 41, received 42", case)
        assert 41 == 42, f"invalid invoice checksum for order {case}"


@pytest.fixture
def unavailable_database():
    raise ConnectionError("database checkout-db.local refused connection (fixture setup)")


def test_database(unavailable_database):
    assert unavailable_database
