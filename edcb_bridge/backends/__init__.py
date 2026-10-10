"""Recording backends: EPGStation and mirakc."""

from __future__ import annotations

from typing import TYPE_CHECKING

from edcb_bridge.backends.base import BackendError, RecorderBackend
from edcb_bridge.backends.epgstation import EPGStationBackend
from edcb_bridge.backends.mirakc import MirakcRecorderBackend

if TYPE_CHECKING:
    from edcb_bridge.config import Config

__all__ = ["BackendError", "EPGStationBackend", "MirakcRecorderBackend", "RecorderBackend", "create_backend"]


def create_backend(config: Config) -> RecorderBackend:
    """Create the recording backend selected by `config.recorder`.

    Returns:
        The backend.
    """
    if config.recorder == "mirakc":
        return MirakcRecorderBackend(config)
    return EPGStationBackend(config)
