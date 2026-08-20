"""Tests for `keel.governance.audit`.

The log is the evidence the whole run report rests on, so these tests care
about three things in particular: that a line, once written, is on disk and
never moves; that a file which was edited after the fact says so; and that no
credential survives the trip to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keel.governance.audit import AUDIT_FILENAME, REDACTED, AuditLog, redact, scrub
from keel.models import (
    AdapterRequest,
    AdapterResponse,
    AuditEventType,
    ModelTier,
    StageKind,
)

# Assembled at import time rather than written as one literal, so a real-format
# key never sits in the source of a public repo where a secret scanner will
# flag it. The value below is the exact shape Anthropic issues: the
# `sk-ant-api03-` prefix followed by a long base64url body.
FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + "R7xQ2mA9pL4vK1sT6wY3" * 5 + "-AA"


def make_request(prompt: str = "write the design", skill_id: str = "design.v1") -> AdapterRequest:
    return AdapterRequest(
        node_id="design",
        skill_id=skill_id,
        tier=ModelTier.DEEP,
        system="You are a design agent.",
        prompt=prompt,
    )


def make_response(text: str = "# design", **kwargs: object) -> AdapterResponse:
    fields: dict[str, object] = {
        "parsed": {"sections": 3},
        "model": "claude-opus-5",
        "input_tokens": 1000,
        "output_tokens": 200,
        "latency_seconds": 1.5,
    }
    fields.update(kwargs)
    return AdapterResponse(text=text, **fields)  # type: ignore[arg-type]


def raw_lines(log: AuditLog) -> list[str]:
    return log.path.read_text(encoding="utf-8").splitlines()


def rewrite(log: AuditLog, lines: list[str]) -> None:
    """Tamper with the file behind the log's back, the way an editor would."""
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Append-only writing
# --------------------------------------------------------------------------


