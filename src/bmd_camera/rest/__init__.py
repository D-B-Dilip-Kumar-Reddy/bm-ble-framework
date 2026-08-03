"""
bmd_camera.rest — REST/WebSocket transport for Blackmagic camera control (8.6 firmware).
"""

from .client import RestClient
from .events import RestEventRouter
from .exceptions import BMDRestError

__all__ = [
    "BMDRestError",
    "RestClient",
    "RestEventRouter",
]
