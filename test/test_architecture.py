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
    assert "/history" in paths


def test_compose_keeps_root_exception_and_uses_docker_api_file():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    assert (PROJECT_ROOT / "Docker" / "api.Dockerfile").is_file()
    assert "dockerfile: Docker/api.Dockerfile" in compose
    assert '"127.0.0.1:5252:5432"' in compose
    assert "qdrant" not in compose.lower()
