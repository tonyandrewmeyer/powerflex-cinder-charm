#! /usr/bin/env python3

# Copyright 2024 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Charmed operator for Dell PowerFlex Cinder driver."""

import logging
import os
import subprocess
from typing import Iterable, Union

import charmhelpers.core as ch_core
from charmhelpers.core.host import service_running
from charmhelpers.core.templating import render
from ops import model
from ops.main import main
from ops_openstack.plugins.classes import CinderStoragePluginCharm

VOLUME_DRIVER = "cinder.volume.drivers.dell_emc.powerflex.driver.PowerFlexDriver"
CONNECTOR_DIR = "/opt/emc/scaleio/openstack"
CONNECTOR_FILE = "connector.conf"

logger = logging.getLogger(__name__)


class CinderPowerflexCharm(CinderStoragePluginCharm):
    """Cinder subordinate charm for Dell PowerFlex drivers."""

    PACKAGES = ["cinder-common"]

    # Defines the required relations/integrations that must be connected
    # in order for this charm to function correctly. The charm will go into
    # the blocked status if these relations are not present.
    REQUIRED_RELATIONS = ["storage-backend"]

    # Defines the mandatory configuration values that need to be specified
    # by the user in order for this charm to function correctly.
    MANDATORY_CONFIG = [
        "powerflexgw-ip",
        "powerflexgw-login",
        "powerflexgw-password",
    ]

    # Restart map is used to map which services should be restarted
    # when the specified file is changed on disk. The key is the file,
    # the value is a list of services which need to be restarted.
    RESTART_MAP = {
        "/opt/emc/scaleio/openstack/connector.conf": ["scini"],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def stateless(self):
        """Indicate whether the cinder driver provides a stateless cinder backend."""
        return True

    @property
    def active_active(self):
        """Indicate whether the cinder driver supports an active/active configuration."""
        # Active/Active configuration is not supported at this time
        return False

    def cinder_configuration(self, charm_config) -> Iterable[tuple[str, Union[str, int, bool]]]:
        """Return the configuration to be set by Cinder."""
        cget = charm_config.get

        raw_options = [
            ("volume_driver", VOLUME_DRIVER),
            ("volume_backend_name", cget("volume-backend-name", self.app.name)),
            ("san_ip", cget("powerflexgw-ip")),
            ("san_login", cget("powerflexgw-login")),
            ("san_password", cget("powerflexgw-password")),
            ("powerflex_storage_pools", cget("powerflex-storage-pools")),
            (
                "powerflex_max_over_subscription_ratio",
                cget("powerflex-max-over-subscription-ratio"),
            ),
            ("san_thin_provision", cget("powerflex-san-thin-provision")),
            (
                "powerflex_allow_migration_during_rebuild",
                cget("powerflex-allow-migration-during-rebuild"),
            ),
            ("powerflex_allow_non_padded_volumes", cget("powerflex-allow-non-padded-volumes")),
            ("powerflex_rest_server_port", cget("powerflex-rest-server-port")),
            ("powerflex_round_volume_capacity", cget("powerflex-round-volume-capacity")),
            ("rest_api_connect_timeout", cget("powerflex-rest-api-connect-timeout")),
            ("rest_api_read_timeout", cget("powerflex-rest-api-read-timeout")),
            ("replication_device", cget("powerflex-replication-config")),
        ]

        options = [(x, y) for x, y in raw_options if y is not None or ""]
        return options

    def on_install(self, event):
        """Handle install event by rendering config files and installing packages."""
        super().on_install(event)
        self.create_connector()
        self.install_sdc()
        self.update_status()

    def create_connector(self):
        """Create the connector.conf file and populate with data."""
        config = dict(self.framework.model.config)
        powerflex_backend = dict(self.cinder_configuration(config))
        powerflex_config = {"cinder_name": self.framework.model.app.name}
        # Get cinder config stanza name.
        filename = os.path.join(CONNECTOR_DIR, CONNECTOR_FILE)
        ch_core.host.mkdir(CONNECTOR_DIR)

        filter_params = ["san_password"]

        # If replication is enabled, add the filter to the filter_params list
        if "replication_device" in powerflex_backend:
            filter_params.append("replication_device")

        for param in filter_params:
            if param in powerflex_backend:
                if param == "replication_device":
                    # Extract the password from the content
                    # 'backendid:acme,san_ip:10.20.30.41,san_login:admin,san_password:password'
                    powerflex_config["rep_san_password"] = (
                        powerflex_backend["replication_device"].split(",")[3].split(":")[1]
                    )
                else:
                    powerflex_config[param] = powerflex_backend[param]

        # Render the templates/connector.conf and create the
        # /opt/emc/scaleio/openstack/connector.conf with root access only
        logger.debug("Rendering connector.conf template with config {}".format(powerflex_config))
        render(
            source="connector.conf",
            target=filename,
            context={"backends": powerflex_config},
            perms=0o600,
        )

    def install_sdc(self):
        """Install the SDC debian package in order to get access to the PowerFlex volumes."""
        sdc_package_file = self.model.resources.fetch("sdc-deb-package")
        # Check if the file exists
        if os.path.isfile(sdc_package_file):
            # Get the MDM IP from config file
            sdc_mdm_ips = self.model.config["powerflex-sdc-mdm-ips"]
            # Install the SDC package
            install_cmd = f"sudo MDM_IP={sdc_mdm_ips} dpkg -i {sdc_package_file}"
            logger.info("Installing SDC kernel module with MDM(s) {}".format(sdc_mdm_ips))
            result = subprocess.run(install_cmd.split(), capture_output=True, text=True)
            exit_code = result.returncode
            if exit_code != 0:
                logger.error(
                    "An error occurred during the SDC " "installation: {}.".format(result.stderr)
                )
            else:
                logger.info("SDC installed successfully, stdout: {}".format(result.stdout))
                # Check if service scini is running
                if service_running("scini"):
                    logger.info("SDC scini service running. SDC Installation complete.")
                    # Make sure to mark the state as started.
                    self._stored.is_started = True
                else:
                    logger.error("SDC scini service has encountered errors while starting")
                    self._stored.is_started = False
        else:
            logger.error("The package required for SDC installation is missing.")
            self.unit.status = model.BlockedStatus("SDC package missing")


if __name__ == "__main__":
    main(CinderPowerflexCharm)
