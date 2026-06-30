"""
apps/credits/kafka_producer.py

Async Kafka credit event publisher.

RULES:
1. Kafka publishing must NEVER block the API response
2. If the Kafka broker is unavailable, the message must NOT be lost
3. Use a fallback strategy -- described in the comment block below
4. All messages must include: event_type, payload, timestamp,
   correlation_id
5. Producer errors must be logged, never silently swallowed

This service runs inside a Kuma service mesh.
The Kafka broker address comes from environment variables only
(settings.KAFKA_CONFIG, populated from KAFKA_BOOTSTRAP_SERVERS).
Never hardcode broker addresses.

--- FALLBACK STRATEGY ---
When the broker is unavailable (produce raises, the local queue is full, or
the async delivery callback reports failure), the fully-built message
envelope is pushed to a Redis list ("kafka:outbox:fallback") -- the same
Redis the app already depends on. This is a transactional-outbox-style
buffer: the event survives the broker outage in durable-ish storage and a
background worker (e.g. a Celery beat task) drains the list and re-publishes
once the broker is healthy.

Guarantees:
- AT-LEAST-ONCE delivery: an event is either delivered now or queued for
  retry; it is not dropped on a transient broker outage. Re-publishing after
  a partial failure may produce duplicates, so downstream consumers must be
  idempotent (they can dedupe on correlation_id).
Explicitly NOT guaranteed:
- Exactly-once delivery (duplicates are possible on retry).
- Strict global ordering (a fallback-then-drain event can land after a
  later live event).
- Durability beyond Redis: if Redis ALSO is unavailable, the event is logged
  at ERROR and lost -- that boundary is logged loudly, never swallowed.
---
"""

import json
import uuid
import logging
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# Module-level singleton. confluent_kafka.Producer is thread-safe and is meant
# to be reused; creating one per request would be slow and would defeat
# batching. Created lazily so importing this module never requires a broker.
_producer = None

FALLBACK_OUTBOX_KEY = "kafka:outbox:fallback"


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


def _get_producer():
    """Lazily build and cache the Producer from settings.KAFKA_CONFIG only."""
    global _producer
    if _producer is None:
        from confluent_kafka import Producer

        _producer = Producer(settings.KAFKA_CONFIG)
    return _producer


def _store_fallback(message: dict):
    """Persist a message to the Redis outbox for later retry (see strategy)."""
    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
        conn.rpush(FALLBACK_OUTBOX_KEY, json.dumps(message))
        logger.warning(
            "Kafka unavailable; message %s queued in Redis outbox for retry.",
            message.get("correlation_id"),
        )
    except Exception:
        # Redis fallback itself failed -- this is the only path where the
        # message can be lost, so log loudly and never swallow it silently.
        logger.exception(
            "Kafka fallback to Redis FAILED; message %s may be lost.",
            message.get("correlation_id"),
        )


def _delivery_report(err, msg):
    """
    Async delivery callback invoked by a later poll() call -- NOT in the
    request path. Logs success/failure and re-queues failures.
    """
    if err is not None:
        logger.error("Kafka delivery failed (topic=%s): %s", msg.topic(), err)
        try:
            _store_fallback(json.loads(msg.value()))
        except Exception:
            logger.exception("Failed to enqueue undelivered Kafka message.")
    else:
        logger.info(
            "Kafka delivered (topic=%s partition=%s offset=%s).",
            msg.topic(),
            msg.partition(),
            msg.offset(),
        )


def _publish(topic: str, event_type: str, payload: dict):
    """
    Non-blocking publish.

    Why non-blocking matters: this runs inside the synchronous request/
    response cycle of a WSGI worker thread. If we blocked on the broker
    (e.g. flush()), the user's HTTP response would wait on Kafka's latency
    and availability -- a slow or down broker would stall the worker, inflate
    p99 latency, exhaust the worker pool, and ultimately time out a booking
    that has ALREADY been committed to the database. The event is a
    side-effect; it must never gate the response.

    produce() only enqueues into the local librdkafka buffer; poll(0) serves
    any delivery callbacks that have ALREADY completed and returns
    immediately. We deliberately never call flush() here.
    """
    message = build_message(event_type, payload)
    encoded = json.dumps(message).encode("utf-8")

    try:
        producer = _get_producer()
        producer.produce(topic, value=encoded, callback=_delivery_report)
        producer.poll(0)  # non-blocking: drain completed callbacks only
    except BufferError:
        # Local queue is full (broker likely backed up/down). Serve callbacks
        # once to free space, then fall back rather than block.
        logger.error("Kafka local queue full for topic=%s; falling back.", topic)
        try:
            _get_producer().poll(0)
        except Exception:
            logger.exception("poll(0) failed while handling BufferError.")
        _store_fallback(message)
    except Exception as exc:
        logger.error("Kafka produce failed (topic=%s): %s; falling back.", topic, exc)
        _store_fallback(message)


def publish_credit_consumed(booking_data: dict):
    """
    Publish a credit consumed event after a successful booking.
    Topic: settings.KAFKA_TOPICS["credit_consumed"].

    Example payload:
    {
        "booking_id": 88, "plan_id": 14, "user_id": 201, "workspace_id": 3,
        "credits_consumed": 5, "status": "confirmed",
        "booked_at": "2026-06-01T09:00:00Z"
    }
    """
    _publish(
        settings.KAFKA_TOPICS["credit_consumed"], "credit_consumed", booking_data
    )


def publish_credit_granted(grant_data: dict):
    """
    Publish a credit granted event after a successful top-up.
    Topic: settings.KAFKA_TOPICS["credit_granted"].

    Example payload:
    {
        "grant_id": 19, "user_id": 201, "workspace_id": 3, "acting_user_id": 7,
        "source": "topup", "amount": 50, "granted_at": "2026-06-01T09:00:00Z"
    }
    """
    _publish(
        settings.KAFKA_TOPICS["credit_granted"], "credit_granted", grant_data
    )
