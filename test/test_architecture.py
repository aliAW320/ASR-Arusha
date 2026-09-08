import tomllib
from pathlib import Path

from app.main import BACKGROUND_WORKERS, app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_api_dependencies_do_not_include_ml_runtimes():
    project_source = (PROJECT_ROOT / "pyproject.toml").read_text()
    tomllib.loads(project_source)
    for forbidden in (
        "faster-whisper",
        "pyannote-audio",
        "torch",
        "torchaudio",
        "torchcodec",
        "qdrant-client",
    ):
        assert forbidden not in project_source


def test_backend_openapi_exposes_required_phase_endpoints():
    paths = app.openapi()["paths"]
    assert "/auth/register" in paths
    assert "/auth/login" in paths
    assert "/meetings" in paths
    assert "/meetings/{meeting_id}/members" in paths
    assert "/meetings/{meeting_id}/voices" in paths
    assert "/meetings/{meeting_id}/process" in paths
    assert "/meetings/{meeting_id}/processing" in paths
    assert "/processing/jobs/{job_id}" in paths
    assert "/voices/{voice_id}/results" in paths
    assert "/results/{result_id}" in paths
    assert "/results/{result_id}/transcript" in paths
    assert "/history" in paths


def test_backend_openapi_exposes_meeting_composer_endpoints():
    paths = app.openapi()["paths"]
    assert "post" in paths["/meetings/{meeting_id}/compose"]
    assert "get" in paths["/meetings/{meeting_id}/results"]
    assert "get" in paths["/meeting-results/{meeting_result_id}"]
    assert "get" in paths["/meeting-results/{meeting_result_id}/transcript"]
    assert "get" in paths["/meetings/{meeting_id}/transcript"]


def test_compose_keeps_root_exception_and_uses_docker_api_file():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    assert (PROJECT_ROOT / "Docker" / "api.Dockerfile").is_file()
    assert "dockerfile: Docker/api.Dockerfile" in compose
    assert '"127.0.0.1:5252:5432"' in compose
    assert "qdrant" not in compose.lower()


def test_only_api_ui_and_diarization_have_dockerfiles_and_compose_services():
    # asr, cleaner, meeting-composer, mcp-worker, and the outbox dispatcher
    # all run as background asyncio tasks inside the api process now (see
    # app.main.BACKGROUND_WORKERS) instead of their own container. Only
    # diarization keeps a separate image, because it alone carries heavy,
    # non-portable ML dependencies (torch/pyannote) that the rest of the
    # system must not be forced to ship.
    dockerfiles = {path.name for path in (PROJECT_ROOT / "Docker").glob("*.Dockerfile")}
    assert dockerfiles == {"api.Dockerfile", "ui.Dockerfile", "diarization.Dockerfile"}

    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    for removed_service in (
        "asr:",
        "cleaner:",
        "meeting-composer:",
        "mcp-worker:",
        "broker-dispatcher:",
    ):
        assert removed_service not in compose


def test_background_workers_cover_every_merged_service():
    names = {name for name, _ in BACKGROUND_WORKERS}
    assert names == {
        "broker-dispatcher",
        "asr-worker",
        "cleaner-worker",
        "meeting-composer-worker",
        "mcp-worker",
    }
    # Every runner must be the embeddable `run(settings)` coroutine, not the
    # standalone `run_forever()` -- the latter reconfigures global logging
    # and disposes the shared DB engine on exit, which would break the
    # other workers/the API sharing that same process and engine.
    for name, runner in BACKGROUND_WORKERS:
        assert runner.__name__ == "run", name


def test_merged_worker_packages_live_under_fast_backend_not_src():
    # These packages used to ship in their own Docker image each; they now
    # run inside the api process, so they belong next to app/ under
    # fast_Backend/ rather than in the standalone src/ tree.
    for package in ("asr", "cleaner", "meeting_composer", "mcp_worker", "alignment"):
        assert (PROJECT_ROOT / "fast_Backend" / package / "__init__.py").is_file(), package
        assert not (PROJECT_ROOT / "src" / package).exists(), package


def test_asr_benchmark_marker_is_still_excluded_from_ci():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()
    assert '-m "not asr_benchmark"' in workflow


