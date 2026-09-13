"""
Win32 Desktop Management Module for HVNC.
Provides native Win32 Desktop creation, switching, thread attachment, and window enumeration.
"""

import ctypes
from ctypes import wintypes
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger("hvnc.desktop")

# Win32 Constants
GENERIC_ALL = 0x10000000
DESKTOP_ALL = 0x01FF
DESKTOP_CREATEMENU = 0x0004
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_ENUMERATE = 0x0040
DESKTOP_HOOKCONTROL = 0x0020
DESKTOP_JOURNALPLAYBACK = 0x0010
DESKTOP_JOURNALRECORD = 0x0008
DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_WRITEOBJECTS = 0x0080

DESKTOP_ACCESS_MASK = (
    DESKTOP_CREATEMENU
    | DESKTOP_CREATEWINDOW
    | DESKTOP_ENUMERATE
    | DESKTOP_HOOKCONTROL
    | DESKTOP_JOURNALPLAYBACK
    | DESKTOP_JOURNALRECORD
    | DESKTOP_READOBJECTS
    | DESKTOP_SWITCHDESKTOP
    | DESKTOP_WRITEOBJECTS
    | 0x00020000  # READ_CONTROL
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)

# DLLs
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# Function Prototypes
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
user32.CreateDesktopW.restype = wintypes.HANDLE

user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
user32.OpenDesktopW.restype = wintypes.HANDLE

user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL

user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL

user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, WNDENUMPROC, wintypes.LPARAM]
user32.EnumDesktopWindows.restype = wintypes.BOOL

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int


# ── BITMAPINFO structures for GDI DIB Sections ─────────────────────────────
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wintypes.DWORD),
        ("biWidth",         wintypes.LONG),
        ("biHeight",        wintypes.LONG),
        ("biPlanes",        wintypes.WORD),
        ("biBitCount",      wintypes.WORD),
        ("biCompression",   wintypes.DWORD),
        ("biSizeImage",     wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed",       wintypes.DWORD),
        ("biClrImportant",  wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP


class HiddenDesktop:
    """Manages the lifecycle of a hidden Win32 desktop."""

    def __init__(self, desktop_name: str = "TRMM_Hidden_Workspace"):
        self.desktop_name = desktop_name
        self.h_desktop: Optional[int] = None
        self._original_thread_desktop: Optional[int] = None

    def initialize(self) -> bool:
        """Creates or opens the hidden desktop."""
        self._original_thread_desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())

        # Try to open existing desktop first
        self.h_desktop = user32.OpenDesktopW(
            self.desktop_name,
            0,
            False,
            DESKTOP_ACCESS_MASK,
        )

        if not self.h_desktop:
            # Create a brand new desktop in the current window station (WinSta0)
            self.h_desktop = user32.CreateDesktopW(
                self.desktop_name,
                None,
                None,
                0,
                DESKTOP_ACCESS_MASK,
                None,
            )

        if not self.h_desktop:
            err = kernel32.GetLastError()
            logger.error(f"Failed to create/open hidden desktop '{self.desktop_name}'. Error code: {err}")
            return False

        logger.info(f"Hidden desktop '{self.desktop_name}' created/opened successfully. Handle: {self.h_desktop}")

        # Ensure Windows 11 uses classic Win32 context menus (#32768) for 100% GDI capture & click fidelity
        try:
            import winreg
            k_path = r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, k_path) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "")
        except Exception as e:
            logger.debug(f"Could not register classic context menu key: {e}")

        return True

    def attach_current_thread(self) -> bool:
        """Attaches the calling thread to the hidden desktop."""
        if not self.h_desktop:
            return False
        success = user32.SetThreadDesktop(self.h_desktop)
        if not success:
            err = kernel32.GetLastError()
            logger.warning(f"SetThreadDesktop failed for thread {kernel32.GetCurrentThreadId()}. Error: {err}")
        return bool(success)

    def restore_thread_desktop(self) -> bool:
        """Restores the calling thread to its original desktop."""
        if self._original_thread_desktop:
            return bool(user32.SetThreadDesktop(self._original_thread_desktop))
        return True

    def enumerate_windows(self, include_minimized: bool = False):
        """
        Enumerates windows on the hidden desktop.
        If include_minimized is False:
            Returns list of (hwnd, title, class_name, (left, top, right, bottom))
        If include_minimized is True:
            Returns list of (hwnd, title, class_name, (left, top, right, bottom), is_minimized)
        """
        windows = []

        def enum_callback(hwnd: int, lparam: int) -> bool:
            is_min = bool(user32.IsIconic(hwnd))
            if not user32.IsWindowVisible(hwnd) and not is_min:
                return True

            if not include_minimized and is_min:
                return True

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            title = title_buf.value

            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            class_name = class_buf.value

            if class_name in ("Progman", "WorkerW", "DV2ControlHost", "IME", "MSCTFIME UI", "tooltips_class32", "Default IME"):
                return True

            # If not minimized, verify window has positive size and visible position
            if not is_min:
                if width < 8 or height < 8 or rect.left < -1000 or rect.top < -1000:
                    return True

            if include_minimized:
                windows.append((hwnd, title, class_name, (rect.left, rect.top, rect.right, rect.bottom), is_min))
            else:
                windows.append((hwnd, title, class_name, (rect.left, rect.top, rect.right, rect.bottom)))
            return True

        self.attach_current_thread()
        cb = WNDENUMPROC(enum_callback)
        user32.EnumDesktopWindows(self.h_desktop, cb, 0)
        return windows

    def close(self):
        """Closes the desktop handle."""
        if self.h_desktop:
            user32.CloseDesktop(self.h_desktop)
            self.h_desktop = None
            logger.info(f"Hidden desktop '{self.desktop_name}' closed.")
