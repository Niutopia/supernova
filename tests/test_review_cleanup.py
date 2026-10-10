"""Regressions for job isolation, locked-chapter backoff and coverage gating."""

import threading
import time
from types import SimpleNamespace

import pytest

import main
from api.base import StudyResult


COURSE = {"courseId": "c1", "clazzId": "class", "cpi": "cpi", "title": "课程"}


def _config(**values):
    config = {
        "speed": 1.0,
        "jobs": 2,
        "notopen_action": "retry",
        "cancel_event": threading.Event(),
    }
    config.update(values)
    return config


@pytest.mark.parametrize("kind", ["video", "document", "read"])
def test_idempotent_job_exception_fails_only_that_job(kind):
    def boom(*_args, **_kwargs):
        raise IndexError("synthetic parser failure")

    engine = SimpleNamespace(study_video=boom, study_document=boom, study_read=boom)
    job = {"type": kind, "jobid": "j1"}

    result = main.process_job(engine, COURSE, job, {}, 1.0, config=_config())

    assert result is StudyResult.ERROR
    assert job["reason"]
    assert job["next_action"]


def test_quiz_exception_still_propagates():
    def boom(*_args, **_kwargs):
        raise ValueError("submit response lost")

    engine = SimpleNamespace(study_work=boom)

    with pytest.raises(ValueError):
        main.process_job(
            engine, COURSE, {"type": "workid", "jobid": "w1"}, {}, 1.0, config=_config()
        )


def test_failing_document_does_not_abort_sibling_chapters(monkeypatch):
    processed = []

    class Engine:
        rate_limiter = SimpleNamespace(limit_rate=lambda **_kwargs: None)

        def get_job_list(self, _course, point):
            return [{"type": "document", "jobid": point["id"]}], {}

        def study_document(self, _course, job):
            processed.append(job["jobid"])
            if job["jobid"] == "bad":
                raise IndexError("synthetic parser failure")
            return StudyResult.SUCCESS

    items = [
        main.ChapterTask(index=i, point={"id": name})
        for i, name in enumerate(("bad", "good"))
    ]
    processor = main.JobProcessor(Engine(), COURSE, items, _config(jobs=1))
    processor.max_tries = 1

    processor.run()

    assert "good" in processed
    assert items[1].result is main.ChapterResult.SUCCESS
    assert processor.failed_tasks == [items[0]]


def test_locked_chapter_backs_off_while_prerequisite_runs(monkeypatch):
    release = threading.Event()
    locked_checks = []

    def chapter(_engine, _course, point, _speed, _config):
        if point["id"] == "prerequisite":
            assert release.wait(5)
            return main.ChapterResult.SUCCESS
        locked_checks.append(time.monotonic())
        return (
            main.ChapterResult.SUCCESS
            if release.is_set()
            else main.ChapterResult.NOT_OPEN
        )

    monkeypatch.setattr(main, "process_chapter", chapter)
    items = [
        main.ChapterTask(index=i, point={"id": name})
        for i, name in enumerate(("prerequisite", "locked"))
    ]
    processor = main.JobProcessor(None, COURSE, items, _config())
    worker = threading.Thread(target=processor.run)
    worker.start()
    try:
        time.sleep(2.5)
        # Without backoff the retry thread re-polls about once per second.
        assert len(locked_checks) == 1
    finally:
        release.set()
    worker.join(5)

    assert not worker.is_alive()
    # The prerequisite settling wakes the waiting chapter immediately.
    assert len(locked_checks) == 2
    assert all(item.result is main.ChapterResult.SUCCESS for item in items)
    assert not processor.failed_tasks


@pytest.fixture
def guarded_client(tmp_path):
    from webapp import create_app

    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    return app.test_client()


def test_foreign_host_header_is_rejected(guarded_client):
    reply = guarded_client.get(
        "/api/accounts", headers={"Host": "rebind.evil.example:5001"}
    )
    assert reply.status_code == 403
    assert reply.get_json()["code"] == "origin_forbidden"


def test_cross_site_write_is_rejected(guarded_client):
    reply = guarded_client.post(
        "/api/accounts",
        json={"name": "x", "username": "u", "password": "p"},
        headers={"Origin": "https://evil.example"},
    )
    assert reply.status_code == 403
    assert not guarded_client.get("/api/accounts").get_json()["data"]


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:5001", "http://localhost:3000", None]
)
def test_local_and_non_browser_writes_are_allowed(guarded_client, origin):
    headers = {"Origin": origin} if origin else {}
    reply = guarded_client.post(
        "/api/accounts",
        json={"name": "x", "username": "u", "password": "p"},
        headers=headers,
    )
    assert reply.status_code == 201


def test_configured_host_is_allowed(tmp_path):
    from webapp import create_app

    app = create_app(
        {"TESTING": True, "DATA_DIR": tmp_path, "ALLOWED_HOSTS": "study.example"}
    )
    reply = app.test_client().get(
        "/api/health", headers={"Host": "study.example"}
    )
    assert reply.status_code == 200
    assert reply.headers["X-Frame-Options"] == "DENY"


def test_config_template_matches_the_keys_the_cli_reads():
    from pathlib import Path

    from api.answer import AI, Tiku

    common, tiku_config, notification = main.load_config_from_file(
        Path(__file__).resolve().parent.parent / "config.ini.example"
    )
    tiku_config["key"] = "synthetic-key"

    tiku = Tiku()
    tiku.config_set(tiku_config)
    provider = tiku.get_tiku_from_config()

    assert isinstance(provider, AI)
    assert not provider.DISABLE
    assert "provider" in notification


def test_unknown_tiku_provider_disables_instead_of_crashing():
    from api.answer import Tiku

    tiku = Tiku()
    tiku.config_set({"provider": "Notification"})

    assert tiku.get_tiku_from_config().DISABLE
