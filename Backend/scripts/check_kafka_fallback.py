import os, sys, django, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.credits import kafka_producer as kp
from django_redis import get_redis_connection

conn = get_redis_connection("default")
conn.delete(kp.FALLBACK_OUTBOX_KEY)

# Simulate broker unavailability: make the producer raise on use.
def boom():
    raise RuntimeError("broker unreachable (simulated)")
kp._get_producer = boom            # force the except-branch in _publish

kp.publish_credit_consumed({"booking_id": 999, "plan_id": 1, "user_id": 2,
                            "workspace_id": 3, "credits_consumed": 5,
                            "status": "confirmed", "booked_at": "2026-06-01T09:00:00Z"})

queued = conn.lrange(kp.FALLBACK_OUTBOX_KEY, 0, -1)
assert len(queued) == 1, f"FAIL: expected 1 queued message, got {len(queued)}"
env = json.loads(queued[0])
assert env["payload"]["booking_id"] == 999, env
print("FALLBACK OUTBOX CONTENTS:", json.dumps(env, indent=2))
print("REDIS FALLBACK ON BROKER OUTAGE: PASS (message preserved, not lost)")
conn.delete(kp.FALLBACK_OUTBOX_KEY)
