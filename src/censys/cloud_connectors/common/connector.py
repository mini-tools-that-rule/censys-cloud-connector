"""Base class for all cloud connectors."""
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Coroutine
from enum import Enum
from functools import partial
from logging import Logger
from typing import Any, Callable, Optional, Union

import aiometer
from requests.exceptions import JSONDecodeError

from censys.asm import Seeds
from censys.common.exceptions import CensysAsmException

from .cloud_asset import CloudAsset
from .enums import EventTypeEnum, ProviderEnum
from .logger import get_logger
from .plugins import CloudConnectorPluginRegistry, EventContext
from .seed import Seed
from .settings import ProviderSpecificSettings, Settings


class CloudConnector(ABC):
    """Base class for Cloud Connectors."""

    provider: ProviderEnum
    provider_settings: ProviderSpecificSettings
    label_prefix: str
    settings: Settings
    logger: Logger
    seeds_api: Seeds
    seeds: dict[str, set[Seed]]
    cloud_assets: dict[str, set[CloudAsset]]
    seed_scanners: dict[str, Callable[..., Coroutine[Any, Any, Any]]]
    cloud_asset_scanners: dict[str, Callable[..., Coroutine[Any, Any, Any]]]
    # current_service: Optional[Union[str, Enum]]

    def __init__(self, settings: Settings):
        """Initialize the Cloud Connector.

        Args:
            settings (Settings): The settings to use.

        Raises:
            ValueError: If the provider is not set.
        """
        if not self.provider:
            raise ValueError("The provider must be set.")
        self.label_prefix = self.provider.label() + ": "
        self.settings = settings
        self.logger = get_logger(
            log_name=f"{self.provider.lower()}_cloud_connector",
            level=settings.logging_level,
        )

        self.seeds_api = Seeds(
            settings.censys_api_key,
            url=settings.censys_asm_api_base_url,
            user_agent=settings.censys_user_agent,
            cookies=settings.censys_cookies,
        )
        self._add_cloud_asset_path = (
            f"{settings.censys_asm_api_base_url}/beta/cloudConnector/addCloudAssets"
        )

        self.seeds = defaultdict(set)
        self.cloud_assets = defaultdict(set)

    async def get_seeds(self, provider_settings, **kwargs) -> None:
        """Gather seeds.

        Args:
            provider_settings (ProviderSpecificSettings): The provider settings.
            **kwargs: Any additional keyword arguments.
        """
        await aiometer.run_all(
            [  # type: ignore
                partial(
                    seed_scanner, provider_settings, current_service=seed_type, **kwargs
                )
                for seed_type, seed_scanner in self.seed_scanners.items()
            ],
            max_at_once=self.settings.max_concurrent_scans,
        )

    async def get_cloud_assets(self, provider_settings, **kwargs) -> None:
        """Gather cloud assets.

        Args:
            provider_settings (ProviderSpecificSettings): The provider settings.
            **kwargs: Any additional keyword arguments.
        """
        await aiometer.run_all(
            [  # type: ignore
                partial(
                    cloud_asset_scanner,
                    provider_settings,
                    current_service=cloud_asset_type,
                    **kwargs,
                )
                for cloud_asset_type, cloud_asset_scanner in self.cloud_asset_scanners.items()
            ],
            max_at_once=self.settings.max_concurrent_scans,
        )

    def get_event_context(
        self,
        event_type: EventTypeEnum,
        service: Optional[Union[str, Enum]] = None,
    ) -> EventContext:
        """Get the event context.

        Args:
            event_type (EventTypeEnum): The event type.
            service (Union[str, Enum], optional): The service. Defaults to None.

        Returns:
            EventContext: The event context.
        """
        return {
            "event_type": event_type,
            "connector": self,
            "provider": self.provider,
            "service": service,
        }

    def dispatch_event(
        self,
        event_type: EventTypeEnum,
        service: Optional[Union[str, Enum]] = None,
        **kwargs,
    ):
        """Dispatch an event.

        Args:
            event_type (EventTypeEnum): The event type.
            service (Union[str, Enum], optional): The service. Defaults to None.
            **kwargs: The event data.
        """
        context = self.get_event_context(event_type, service)
        CloudConnectorPluginRegistry.dispatch_event(context=context, **kwargs)

    def add_seed(self, seed: Seed, **kwargs):
        """Add a seed.

        Args:
            seed (Seed): The seed to add.
            **kwargs: Additional data for event dispatching.
        """
        if not seed.label.startswith(self.label_prefix):
            seed.label = self.label_prefix + seed.label
        self.seeds[seed.label].add(seed)
        self.logger.debug(f"Found Seed: {seed.to_dict()}")
        self.dispatch_event(EventTypeEnum.SEED_FOUND, seed=seed, **kwargs)

    def add_cloud_asset(self, cloud_asset: CloudAsset, **kwargs):
        """Add a cloud asset.

        Args:
            cloud_asset (CloudAsset): The cloud asset to add.
            **kwargs: Additional data for event dispatching.
        """
        if not cloud_asset.uid.startswith(self.label_prefix):
            cloud_asset.uid = self.label_prefix + cloud_asset.uid
        self.cloud_assets[cloud_asset.uid].add(cloud_asset)
        self.logger.debug(f"Found Cloud Asset: {cloud_asset.to_dict()}")
        self.dispatch_event(
            EventTypeEnum.CLOUD_ASSET_FOUND, cloud_asset=cloud_asset, **kwargs
        )

    async def submit_seeds(self):
        """Submit the seeds to the Censys ASM."""
        submitted_seeds = 0
        for label, seeds in self.seeds.items():
            try:
                self.seeds_api.replace_seeds_by_label(
                    label, [seed.to_dict() for seed in seeds]
                )
                submitted_seeds += len(seeds)
            except CensysAsmException as e:
                self.logger.error(f"Error submitting seeds for {label}: {e}")
        self.logger.info(f"Submitted {submitted_seeds} seeds.")
        self.dispatch_event(EventTypeEnum.SEEDS_SUBMITTED, count=submitted_seeds)

    async def submit_cloud_assets(self):
        """Submit the cloud assets to the Censys ASM."""
        submitted_assets = 0
        for uid, cloud_assets in self.cloud_assets.items():
            try:
                data = {
                    "cloudConnectorUid": uid,
                    "cloudAssets": [asset.to_dict() for asset in cloud_assets],
                }
                await self._add_cloud_assets(data)
                submitted_assets += len(cloud_assets)
            except (CensysAsmException, JSONDecodeError) as e:
                self.logger.error(f"Error submitting cloud assets for {uid}: {e}")
        self.logger.info(f"Submitted {submitted_assets} cloud assets.")
        self.dispatch_event(
            EventTypeEnum.CLOUD_ASSETS_SUBMITTED, count=submitted_assets
        )

    async def _add_cloud_assets(self, data: dict) -> dict:
        """Add cloud assets to the Censys ASM.

        Args:
            data (dict): The data to add.

        Returns:
            dict: The response from the Censys ASM.

        Raises:
            CensysAsmException: If the response is not valid.
        """
        res = self.seeds_api._session.post(self._add_cloud_asset_path, json=data)
        json_data = res.json()
        if error_message := json_data.get("error"):
            raise CensysAsmException(
                res.status_code,
                error_message,
                res.text,
                error_code=json_data.get("errorCode"),
            )
        return json_data

    def clear(self):
        """Clear the seeds and cloud assets."""
        self.seeds.clear()
        self.cloud_assets.clear()

    async def submit(self):  # pragma: no cover
        """Submit the seeds and cloud assets to the Censys ASM."""
        if self.settings.dry_run:
            self.logger.info("Dry run enabled. Skipping submission.")
        else:
            self.logger.info("Submitting seeds and assets...")
            await self.submit_seeds()
            await self.submit_cloud_assets()
        self.clear()

    async def scan(self, provider_settings, **kwargs):
        """Scan the seeds and cloud assets.

        Args:
            provider_settings (ProviderSpecificSettings): The provider settings.
            **kwargs: Any additional keyword arguments.
        """
        self.logger.info("Gathering seeds and assets...")
        self.dispatch_event(EventTypeEnum.SCAN_STARTED)
        await self.get_seeds(provider_settings, **kwargs)
        await self.get_cloud_assets(provider_settings, **kwargs)
        await self.submit()
        self.dispatch_event(EventTypeEnum.SCAN_FINISHED)

    @abstractmethod
    async def scan_all(self):
        """Scan all the seeds and cloud assets."""
        pass
