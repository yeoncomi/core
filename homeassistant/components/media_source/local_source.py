"""Local Media Source Implementation."""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
import shutil

from aiohttp import web
from aiohttp.web_request import FileField
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player.const import MEDIA_CLASS_DIRECTORY
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.util import raise_if_invalid_filename, raise_if_invalid_path

from .const import DOMAIN, MEDIA_CLASS_MAP, MEDIA_MIME_TYPES
from .error import Unresolvable
from .models import BrowseMediaSource, MediaSource, MediaSourceItem, PlayMedia

MAX_UPLOAD_SIZE = 1024 * 1024 * 10
LOGGER = logging.getLogger(__name__)


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Set up local media source."""
    source = LocalSource(hass)
    hass.data[DOMAIN][DOMAIN] = source
    hass.http.register_view(LocalMediaView(hass, source))
    hass.http.register_view(UploadMediaView(hass, source))


class LocalSource(MediaSource):
    """Provide local directories as media sources."""

    name: str = "Local Media"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize local source."""
        super().__init__(DOMAIN)
        self.hass = hass

    @callback
    def async_full_path(self, source_dir_id: str, location: str) -> Path:
        """Return full path."""
        return Path(self.hass.config.media_dirs[source_dir_id], location)

    @callback
    def async_parse_identifier(self, item: MediaSourceItem) -> tuple[str, str]:
        """Parse identifier."""
        if item.domain != DOMAIN:
            raise Unresolvable("Unknown domain.")

        if not item.identifier:
            # Empty source_dir_id and location
            return "", ""

        source_dir_id, _, location = item.identifier.partition("/")
        if source_dir_id not in self.hass.config.media_dirs:
            raise Unresolvable("Unknown source directory.")

        try:
            raise_if_invalid_path(location)
        except ValueError as err:
            raise Unresolvable("Invalid path.") from err

        return source_dir_id, location

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve media to a url."""
        source_dir_id, location = self.async_parse_identifier(item)
        if source_dir_id == "" or source_dir_id not in self.hass.config.media_dirs:
            raise Unresolvable("Unknown source directory.")

        mime_type, _ = mimetypes.guess_type(
            str(self.async_full_path(source_dir_id, location))
        )
        assert isinstance(mime_type, str)
        return PlayMedia(f"/media/{item.identifier}", mime_type)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return media."""
        try:
            source_dir_id, location = self.async_parse_identifier(item)
        except Unresolvable as err:
            raise BrowseError(str(err)) from err

        result = await self.hass.async_add_executor_job(
            self._browse_media, source_dir_id, location
        )
        return result

    def _browse_media(self, source_dir_id: str, location: str) -> BrowseMediaSource:
        """Browse media."""

        # If only one media dir is configured, use that as the local media root
        if source_dir_id == "" and len(self.hass.config.media_dirs) == 1:
            source_dir_id = list(self.hass.config.media_dirs)[0]

        # Multiple folder, root is requested
        if source_dir_id == "":
            if location:
                raise BrowseError("Folder not found.")

            base = BrowseMediaSource(
                domain=DOMAIN,
                identifier="",
                media_class=MEDIA_CLASS_DIRECTORY,
                media_content_type=None,
                title=self.name,
                can_play=False,
                can_expand=True,
                children_media_class=MEDIA_CLASS_DIRECTORY,
            )

            base.children = [
                self._browse_media(source_dir_id, "")
                for source_dir_id in self.hass.config.media_dirs
            ]

            return base

        full_path = Path(self.hass.config.media_dirs[source_dir_id], location)

        if not full_path.exists():
            if location == "":
                raise BrowseError("Media directory does not exist.")
            raise BrowseError("Path does not exist.")

        if not full_path.is_dir():
            raise BrowseError("Path is not a directory.")

        result = self._build_item_response(source_dir_id, full_path)
        if not result:
            raise BrowseError("Unknown source directory.")
        return result

    def _build_item_response(
        self, source_dir_id: str, path: Path, is_child: bool = False
    ) -> BrowseMediaSource | None:
        mime_type, _ = mimetypes.guess_type(str(path))
        is_file = path.is_file()
        is_dir = path.is_dir()

        # Make sure it's a file or directory
        if not is_file and not is_dir:
            return None

        # Check that it's a media file
        if is_file and (
            not mime_type or mime_type.split("/")[0] not in MEDIA_MIME_TYPES
        ):
            return None

        title = path.name

        media_class = MEDIA_CLASS_DIRECTORY
        if mime_type:
            media_class = MEDIA_CLASS_MAP.get(
                mime_type.split("/")[0], MEDIA_CLASS_DIRECTORY
            )

        media = BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{source_dir_id}/{path.relative_to(self.hass.config.media_dirs[source_dir_id])}",
            media_class=media_class,
            media_content_type=mime_type or "",
            title=title,
            can_play=is_file,
            can_expand=is_dir,
        )

        if is_file or is_child:
            return media

        # Append first level children
        media.children = []
        for child_path in path.iterdir():
            child = self._build_item_response(source_dir_id, child_path, True)
            if child:
                media.children.append(child)

        # Sort children showing directories first, then by name
        media.children.sort(key=lambda child: (child.can_play, child.title))

        return media


