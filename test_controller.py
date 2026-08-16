#!/usr/bin/env python3
"""Test script: simulate sub-app publishing to Redis and verify controller behavior."""

import json
import time
import sys
from pathlib import Path

# Add controller to path
sys.path.insert(0, str(Path(__file__).parent))

from controller.message import DisplayMessage

# Lazy-load redis to avoid import issues
redis = None


def test_display_message():
    """Test DisplayMessage validation."""
    print("Testing DisplayMessage validation...")
    
    # Valid message
    msg = DisplayMessage(
        app_id="test",
        priority=50,
        duration_seconds=10,
        elements=[{"id": "text", "type": "text", "text": "Test"}],
    )
    print(f"✓ Created valid message: {msg.app_id} (priority={msg.priority})")
    
    # Test JSON serialization
    json_str = msg.to_json()
    msg2 = DisplayMessage.from_json(json_str)
    print(f"✓ Serialized and deserialized: {msg2.app_id}")
    
    # Test expiration
    assert not msg.is_expired(msg.timestamp + 5), "Should not be expired at 5s"
    assert msg.is_expired(msg.timestamp + 15), "Should be expired at 15s"
    print(f"✓ Expiration logic works")
    
    # Invalid priority
    try:
        DisplayMessage(
            app_id="test",
            priority=101,
            duration_seconds=10,
            elements=[{"id": "text", "type": "text"}],
        )
        print("✗ Should have rejected priority=101")
        return False
    except ValueError:
        print("✓ Rejected invalid priority")
    
    return True


def test_redis_publish():
    """Test publishing a message to Redis."""
    import redis  # Import here to avoid top-level dependency
    
    print("\nTesting Redis pub/sub...")
    
    try:
        client = redis.Redis(host="localhost", port=6379, decode_responses=True)
        client.ping()
    except redis.ConnectionError:
        print("✗ Redis not running. Start it with: redis-server")
        return False
    
    print("✓ Connected to Redis")
    
    # Create a test message
    msg = DisplayMessage(
        app_id="test_app",
        priority=75,
        duration_seconds=20,
        elements=[
            {
                "id": "text",
                "type": "text",
                "text": "Test Message",
                "x": 0,
                "y": 0,
                "font": "medium",
                "color": "#FFFFFFFF",
                "width": 72,
                "scroll_rate": 0,
                "timeout": 6,
            }
        ],
    )
    
    # Publish to Redis
    channel = f"busybar:app:{msg.app_id}"
    client.publish(channel, msg.to_json())
    print(f"✓ Published message to {channel}")
    
    # Verify with subscriber
    pubsub = client.pubsub()
    pubsub.subscribe(channel)
    
    # Publish again
    client.publish(channel, msg.to_json())
    
    # Receive on subscriber
    message = pubsub.get_message(timeout=2)
    while message and message["type"] != "message":
        message = pubsub.get_message(timeout=2)
    
    if message:
        received_msg = DisplayMessage.from_json(message["data"])
        print(f"✓ Received message from subscriber: {received_msg.app_id}")
    else:
        print("✗ Failed to receive message")
        return False
    
    return True


def main():
    """Run tests."""
    print("=" * 60)
    print("BusyBar Controller - Phase 1 Tests")
    print("=" * 60)
    
    all_passed = True
    
    if not test_display_message():
        all_passed = False
    
    if not test_redis_publish():
        all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✓ All tests passed!")
        print("\nNext steps:")
        print("1. Install dependencies: pip install -r requirements.txt")
        print("2. Start controller: python main.py")
        print("3. In another terminal, start weather app: python apps/weather_app.py")
        return 0
    else:
        print("✗ Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
