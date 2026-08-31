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
