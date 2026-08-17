"""Monitor Docker update component."""

import asyncio
import logging
from typing import Any

from homeassistant.components.update import (
    ENTITY_ID_FORMAT,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import slugify

from .const import (
    API,
    CONF_CONTAINERS,
    CONF_CONTAINERS_EXCLUDE,
    CONF_UPDATE_CHECK_ENABLED,
    CONFIG,
    CONTAINER,
    DOMAIN,
)
from .helpers import DockerContainerAPI, DockerContainerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Update set up for Hass.io config entry."""
    await async_setup_platform(
        hass=hass,
        config=config_entry.data,
        async_add_entities=async_add_entities,
        discovery_info={"name": config_entry.data[CONF_NAME]},
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
):
    """Set up the Monitor Docker Update entities."""

    if discovery_info is None:
        return

    instance = discovery_info[CONF_NAME]
    api = hass.data[DOMAIN][instance][API]
    config = hass.data[DOMAIN][instance][CONFIG]

    if not config[CONF_UPDATE_CHECK_ENABLED]:
        _LOGGER.debug("[%s]: Update checking is disabled", instance)
        return True

    _LOGGER.debug("[%s]: Setting up update entity(-ies)", instance)

    entities = []

    # We support add/re-add of a container
    if CONTAINER in discovery_info:
        clist = [discovery_info[CONTAINER]]
    else:
        clist = api.list_containers()

    for cname in clist:
        includeContainer = False
        if cname in config[CONF_CONTAINERS] or not config[CONF_CONTAINERS]:
            includeContainer = True

        if config[CONF_CONTAINERS_EXCLUDE] and cname in config[CONF_CONTAINERS_EXCLUDE]:
            includeContainer = False

        if includeContainer:
            _LOGGER.debug("[%s] %s: Adding component Update", instance, cname)
            entities.append(
                DockerContainerUpdate(
                    api.get_container(cname),
                    instance=instance,
                    cname=cname,
                )
            )

    if not entities:
        return False

    async_add_entities(entities, True)

    return True


#################################################################
class DockerContainerUpdate(UpdateEntity, DockerContainerEntity):
    """Reports whether a newer image is available for a container, and can
    pull + recreate the container with it.

    Recreating rebuilds the container's config from `docker inspect`
    (env, mounts, ports, restart policy, networks...) rather than re-reading
    a docker-compose.yml, so it can in principle drift from what compose
    would produce; see README for the caveats (notably: anonymous volumes
    are not preserved across any recreate, Docker-native tools included).
    """

    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(
        self,
        container: DockerContainerAPI,
        instance: str,
        cname: str,
    ):
        super().__init__(container, instance, cname)

        self._container = container
        self._instance = instance
        self._cname = cname
        self._removed = False

        self._attr_unique_id = ENTITY_ID_FORMAT.format(
            slugify(f"{self._instance}_{self._cname}_update")
        )
        self._attr_name = f"{self._instance} {self._cname} Update"
        self._attr_installed_version = None
        self._attr_latest_version = None

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def icon(self) -> str:
        return "mdi:docker"

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self._container.register_callback(self.event_callback, "update")

        # Call event callback for possible information available
        self.event_callback()

    def event_callback(self, name="", remove=False) -> None:
        """Callback for update of container information."""

        if remove:
            # If already called before, do not remove it again
            if self._removed:
                return

            _LOGGER.info("[%s] %s: Removing update entity", self._instance, self._cname)
            asyncio.create_task(self.async_remove())
            self._removed = True
            return

        try:
            update_info = self._container.get_update_info()
        except Exception as err:
            _LOGGER.error(
                "[%s] %s: Cannot request update info (%s)",
                self._instance,
                self._cname,
                str(err),
            )
            return

        installed = update_info.get("installed_version")
        latest = update_info.get("latest_version")

        if (
            installed != self._attr_installed_version
            or latest != self._attr_latest_version
        ):
            self._attr_installed_version = installed
            self._attr_latest_version = latest
            self.async_schedule_update_ha_state()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Pull the current tag's new image and recreate the container."""
        update_info = self._container.get_update_info()
        image = update_info.get("image")

        if not image:
            raise HomeAssistantError(
                f"No image reference known yet for '{self._cname}', try again "
                "after the next update check"
            )

        self._attr_in_progress = True
        self.async_write_ha_state()

        try:
            success = await self._container.recreate_with_image(image)
        finally:
            self._attr_in_progress = False

        if not success:
            raise HomeAssistantError(
                f"Recreating '{self._cname}' with the new image failed and was "
                "rolled back - check the Home Assistant log for details"
            )

        # Reflect the new version immediately instead of waiting up to
        # UPDATE_CHECK_INTERVAL for the next scheduled check to confirm it.
        self._attr_installed_version = self._attr_latest_version
        self._container.force_update_check()
        self.async_write_ha_state()
