#!/usr/bin/env python3
"""Test script: verify DisplayMessage, multi-display DisplayQueue, and NHL stock logo logic."""

import json
import time
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add repo root and apps/ to path
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "apps"))

from controller.message import DisplayMessage
from controller.display_queue import DisplayQueue


def test_display_message():
    """Test DisplayMessage validation and card_id / ttl_seconds support."""
    print("Testing DisplayMessage validation...")
    
    # Valid message
    msg = DisplayMessage(
        app_id="test",
        priority=50,
        duration_seconds=10,
        elements=[{"id": "text", "type": "text", "text": "Test"}],
    )
    print(f"✓ Created valid message: {msg.app_id} (priority={msg.priority}, slot={msg.slot_key})")
    
    # Test JSON serialization
    json_str = msg.to_json()
    msg2 = DisplayMessage.from_json(json_str)
    print(f"✓ Serialized and deserialized: {msg2.app_id}")
    
    # Test expiration
    assert not msg.is_expired(msg.timestamp + 5), "Should not be expired at 5s"
    assert msg.is_expired(msg.timestamp + 15), "Should be expired at 15s"
    print("✓ Expiration logic works")
    
    # Test explicit ttl_seconds overrides queue expiration without changing duration_seconds
    msg_ttl = DisplayMessage(
        app_id="stocks",
        card_id="AAPL",
        priority=60,
        duration_seconds=4,
        ttl_seconds=60,
        elements=[{"id": "text", "type": "text", "text": "AAPL"}],
    )
    assert not msg_ttl.is_expired(msg_ttl.timestamp + 20), "Should respect ttl_seconds=60 while queued"
    assert msg_ttl.is_expired(msg_ttl.timestamp + 65), "Should expire after ttl_seconds=60"
    print("✓ Separate ttl_seconds + card_id logic works")
    
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


def test_multi_display_queue():
    """Verify DisplayQueue stores multiple cards per app and across separate apps."""
    print("\nTesting multi-display DisplayQueue arbitration...")
    q = DisplayQueue()
    t0 = 1000.0
    
    # Queue multiple cards from 'stocks' and separate cards from 'nhl' and 'weather'
    s1 = DisplayMessage(
        app_id="stocks",
        card_id="VOO",
        priority=60,
        duration_seconds=4,
        ttl_seconds=60,
        timestamp=t0,
        elements=[{"id": "t", "type": "text", "text": "VOO"}],
    )
    s2 = DisplayMessage(
        app_id="stocks",
        card_id="AAPL",
        priority=60,
        duration_seconds=4,
        ttl_seconds=60,
        timestamp=t0 + 0.1,
        elements=[{"id": "t", "type": "text", "text": "AAPL"}],
    )
    n1 = DisplayMessage(
        app_id="nhl",
        card_id="WSH",
        priority=60,
        duration_seconds=5,
        ttl_seconds=60,
        timestamp=t0 + 0.2,
        elements=[{"id": "t", "type": "text", "text": "WSH"}],
    )
    w1 = DisplayMessage(
        app_id="weather",
        card_id="default",
        priority=50,
        duration_seconds=300,
        timestamp=t0,
        elements=[{"id": "t", "type": "text", "text": "72F"}],
    )
    
    q.add_message(s1)
    q.add_message(s2)
    q.add_message(n1)
    q.add_message(w1)
    
    assert len(q.messages) == 4, f"Expected 4 queued cards, got {len(q.messages)}"
    
    # 1. First priority-60 card (stocks:VOO) gets its 4s turn
    cur = q.get_current(t0)
    assert cur.slot_key == ("stocks", "VOO"), f"Expected ('stocks', 'VOO'), got {cur.slot_key}"
    
    # 2. After 4s, fair cross-app tie-breaker hands turn to nhl:WSH (also priority 60)
    cur = q.get_current(t0 + 4.0)
    assert cur.slot_key == ("nhl", "WSH"), f"Expected ('nhl', 'WSH'), got {cur.slot_key}"
    
    # 3. After nhl's 5s turn, second stocks card (stocks:AAPL) gets its 4s turn
    cur = q.get_current(t0 + 9.0)
    assert cur.slot_key == ("stocks", "AAPL"), f"Expected ('stocks', 'AAPL'), got {cur.slot_key}"
    
    # 4. After stocks:AAPL finishes, weather (priority 50) takes over
    cur = q.get_current(t0 + 13.0)
    assert cur.slot_key == ("weather", "default"), f"Expected ('weather', 'default'), got {cur.slot_key}"
    
    print("✓ Multi-display queue stores multiple cards per app and interleaves apps fairly")
    return True


