import tomllib
from pathlib import Path

from app.main import app


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


def test_asr_is_an_isolated_service_and_live_benchmark_is_excluded_from_ci():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    dockerfile = (PROJECT_ROOT / "Docker" / "asr.Dockerfile").read_text()
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "dockerfile: Docker/asr.Dockerfile" in compose
    assert 'python", "-m", "asr.worker"' in dockerfile
    assert "COPY src/asr ./asr" in dockerfile
    assert "dockerfile: Docker/asr.Dockerfile" in workflow
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


def test_cleaner_is_an_isolated_remote_api_worker():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    dockerfile = (PROJECT_ROOT / "Docker" / "cleaner.Dockerfile").read_text()
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "dockerfile: Docker/cleaner.Dockerfile" in compose
    assert 'python", "-m", "cleaner.worker"' in dockerfile
    assert "COPY src/cleaner ./cleaner" in dockerfile
    assert "dockerfile: Docker/cleaner.Dockerfile" in workflow
    assert "torch" not in dockerfile


def test_meeting_composer_is_an_isolated_cpu_only_worker():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    dockerfile = (PROJECT_ROOT / "Docker" / "meeting-composer.Dockerfile").read_text()
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "dockerfile: Docker/meeting-composer.Dockerfile" in compose
    assert 'python", "-m", "meeting_composer.worker"' in dockerfile
    assert "COPY src/meeting_composer ./meeting_composer" in dockerfile
    assert "dockerfile: Docker/meeting-composer.Dockerfile" in workflow
    assert "torch" not in dockerfile
    assert "pyannote" not in dockerfile.lower()


def test_every_image_that_imports_app_services_processing_ships_meeting_composer():
    # app/services/processing.py imports meeting_composer.composer at module
    # level (shared fingerprint/offset algorithm), so importing *anything*
    # from that module -- even just queue_result_diarization or
    # queue_result_cleaning -- transitively requires the meeting_composer
    # package to be on the image. Each of these Dockerfiles copies its own
    # file list independently, so each must be checked independently: a
    # missing COPY here is a container that builds fine and then
    # ModuleNotFoundError-crash-loops at runtime (caught live in this repo
    # while wiring the feature in).
    for dockerfile_name in (
        "api.Dockerfile",
        "cleaner.Dockerfile",
        "asr.Dockerfile",
        "diarization.Dockerfile",
    ):
        dockerfile = (PROJECT_ROOT / "Docker" / dockerfile_name).read_text()
        assert "COPY src/meeting_composer ./meeting_composer" in dockerfile, dockerfile_name
