"""Configuration and runtime state management."""

import json
import os
from typing import Dict, Any, Optional, List
from pathlib import Path


class ConfigManager:
    """Load/save controller config and runtime state."""
    
    def __init__(self, config_path: str, state_path: str):
        """Initialize with paths to config and state JSON files.
        
        Args:
            config_path: Path to apps.json (app definitions + settings)
            state_path: Path to state.json (runtime state)
        """
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
    
    def load_config(self) -> Dict[str, Any]:
        """Load config from JSON file.
        
        If file doesn't exist, return defaults.
        """
        if self.config_path.exists():
            with open(self.config_path, "r") as f:
                return json.load(f)
        
        # Return defaults
        return self._default_config()
    
    def save_config(self, config: Dict[str, Any]) -> None:
        """Save config to JSON file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)
    
    def load_state(self) -> Dict[str, Any]:
        """Load runtime state from JSON file.
        
        If file doesn't exist, return defaults.
        """
        if self.state_path.exists():
            with open(self.state_path, "r") as f:
                return json.load(f)
        
        return self._default_state()
    
    def save_state(self, state: Dict[str, Any]) -> None:
        """Save runtime state to JSON file."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(state, f, indent=2)
    
    @staticmethod
    def _default_config() -> Dict[str, Any]:
        """Default configuration."""
        return {
            "device_ip": "10.0.4.20",
            "redis_host": "localhost",
            "redis_port": 6379,
            "apps": [
                {
                    "id": "weather",
                    "path": "./apps/weather_app.py",
                    "enabled": True,
                    "proposed_interval_seconds": 300,
                    "override_interval_seconds": None,
                    "default_priority": 50,
                }
            ],
            "idle_rotation": {
                "enabled": True,
                "cycle_seconds": 10,
                "content": [
                    {
                        "id": "idle_text",
                        "type": "text",
                        "text": "Idle...",
                        "x": 20,
                        "y": 8,
                        "font": "small",
                        "color": "#FFFFFFFF",
                        "width": 72,
                        "scroll_rate": 0,
                        "timeout": 6,
                    }
                ],
            },
        }
    
    @staticmethod
    def _default_state() -> Dict[str, Any]:
        """Default runtime state."""
        return {
            "current_owner": None,
            "queue": [],
            "last_display": None,
        }
