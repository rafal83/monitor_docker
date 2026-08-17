"""Config flow for integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    Mapping,
)
from homeassistant.const import (
    CONF_MONITORED_CONDITIONS,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_URL,
)
from homeassistant.helpers import issue_registry as ir, selector

from .const import (
    API,
    CONF_BUTTONENABLED,
    CONF_CERTPATH,
    CONF_CONNECTION_TYPE,
    CONF_CONTAINERS,
    CONF_CONTAINERS_EXCLUDE,
    CONF_MEMORYCHANGE,
    CONF_MONITORED_CONTAINER_CONDITIONS,
    CONF_MONITORED_DOCKER_CONDITIONS,
    CONF_PORTAINER_APIKEY,
    CONF_PORTAINER_ENDPOINT_ID,
    CONF_PORTAINER_HOST,
    CONF_PORTAINER_HTTPS,
    CONF_PORTAINER_PORT,
    CONF_PRECISION_CPU,
    CONF_PRECISION_DISK_MB,
    CONF_PRECISION_MEMORY_MB,
    CONF_PRECISION_MEMORY_PERCENTAGE,
    CONF_PRECISION_NETWORK_KB,
    CONF_PRECISION_NETWORK_MB,
    CONF_RETRY,
    CONF_SWITCHENABLED,
    CONF_VERSION,
    CONTAINER_MONITOR_LIST,
    CONTAINER_PRE_SELECTION,
    DEFAULT_NAME,
    DEFAULT_PORTAINER_PORT,
    DEFAULT_RETRY,
    DEFAULT_SCAN_INTERVAL,
    DOCKER_MONITOR_LIST,
    DOCKER_PRE_SELECTION,
    DOMAIN,
    PRECISION,
)
from .helpers import DockerAPI

_LOGGER = logging.getLogger(__name__)


# The config flow's field defaults. Also used by __init__.py to backfill
# entries created before a field existed. Never mutate this directly - the
# flow copies it into its own per-instance self.data in __init__.
DEFAULT_DATA = {
    # User
    CONF_NAME: DEFAULT_NAME,
    CONF_URL: "",
    CONF_VERSION: "auto",
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_CERTPATH: "",
    CONF_PORTAINER_APIKEY: "",
    CONF_PORTAINER_HOST: "",
    CONF_PORTAINER_PORT: DEFAULT_PORTAINER_PORT,
    CONF_PORTAINER_HTTPS: True,
    CONF_PORTAINER_ENDPOINT_ID: "",
    CONF_RETRY: DEFAULT_RETRY,
    # Containers
    CONF_CONTAINERS: [],
    CONF_CONTAINERS_EXCLUDE: [],  # Not relevant as all are selected
    # Conditions
    CONF_MONITORED_CONDITIONS: [],
    CONF_SWITCHENABLED: True,
    CONF_BUTTONENABLED: False,
    CONF_MEMORYCHANGE: 100,
    CONF_PRECISION_CPU: PRECISION,
    CONF_PRECISION_DISK_MB: PRECISION,
    CONF_PRECISION_MEMORY_MB: PRECISION,
    CONF_PRECISION_MEMORY_PERCENTAGE: PRECISION,
    CONF_PRECISION_NETWORK_KB: PRECISION,
    CONF_PRECISION_NETWORK_MB: PRECISION,
}


class DockerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Docker config flow."""

    VERSION = 1
    MINOR_VERSION = 1
    options = None
    _docker_api = None
    _config_entry: ConfigEntry | None = None

    def __init__(self) -> None:
        """Initialize the flow with its own copy of the defaults.

        self.data must be a per-instance dict, not the shared DEFAULT_DATA -
        every step mutates it via self.data.update(...)/self.data[...]=...,
        which would otherwise corrupt DEFAULT_DATA (and leak between
        concurrent/sequential flow runs) since Python resolves an unset
        instance attribute to the class attribute of the same name.
        Same reasoning for _docker_conditions/_container_conditions below.
        """
        super().__init__()
        self.data = dict(DEFAULT_DATA)
        self._docker_conditions = list(DOCKER_PRE_SELECTION)
        self._container_conditions = list(CONTAINER_PRE_SELECTION)

    async def _test_connection_and_check_name(
        self, test_config: dict[str, Any]
    ) -> dict[str, str]:
        """Test Docker/Portainer connectivity and check for a name collision.

        Shared by the docker/portainer connection steps so both end up with
        identical validation. Returns an errors dict (empty if all good).
        """
        errors: dict[str, str] = {}

        try:
            self._docker_api = DockerAPI(self.hass, test_config)
            await self._docker_api.init()
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("Unhandled exception testing the connection")
            errors["base"] = str(e)

        # Unless re-authorization, check and abort if name already exists.
        # When reconfiguring, the entry's own (still unchanged) name is
        # already registered - that's not a collision.
        if self.source != SOURCE_REAUTH:
            unchanged_name = (
                self._config_entry is not None
                and self._config_entry.data.get(CONF_NAME) == self.data[CONF_NAME]
            )
            if (
                not unchanged_name
                and DOMAIN in self.hass.data
                and self.data[CONF_NAME] in self.hass.data[DOMAIN]
            ):
                errors[CONF_NAME] = "name_exists"

            await self.async_set_unique_id(self.data[CONF_NAME])
            if not self._config_entry:
                self._abort_if_unique_id_configured()

        return errors

    async def _async_step_after_connection(self) -> ConfigFlowResult:
        """Continue the flow once a connection has been validated."""
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._config_entry,
                data=self.data,
            )
        return await self.async_step_containers()

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user step: name and how to connect."""

        if user_input is not None:
            self.data.update(user_input)
            if user_input[CONF_CONNECTION_TYPE] == "portainer":
                return await self.async_step_portainer_connection()
            return await self.async_step_docker_connection()

        connection_type_default = (
            "portainer" if self.data[CONF_PORTAINER_APIKEY] else "docker"
        )

        user_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self.data[CONF_NAME]): str,
                vol.Required(
                    CONF_CONNECTION_TYPE, default=connection_type_default
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value="docker", label="Direct Docker connection"
                            ),
                            selector.SelectOptionDict(
                                value="portainer", label="Via Portainer proxy"
                            ),
                        ],
                    ),
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=self.data[CONF_SCAN_INTERVAL]
                ): int,
                vol.Required(CONF_RETRY, default=self.data[CONF_RETRY]): int,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=user_schema,
        )

    async def async_step_docker_connection(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a direct Docker daemon connection (socket or TCP)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            self.data[CONF_PORTAINER_APIKEY] = ""

            test_config = {**self.data}
            if test_config[CONF_URL] == "":
                test_config[CONF_URL] = None

            errors = await self._test_connection_and_check_name(test_config)

            if not errors:
                return await self._async_step_after_connection()

        docker_schema = vol.Schema(
            {
                vol.Optional(CONF_URL, default=self.data[CONF_URL]): str,
                vol.Optional(CONF_VERSION, default=self.data[CONF_VERSION]): str,
                vol.Optional(CONF_CERTPATH, default=self.data[CONF_CERTPATH]): str,
            }
        )

        return self.async_show_form(
            step_id="docker_connection",
            data_schema=docker_schema,
            errors=errors,
        )

    async def async_step_portainer_connection(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a connection through a Portainer Docker-proxy endpoint."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            self.data[CONF_CERTPATH] = ""

            scheme = "https" if self.data[CONF_PORTAINER_HTTPS] else "http"
            self.data[CONF_URL] = (
                f"{scheme}://{self.data[CONF_PORTAINER_HOST]}:"
                f"{self.data[CONF_PORTAINER_PORT]}/api/endpoints/"
                f"{self.data[CONF_PORTAINER_ENDPOINT_ID]}/docker"
            )

            errors = await self._test_connection_and_check_name(self.data)

            if not errors:
                return await self._async_step_after_connection()

        portainer_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PORTAINER_HOST, default=self.data[CONF_PORTAINER_HOST]
                ): str,
                vol.Required(
                    CONF_PORTAINER_PORT, default=self.data[CONF_PORTAINER_PORT]
                ): int,
                vol.Required(
                    CONF_PORTAINER_HTTPS, default=self.data[CONF_PORTAINER_HTTPS]
                ): bool,
                vol.Required(
                    CONF_PORTAINER_ENDPOINT_ID,
                    default=self.data[CONF_PORTAINER_ENDPOINT_ID],
                ): str,
                vol.Required(
                    CONF_PORTAINER_APIKEY, default=self.data[CONF_PORTAINER_APIKEY]
                ): str,
            }
        )

        return self.async_show_form(
            step_id="portainer_connection",
            data_schema=portainer_schema,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfigure step."""
        self._config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._config_entry is None:
            return self.async_abort(reason="reconfigure_failed")
        self.data = {**self._config_entry.data}
        self._docker_api = self.hass.data[DOMAIN][self._config_entry.data[CONF_NAME]][
            API
        ]

        # Pre-fill the conditions form with what's actually configured,
        # not the DOCKER_PRE_SELECTION/CONTAINER_PRE_SELECTION defaults -
        # otherwise every visit to Reconfigure looks like past changes to
        # the monitored conditions were never saved.
        monitored = self.data.get(CONF_MONITORED_CONDITIONS, [])
        self._docker_conditions = [c for c in monitored if c in DOCKER_MONITOR_LIST]
        self._container_conditions = [
            c for c in monitored if c in CONTAINER_MONITOR_LIST
        ]

        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["user", "containers", "conditions"],
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error.

        Goes straight to the relevant connection sub-step (pre-filled from
        the entry, asking the user to fix/confirm credentials) rather than
        through async_step_user - entry_data is the entry's existing data,
        not a freshly submitted form, and doesn't have every field the
        user step's own form now asks for (e.g. connection_type).
        """
        self._config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._config_entry is not None:
            self.data = {**self._config_entry.data}

        if self.data[CONF_PORTAINER_APIKEY]:
            return await self.async_step_portainer_connection()
        return await self.async_step_docker_connection()

    async def async_step_containers(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user step."""
        errors = {}

        if user_input is not None:
            self.data.update(user_input)
            if self.source == SOURCE_RECONFIGURE:
                # self.async_set_unique_id(self.data[CONF_NAME])
                # self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    self._config_entry,
                    data=self.data,
                )
            return await self.async_step_conditions()

        container_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CONTAINERS, default=self.data[CONF_CONTAINERS]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(self._docker_api.list_containers()),
                        multiple=True,
                    ),
                )
            }
        )

        return self.async_show_form(
            step_id="containers",
            data_schema=container_schema,
            errors=errors,
        )

    async def async_step_conditions(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user step."""
        errors = {}

        if user_input is not None:
            self._docker_conditions = user_input.pop(CONF_MONITORED_DOCKER_CONDITIONS)
            self._container_conditions = user_input.pop(
                CONF_MONITORED_CONTAINER_CONDITIONS
            )
            self.data.update(user_input)

            if not errors:
                self.data[CONF_MONITORED_CONDITIONS] = (
                    self._docker_conditions + self._container_conditions
                )
                if self.source == SOURCE_RECONFIGURE:
                    # self.async_set_unique_id(self.data[CONF_NAME])
                    # self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        self._config_entry,
                        data=self.data,
                        reason="reconfigure_successful",
                    )
                return self.async_create_entry(
                    title=self.data[CONF_NAME], data=self.data
                )

        conditions_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MONITORED_DOCKER_CONDITIONS, default=self._docker_conditions
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=key, label=desc.name or key
                            )
                            for key, desc in DOCKER_MONITOR_LIST.items()
                        ],
                        multiple=True,
                    ),
                ),
                vol.Optional(
                    CONF_MONITORED_CONTAINER_CONDITIONS,
                    default=self._container_conditions,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=key, label=desc.name or key
                            )
                            for key, desc in CONTAINER_MONITOR_LIST.items()
                        ],
                        multiple=True,
                    ),
                ),
                vol.Required(
                    CONF_SWITCHENABLED, default=self.data[CONF_SWITCHENABLED]
                ): bool,
                vol.Required(
                    CONF_BUTTONENABLED, default=self.data[CONF_BUTTONENABLED]
                ): bool,
                vol.Required(
                    CONF_MEMORYCHANGE, default=self.data[CONF_MEMORYCHANGE]
                ): int,
                vol.Required(
                    CONF_PRECISION_CPU, default=self.data[CONF_PRECISION_CPU]
                ): int,
                vol.Required(
                    CONF_PRECISION_DISK_MB,
                    default=self.data[CONF_PRECISION_DISK_MB],
                ): int,
                vol.Required(
                    CONF_PRECISION_MEMORY_MB,
                    default=self.data[CONF_PRECISION_MEMORY_MB],
                ): int,
                vol.Required(
                    CONF_PRECISION_MEMORY_PERCENTAGE,
                    default=self.data[CONF_PRECISION_MEMORY_PERCENTAGE],
                ): int,
                vol.Required(
                    CONF_PRECISION_NETWORK_KB,
                    default=self.data[CONF_PRECISION_NETWORK_KB],
                ): int,
                vol.Required(
                    CONF_PRECISION_NETWORK_MB,
                    default=self.data[CONF_PRECISION_NETWORK_MB],
                ): int,
            }
        )

        return self.async_show_form(
            step_id="conditions",
            data_schema=conditions_schema,
            errors=errors,
        )

    async def async_step_import(
        self, import_info: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Import config from configuration.yaml."""
        _LOGGER.debug("Starting async_step_import - %s", import_info)
        if import_info[CONF_URL] == "":
            import_info[CONF_URL] = None
        await self.async_set_unique_id(import_info[CONF_NAME])
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"remove_configuration_yaml_{import_info[CONF_NAME]}",
            is_fixable=True,
            is_persistent=True,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key="remove_configuration_yaml",
            translation_placeholders={
                "domain": DOMAIN,
                "integration_title": import_info[CONF_NAME],
            },
        )
        self._abort_if_unique_id_configured()
        if exclude := import_info.pop(CONF_CONTAINERS_EXCLUDE, None):
            import_info[CONF_CONTAINERS] = [
                container
                for container in import_info[CONF_CONTAINERS]
                if container not in exclude
            ]
        for key, value in import_info.items():
            if key in self.data and key not in [CONF_CONTAINERS_EXCLUDE]:
                self.data[key] = value
        return self.async_create_entry(title=self.data[CONF_NAME], data=self.data)
