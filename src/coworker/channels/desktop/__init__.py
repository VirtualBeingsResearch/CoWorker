from coworker.channels.desktop.channel import DesktopChannel
from coworker.channels.desktop.communicate_sender import (
    DESKTOP_PREFIX,
    DesktopCommunicateSender,
)
from coworker.channels.desktop.dispatcher import DesktopDispatcher
from coworker.channels.desktop.registry import DesktopRegistry

__all__ = [
    "DESKTOP_PREFIX",
    "DesktopChannel",
    "DesktopCommunicateSender",
    "DesktopDispatcher",
    "DesktopRegistry",
]
