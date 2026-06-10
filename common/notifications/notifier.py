"""
GCP → Discord Notification Forwarder

Generic Cloud Run notifier that receives GCP Pub/Sub push messages
and forwards them to Discord via rich embeds.

Usage (Cloud Run entrypoint):
    from common.notifications import app
    # Or: uvicorn common.notifications:app --host 0.0.0.0 --port 8080

Supported event types:
    - Cloud Build (SUCCESS, FAILURE, TIMEOUT, CANCELLED, etc.)
    - Extensible: add new handlers in handle_pubsub_push()

Environment:
    DISCORD_WEBHOOK_URL — required, the Discord webhook to POST to
"""

import base64
import html
import json
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


# ─── Config ─────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "gcp-discord-notifier")


# ─── Discord formatting ─────────────────────────────────────────────────────


def _build_discord_embed(event_type: str, data: dict) -> dict:
    """Build a Discord embed from a Pub/Sub notification payload."""
    project = data.get("projectData", [{}])[0].get("projectId", "unknown")
    build_data = data.get("build", {})

    status = build_data.get("status", "UNKNOWN")
    build_id = build_data.get("id", "")
    source = build_data.get("source", {}).get("provenance", {}).get("git", {})
    repo = source.get("repo", "unknown")
    commit = source.get("commitSha", "")[:7]
    branch = source.get("branch", "")
    trigger_id = build_data.get("buildTriggerId", "")

    status_config = {
        "SUCCESS": ("✅", 3066993),
        "FAILURE": ("🚨", 15158332),
        "CANCELLED": ("⚠️", 10070724),
        "INTERNAL_ERROR": ("❌", 15158332),
        "TIMEOUT": ("⏱️", 15158332),
        "QUEUED": ("🟡", 16776960),
        "WORKING": ("🔄", 3447003),
    }
    emoji, color = status_config.get(status, ("🔔", 7506394))

    log_url = ""
    if project and build_id:
        log_url = (
            f"https://console.cloud.google.com/cloud-build/builds/{build_id}"
            f"?project={project}"
        )

    description_parts = [
        f"**Project:** `{project}`",
        f"**Repo:** {repo}",
        f"**Branch:** `{branch}`",
        f"**Commit:** `{commit}`",
    ]
    if trigger_id:
        description_parts.append(f"**Trigger:** `{trigger_id}`")

    description = "\n".join(description_parts)

    images = build_data.get("images", [])
    fields = []
    if images:
        img_names = ", ".join(
            i.get("name", "").split("@")[0].split(":")[0]
            for i in images
            if i.get("name")
        )
        if img_names:
            fields.append({"name": "Images", "value": img_names, "inline": False})

    status_detail = build_data.get("statusDetail", "")
    if status_detail:
        fields.append(
            {
                "name": "Details",
                "value": html.escape(status_detail[:200]),
                "inline": False,
            }
        )

    embed = {
        "username": "Cloud Build Bot",
        "embeds": [
            {
                "title": f"{emoji} Cloud Build {status}",
                "description": description,
                "color": color,
                "url": log_url or None,
                "fields": fields or None,
                "footer": {"text": f"Build: {build_id}"},
                "timestamp": build_data.get("createTime", ""),
            }
        ],
    }

    embed["embeds"][0] = {k: v for k, v in embed["embeds"][0].items() if v is not None}

    return embed


async def _forward_to_discord(embed: dict) -> None:
    """POST embed payload to Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set, skipping Discord notification")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(DISCORD_WEBHOOK_URL, json=embed)
        if resp.status_code not in (200, 204):
            print(f"DISCORD ERROR: {resp.status_code} {resp.text}")


# ─── Event handlers ─────────────────────────────────────────────────────────


async def handle_build_event(data: dict) -> None:
    """Process a Cloud Build notification payload."""
    embed = _build_discord_embed("cloud_build", data)
    await _forward_to_discord(embed)


# ─── Pub/Sub push endpoint ──────────────────────────────────────────────────


async def handle_pubsub_push(request: Request) -> JSONResponse:
    """
    Receives HTTP POST from Pub/Sub push subscription.
    Message payload is base64-encoded in body.message.data.
    """
    if request.headers.get("content-type") != "application/json":
        return JSONResponse({"error": "unexpected content-type"}, status_code=400)

    body = await request.json()
    message = body.get("message", {})
    data_b64 = message.get("data", "")

    if not data_b64:
        print("Subscription ping / verification message")
        return JSONResponse({"ok": True})

    try:
        data_json = base64.b64decode(data_b64).decode("utf-8")
        data = json.loads(data_json)
    except Exception as e:
        print(f"Failed to decode message: {e}")
        return JSONResponse({"error": "decode error"}, status_code=400)

    attrs = message.get("attributes", {})
    event_type = attrs.get("eventType", attrs.get("type", "build"))

    print(
        f"[notifier] event_type={event_type} build_status={data.get('build', {}).get('status', '?')}"
    )

    if "build" in event_type.lower() or "build" in data:
        await handle_build_event(data)
    else:
        print(f"Unhandled event type: {event_type}")

    return JSONResponse({"ok": True})


# ─── Health check ───────────────────────────────────────────────────────────


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE_NAME})


# ─── ASGI app ───────────────────────────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/push", handle_pubsub_push, methods=["POST"]),
    ]
)
