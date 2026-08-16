#!/usr/bin/env python3
"""Entry point for BusyBar controller."""

import sys
from pathlib import Path

from controller.controller import BusyBarController


def main():
    """Run the controller."""
    base_dir = Path(__file__).parent
    config_path = base_dir / "apps.json"
    state_path = base_dir / "state.json"
    
    controller = BusyBarController(str(config_path), str(state_path))
    controller.start()


if __name__ == "__main__":
    main()
