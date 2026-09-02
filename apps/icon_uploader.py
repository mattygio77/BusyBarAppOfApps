"""Shared icon-upload helper for BusyBar sub-apps.

Every sub-app that draws an image element (weather's condition icons,
stocks' up/down arrows, etc.) needs to push the local PNG to the device
before the controller can reference it by filename in a display element's
"path". This used to be duplicated per-app; now it's one place.

Usage:
    from icon_uploader import IconUploader

    uploader = IconUploader(device_ip, icon_folder=Path(__file__).parent / "weather" / "icons")
    icon_path = uploader.upload("sun.png")   # uploads once, then just returns "sun.png"
    ...
    uploader.close()
"""

import logging
from pathlib import Path

from busylib import BusyBar
from busylib.exceptions import BusyBarAPIError, BusyBarError

logger = logging.getLogger(__name__)


class IconUploader:
    """Uploads local icon PNGs to a BusyBar device, once each per run.

    Owns its own BusyBar client (icon upload is the only thing sub-apps
    use busylib for - the controller, not the sub-app, is what actually
    draws to the device), so callers don't need to import busylib
    directly just to push icons. Call close() when done, same as you
    would with a BusyBar client.
    """

    def __init__(
        self,
        device_ip: str,
        icon_folder,
        application_name: str = "busybar_controller",
    ):
        """
        Args:
            device_ip: BusyBar device IP
            icon_folder: Directory local icon PNGs are read from, e.g.
                Path(__file__).parent / "weather" / "icons"
            application_name: Name the controller draws under. Icons must
                be uploaded under this same name so the controller can
                find them later when it references this filename as a
                display element's "path" (default: "busybar_controller",
                matching controller.py's APPLICATION_NAME).
        """
        self.busybar = BusyBar(device_ip)
        self.icon_folder = Path(icon_folder)
        self.application_name = application_name

        # Filenames already pushed to the device this run, so repeatedly
        # requesting the same icon (e.g. every fetch cycle) doesn't
        # re-upload it every time.
        self._uploaded = set()

    def upload(self, filename: str, *, fallback: str = None) -> str:
        """Upload one icon if it hasn't already been uploaded this run.

        Args:
            filename: Icon filename, e.g. "sun.png"
            fallback: Filename to fall back to if the local file doesn't
                exist on disk. If omitted, the originally requested
                filename is returned anyway - the device may already have
                it cached from a previous run even if this run couldn't
                read it locally.

        Returns:
            The filename to use in a display element's "path" - normally
            just `filename` echoed back; only differs from that if the
            local file was missing and a `fallback` was given.
        """
        if filename in self._uploaded:
            return filename

        local_path = self.icon_folder / filename
        if not local_path.exists():
            logger.error(f"Icon not found: {local_path}")
            return fallback if fallback is not None else filename

        try:
            with open(local_path, "rb") as f:
                icon_data = f.read()

            self.busybar.assets_upload(
                application_name=self.application_name,
                filename=filename,
                data=icon_data,
            )

            logger.debug(f"Uploaded icon: {filename}")
            self._uploaded.add(filename)

        except BusyBarAPIError as e:
            logger.warning(f"Failed to upload icon {filename}: {e}")
            # Try anyway - the device might already have it cached from a
            # previous run.
        except BusyBarError as e:
            logger.error(f"Error uploading icon {filename}: {e}")

        return filename

    def upload_all(self, filenames) -> None:
        """Upload several icons up front (e.g. at startup) rather than
        lazily on first use. Each is skipped if already uploaded this run.
        """
        for filename in filenames:
            self.upload(filename)

    def close(self) -> None:
        """Close the underlying BusyBar client."""
        self.busybar.close()