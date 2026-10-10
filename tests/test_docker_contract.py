"""Static contracts for the local Docker deployment and release hygiene."""

import re
from pathlib import Path

import pytest
import yaml


def test_compose_binds_only_localhost_and_persists_data():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    web = compose["services"]["web"]
    assert web["ports"] == ["127.0.0.1:5001:5000"]
    assert (
        web["depends_on"]["data-init"]["condition"]
        == "service_completed_successfully"
    )
    assert "supernova-data:/app/data" in web["volumes"]
    assert "host.docker.internal:host-gateway" in web["extra_hosts"]
    assert web["restart"] == "unless-stopped"
    assert web["healthcheck"]["test"][:3] == ["CMD", "python", "-c"]
    assert "/api/health" in web["healthcheck"]["test"][3]
    assert "supernova-data" in compose["volumes"]


def test_dockerfile_builds_frontend_and_runs_one_gunicorn_worker():
    dockerfile = Path("Dockerfile").read_text()
    assert "npm run build" in dockerfile
    assert "COPY --from=web-builder" in dockerfile
    assert "EXPOSE 5000" in dockerfile
    assert '"--workers", "1"' in dockerfile
    assert '"--threads", "8"' in dockerfile
    assert '"--bind", "0.0.0.0:5000"' in dockerfile
    assert 'VOLUME ["/app/data"]' in dockerfile


