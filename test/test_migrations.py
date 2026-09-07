import os
import subprocess
import sys
from pathlib import Path

import uuid

from sqlalchemy import create_engine, inspect, text


def test_initial_migration_builds_the_backend_schema(tmp_path):
    database_path = tmp_path / "migration.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    environment["JWT_SECRET_KEY"] = "test-secret-key-that-is-at-least-32-characters"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    migrated_engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(migrated_engine)
    tables = set(inspector.get_table_names())
    assert {
        "alembic_version",
        "users",
        "auth_identities",
        "meetings",
        "meeting_members",
        "voices",
        "history",
        "broker_outbox_messages",
    } <= tables
    history_columns = {column["name"] for column in inspector.get_columns("history")}
    assert "correlation_id" in history_columns
    outbox_columns = {
        column["name"]
        for column in inspector.get_columns("broker_outbox_messages")
    }
    assert {
        "id",
        "queue_name",
        "payload",
        "deduplication_key",
        "available_at",
        "published_at",
        "publish_attempts",
        "last_error",
        "created_at",
    } == outbox_columns


def test_migration_adopts_legacy_create_all_schema(tmp_path):
    database_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database_path}")
    user_id = str(uuid.uuid4())
    meeting_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE users (
                id CHAR(32) PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                full_name VARCHAR(150),
                hashed_password VARCHAR(255) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE meetings (
                id CHAR(32) PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                meeting_date DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                owner_id CHAR(32) NOT NULL REFERENCES users(id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE meeting_members (
                meeting_id CHAR(32) NOT NULL REFERENCES meetings(id),
                user_id CHAR(32) NOT NULL REFERENCES users(id),
                role VARCHAR(11) NOT NULL,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT meeting_member_role CHECK (role IN ('contributor', 'viewer')),
                PRIMARY KEY (meeting_id, user_id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE history (
                id CHAR(32) PRIMARY KEY,
                action_description TEXT NOT NULL,
                actor_user_id CHAR(32) NOT NULL REFERENCES users(id),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
        """))
        connection.execute(
            text("INSERT INTO users (id, email, hashed_password) VALUES (:id, :email, :password)"),
            {"id": user_id.replace("-", ""), "email": "legacy@example.com", "password": "hash"},
        )
        connection.execute(
            text("INSERT INTO meetings (id, title, owner_id) VALUES (:id, 'Legacy', :owner)"),
            {"id": meeting_id.replace("-", ""), "owner": user_id.replace("-", "")},
        )

    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    environment["JWT_SECRET_KEY"] = "test-secret-key-that-is-at-least-32-characters"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    history_columns = {column["name"] for column in inspect(engine).get_columns("history")}
    assert "role" in columns
    assert "hashed_password" not in columns
    assert "correlation_id" in history_columns
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT provider, subject, secret_hash FROM auth_identities")
        ).one()
        owner_role = connection.execute(text("SELECT role FROM meeting_members")).scalar_one()
    assert identity == ("local", "legacy@example.com", "hash")
    assert owner_role == "owner"


def test_enum_check_constraints_are_limited_to_known_reflection_exceptions():
    environment_source = (Path(os.getcwd()) / "alembic" / "env.py").read_text()
    assert '"user_role"' in environment_source
    assert '"meeting_member_role"' in environment_source
    assert '"voice_status"' in environment_source


def test_meeting_composer_migration_adds_source_lineage_and_fingerprint(tmp_path):
    database_path = tmp_path / "composer.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    environment["JWT_SECRET_KEY"] = "test-secret-key-that-is-at-least-32-characters"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)

    meeting_result_columns = {
        column["name"] for column in inspector.get_columns("meeting_results")
    }
    assert {"source_fingerprint", "schema_version"} <= meeting_result_columns

    source_columns = {
        column["name"] for column in inspector.get_columns("meeting_result_sources")
    }
    assert {
        "meeting_result_id",
        "result_id",
        "position",
        "voice_sequence_snapshot",
        "source_offset_ms",
        "source_duration_ms",
        "source_artifact_id",
    } <= source_columns

    unique_constraints = {
        tuple(sorted(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints("meeting_results")
    }
    assert tuple(sorted(("meeting_id", "source_fingerprint"))) in unique_constraints

    source_unique_constraints = {
        tuple(sorted(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints("meeting_result_sources")
    }
    assert (
        tuple(sorted(("meeting_result_id", "position")))
        in source_unique_constraints
    )

    with engine.connect() as connection:
        foreign_keys = inspector.get_foreign_keys("meeting_result_sources")
    artifact_fk = next(
        fk for fk in foreign_keys if fk["constrained_columns"] == ["source_artifact_id"]
    )
    assert artifact_fk["referred_table"] == "result_artifacts"


def test_meeting_composer_migration_downgrade_restores_plain_association_table(tmp_path):
    database_path = tmp_path / "composer-downgrade.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    environment["JWT_SECRET_KEY"] = "test-secret-key-that-is-at-least-32-characters"
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "b7c21e08d4f1"],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(engine)
    source_columns = {
        column["name"] for column in inspector.get_columns("meeting_result_sources")
    }
    assert source_columns == {"meeting_result_id", "result_id"}
    meeting_result_columns = {
        column["name"] for column in inspector.get_columns("meeting_results")
    }
    assert "source_fingerprint" not in meeting_result_columns
    assert "schema_version" not in meeting_result_columns
