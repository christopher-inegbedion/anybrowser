"""Plugin discovery.

Three extension points, all the same mechanism -- a setuptools entry-point group
plus an in-process override for tests::

    [project.entry-points."relaykit.engines"]
    firefox = "my_package.engine:FirefoxEngine"

    [project.entry-points."relaykit.transports"]
    grpc = "my_package.transport:GrpcTransport"

    [project.entry-points."relaykit.models"]
    bedrock = "my_package.model:BedrockProvider"

Nothing here imports a backend until it is asked for, so an optional dependency
that is not installed costs an import error only at the moment someone selects
it -- never at ``import relaykit``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Generic, TypeVar

from .errors import RelayKitError

logger = logging.getLogger(__name__)

__all__ = ["PluginNotFound", "Registry", "engines", "models", "transports"]

T = TypeVar("T")

ENGINE_GROUP = "relaykit.engines"
TRANSPORT_GROUP = "relaykit.transports"
MODEL_GROUP = "relaykit.models"


class PluginNotFound(RelayKitError):
    """No plugin is registered under that name."""


@dataclass(slots=True)
class Registry(Generic[T]):
    """Lazy name -> class lookup over one entry-point group."""

    group: str
    _local: dict[str, type[T]] | None = None

    def __post_init__(self) -> None:
        if self._local is None:
            self._local = {}

    # -- registration ------------------------------------------------- #

    def register(self, name: str, cls: type[T]) -> type[T]:
        """Register in-process. Wins over entry points; usable as a decorator."""
        assert self._local is not None
        self._local[name] = cls
        return cls

    def unregister(self, name: str) -> None:
        assert self._local is not None
        self._local.pop(name, None)

    # -- lookup -------------------------------------------------------- #

    def _entry_points(self) -> dict[str, EntryPoint]:
        try:
            found = entry_points(group=self.group)
        except Exception:  # pragma: no cover - broken metadata in the wild
            logger.warning("could not read entry points for %s", self.group, exc_info=True)
            return {}
        return {ep.name: ep for ep in found}

    def names(self) -> list[str]:
        """Every registered name, local overrides included, sorted."""
        assert self._local is not None
        return sorted({*self._local, *self._entry_points()})

    def get(self, name: str) -> type[T]:
        assert self._local is not None
        if name in self._local:
            return self._local[name]
        ep = self._entry_points().get(name)
        if ep is None:
            available = ", ".join(self.names()) or "none installed"
            raise PluginNotFound(f"no {self.group} plugin named {name!r}", available=available)
        try:
            loaded: type[T] = ep.load()
            return loaded
        except Exception as exc:  # pragma: no cover - depends on third-party code
            raise PluginNotFound(
                f"{self.group} plugin {name!r} failed to import: {exc}", plugin=name
            ) from exc

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.names()

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())


#: Browser backends. See :mod:`relaykit.core.engine`.
engines: Registry[Any] = Registry(ENGINE_GROUP)
#: Daemon transports. See :mod:`relaykit.daemon.transport`.
transports: Registry[Any] = Registry(TRANSPORT_GROUP)
#: LLM providers. See :mod:`relaykit.models.provider`.
models: Registry[Any] = Registry(MODEL_GROUP)