def test_nhl_caps_stock_logo():
    """Verify NHLApp uses stock wsh.png logo instead of alternate weagle.png by default."""
    print("\nTesting NHL Capitals stock logo resolution...")
    import nhl_app
    with patch.object(nhl_app, "IconUploader", MagicMock()):
        app = nhl_app.NHLApp(
            app_id="nhl",
            team="WSH",
            my_logo=None,
            priority=70,
        )
        # Mock network download if wsh.png is not yet cached on disk
        with patch.object(nhl_app.requests, "get") as mock_get:
            mock_resp = MagicMock()
            # Use car.png bytes as valid PNG sample for PIL
            car_bytes = (BASE_DIR / "apps" / "nhl" / "icons" / "car.png").read_bytes()
            mock_resp.content = car_bytes
            mock_get.return_value = mock_resp
            
            wsh_path = BASE_DIR / "apps" / "nhl" / "icons" / "wsh.png"
            had_wsh = wsh_path.exists()
            try:
                logo_filename = app.get_team_logo("WSH")
                assert logo_filename == "wsh.png", f"Expected 'wsh.png', got '{logo_filename}'"
                if not had_wsh:
                    mock_get.assert_called_once_with(
                        "https://a.espncdn.com/i/teamlogos/nhl/500/wsh.png",
                        timeout=10,
                    )
            finally:
                if not had_wsh and wsh_path.exists():
                    wsh_path.unlink()
    print("✓ NHLApp resolves 'WSH' to stock logo 'wsh.png' from ESPN CDN")
    return True


def test_redis_publish():
    """Test publishing a message to Redis."""
    import redis
    
    print("\nTesting Redis pub/sub...")
    
    try:
        client = redis.Redis(host="localhost", port=6379, decode_responses=True)
        client.ping()
    except redis.ConnectionError:
        print("⚠ Redis not currently running (skipping live pub/sub check; start with: redis-server)")
        return True
    
    print("✓ Connected to Redis")
    
    msg = DisplayMessage(
        app_id="test_app",
        card_id="card_1",
        priority=75,
        duration_seconds=20,
        elements=[
            {
                "id": "text",
                "type": "text",
                "text": "Test Message",
                "x": 0,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": 72,
                "scroll_rate": 0,
                "display": "front",
            }
        ],
    )
    
    channel = f"busybar:app:{msg.app_id}"
    pubsub = client.pubsub()
    pubsub.subscribe(channel)
    pubsub.get_message(timeout=1)
    
    client.publish(channel, msg.to_json())
    print(f"✓ Published message to {channel}")
    
    message = pubsub.get_message(timeout=2)
    while message and message["type"] != "message":
        message = pubsub.get_message(timeout=2)
    
    if message:
        received_msg = DisplayMessage.from_json(message["data"])
        print(f"✓ Received message from subscriber: {received_msg.slot_key}")
        return True
    else:
        print("✗ Failed to receive message")
        return False


def main():
    """Run tests."""
    print("=" * 60)
    print("BusyBar Controller - Verification Suite")
    print("=" * 60)
    
    all_passed = True
    
    if not test_display_message():
        all_passed = False
    
    if not test_multi_display_queue():
        all_passed = False
    
    if not test_nhl_caps_stock_logo():
        all_passed = False
    
    if not test_redis_publish():
        all_passed = False
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
