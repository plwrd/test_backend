import json
import uuid
import logging
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

_producer = None
FALLBACK_OUTBOX_KEY = "kafka:outbox:fallback"


def build_message(event_type: str, payload: dict) -> dict:
    return {
        "event_type": event_type,
        "correlation_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _get_producer():
    global _producer
    if _producer is None:
        from confluent_kafka import Producer

        _producer = Producer(settings.KAFKA_CONFIG)
    return _producer


def _store_fallback(message: dict):
    try:
        from django_redis import get_redis_connection

        conn = get_redis_connection("default")
        conn.rpush(FALLBACK_OUTBOX_KEY, json.dumps(message))
        logger.warning(
            "Kafka unavailable; message %s queued in Redis outbox for retry.",
            message.get("correlation_id"),
        )
    except Exception:
        logger.exception(
            "Kafka fallback to Redis failed; message %s may be lost.",
            message.get("correlation_id"),
        )


def _delivery_report(err, msg):
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
    message = build_message(event_type, payload)
    encoded = json.dumps(message).encode("utf-8")

    try:
        producer = _get_producer()
        producer.produce(topic, value=encoded, callback=_delivery_report)
        producer.poll(0)
    except BufferError:
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
    _publish(settings.KAFKA_TOPICS["credit_consumed"], "credit_consumed", booking_data)


def publish_credit_granted(grant_data: dict):
    _publish(settings.KAFKA_TOPICS["credit_granted"], "credit_granted", grant_data)
