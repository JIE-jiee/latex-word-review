"""JSON I/O rejects tamper, duplicate keys, and overwrites."""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.jsonio import read_contract_file, read_json_file, write_new_json
from tests.test_contracts import _golden_contracts


def test_json_round_trip_and_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "object.json"
    document = _golden_contracts()["ChangeSet"]
    write_new_json(path, document, contract=True)

    assert read_contract_file(path, expected_schema="ChangeSet") == document
    with pytest.raises(ContractError) as raised:
        write_new_json(path, document, contract=True)
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_duplicate_keys_and_wrong_contract_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(ContractError):
        read_json_file(duplicate)

    change = tmp_path / "change.json"
    write_new_json(change, _golden_contracts()["ChangeSet"], contract=True)
    with pytest.raises(ContractError) as raised:
        read_contract_file(change, expected_schema="SourceMap")
    assert raised.value.code is ErrorCode.SCHEMA_INVALID
