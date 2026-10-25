from __future__ import annotations

from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN


class RawCaptureDownloadView(HomeAssistantView):
    """Serve raw capture files without caching."""

    url = "/api/tibber_pulse_mqtt/raw_capture/{filename}"
    name = "api:tibber_pulse_mqtt:raw_capture"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self,
        request: web.Request,
        filename: str,
    ) -> web.StreamResponse:

        capture_dir = Path(
            self.hass.config.path(
                "www/tibber_pulse_mqtt/raw"
            )
        )

        path = capture_dir / filename

        if not path.is_file():
            raise web.HTTPNotFound()

        response = web.FileResponse(path)

        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

        response.headers["Content-Disposition"] = (
            f'attachment; filename="{path.name}"'
        )

        return response