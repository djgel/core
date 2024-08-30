"""Media source for Google Photos."""

from dataclasses import dataclass
import logging
from typing import Any, cast

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseError,
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.core import HomeAssistant

from . import GooglePhotosConfigEntry
from .const import DOMAIN
from .exceptions import GooglePhotosApiError

_LOGGER = logging.getLogger(__name__)

# Media Sources do not support paging, so we only show a subset of recent
# photos when displaying the users library. We fetch a minimum of 50 photos
# unless we run out, but in pages of 100 at a time given sometimes responses
# may only contain a handful of items Fetches at least 50 photos.
MAX_PHOTOS = 50
PAGE_SIZE = 100

THUMBNAIL_SIZE = 256
LARGE_IMAGE_SIZE = 2048


# Markers for parts of PhotosIdentifier url pattern.
# The PhotosIdentifier can be in the following forms:
#  config-entry-id
#  config-entry-id/a/album-media-id
#  config-entry-id/p/photo-media-id
#
# The album-media-id can contain special reserved folder names for use by
# this integration for virtual folders like the `recent` album.
PHOTO_SOURCE_IDENTIFIER_PHOTO = "p"
PHOTO_SOURCE_IDENTIFIER_ALBUM = "a"

# Currently supports a single album of recent photos
RECENT_PHOTOS_ALBUM = "recent"
RECENT_PHOTOS_TITLE = "Recent Photos"


@dataclass
class PhotosIdentifier:
    """Google Photos item identifier in a media source URL."""

    config_entry_id: str
    """Identifies the account for the media item."""

    album_media_id: str | None = None
    """Identifies the album contents to show.

    Not present at the same time as `photo_media_id`.
    """

    photo_media_id: str | None = None
    """Identifies an indiviidual photo or video.

    Not present at the same time as `album_media_id`.
    """

    def as_string(self) -> str:
        """Serialize the identiifer as a string.

        This is the opposite if parse_identifier().
        """
        if self.photo_media_id is None:
            if self.album_media_id is None:
                return self.config_entry_id
            return f"{self.config_entry_id}/{PHOTO_SOURCE_IDENTIFIER_ALBUM}/{self.album_media_id}"
        return f"{self.config_entry_id}/{PHOTO_SOURCE_IDENTIFIER_PHOTO}/{self.photo_media_id}"


