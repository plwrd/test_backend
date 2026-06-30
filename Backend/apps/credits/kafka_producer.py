"""
apps/credits/kafka_producer.py

Async Kafka credit event publisher.

RULES:
1. Kafka publishing must NEVER block the API response
2. If the Kafka broker is unavailable, the message must NOT be lost
3. Use a fallback strategy -- describe it in the comment block below
4. All messages must include: event_type, payload, timestamp,
   correlation_id
5. Producer errors must be logged, never silently swallowed

This service runs inside a Kuma service mesh.
The Kafka broker address comes from environment variables only.
Never hardcode broker addresses.

--- YOUR FALLBACK STRATEGY ---
When the Kafka broker is unavailable:
[Describe your fallback here. What happens to the message? How is it
retried? Where is it stored while the broker is down? What guarantees
does your strategy provide and what does it explicitly NOT guarantee?]
---
"""

import uuid
import logging
from datetime import datetime, timezone
from django.conf import settings

logger = logging.getLogger(__name__)


def build_message(event_type: str, payload: dict) -> dict:
    """
    Build a standardized Kafka message envelope.

    Every message published to Kafka must use this envelope.
    Do not publish raw payloads.

    This function is already implemented. Do not modify it.
    """
    return {
        "event_type": event_type,
        "correlation_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def publish_credit_consumed(booking_data: dict):
    """
    Publish a credit consumed event to Kafka after a successful booking.

    Topic: settings.KAFKA_TOPICS["credit_consumed"]

    TODO: Implement this function.

    Requirements:
    1. Must be non-blocking -- the API response must NOT wait for Kafka
    2. Use build_message() to wrap the payload
    3. Use confluent_kafka.Producer to publish
    4. Implement a delivery callback that logs success or failure
    5. If the broker is unavailable, execute your fallback strategy
       as described in the comment block above
    6. Producer config must come from settings.KAFKA_CONFIG only
    7. TODO: Add a comment explaining why this must be non-blocking and
       what the consequence would be if it blocked the response thread

    Example payload structure:
    {
        "booking_id": 88,
        "plan_id": 14,
        "user_id": 201,
        "workspace_id": 3,
        "credits_consumed": 5,
        "status": "confirmed",
        "booked_at": "2026-06-01T09:00:00Z"
    }
    """
    raise NotImplementedError("TODO: implement publish_credit_consumed")


def publish_credit_granted(grant_data: dict):
    """
    Publish a credit granted event to Kafka after a successful top-up.

    Topic: settings.KAFKA_TOPICS["credit_granted"]

    TODO: Implement this function.

    Same requirements as publish_credit_consumed.

    Example payload structure:
    {
        "grant_id": 19,
        "user_id": 201,
        "workspace_id": 3,
        "acting_user_id": 7,
        "source": "topup",
        "amount": 50,
        "granted_at": "2026-06-01T09:00:00Z"
    }
    """
    raise NotImplementedError("TODO: implement publish_credit_granted")