def test_pyannote_is_an_isolated_local_cpu_service():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    dockerfile = (PROJECT_ROOT / "Docker" / "diarization.Dockerfile").read_text()
    requirements = (
        PROJECT_ROOT / "Docker" / "diarization.requirements.lock"
    ).read_text()

    assert "dockerfile: Docker/diarization.Dockerfile" in compose
    assert "DIARIZATION_DEVICE: cpu" in compose
    assert 'python", "-m", "diarization.bootstrap"' in dockerfile
    assert "HF_HUB_DOWNLOAD_TIMEOUT" in compose
    assert "TORCH_HOME: /var/cache/huggingface/torch" in compose
    assert "PYANNOTE_CACHE: /var/cache/huggingface/pyannote" in compose
    assert "COPY src/diarization ./diarization" in dockerfile
    assert "pyannote-audio==4.0.7" in requirements
    assert "torch-2.8.0%2Bcpu" in requirements
    assert "torchaudio-2.8.0%2Bcpu" in requirements
    assert "torchcodec-0.7.0" in requirements
    assert "nvidia-" not in requirements


def test_cleaner_and_meeting_composer_carry_no_ml_runtime_deps():
    # These now share the api image, so nothing about *their* code should
    # be what pulls torch/pyannote in -- test_api_dependencies_do_not_include_ml_runtimes
    # already guards pyproject.toml itself.
    for package in ("cleaner", "meeting_composer", "asr", "mcp_worker"):
        for source_file in (PROJECT_ROOT / "fast_Backend" / package).rglob("*.py"):
            source = source_file.read_text()
            assert "import torch" not in source, source_file
            assert "pyannote" not in source.lower(), source_file


def test_rabbitmq_topology_and_dispatcher_are_part_of_compose():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    environment = (PROJECT_ROOT / ".env.example").read_text()
    project = (PROJECT_ROOT / "pyproject.toml").read_text()

    assert "rabbitmq:4.1-management-alpine" in compose
    assert "rabbitmq_data:/var/lib/rabbitmq" in compose
    assert "aio-pika" in project
    for queue_name in ("asr.queue", "diar.queue", "cleaning.queue", "mcp.queue"):
        assert queue_name in environment


def test_every_image_that_imports_app_services_processing_ships_its_dependencies():
    # app/services/processing.py imports meeting_composer.composer (shared
    # fingerprint/offset algorithm) and alignment.merge/alignment.types
    # (shared word/speaker merge algorithm + DiarizationTurn/DiarizationError)
    # at module level, so importing *anything* from that module -- even just
    # queue_result_cleaning -- transitively requires both packages on the
    # image. Both are dependency-free by design (no ML imports at module
    # level, verified in test_cleaner_and_meeting_composer_carry_no_ml_runtime_deps),
    # which is exactly what makes them safe to share -- unlike the real
    # diarization worker code (Pyannote/torch), which must stay inside the
    # diarization image alone (see the isolation test right after this one).
    for dockerfile_name in ("api.Dockerfile", "diarization.Dockerfile"):
        dockerfile = (PROJECT_ROOT / "Docker" / dockerfile_name).read_text()
        assert "COPY fast_Backend/meeting_composer ./meeting_composer" in dockerfile, dockerfile_name
        assert "COPY fast_Backend/alignment ./alignment" in dockerfile, dockerfile_name

    api_dockerfile = (PROJECT_ROOT / "Docker" / "api.Dockerfile").read_text()
    for package in ("asr", "cleaner", "mcp_worker"):
        assert f"COPY fast_Backend/{package} ./{package}" in api_dockerfile


def test_diarization_service_code_stays_inside_its_own_image():
    # The inverse of the test above: only the diarization image may ship the
    # actual diarization service code (Pyannote provider, worker, chunking).
    # Every other service must talk to diarization results through the
    # database/object storage only, never by importing its worker code.
    api_dockerfile = (PROJECT_ROOT / "Docker" / "api.Dockerfile").read_text()
    assert "diarization" not in api_dockerfile

    for package in ("asr", "cleaner", "meeting_composer", "mcp_worker"):
        for source_file in (PROJECT_ROOT / "fast_Backend" / package).rglob("*.py"):
            source = source_file.read_text()
            assert not any(
                line.startswith(("import diarization", "from diarization"))
                for line in source.splitlines()
            ), source_file

    processing_source = (
        PROJECT_ROOT / "fast_Backend" / "app" / "services" / "processing.py"
    ).read_text()
    assert not any(
        line.startswith(("import diarization", "from diarization"))
        for line in processing_source.splitlines()
    )
