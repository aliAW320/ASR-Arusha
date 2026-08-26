import os
import uuid

import httpx
import pytest


BASE_URL = os.getenv("INTEGRATION_BASE_URL")


@pytest.mark.skipif(not BASE_URL, reason="INTEGRATION_BASE_URL is not configured")
def test_auth_meeting_and_real_minio_upload_round_trip():
    email = f"integration-{uuid.uuid4()}@example.com"
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        registered = client.post(
            "/auth/register",
            json={"email": email, "password": "integration-password"},
            headers={
                "X-Request-ID": "compose-registration",
                "X-Correlation-ID": "compose-flow",
            },
        )
        assert registered.status_code == 201, registered.text
        assert registered.headers["x-request-id"] == "compose-registration"
        assert registered.headers["x-correlation-id"] == "compose-flow"
        headers = {
            "Authorization": f"Bearer {registered.json()['access_token']}"
        }

        meeting = client.post(
            "/meetings",
            headers=headers,
            json={"title": "Compose integration"},
        )
        assert meeting.status_code == 201, meeting.text
        meeting_id = meeting.json()["id"]

        uploaded = client.post(
            f"/meetings/{meeting_id}/voices",
            headers=headers,
            files={"upload": ("integration.wav", b"RIFF-integration", "audio/wav")},
        )
        assert uploaded.status_code == 201, uploaded.text
        voice_id = uploaded.json()["id"]
        assert uploaded.json()["minio_key"].startswith(
            f"meetings/{meeting_id}/voices/{voice_id}/source/"
        )

        listed = client.get(f"/meetings/{meeting_id}/voices", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [voice_id]

        removed = client.delete(f"/voices/{voice_id}", headers=headers)
        assert removed.status_code == 204, removed.text
        deleted_meeting = client.delete(f"/meetings/{meeting_id}", headers=headers)
        assert deleted_meeting.status_code == 204, deleted_meeting.text
