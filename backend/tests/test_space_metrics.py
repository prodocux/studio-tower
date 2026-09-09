from app.core.space_metrics import _activity_attributes
from app.core.telemetry_sanitizer import sanitize_span_attributes
from app.models.activity import ActivityEvent, ActivityEventType
from app.models.space import MembershipRole, Space
from app.models.user import User
from app.services.storage import store


def test_activity_attributes_carry_correlation_fields():
    event = ActivityEvent(
        event_id="run.completed:art_1",
        event_type=ActivityEventType.RUN_COMPLETED,
        space_id="spc_corr",
        resource_type="artifact",
        resource_id="art_abc123",
        summary="Action completed artifact shoot_schedule.csv",
        details={"filename": "shoot_schedule.csv", "run_id": "run_9", "file_id": "file_src_1"},
    )
    clean = sanitize_span_attributes(_activity_attributes(event))
    assert clean["space_id"] == "spc_corr"
    assert clean["event_type"] == "run.completed"
    assert clean["resource_type"] == "artifact"
    assert clean["resource_id"] == "art_abc123"
    assert clean["resource_name"] == "shoot_schedule.csv"
    assert clean["run_id"] == "run_9"
    assert clean["file_id"] == "file_src_1"
    assert clean["artifact_id"] == "art_abc123"


def test_file_upload_attributes_use_file_id_as_resource():
    event = ActivityEvent(
        event_id="file.uploaded:file_src_1",
        event_type=ActivityEventType.FILE_UPLOADED,
        space_id="spc_corr",
        resource_type="file",
        resource_id="file_src_1",
        summary="File 'Treatment.pdf' uploaded",
        details={"filename": "Treatment.pdf", "file_id": "file_src_1"},
    )
    clean = sanitize_span_attributes(_activity_attributes(event))
    assert clean["resource_type"] == "file"
    assert clean["resource_id"] == "file_src_1"
    assert clean["file_id"] == "file_src_1"
    assert clean["resource_name"] == "Treatment.pdf"
    assert "run_id" not in clean
    assert "artifact_id" not in clean


def test_run_and_gate_attributes_carry_source_file():
    run_event = ActivityEvent(
        event_id="run.started:run_9:r1",
        event_type=ActivityEventType.RUN_STARTED,
        space_id="spc_corr",
        resource_type="run",
        resource_id="run_9",
        summary="Execution run started",
        details={"run_id": "run_9", "file_id": "file_src_1", "filename": "Treatment.pdf"},
    )
    run_clean = sanitize_span_attributes(_activity_attributes(run_event))
    assert run_clean["run_id"] == "run_9"
    assert run_clean["file_id"] == "file_src_1"
    assert run_clean["resource_name"] == "Treatment.pdf"
    assert "artifact_id" not in run_clean

    gate_event = ActivityEvent(
        event_id="gate.approved:run_9:r2",
        event_type=ActivityEventType.GATE_APPROVED,
        space_id="spc_corr",
        resource_type="gate",
        resource_id="run_9",
        summary="Approval gate approved",
        details={"run_id": "run_9", "file_id": "file_src_1", "filename": "Treatment.pdf"},
    )
    gate_clean = sanitize_span_attributes(_activity_attributes(gate_event))
    assert gate_clean["resource_type"] == "gate"
    assert gate_clean["run_id"] == "run_9"
    assert gate_clean["file_id"] == "file_src_1"
    assert gate_clean["resource_name"] == "Treatment.pdf"


def test_message_attributes_do_not_copy_chat_text():
    event = ActivityEvent(
        event_id="message.created:msg_1",
        event_type=ActivityEventType.MESSAGE_CREATED,
        space_id="spc_corr",
        resource_type="message",
        resource_id="msg_1",
        summary="Message sent by Alice: secret plot twist",
        details={
            "message_id": "msg_1",
            "content_preview": "secret plot twist",
            "file_id": "file_src_1",
        },
    )
    clean = sanitize_span_attributes(_activity_attributes(event))
    assert clean["resource_id"] == "msg_1"
    assert clean["file_id"] == "file_src_1"
    assert "resource_name" not in clean
    assert "secret plot twist" not in clean.values()


def test_source_file_correlation_adds_basename(monkeypatch):
    from app.services.activity_service import _source_file_correlation

    class FakeFile:
        filename = "Treatment.pdf"

    monkeypatch.setattr("app.services.activity_service.store.get_file", lambda fid: FakeFile())

    class FakeRun:
        source_file_id = "file_src_1"

    assert _source_file_correlation(FakeRun()) == {"file_id": "file_src_1", "filename": "Treatment.pdf"}


def test_new_activity_event_emits_space_metric(monkeypatch):
    recorded = []
    monkeypatch.setattr("app.services.storage.record_space_activity", lambda event: recorded.append(event))

    user = User(uid="u_metric", email="m@test.com", display_name="Metric")
    space = Space(space_id="spc_metric", name="Metric Space", created_by="u_metric")
    store.save_user(user)
    store.create_space(space, creator_uid="u_metric")
    store.add_member("spc_metric", "u_metric", MembershipRole.OWNER)

    event = ActivityEvent(
        event_id="message.created:msg_metric_1",
        event_type=ActivityEventType.MESSAGE_CREATED,
        space_id="spc_metric",
        resource_type="message",
        resource_id="msg_metric_1",
        summary="User asked a question",
        actor_uid="u_metric",
    )
    store.record_activity_event(event)
    assert len(recorded) == 1
    assert recorded[0].space_id == "spc_metric"
    assert recorded[0].event_type == ActivityEventType.MESSAGE_CREATED

    store.record_activity_event(event)
    assert len(recorded) == 1
