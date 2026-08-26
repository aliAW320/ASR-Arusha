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

    tables = set(inspect(create_engine(f"sqlite:///{database_path}")).get_table_names())
    assert {
        "alembic_version",
        "users",
        "auth_identities",
        "meetings",
        "meeting_members",
        "voices",
        "history",
    } <= tables


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
    assert "role" in columns
    assert "hashed_password" not in columns
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
