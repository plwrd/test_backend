import os, sys, django, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
from apps.credits import kafka_producer as kp
from confluent_kafka import Consumer, TopicPartition

topic = settings.KAFKA_TOPICS["credit_consumed"]
servers = settings.KAFKA_CONFIG["bootstrap.servers"]
print("Topic:", topic, "servers:", servers)

# Seek to the CURRENT end of the partition before publishing, so we only read
# the message this run produces (the topic may already hold events from tests).
c = Consumer({
    "bootstrap.servers": servers,
    "group.id": "integration-check-group",
    "auto.offset.reset": "latest",
})
tp = TopicPartition(topic, 0)
_, high = c.get_watermark_offsets(tp, timeout=10)  # next offset to be written
print("partition end offset before publish:", high)
c.assign([TopicPartition(topic, 0, high)])

# Publish through the PROJECT'S real producer code path.
kp.publish_credit_consumed({
    "booking_id": 88, "plan_id": 14, "user_id": 201, "workspace_id": 3,
    "credits_consumed": 5, "status": "confirmed", "booked_at": "2026-06-01T09:00:00Z",
})
remaining = kp._get_producer().flush(10)  # force delivery (request path never flushes)
print("flush remaining (0 = all delivered):", remaining)

got = None
deadline = time.time() + 15
while time.time() < deadline:
    msg = c.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        print("consumer error:", msg.error()); continue
    got = json.loads(msg.value())
    break
c.close()

assert got is not None, "FAIL: no message consumed from Kafka"
assert got["event_type"] == "credit_consumed", got
assert got["payload"]["booking_id"] == 88, got
assert "correlation_id" in got and "timestamp" in got, got
print("CONSUMED ENVELOPE:", json.dumps(got, indent=2))
print("REAL KAFKA ROUND-TRIP: PASS")