def test_emit_assigns_sequential_seq_and_one_line_per_event(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    first = log.emit(AuditEventType.RUN_STARTED)
    second = log.emit(AuditEventType.NODE_STARTED, {"kind": "design"}, node_id="design")
    third = log.emit(AuditEventType.RUN_FINISHED)

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert len(raw_lines(log)) == 3
    assert len(log) == 3


def test_log_is_created_under_the_run_directory(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    assert log.path == tmp_path / "run-a" / AUDIT_FILENAME
    assert log.path.exists()
    assert log.verify_integrity() == []


def test_emit_is_on_disk_before_it_returns(tmp_path: Path) -> None:
    """A crash after `emit` returns must not lose the event."""
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.INTAKE, {"requirement": "add a health endpoint"})

    # Read through a completely separate handle, without closing anything.
    record = json.loads((tmp_path / "run-a" / AUDIT_FILENAME).read_text().splitlines()[0])
    assert record["event_type"] == "intake"
    assert record["payload"]["requirement"] == "add a health endpoint"


def test_emit_preserves_node_id_and_payload(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    event = log.emit(
        AuditEventType.GATE_DECISION,
        {"gate": "exit", "allowed": False, "violations": 2},
        node_id="implement",
    )
    assert event.node_id == "implement"
    assert event.payload == {"gate": "exit", "allowed": False, "violations": 2}
    assert event.run_id == "run-a"


def test_earlier_lines_are_never_rewritten(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    before = raw_lines(log)[0]

    log.emit(AuditEventType.NODE_STARTED, node_id="analyze")
    log.emit(AuditEventType.NODE_FINISHED, node_id="analyze")

    assert raw_lines(log)[0] == before


def test_timestamps_never_decrease(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    for _ in range(5):
        log.emit(AuditEventType.RETRY)
    stamps = [event.at for event in log.events()]
    assert stamps == sorted(stamps)


def test_events_can_be_filtered_by_type(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.NODE_STARTED, node_id="a")
    log.emit(AuditEventType.RETRY, node_id="a")
    log.emit(AuditEventType.NODE_FINISHED, node_id="a")

    assert [e.event_type for e in log.events()] == [
        AuditEventType.NODE_STARTED,
        AuditEventType.RETRY,
        AuditEventType.NODE_FINISHED,
    ]
    assert [e.node_id for e in log.events(AuditEventType.RETRY)] == ["a"]


def test_events_accessor_returns_a_copy(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.events().clear()
    assert len(log.events()) == 1


# --------------------------------------------------------------------------
# Reading and resuming
# --------------------------------------------------------------------------


def test_reopening_a_run_continues_the_sequence(tmp_path: Path) -> None:
    first = AuditLog("run-a", root=tmp_path)
    first.emit(AuditEventType.RUN_STARTED)
    first.emit(AuditEventType.NODE_STARTED, node_id="analyze")

    resumed = AuditLog("run-a", root=tmp_path)
    event = resumed.emit(AuditEventType.RUN_FINISHED)

    assert event.seq == 3
    assert len(raw_lines(resumed)) == 3
    assert resumed.verify_integrity() == []


def test_read_loads_an_existing_log(tmp_path: Path) -> None:
    written = AuditLog("run-a", root=tmp_path)
    written.emit(AuditEventType.INTAKE, {"requirement": "ship it"})
    written.emit(AuditEventType.PLAN_CREATED, {"nodes": 4}, node_id=None)

    loaded = AuditLog.read("run-a", root=tmp_path)
    assert [e.event_type for e in loaded.events()] == [
        AuditEventType.INTAKE,
        AuditEventType.PLAN_CREATED,
    ]
    assert loaded.events()[0].payload == {"requirement": "ship it"}
    assert [e.seq for e in loaded.events()] == [1, 2]


def test_read_raises_when_the_run_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        AuditLog.read("run-missing", root=tmp_path)
    assert not (tmp_path / "run-missing").exists()


def test_read_skips_a_damaged_line_and_keeps_the_rest(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.emit(AuditEventType.NODE_STARTED, node_id="analyze")
    log.emit(AuditEventType.RUN_FINISHED)

    lines = raw_lines(log)
    lines[1] = lines[1][:40]  # truncated write, the classic crash artifact
    rewrite(log, lines)

    loaded = AuditLog.read("run-a", root=tmp_path)
    assert [e.event_type for e in loaded.events()] == [
        AuditEventType.RUN_STARTED,
        AuditEventType.RUN_FINISHED,
    ]


# --------------------------------------------------------------------------
# Cassettes
# --------------------------------------------------------------------------


def test_record_model_call_payload_carries_cassette_text_and_cost(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    request = make_request()
    response = make_response(text="# design\ntwo services")

    event = log.record_model_call("design", request, response)

    assert event.event_type is AuditEventType.MODEL_CALL
    assert event.node_id == "design"
    assert event.payload["cassette_key"] == request.cassette_key
    assert event.payload["text"] == "# design\ntwo services"
    assert event.payload["model"] == "claude-opus-5"
    assert event.payload["input_tokens"] == 1000
    assert event.payload["output_tokens"] == 200
    assert event.payload["cost_usd"] == pytest.approx(0.01)
    assert event.payload["tier"] == ModelTier.DEEP.value


def test_cassettes_round_trip_through_disk(tmp_path: Path) -> None:
    """What the replay adapter does: same request, same key, recorded answer."""
    log = AuditLog("run-a", root=tmp_path)
    request = make_request(prompt="write the design")
    log.record_model_call("design", request, make_response(text="# design\ntwo services"))
    log.record_model_call(
        "implement",
        make_request(prompt="write the code", skill_id="implement.v1"),
        make_response(text="def main(): ..."),
    )

    reloaded = AuditLog.read("run-a", root=tmp_path)
    cassettes = reloaded.cassettes()

    # The replay adapter recomputes the key from its own identical request.
    lookup = make_request(prompt="write the design").cassette_key
    assert lookup in cassettes
    assert cassettes[lookup]["text"] == "# design\ntwo services"
    assert cassettes[lookup]["parsed"] == {"sections": 3}
    assert len(cassettes) == 2


def test_cassette_key_ignores_the_node_it_ran_on(tmp_path: Path) -> None:
    """Two nodes issuing the identical request share one recording."""
    log = AuditLog("run-a", root=tmp_path)
    request_a = AdapterRequest(
        node_id="design-a",
        skill_id="design.v1",
        tier=ModelTier.DEEP,
        system="You are a design agent.",
        prompt="write the design",
    )
    request_b = AdapterRequest(
        node_id="design-b",
        skill_id="design.v1",
        tier=ModelTier.DEEP,
        system="You are a design agent.",
        prompt="write the design",
    )
    assert request_a.cassette_key == request_b.cassette_key

    log.record_model_call("design-a", request_a, make_response(text="first"))
    assert len(log.cassettes()) == 1


def test_first_recording_wins_for_a_repeated_request(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    request = make_request()
    log.record_model_call("design", request, make_response(text="first answer"))
    log.record_model_call("design", request, make_response(text="second answer"))

    assert log.cassettes()[request.cassette_key]["text"] == "first answer"
    assert len(log.events(AuditEventType.MODEL_CALL)) == 2


def test_cassettes_ignore_non_model_events(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED, {"cassette_key": "not-a-cassette"})
    log.record_model_call("design", make_request(), make_response())

    assert list(log.cassettes()) == [make_request().cassette_key]


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------


def test_verify_integrity_passes_on_an_untouched_log(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    for index in range(6):
        log.emit(AuditEventType.NODE_STARTED, {"i": index}, node_id=f"n{index}")
    assert log.verify_integrity() == []


def test_verify_integrity_catches_a_deleted_line(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    for _ in range(4):
        log.emit(AuditEventType.RETRY)

    lines = raw_lines(log)
    del lines[1]  # seq 2 quietly disappears
    rewrite(log, lines)

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert len(problems) == 1
    assert "sequence gap" in problems[0]
    assert "expected seq 2, found 3" in problems[0]


def test_verify_integrity_catches_a_tampered_line(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.emit(AuditEventType.MODEL_CALL, {"text": "the original answer"})
    log.emit(AuditEventType.RUN_FINISHED)

    lines = raw_lines(log)
    lines[1] = lines[1].replace("the original answer", "a nicer answer")[:-1]
    rewrite(log, lines)

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert any("malformed JSON" in problem for problem in problems)
    assert any("line 2" in problem for problem in problems)


def test_verify_integrity_catches_a_backwards_timestamp(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.emit(AuditEventType.NODE_STARTED, node_id="analyze")
    log.emit(AuditEventType.RUN_FINISHED)

    lines = raw_lines(log)
    record = json.loads(lines[2])
    record["at"] = record["at"] - 500.0
    lines[2] = json.dumps(record)
    rewrite(log, lines)

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert len(problems) == 1
    assert "timestamp moves backwards" in problems[0]


def test_verify_integrity_catches_a_duplicated_line(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.emit(AuditEventType.RUN_FINISHED)

    lines = raw_lines(log)
    rewrite(log, [lines[0], lines[1], lines[1]])

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert any("duplicate seq 2" in problem for problem in problems)


def test_verify_integrity_catches_a_foreign_run_id(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.emit(AuditEventType.RUN_FINISHED)

    lines = raw_lines(log)
    record = json.loads(lines[1])
    record["run_id"] = "run-somewhere-else"
    lines[1] = json.dumps(record)
    rewrite(log, lines)

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert any("does not belong to run" in problem for problem in problems)


def test_verify_integrity_catches_an_unknown_event_type(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)

    record = json.loads(raw_lines(log)[0])
    record["event_type"] = "invented_event"
    rewrite(log, [json.dumps(record)])

    problems = AuditLog.read("run-a", root=tmp_path).verify_integrity()
    assert any("unknown event_type" in problem for problem in problems)


def test_verify_integrity_catches_a_missing_field(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)

    record = json.loads(raw_lines(log)[0])
    del record["at"]
    rewrite(log, [json.dumps(record)])

    problems = log.verify_integrity()
    assert any("missing field(s): at" in problem for problem in problems)


def test_verify_integrity_catches_a_deleted_file(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    log.emit(AuditEventType.RUN_STARTED)
    log.path.unlink()

    problems = log.verify_integrity()
    assert len(problems) == 1
    assert "missing audit log" in problems[0]


def test_verify_integrity_is_clean_for_a_run_that_recorded_nothing(tmp_path: Path) -> None:
    assert AuditLog("run-a", root=tmp_path).verify_integrity() == []


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_the_fake_key_really_has_the_anthropic_shape() -> None:
    """Guards the test below: a mangled fixture would make redaction look fine."""
    assert FAKE_ANTHROPIC_KEY.startswith("sk-ant-api03-")
    assert len(FAKE_ANTHROPIC_KEY) > 100


def test_anthropic_key_never_reaches_disk(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    event = log.emit(
        AuditEventType.NODE_FINISHED,
        {
            "error": f"401 from Anthropic using {FAKE_ANTHROPIC_KEY}",
            "env": {"ANTHROPIC_API_KEY": FAKE_ANTHROPIC_KEY},
            "attempts": [{"key": FAKE_ANTHROPIC_KEY}],
        },
        node_id="implement",
    )

    on_disk = log.path.read_text(encoding="utf-8")
    assert FAKE_ANTHROPIC_KEY not in on_disk
    assert "sk-ant-" not in on_disk
    assert REDACTED in on_disk
    assert event.payload["error"] == f"401 from Anthropic using {REDACTED}"
    assert event.payload["env"]["ANTHROPIC_API_KEY"] == REDACTED
    assert event.payload["attempts"][0]["key"] == REDACTED


def test_a_key_echoed_in_a_model_response_is_redacted(tmp_path: Path) -> None:
    """The cassette is model output, so it is exactly where a leak turns up."""
    log = AuditLog("run-a", root=tmp_path)
    log.record_model_call(
        "implement",
        make_request(prompt=f"the key is {FAKE_ANTHROPIC_KEY}"),
        make_response(text=f'client = Anthropic(api_key="{FAKE_ANTHROPIC_KEY}")'),
    )

    payload = log.events(AuditEventType.MODEL_CALL)[0].payload
    assert FAKE_ANTHROPIC_KEY not in payload["text"]
    assert FAKE_ANTHROPIC_KEY not in payload["prompt"]
    assert REDACTED in payload["text"]
    assert FAKE_ANTHROPIC_KEY not in log.path.read_text(encoding="utf-8")


def test_redaction_keeps_the_surrounding_diagnostic(tmp_path: Path) -> None:
    log = AuditLog("run-a", root=tmp_path)
    event = log.emit(AuditEventType.RETRY, {"reason": f"auth failed for {FAKE_ANTHROPIC_KEY} at 03:11"})
    assert event.payload["reason"] == f"auth failed for {REDACTED} at 03:11"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "a1b2c3d4e5f6g7h8i9j0" * 2,
        "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2,
        "github_pat_" + "A1b2C3d4E5f6G7h8I9j0" * 2,
        "AKIAIOSFODNN7EXAMPLE",
        "AIza" + "A1b2C3d4E5f6G7h8I9j0A1b2C3d4E5",
        "xoxb-" + "1234567890-0987654321-abcdefgh",
        "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "Bearer abcdefghijklmnop1234567890",
    ],
)
def test_other_credential_shapes_are_redacted(secret: str) -> None:
    scrubbed = scrub(f"config says {secret} here")
    assert secret not in scrubbed
    assert REDACTED in scrubbed


def test_fields_named_like_credentials_are_redacted_whatever_they_hold() -> None:
    payload = redact(
        {
            "password": "hunter2",
            "auth_token": "short",
            "api_key": "abc",
            "note": "hunter2 is fine in prose",
        }
    )
    assert payload["password"] == REDACTED
    assert payload["auth_token"] == REDACTED
    assert payload["api_key"] == REDACTED
    assert payload["note"] == "hunter2 is fine in prose"


def test_token_counts_survive_redaction(tmp_path: Path) -> None:
    """`input_tokens` reads like a credential name and must not be scrubbed."""
    log = AuditLog("run-a", root=tmp_path)
    log.record_model_call("design", make_request(), make_response())
    payload = log.events(AuditEventType.MODEL_CALL)[0].payload
    assert payload["input_tokens"] == 1000
    assert payload["output_tokens"] == 200


def test_a_token_count_that_arrived_as_a_string_still_survives() -> None:
    """Counts parsed out of a model's own JSON come back as strings.

    Integers are never scrubbed, so the field-name rule only bites here. This
    is the case the exemption for count fields exists for.
    """
    payload = redact({"input_tokens": "1000", "output_tokens": "200", "auth_token": "abc"})
    assert payload["input_tokens"] == "1000"
    assert payload["output_tokens"] == "200"
    assert payload["auth_token"] == REDACTED


def test_ordinary_text_is_left_alone() -> None:
    text = "implement returned 3 files and the test gate passed in 12.4s"
    assert scrub(text) == text


def test_payloads_are_coerced_to_json_native_types(tmp_path: Path) -> None:
    """Unknown objects are flattened here, where the scrubber can still see them."""
    log = AuditLog("run-a", root=tmp_path)
    event = log.emit(
        AuditEventType.NODE_FINISHED,
        {
            "stage": StageKind.IMPLEMENT,
            "path": Path("src/app.py"),
            "produced": ("a.py", "b.py"),
            "labels": {"beta", "alpha"},
        },
    )
    assert event.payload["stage"] == "implement"
    assert event.payload["path"] == "src/app.py"
    assert event.payload["produced"] == ["a.py", "b.py"]
    assert event.payload["labels"] == ["alpha", "beta"]

    # Round trips through JSON without a custom encoder.
    assert json.loads(raw_lines(log)[0])["payload"]["stage"] == "implement"


def test_an_object_carrying_a_key_is_scrubbed_after_flattening(tmp_path: Path) -> None:
    class Settings:
        def __repr__(self) -> str:
            return f"Settings(key={FAKE_ANTHROPIC_KEY})"

    log = AuditLog("run-a", root=tmp_path)
    event = log.emit(AuditEventType.RUN_STARTED, {"settings": Settings()})

    assert FAKE_ANTHROPIC_KEY not in event.payload["settings"]
    assert REDACTED in event.payload["settings"]
    assert FAKE_ANTHROPIC_KEY not in log.path.read_text(encoding="utf-8")
