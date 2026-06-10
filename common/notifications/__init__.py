"""GCP → Discord notification forwarder.

Generic Cloud Run service that forwards GCP Pub/Sub events
(builds, deployments, etc.) to Discord webhooks.
"""

from .notifier import app, handle_pubsub_push, health

__all__ = ["app", "handle_pubsub_push", "health"]