def parse_identifier(identifier: str) -> PhotosIdentifier:
    """Parse a PhotosIdentifier form a string.

    This is the opposite of as_string().
    """
    parts = identifier.split("/")
    if len(parts) == 1:
        return PhotosIdentifier(parts[0])
    if len(parts) != 3:
        raise BrowseError(f"Invalid identifier: {identifier}")
    if parts[1] == PHOTO_SOURCE_IDENTIFIER_PHOTO:
        return PhotosIdentifier(parts[0], photo_media_id=parts[2])
    return PhotosIdentifier(parts[0], album_media_id=parts[2])


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up Synology media source."""
    return GooglePhotosMediaSource(hass)


class GooglePhotosMediaSource(MediaSource):
    """Provide Google Photos as media sources."""

    name = "Google Photos"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Google Photos source."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve media identifier to a url.

        This will resolve a specific media item to a url for the full photo or video contents.
        """
        identifier = parse_identifier(item.identifier)
        if identifier.photo_media_id is None:
            raise BrowseError(
                f"Could not resolve identifier without a photo_media_id: {identifier}"
            )
        entry = self._async_config_entry(identifier.config_entry_id)
        client = entry.runtime_data
        media_item = await client.get_media_item(
            media_item_id=identifier.photo_media_id
        )
        is_video = media_item["mediaMetadata"].get("video") is not None
        return PlayMedia(
            url=(
                _video_url(media_item)
                if is_video
                else _media_url(media_item, LARGE_IMAGE_SIZE)
            ),
            mime_type=media_item["mimeType"],
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return details about the media source.

        This renders the multi-level album structure for an account, its albums,
        or the contents of an album. This will return a BrowseMediaSource with a
        single level of children at the next level of the hierarchy.
        """
        if not item.identifier:
            # Top level view that lists all accounts.
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaClass.IMAGE,
                title="Google Photos",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
                children=[
                    _build_account(entry, PhotosIdentifier(cast(str, entry.unique_id)))
                    for entry in self.hass.config_entries.async_loaded_entries(DOMAIN)
                ],
            )

        # Determine the configuration entry for this item
        identifier = parse_identifier(item.identifier)
        entry = self._async_config_entry(identifier.config_entry_id)
        client = entry.runtime_data

        if identifier.album_media_id is None:
            source = _build_account(entry, identifier)
            source.children = [
                _build_album(
                    RECENT_PHOTOS_TITLE,
                    PhotosIdentifier(
                        identifier.config_entry_id, album_media_id=RECENT_PHOTOS_ALBUM
                    ),
                )
            ]
            return source

        # Currently only supports listing a single album of recent photos.
        if identifier.album_media_id != RECENT_PHOTOS_ALBUM:
            raise BrowseError(f"Unsupported album: {identifier}")

        # Fetch recent items
        media_items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(media_items) < MAX_PHOTOS:
            try:
                result = await client.list_media_items(
                    page_size=PAGE_SIZE, page_token=page_token
                )
            except GooglePhotosApiError as err:
                raise BrowseError(f"Error listing media items: {err}") from err
            media_items.extend(result["mediaItems"])
            page_token = result.get("nextPageToken")
            if page_token is None:
                break

        # Render the grid of media item results
        source = _build_account(entry, PhotosIdentifier(cast(str, entry.unique_id)))
        source.children = [
            _build_media_item(
                PhotosIdentifier(
                    identifier.config_entry_id, photo_media_id=media_item["id"]
                ),
                media_item,
            )
            for media_item in media_items
        ]
        return source

    def _async_config_entry(self, config_entry_id: str) -> GooglePhotosConfigEntry:
        """Return a config entry with the specified id."""
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, config_entry_id
        )
        if not entry:
            raise BrowseError(
                f"Could not find config entry for identifier: {config_entry_id}"
            )
        return entry


def _build_account(
    config_entry: GooglePhotosConfigEntry,
    identifier: PhotosIdentifier,
) -> BrowseMediaSource:
    """Build the root node for a Google Photos account for a config entry."""
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier.as_string(),
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaClass.IMAGE,
        title=config_entry.title,
        can_play=False,
        can_expand=True,
    )


def _build_album(title: str, identifier: PhotosIdentifier) -> BrowseMediaSource:
    """Build an album node."""
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier.as_string(),
        media_class=MediaClass.ALBUM,
        media_content_type=MediaClass.ALBUM,
        title=title,
        can_play=False,
        can_expand=True,
    )


def _build_media_item(
    identifier: PhotosIdentifier, media_item: dict[str, Any]
) -> BrowseMediaSource:
    """Build the node for an individual photos or video."""
    is_video = media_item["mediaMetadata"].get("video") is not None
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier.as_string(),
        media_class=MediaClass.IMAGE if not is_video else MediaClass.VIDEO,
        media_content_type=MediaType.IMAGE if not is_video else MediaType.VIDEO,
        title=media_item["filename"],
        can_play=is_video,
        can_expand=False,
        thumbnail=_media_url(media_item, THUMBNAIL_SIZE),
    )


def _media_url(media_item: dict[str, Any], max_size: int) -> str:
    """Return a media item url with the specified max thumbnail size on the longest edge.

    See https://developers.google.com/photos/library/guides/access-media-items#base-urls
    """
    width = media_item["mediaMetadata"]["width"]
    height = media_item["mediaMetadata"]["height"]
    key = "h" if height > width else "w"
    return f"{media_item["baseUrl"]}={key}{max_size}"


def _video_url(media_item: dict[str, Any]) -> str:
    """Return a video url for the item.

    See https://developers.google.com/photos/library/guides/access-media-items#base-urls
    """
    return f"{media_item["baseUrl"]}=dv"
