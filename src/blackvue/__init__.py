"""
blackvue-tools

Public package API.
"""

from .core.blackvue_camera import BlackVueCamera
from .core.blackvue_client import BlackVueClient

# Temporary backwards compatibility
BlackVue = BlackVueCamera

__all__ = [
    "BlackVue",
    "BlackVueCamera",
    "BlackVueClient",
]
