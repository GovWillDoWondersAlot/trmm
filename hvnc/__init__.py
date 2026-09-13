"""
TRMM Hidden Virtual Network Computing (HVNC) Package.
"""

from .desktop import HiddenDesktop, BITMAPINFO, BITMAPINFOHEADER
from .spawner import AppSpawner
from .compositor import WindowCompositor
from .input_handler import InputHandler
from .mirror import MirrorCapture, MirrorInput
from .cdp_controller import CDPController

__all__ = [
    "HiddenDesktop",
    "AppSpawner",
    "WindowCompositor",
    "InputHandler",
    "MirrorCapture",
    "MirrorInput",
    "CDPController",
    "BITMAPINFO",
    "BITMAPINFOHEADER",
]