class LocalMediaView(HomeAssistantView):
    """
    Local Media Finder View.

    Returns media files in config/media.
    """

    url = "/media/{source_dir_id}/{location:.*}"
    name = "media"

    def __init__(self, hass: HomeAssistant, source: LocalSource) -> None:
        """Initialize the media view."""
        self.hass = hass
        self.source = source

    async def get(
        self, request: web.Request, source_dir_id: str, location: str
    ) -> web.FileResponse:
        """Start a GET request."""
        try:
            raise_if_invalid_path(location)
        except ValueError as err:
            raise web.HTTPBadRequest() from err

        if source_dir_id not in self.hass.config.media_dirs:
            raise web.HTTPNotFound()

        media_path = self.source.async_full_path(source_dir_id, location)

        # Check that the file exists
        if not media_path.is_file():
            raise web.HTTPNotFound()

        # Check that it's a media file
        mime_type, _ = mimetypes.guess_type(str(media_path))
        if not mime_type or mime_type.split("/")[0] not in MEDIA_MIME_TYPES:
            raise web.HTTPNotFound()

        return web.FileResponse(media_path)


class UploadMediaView(HomeAssistantView):
    """View to upload images."""

    url = "/api/media_source/local_source/upload"
    name = "api:media_source:local_source:upload"

    def __init__(self, hass: HomeAssistant, source: LocalSource) -> None:
        """Initialize the media view."""
        self.hass = hass
        self.source = source
        self.schema = vol.Schema(
            {
                "media_content_id": str,
                "file": FileField,
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        """Handle upload."""
        if not request["hass_user"].is_admin:
            raise Unauthorized()

        # Increase max payload
        request._client_max_size = MAX_UPLOAD_SIZE  # pylint: disable=protected-access

        try:
            data = self.schema(dict(await request.post()))
        except vol.Invalid as err:
            LOGGER.error("Received invalid upload data: %s", err)
            raise web.HTTPBadRequest() from err

        try:
            item = MediaSourceItem.from_uri(self.hass, data["media_content_id"])
        except ValueError as err:
            LOGGER.error("Received invalid upload data: %s", err)
            raise web.HTTPBadRequest() from err

        try:
            source_dir_id, location = self.source.async_parse_identifier(item)
        except Unresolvable as err:
            LOGGER.error("Invalid local source ID")
            raise web.HTTPBadRequest() from err

        uploaded_file: FileField = data["file"]

        if not uploaded_file.content_type.startswith(("image/", "video/")):
            LOGGER.error("Content type not allowed")
            raise vol.Invalid("Only images and video are allowed")

        try:
            raise_if_invalid_filename(uploaded_file.filename)
        except ValueError as err:
            LOGGER.error("Invalid filename")
            raise web.HTTPBadRequest() from err

        try:
            await self.hass.async_add_executor_job(
                self._move_file,
                self.source.async_full_path(source_dir_id, location),
                uploaded_file,
            )
        except ValueError as err:
            LOGGER.error("Moving upload failed: %s", err)
            raise web.HTTPBadRequest() from err

        return self.json(
            {"media_content_id": f"{data['media_content_id']}/{uploaded_file.filename}"}
        )

    def _move_file(  # pylint: disable=no-self-use
        self, target_dir: Path, uploaded_file: FileField
    ) -> None:
        """Move file to target."""
        if not target_dir.is_dir():
            raise ValueError("Target is not an existing directory")

        target_path = target_dir / uploaded_file.filename

        target_path.relative_to(target_dir)
        raise_if_invalid_path(str(target_path))

        with target_path.open("wb") as target_fp:
            shutil.copyfileobj(uploaded_file.file, target_fp)
