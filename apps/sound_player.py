"""Shared sound player and uploader for BusyBar sub-apps.

Manages sound assets for BusyBar devices and local host playback.
Uploads audio assets to the BusyBar device via busylib and triggers
on-device playback, as well as playing locally on macOS via afplay.
"""

import logging
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Optional

from busylib import BusyBar
from busylib.exceptions import BusyBarAPIError, BusyBarError

logger = logging.getLogger(__name__)


class SoundPlayer:
    """Uploads and plays audio effects on BusyBar and/or local machine."""

    def __init__(
        self,
        device_ip: str,
        sound_folder: Optional[Path] = None,
        application_name: str = "busybar_controller",
        enable_device_audio: bool = True,
        enable_local_audio: bool = True,
    ):
        """
        Args:
            device_ip: BusyBar device IP or hostname
            sound_folder: Directory where local WAV files live
            application_name: App name used for asset uploads and playback
            enable_device_audio: Whether to upload and trigger audio on BusyBar
            enable_local_audio: Whether to play audio on local host (via afplay)
        """
        self.device_ip = device_ip
        self.sound_folder = (
            Path(sound_folder)
            if sound_folder
            else Path(__file__).parent / "nhl" / "sounds"
        )
        self.application_name = application_name
        self.enable_device_audio = enable_device_audio
        self.enable_local_audio = enable_local_audio

        self._busybar: Optional[BusyBar] = None
        if self.enable_device_audio:
            try:
                self._busybar = BusyBar(device_ip, timeout=5.0)
            except Exception as e:
                logger.warning(
                    f"Could not initialize BusyBar client for audio on {device_ip}: {e}"
                )

        self._uploaded = set()
        self._afplay_path = shutil.which("afplay")

    def upload_sound(self, filename: str) -> bool:
        """Upload sound file to the device if not already uploaded.

        Args:
            filename: Sound filename, e.g. "goal_horn.wav"

        Returns:
            True if uploaded or already on device, False otherwise.
        """
        if not self.enable_device_audio or not self._busybar:
            return False
        if filename in self._uploaded:
            return True

        local_path = self.sound_folder / filename
        if not local_path.exists():
            logger.error(f"Sound file not found: {local_path}")
            return False

        try:
            with open(local_path, "rb") as f:
                audio_data = f.read()

            self._busybar.assets_upload(
                application_name=self.application_name,
                filename=filename,
                data=audio_data,
            )
            logger.debug(f"Uploaded sound to BusyBar: {filename}")
            self._uploaded.add(filename)
            return True
        except BusyBarAPIError as e:
            logger.warning(f"Failed to upload sound {filename} to device: {e}")
            return False
        except BusyBarError as e:
            logger.error(f"Error uploading sound {filename}: {e}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error uploading sound {filename}: {e}")
            return False

    def play(self, filename: str) -> None:
        """Play sound asynchronously on BusyBar device and/or local host speakers.

        Args:
            filename: Sound filename, e.g. "goal_horn.wav"
        """
        local_path = self.sound_folder / filename

        # 1. Local playback (macOS afplay) in background thread
        if self.enable_local_audio and self._afplay_path and local_path.exists():
            def _play_local():
                try:
                    subprocess.run(
                        [self._afplay_path, str(local_path)],
                        check=False,
                        capture_output=True,
                    )
                except Exception as e:
                    logger.warning(f"Failed to play local audio {filename}: {e}")

            threading.Thread(target=_play_local, daemon=True).start()

        # 2. BusyBar device playback in background thread
        if self.enable_device_audio and self._busybar:
            def _play_device():
                try:
                    if filename not in self._uploaded:
                        self.upload_sound(filename)
                    self._busybar.audio_play(
                        path=filename,
                        application_name=self.application_name,
                    )
                    logger.info(f"Triggered BusyBar audio playback: {filename}")
                except Exception as e:
                    logger.warning(f"Device audio playback failed for {filename}: {e}")

            threading.Thread(target=_play_device, daemon=True).start()

    def close(self) -> None:
        """Close the underlying BusyBar client."""
        if self._busybar:
            try:
                self._busybar.close()
            except Exception:
                pass
