#!/usr/bin/env python3
"""Test script: verify DisplayMessage, multi-display DisplayQueue, NHL stock logo logic, goal sounds, moving goal animation, and away/home team ordering."""

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
            enable_device_audio=False,
            enable_local_audio=False,
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


def test_nhl_goal_sounds():
    """Verify goal horn sounds for configured team(s) and bad sound for non-configured opponent."""
    print("\nTesting NHL goal horn, bad sound, and 2-configured-team games...")
    import nhl_app
    
    # Check sound files exist on disk
    sounds_dir = BASE_DIR / "apps" / "nhl" / "sounds"
    assert (sounds_dir / "goal_horn.wav").exists(), "goal_horn.wav should exist in apps/nhl/sounds/"
    assert (sounds_dir / "bad_sound.wav").exists(), "bad_sound.wav should exist in apps/nhl/sounds/"
    
    mock_sound_player = MagicMock()
    with patch.object(nhl_app, "IconUploader", MagicMock()), \
         patch.object(nhl_app, "SoundPlayer", return_value=mock_sound_player):
        app = nhl_app.NHLApp(
            app_id="nhl",
            team="WSH",
            my_logo=None,
            priority=70,
        )
        assert app.sound_player is mock_sound_player, "SoundPlayer should be initialized"
        
        # Initial score 0-0
        app.current_game = {
            "awayTeam": {"abbrev": "WSH", "score": 0},
            "homeTeam": {"abbrev": "PIT", "score": 0},
            "gameState": "LIVE",
        }
        app.check_goals()
        assert app.last_score == 0
        assert app.last_opp_score == 0
        mock_sound_player.play.assert_not_called()
        
        # 1. Configured team (WSH) scores -> goal horn
        app.current_game["awayTeam"]["score"] = 1
        app.check_goals()
        mock_sound_player.play.assert_called_once_with("goal_horn.wav")
        assert app.animating_goal is True
        assert app.animating_opp_goal is False
        
        # 2. Opponent (PIT) scores -> bad sound
        mock_sound_player.reset_mock()
        app.current_game["homeTeam"]["score"] = 1
        app.check_goals()
        mock_sound_player.play.assert_called_once_with("bad_sound.wav")
        assert app.animating_opp_goal is True
        assert app.animating_goal is False
        
        # 3. Two configured teams playing each other (WSH vs CAR) -> goal horn for BOTH
        mock_sound_player.reset_mock()
        app_two = nhl_app.NHLApp(
            app_id="nhl",
            team="WSH,CAR",
            my_logo=None,
            priority=70,
        )
        app_two.current_game = {
            "awayTeam": {"abbrev": "CAR", "score": 0},
            "homeTeam": {"abbrev": "WSH", "score": 0},
            "gameState": "LIVE",
        }
        app_two.check_goals()
        mock_sound_player.play.assert_not_called()
        
        # CAR (away configured team) scores -> goal horn
        app_two.current_game["awayTeam"]["score"] = 1
        app_two.check_goals()
        mock_sound_player.play.assert_called_once_with("goal_horn.wav")
        assert app_two.animating_goal is True
        assert app_two.animating_opp_goal is False
        assert app_two.scoring_team == "CAR"
        
        # WSH (home configured team) scores -> goal horn as well
        mock_sound_player.reset_mock()
        app_two.current_game["homeTeam"]["score"] = 1
        app_two.check_goals()
        mock_sound_player.play.assert_called_once_with("goal_horn.wav")
        assert app_two.animating_goal is True
        assert app_two.animating_opp_goal is False
        assert app_two.scoring_team == "WSH"
        
    print("✓ Goal horn plays for configured team(s) (including when 2 configured teams play), and bad sound plays for non-configured opponents")
    return True


def test_nhl_animation_and_away_home_order():
    """Verify moving horn icons on goal animation and away-first / home-second ordering."""
    print("\nTesting NHL moving goal animation and away-first / home-second ordering...")
    import nhl_app
    
    icons_dir = BASE_DIR / "apps" / "nhl" / "icons"
    assert (icons_dir / "horn_left.png").exists(), "horn_left.png should exist in apps/nhl/icons/"
    assert (icons_dir / "horn_right.png").exists(), "horn_right.png should exist in apps/nhl/icons/"
    
    with patch.object(nhl_app, "IconUploader", MagicMock()), \
         patch.object(nhl_app, "SoundPlayer", MagicMock()):
        app = nhl_app.NHLApp(
            app_id="nhl",
            team="WSH",
            my_logo=None,
            priority=70,
        )
        
        # 1. Verify goal animation has horn icons that actually move across frames
        app.animating_goal = True
        app.scoring_team = "WSH"
        frame_1 = app.build_animation_elements()
        frame_2 = app.build_animation_elements()
        
        by_id_1 = {e["id"]: e for e in frame_1}
        by_id_2 = {e["id"]: e for e in frame_2}
        assert "horn_left" in by_id_1 and "horn_right" in by_id_1, "Goal animation must include horn icons"
        assert (by_id_1["horn_left"]["x"], by_id_1["horn_left"]["y"]) != (
            by_id_2["horn_left"]["x"],
            by_id_2["horn_left"]["y"],
        ), "Left horn icon must move between animation frames"
        assert (by_id_1["horn_right"]["x"], by_id_1["horn_right"]["y"]) != (
            by_id_2["horn_right"]["x"],
            by_id_2["horn_right"]["y"],
        ), "Right horn icon must move between animation frames"
        
        # 2. Verify away team is listed first (x=0, first score) and home team second (x=56, second score)
        # even when configured team (WSH) is the home team
        app.animating_goal = False
        app.current_game = {
            "awayTeam": {"abbrev": "CAR", "score": 2},
            "homeTeam": {"abbrev": "WSH", "score": 4},
            "gameState": "LIVE",
            "clock": {"timeRemaining": "10:00", "inIntermission": False, "running": False},
            "periodDescriptor": {"number": 3},
        }
        elements = app.build_game_elements()
        elem_by_id = {e["id"]: e for e in elements}
        
        assert elem_by_id["away_logo"]["path"] == "car.png"
        assert elem_by_id["away_logo"]["x"] == 0
        assert elem_by_id["home_logo"]["path"] == "wsh.png"
        assert elem_by_id["home_logo"]["x"] == 56
        assert elem_by_id["score"]["text"] == "2-4", f"Expected away-home score '2-4', got {elem_by_id['score']['text']}"
        
    print("✓ Goal animation moves horn icons across frames, and away team is listed first with home team second")
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
        
    if not test_nhl_goal_sounds():
        all_passed = False

    if not test_nhl_animation_and_away_home_order():
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
