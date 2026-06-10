"""Tests for the GCP → Discord notification forwarder."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from common.notifications import app
from common.notifications.notifier import (
    _build_discord_embed,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_build_payload():
    """Realistic Cloud Build Pub/Sub message body (decoded)."""
    return {
        "projectData": [{"projectId": "my-gcp-project"}],
        "build": {
            "id": "abc123def456",
            "status": "SUCCESS",
            "createTime": "2026-06-10T12:00:00Z",
            "buildTriggerId": "trigger-1",
            "source": {
                "provenance": {
                    "git": {
                        "repo": "https://github.com/owner/my-repo",
                        "commitSha": "deadbeef1234567890abcdef",
                        "branch": "main",
                    }
                }
            },
            "images": [
                {"name": "us-central1-docker.pkg.dev/proj/app/app:abc123"},
            ],
        },
    }


# ─── Embed formatting ───────────────────────────────────────────────────────


def test_embed_success_uses_green_emoji():
    embed = _build_discord_embed(
        "cloud_build",
        {
            "projectData": [{"projectId": "p"}],
            "build": {
                "id": "b1",
                "status": "SUCCESS",
                "source": {
                    "provenance": {
                        "git": {"repo": "r", "commitSha": "abcdef0", "branch": "main"}
                    }
                },
            },
        },
    )
    title = embed["embeds"][0]["title"]
    assert "✅" in title
    assert "SUCCESS" in title
    assert embed["embeds"][0]["color"] == 3066993


def test_embed_failure_uses_red_emoji():
    embed = _build_discord_embed(
        "cloud_build",
        {
            "projectData": [{"projectId": "p"}],
            "build": {
                "id": "b1",
                "status": "FAILURE",
                "source": {
                    "provenance": {
                        "git": {"repo": "r", "commitSha": "abcdef0", "branch": "main"}
                    }
                },
            },
        },
    )
    assert "🚨" in embed["embeds"][0]["title"]
    assert embed["embeds"][0]["color"] == 15158332


def test_embed_truncates_long_commit_sha():
    embed = _build_discord_embed(
        "cloud_build",
        {
            "projectData": [{"projectId": "p"}],
            "build": {
                "id": "b1",
                "status": "SUCCESS",
                "source": {
                    "provenance": {
                        "git": {"repo": "r", "commitSha": "a" * 40, "branch": "main"}
                    }
                },
            },
        },
    )
    description = embed["embeds"][0]["description"]
    assert "`" + "a" * 7 + "`" in description
    assert "a" * 40 not in description


def test_embed_unknown_status_uses_default_emoji():
    embed = _build_discord_embed(
        "cloud_build",
        {
            "projectData": [{"projectId": "p"}],
            "build": {
                "id": "b1",
                "status": "WEIRD_STATUS",
                "source": {
                    "provenance": {
                        "git": {"repo": "r", "commitSha": "abc", "branch": "main"}
                    }
                },
            },
        },
    )
    assert "🔔" in embed["embeds"][0]["title"]


# ─── Health endpoint ─────────────────────────────────────────────────────────


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "service" in data


# ─── Pub/Sub push endpoint ───────────────────────────────────────────────────


def test_push_rejects_non_json_content_type(client):
    resp = client.post("/push", data="not json", headers={"content-type": "text/plain"})
    assert resp.status_code == 400


def test_push_handles_subscription_verification_ping(client):
    """Empty data field means GCP is verifying the push endpoint."""
    resp = client.post("/push", json={"message": {"data": ""}})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_push_decodes_build_event_and_forwards(
    client, sample_build_payload, monkeypatch
):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    forward_mock = AsyncMock()
    monkeypatch.setattr(
        "common.notifications.notifier._forward_to_discord",
        forward_mock,
    )

    encoded = base64.b64encode(json.dumps(sample_build_payload).encode()).decode()
    body = {
        "message": {
            "data": encoded,
            "attributes": {"eventType": "build"},
        }
    }

    resp = client.post("/push", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    forward_mock.assert_awaited_once()
    embed = forward_mock.call_args[0][0]
    assert "SUCCESS" in embed["embeds"][0]["title"]


def test_push_skips_discord_when_webhook_unset(
    client, sample_build_payload, monkeypatch, capsys
):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    encoded = base64.b64encode(json.dumps(sample_build_payload).encode()).decode()
    body = {"message": {"data": encoded, "attributes": {"eventType": "build"}}}

    resp = client.post("/push", json=body)
    assert resp.status_code == 200
    captured = capsys.readouterr()
    assert "DISCORD_WEBHOOK_URL not set" in captured.out


def test_push_returns_decode_error_on_bad_base64(client):
    body = {"message": {"data": "!!!not base64!!!"}}
    resp = client.post("/push", json=body)
    assert resp.status_code == 400
    assert "decode" in resp.json()["error"]


def test_push_handles_unrecognized_event_type(client, monkeypatch, capsys):
    """Non-build events are logged but don't fail."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    payload = {"some": "data"}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    body = {"message": {"data": encoded, "attributes": {"eventType": "audit_log"}}}

    resp = client.post("/push", json=body)
    assert resp.status_code == 200
    captured = capsys.readouterr()
    assert "Unhandled event type" in captured.out
