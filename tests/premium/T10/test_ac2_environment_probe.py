"""T10-AC2: a host that lacks the software, version, license, or data is refused.

The probe is the gate between a valid package and a start. Each refusal is a
different next step for Master: install software, install a supported release,
restore the license record, repair the bundled data, or restore the binary the
package was validated against. The tests inject a runner for the cases that
describe a host, and use a real subprocess for the one that only needs a process
to answer.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.premium.T10.support import schema_document

from ravel.compute.raspa.environment import (
    DEFAULT_COMMAND,
    CommandResult,
    ProbeRefusal,
    RaspaStartRefused,
    probe_environment,
)
from ravel.compute.raspa.provenance import read_bundled, source_record


def a_runner(
    version_line: str, *, exit_code: int = 0
) -> Callable[[tuple[str, ...]], CommandResult]:
    def run(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(exit_code=exit_code, stdout=version_line, stderr="")

    return run


def success(**options: object):
    return probe_environment(runner=a_runner("RASPA 2.0.45\n"), **options)  # type: ignore[arg-type]


def refusal(**options: object) -> RaspaStartRefused:
    with pytest.raises(RaspaStartRefused) as caught:
        success(**options)
    return caught.value


def test_a_supported_host_passes_and_records_its_evidence() -> None:
    probe = success()
    assert probe.version == "2.0.45"
    assert probe.version_series == "2.0"
    assert probe.command == DEFAULT_COMMAND
    assert probe.license_sha256 == hashlib.sha256(read_bundled("raspa2-license")).hexdigest()
    for key, digest in probe.data_sha256.items():
        assert digest == source_record(key).sha256
    assert {check.name for check in probe.checks} == {
        "license_available",
        "reference_data_pinned",
        "software_version_supported",
        "binary_digest_recorded",
    }
    document = probe.to_document()
    assert document["data_sha256"] == dict(sorted(probe.data_sha256.items()))
    assert document["version"] == "2.0.45"


def test_the_probe_runs_a_real_process() -> None:
    """Not every check is injected: a process that answers is a host fact."""
    probe = probe_environment(
        binary=sys.executable,
        command=(sys.executable, "-c", "print('RASPA 2.0.45')"),
        expected_binary_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
    )
    assert probe.version_series == "2.0"
    assert probe.binary_sha256 == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


def test_missing_software_is_refused_before_a_run() -> None:
    def missing(command: tuple[str, ...]) -> CommandResult:
        raise FileNotFoundError(command[0])

    with pytest.raises(RaspaStartRefused) as caught:
        probe_environment(command=("no-such-raspa-binary", "-v"), runner=missing)
    assert caught.value.code is ProbeRefusal.SOFTWARE_MISSING
    assert caught.value.return_to == "MASTER"
    assert caught.value.document()["code"] == "SOFTWARE_MISSING"


def test_a_version_outside_the_pinned_series_is_refused() -> None:
    with pytest.raises(RaspaStartRefused) as caught:
        probe_environment(runner=a_runner("RASPA 2.1.0\n"))
    assert caught.value.code is ProbeRefusal.VERSION_MISMATCH
    assert "2.1" in caught.value.reason


def test_a_binary_that_prints_no_version_is_refused() -> None:
    with pytest.raises(RaspaStartRefused) as caught:
        probe_environment(runner=a_runner("some other program\n"))
    assert caught.value.code is ProbeRefusal.VERSION_MISMATCH


def test_a_probe_that_exits_non_zero_is_refused() -> None:
    with pytest.raises(RaspaStartRefused) as caught:
        probe_environment(runner=a_runner("RASPA 2.0.45\n", exit_code=2))
    assert caught.value.code is ProbeRefusal.PROBE_FAILED


def test_a_missing_license_record_is_refused(tmp_path: Path) -> None:
    refused = refusal(license_path=tmp_path / "absent-COPYING")
    assert refused.code is ProbeRefusal.LICENSE_UNAVAILABLE
    assert "does not exist" in refused.reason


def test_a_file_that_is_not_the_mit_text_is_refused(tmp_path: Path) -> None:
    wrong = tmp_path / "COPYING"
    wrong.write_text("all rights reserved; redistribution forbidden\n", encoding="utf-8")
    refused = refusal(license_path=wrong)
    assert refused.code is ProbeRefusal.LICENSE_UNAVAILABLE
    assert "MIT" in refused.reason or "Permission is hereby granted" in refused.reason


def test_missing_reference_data_is_refused(tmp_path: Path) -> None:
    refused = refusal(data_root=tmp_path / "empty")
    assert refused.code is ProbeRefusal.DATA_MISSING


def test_edited_reference_data_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "data"
    for key in (
        "official-simulation-input",
        "framework-mfi-si",
        "force-field-example-zeolites",
        "pseudo-atoms-example-zeolites",
        "molecule-methane",
    ):
        record = source_record(key)
        target = root / record.bundled_path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = read_bundled(key)
        target.write_bytes(data + b"\n# edited\n" if key == "framework-mfi-si" else data)
    refused = refusal(data_root=root)
    assert refused.code is ProbeRefusal.DATA_MISMATCH
    assert "framework-mfi-si" in refused.reason


def test_a_binary_digest_that_does_not_match_is_refused() -> None:
    refused = refusal(
        binary=sys.executable,
        command=(sys.executable, "-c", "print('RASPA 2.0.45')"),
        expected_binary_sha256="0" * 64,
    )
    assert refused.code is ProbeRefusal.BINARY_DIGEST_MISMATCH


def test_a_named_binary_digest_with_no_readable_file_is_refused() -> None:
    refused = refusal(
        command=("simulate", "-v"),
        expected_binary_sha256="0" * 64,
    )
    assert refused.code is ProbeRefusal.BINARY_DIGEST_MISMATCH


def test_a_refusal_names_master_as_the_return_route() -> None:
    refused = refusal(license_path=Path("/nonexistent/COPYING"))
    assert refused.document() == {
        "code": "LICENSE_UNAVAILABLE",
        "reason": refused.reason,
        "return_to": "MASTER",
    }


def test_the_probe_refusal_codes_are_a_closed_set() -> None:
    assert {member.value for member in ProbeRefusal} == {
        "SOFTWARE_MISSING",
        "PROBE_FAILED",
        "VERSION_MISMATCH",
        "LICENSE_UNAVAILABLE",
        "DATA_MISSING",
        "DATA_MISMATCH",
        "BINARY_DIGEST_MISMATCH",
    }


def test_a_result_schema_exists_for_the_environment_to_hand_forward() -> None:
    """The probe is not a result; it feeds one, and the result shape is fixed."""
    schema = schema_document("raspa_result.schema.json")
    assert "source" in schema["properties"]
    assert set(schema["properties"]["source"]["properties"]) == {
        "kind",
        "evidence_class",
        "evidence_reason",
    }