def test_app_factory_enables_loopback_translation_inside_docker(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SUPERNOVA_RUNNING_IN_DOCKER", "1")

    from webapp import create_app

    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    service = app.extensions["services"]["answer_connection_service"]
    runner = app.extensions["services"]["task_manager"].runner

    assert service.running_in_docker is True
    assert runner.running_in_docker is True


def test_dockerignore_excludes_data_backups_and_operations_uses_external_backup_path():
    dockerignore = {
        line.strip()
        for line in Path(".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "supernova-data-backup.tgz" in dockerignore
    assert "*.tgz" in dockerignore
    assert "*.tar.gz" in dockerignore

    readme = Path("docs/operations.md").read_text()
    assert '-v "$PWD":/backup' not in readme
    assert "mktemp" in readme


def test_dockerignore_covers_coverage_sidecar_files_at_any_depth():
    dockerignore = {
        line.strip()
        for line in Path(".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".coverage.*" in dockerignore
    assert "**/.coverage.*" in dockerignore


def test_env_example_does_not_advertise_unwired_account_limit():
    env_example = Path(".env.example").read_text()
    assert "CHAOXING_MAX_ACTIVE_ACCOUNTS" not in env_example


def test_operations_uses_shell_safe_ocr_placeholder_and_current_docker_troubleshooting():
    readme = Path("docs/operations.md").read_text()
    assert 'export SUPERNOVA_VISION_OCR_KEY="YOUR_VISION_KEY"' in readme
    assert "export SUPERNOVA_VISION_OCR_KEY=<" not in readme
    assert "netstat -ano | findstr :5000" not in readme
    assert "findstr :3000" not in readme
    assert "/config/config.ini" not in readme
    assert "127.0.0.1:5001" in readme
    assert "/app/data" in readme
    assert "Settings" in readme


def test_docker_contract_keeps_runtime_environment_explicit():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    environment = compose["services"]["web"]["environment"]
    if isinstance(environment, list):
        environment = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in environment}
    assert environment["SUPERNOVA_DATA_DIR"] == "/app/data"
    assert environment["SUPERNOVA_RUNNING_IN_DOCKER"] == "1"

    dockerignore = Path(".dockerignore").read_text()
    assert "*.zip" in dockerignore
    assert "**/*.zip" in dockerignore


def test_operations_backup_resolves_the_running_compose_volume_and_cli_docs_are_precise():
    readme = Path("docs/operations.md").read_text()
    assert "docker compose ps -q web" in readme
    assert "docker inspect" in readme
    assert "Destination" in readme and "/app/data" in readme
    assert "chaoxing-fanya_chaoxing-data" not in readme
    assert "python main.py" in readme and "交互式运行" in readme
    assert "删除 node_modules 与锁文件" not in readme


def test_operations_describes_data_init_as_a_compose_up_dependency():
    readme = Path("docs/operations.md").read_text()
    lifecycle_start = readme.index("`data-init`")
    lifecycle_end = readme.index("备份 SQLite", lifecycle_start)
    lifecycle = readme[lifecycle_start:lifecycle_end]

    assert "每次启动前" not in readme
    assert "docker compose up" in readme
    assert "docker compose restart" in readme
    assert "docker compose start" in readme
    assert "docker restart" in readme
    assert "docker compose run --rm data-init" in readme
    assert "不会主动重新运行" in readme or "不会重跑" in readme
    assert "Compose `depends_on` 使用的一次性成功依赖" in lifecycle
    assert "成功退出，然后才启动 `web`" in lifecycle


def test_operations_validates_restore_archive_before_clearing_the_volume():
    readme = Path("docs/operations.md").read_text()
    restore_start = readme.index("恢复时")
    restore_end = readme.index("备份文件应放在", restore_start)
    restore = readme[restore_start:restore_end]

    assert "gzip -t" in restore
    assert "tar -tzf" in restore
    assert restore.index("gzip -t") < restore.index("tar -tzf")
    assert restore.index("tar -tzf") < restore.index("find /target")


def _restore_section(readme: str) -> str:
    start = readme.index("恢复时")
    end = readme.index("备份文件应放在", start)
    return readme[start:end]


def test_restore_stages_and_validates_archive_before_stopping_web_and_replacing_data():
    readme = Path("docs/operations.md").read_text()
    restore = _restore_section(readme)

    # The archive must be expanded into an isolated temporary directory first;
    # validation must happen before any service/volume mutation.
    extract = restore.index("tar -xzf")
    required_db = restore.index("chaoxing-web.sqlite3", extract)
    required_key = restore.index("secret.key", extract)
    stop = restore.index("docker compose stop web")
    safety_backup = restore.index("pre-restore")
    replace = restore.index("find /target")

    assert "mktemp -d" in restore
    assert "tar -tzf" in restore or "gzip -t" in restore
    assert extract < required_db < stop
    assert extract < required_key < stop
    assert stop < safety_backup < replace
    assert 'rm -rf -- "$safety_dir"' not in restore
    assert "supernova-data-pre-restore.tgz" in restore
    assert "docker compose down -v" not in restore
    assert "docker compose down -v" not in readme


def test_restore_replaces_data_only_from_the_verified_staging_directory():
    readme = Path("docs/operations.md").read_text()
    restore = _restore_section(readme)

    replace = restore[restore.index("find /target") :]
    assert "tar -xzf /backup.tgz" not in replace
    assert "/restore:ro" in restore
    assert "cp -a /restore/. /target/" in replace or "cp -R /restore/. /target/" in replace


def test_data_init_docs_describe_explicit_run_without_remove_precondition():
    readme = Path("docs/operations.md").read_text()
    lifecycle = readme[readme.index("`data-init`") : readme.index("备份 SQLite", readme.index("`data-init`"))]

    assert "docker compose run --rm data-init" in lifecycle
    assert "显式" in lifecycle
    assert "restart" in lifecycle and ("不会" in lifecycle or "不會" in lifecycle)
    assert "rm -f data-init" not in lifecycle


def _restore_failure_block(restore, marker, label):
    """Return one restore branch's direct ``else`` block.

    The restore examples intentionally use simple, non-nested shell ``if``
    statements.  Walk that one statement to its matching ``fi`` instead of
    searching for a later ``else``/``fi`` pair in the whole restore section.
    """
    marker_offset = restore.index(marker)
    lines = restore.splitlines()
    start_line = restore.count("\n", 0, marker_offset)
    assert lines[start_line].lstrip().startswith("if "), label

    then_line = next(
        (
            index
            for index in range(start_line, len(lines))
            if re.search(r";\s*then\s*$", lines[index].strip(), re.IGNORECASE)
        ),
        None,
    )
    assert then_line is not None, label

    depth = 1
    else_line = None
    fi_line = None
    for index in range(then_line + 1, len(lines)):
        stripped = lines[index].strip()
        if re.match(r"^if\b", stripped, re.IGNORECASE):
            depth += 1
        elif re.fullmatch(r"fi", stripped, re.IGNORECASE):
            depth -= 1
            if depth == 0:
                fi_line = index
                break
        elif depth == 1 and re.fullmatch(r"else", stripped, re.IGNORECASE):
            assert else_line is None, label
            else_line = index

    assert else_line is not None and fi_line is not None, label
    assert else_line < fi_line, label
    return "\n".join(lines[else_line : fi_line + 1])


def test_compose_and_docs_keep_local_web_port_and_container_answer_host_contract():
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    web = compose["services"]["web"]
    assert web["ports"] == ["127.0.0.1:5001:5000"]
    assert "host.docker.internal:host-gateway" in web["extra_hosts"]

    readme = Path("docs/operations.md").read_text()
    assert "Host answer API:" in readme
    assert "Container target:  http://host.docker.internal:8849/v1" in readme
    assert "127.0.0.1:5001" in readme
    assert "127.0.0.1:5001:5000" in readme or "127.0.0.1:5001" in readme


def test_restore_has_explicit_failure_rollback_after_second_backup():
    readme = Path("docs/operations.md").read_text()
    restore = _restore_section(readme)
    safety = restore.index("supernova-data-pre-restore.tgz")
    replace = restore.index(
        'if docker run --rm \\\n'
        '  -v "${data_volume}:/target"',
        safety,
    )
    rollback_definition = restore.index("rollback_original()")

    assert "set -euo pipefail" not in restore
    assert "set -e" not in restore
    assert "set -uo pipefail" in restore or "set -u -o pipefail" in restore
    assert safety < rollback_definition < replace

    failure_branches = (
        (
            "replace",
            'if docker run --rm \\\n'
            '  -v "${data_volume}:/target" \\\n'
            '  -v "$restore_dir/extracted:/restore:ro"',
        ),
        ("data-init", "if docker compose run --rm data-init; then"),
        ("web-start", "if docker compose up -d web; then"),
        (
            "health",
            "if curl -fsS --retry 30 --retry-delay 1 --retry-connrefused "
            "http://127.0.0.1:5001/api/health >/dev/null; then",
        ),
    )
    for branch_name, marker in failure_branches:
        failure_block = _restore_failure_block(restore, marker, branch_name)
        assert re.search(r"(?m)^\s*fail_after_backup\b", failure_block), branch_name

    rollback = restore[rollback_definition:replace]
    assert rollback.index("docker compose stop web") < rollback.index("tar -xzf /backup/supernova-data-pre-restore.tgz")
    assert rollback.index("tar -xzf /backup/supernova-data-pre-restore.tgz") < rollback.index("docker compose run --rm data-init")
    assert rollback.index("docker compose run --rm data-init") < rollback.index("docker compose up -d web")
    assert "/backup:ro" in rollback

    post_backup = restore[safety:]
    assert "docker compose run --rm data-init" in post_backup
    assert "docker compose up -d web" in post_backup
    assert "/api/health" in post_backup
    assert "curl -fsS" in post_backup
    assert "docker compose down -v" not in restore


def test_data_init_docs_distinguish_restarting_data_init_from_restarting_web():
    readme = Path("docs/operations.md").read_text()
    lifecycle = readme[readme.index("`data-init`") : readme.index("备份 SQLite", readme.index("`data-init`"))]

    assert "docker compose restart data-init" in lifecycle
    assert "docker compose restart web" in lifecycle
    assert "docker restart <web>" in lifecycle
    assert "docker compose start web" in lifecycle
    assert "不会主动重新运行" in lifecycle or "不会重新运行" in lifecycle
    assert "docker compose run --rm data-init" in lifecycle
    assert "docker compose rm -f data-init" not in lifecycle
