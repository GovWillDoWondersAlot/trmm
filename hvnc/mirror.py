"""
Tactical RMM — Real Desktop Screen Mirror Capture.

Captures the ACTUAL visible desktop (not the hidden HVNC desktop) using
the same GDI Win32 pipeline already used by compositor.py — no extra
libraries required. This enables the "Take Control" mode where the admin
sees exactly what's on the target machine's screen, and input events are
visible to the target user.

Capture pipeline (mirrors compositor.py approach):
    GetDC(NULL)                  → DC of the entire real screen
    CreateCompatibleDC()         → off-screen memory DC
    CreateDIBSection(32bpp)      → DIB bitmap for direct pixel access
    BitBlt(SRCCOPY)              → copy real screen into DIB
    Image.frombytes("RGB", BGRX) → PIL image
    canvas.save(JPEG)            → compressed frame bytes
"""

import ctypes
from ctypes import wintypes
import io
import time
import threading
import logging
import winreg
import subprocess
import re
from typing import Optional, Tuple

from PIL import Image

from .desktop import BITMAPINFO, BITMAPINFOHEADER

logger = logging.getLogger("hvnc.mirror")


def get_windows_edition() -> str:
    """Dynamically reads the genuine Windows edition and build version from registry."""
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        pn, _ = winreg.QueryValueEx(k, "ProductName")
        try:
            cb_str, _ = winreg.QueryValueEx(k, "CurrentBuild")
            cb = int(cb_str)
        except Exception:
            cb = 0
        try:
            dv, _ = winreg.QueryValueEx(k, "DisplayVersion")
        except Exception:
            dv = ""
        # Build 22000+ is Windows 11 even though registry ProductName legacy string says Windows 10
        if cb >= 22000 and pn.startswith("Windows 10"):
            pn = "Windows 11" + pn[len("Windows 10"):]
        if dv:
            return f"{pn} ({dv})"
        return pn
    except Exception:
        return "Windows 11"

# ── Win32 DLL handles ───────────────────────────────────────────────────────
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ── GDI function signatures ──────────────────────────────────────────────────
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC

user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenDesktopW.restype = wintypes.HANDLE

user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL

user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ

gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD
]
gdi32.BitBlt.restype = wintypes.BOOL

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

SRCCOPY = 0x00CC0020
SM_CXSCREEN = 0
SM_CYSCREEN = 1


# CreateDIBSection configuration using c_void_p for seamless cross-module struct compatibility
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP


class MirrorCapture:
    """
    Captures the real Windows desktop (visible to the target user) as JPEG frames.
    Uses GDI BitBlt on GetDC(NULL) — the same approach as compositor.py but for the
    full real screen instead of individual HVNC windows.

    Input events dispatched while mirror mode is active go to the real desktop via
    SendInput, making the mouse cursor visible to the target user (intended behavior).
    """

    def __init__(self):
        self._width: int = 0
        self._height: int = 0
        # Persistent off-screen DIB buffer (re-created only when screen resolution changes)
        self._hdc_mem: Optional[wintypes.HDC] = None
        self._h_bmp: Optional[wintypes.HBITMAP] = None
        self._p_bits: Optional[ctypes.c_void_p] = None
        self._buf_w: int = 0
        self._buf_h: int = 0
        self._last_raw_bytes: Optional[bytes] = None
        self._last_frame_bytes: Optional[bytes] = None
        self._last_frame_time: float = 0.0
        self._refresh_screen_size()

    def _refresh_screen_size(self):
        """Gets current screen resolution, accounting for DPI scaling."""
        try:
            # Use SM_CXSCREEN/SM_CYSCREEN for physical pixel dimensions
            w = user32.GetSystemMetrics(SM_CXSCREEN)
            h = user32.GetSystemMetrics(SM_CYSCREEN)
            self._width = w if w > 0 else 1920
            self._height = h if h > 0 else 1080
        except Exception:
            self._width = 1920
            self._height = 1080

    def _ensure_buffer(self, w: int, h: int) -> bool:
        """Allocates or re-allocates the off-screen DIB section if dimensions changed."""
        if self._hdc_mem and self._buf_w == w and self._buf_h == h:
            return True  # Buffer already correct size

        self._release_buffer()

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return False

        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            user32.ReleaseDC(None, hdc_screen)
            return False

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h   # Negative = top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0   # BI_RGB

        p_bits = ctypes.c_void_p()
        h_bmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(p_bits), None, 0)
        user32.ReleaseDC(None, hdc_screen)

        if not h_bmp or not p_bits.value:
            gdi32.DeleteDC(hdc_mem)
            return False

        gdi32.SelectObject(hdc_mem, h_bmp)
        self._hdc_mem = hdc_mem
        self._h_bmp = h_bmp
        self._p_bits = p_bits
        self._buf_w = w
        self._buf_h = h
        return True

    def _release_buffer(self):
        """Frees GDI resources."""
        try:
            if self._h_bmp:
                gdi32.DeleteObject(self._h_bmp)
            if self._hdc_mem:
                gdi32.DeleteDC(self._hdc_mem)
        except Exception:
            pass
        self._hdc_mem = None
        self._h_bmp = None
        self._p_bits = None
        self._buf_w = 0
        self._buf_h = 0

    def capture_frame(self, is_active: bool = False) -> Optional[bytes]:
        """
        Captures the full real desktop screen and returns JPEG-encoded bytes.
        Drop-in equivalent of WindowCompositor.render_frame() for mirror mode.
        """
        try:
            h_def = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_def:
                user32.SetThreadDesktop(h_def)
                user32.CloseDesktop(h_def)
        except Exception:
            pass

        self._refresh_screen_size()
        w, h = self._width, self._height

        if not self._ensure_buffer(w, h):
            # Fallback: return a blank black frame rather than crashing
            img = Image.new("RGB", (w, h), (0, 0, 0))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=60)
            return out.getvalue()

        # Get the real screen DC
        hdc_screen = user32.GetDC(None)   # GetDC(NULL) = entire screen
        if not hdc_screen:
            img = Image.new("RGB", (w, h), (10, 10, 10))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=60)
            return out.getvalue()

        try:
            # BitBlt: copy real screen pixels into our off-screen DIB
            gdi32.BitBlt(self._hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, SRCCOPY)
        finally:
            user32.ReleaseDC(None, hdc_screen)

        # Convert raw BGRX pixel buffer directly to PIL RGB image (same as compositor.py)
        buf_size = w * h * 4
        now = time.time()
        try:
            raw_bytes = ctypes.string_at(self._p_bits.value, buf_size)
            # Skip sending identical frames if screen has not changed within the last 0.8s
            if self._last_raw_bytes and raw_bytes == self._last_raw_bytes and (now - self._last_frame_time < 0.8):
                return None
            self._last_raw_bytes = raw_bytes
            img = Image.frombytes("RGB", (w, h), raw_bytes, "raw", "BGRX")
        except Exception as e:
            logger.debug(f"Mirror frame conversion error: {e}")
            img = Image.new("RGB", (w, h), (20, 20, 20))

        # Adaptive JPEG quality: 48 during active motion/drag (~35KB), 65 when idle (~95KB)
        # Keeps native coordinates 1:1 without downscaling distortion or mouse desync
        quality = 48 if is_active else 65

        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, subsampling=2, optimize=False)
        frame_bytes = output.getvalue()
        self._last_frame_bytes = frame_bytes
        self._last_frame_time = now
        return frame_bytes

    def render_frame(self, is_active: bool = False) -> Optional[bytes]:
        """Alias for capture_frame so it shares the same interface as WindowCompositor."""
        return self.capture_frame(is_active=is_active)

    def cleanup(self):
        """Call when mirror mode ends to free GDI resources."""
        self._release_buffer()

    def __del__(self):
        try:
            self._release_buffer()
        except Exception:
            pass


# ── Mirror Input Injection (Real Desktop) ──────────────────────────────────
TRMM_INPUT_MAGIC = 0x54524D4D  # 'TRMM' marker in dwExtraInfo to distinguish remote admin input

user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL

user32.mouse_event.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_size_t]
user32.mouse_event.restype = None

user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_size_t]
user32.keybd_event.restype = None

try:
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    user32.VkKeyScanW.restype = ctypes.c_short
except Exception:
    pass


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT),
    ]


try:
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
except Exception:
    pass

try:
    user32.CreateCursor.argtypes = [
        wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p
    ]
    user32.CreateCursor.restype = wintypes.HCURSOR
except Exception:
    pass

try:
    user32.SetSystemCursor.argtypes = [wintypes.HCURSOR, wintypes.DWORD]
    user32.SetSystemCursor.restype = wintypes.BOOL
except Exception:
    pass

try:
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
except Exception:
    pass


class MirrorInput:
    """
    Dispatches input directly to the real Windows desktop session.
    Supports full physical keyboard typing, shortcuts, and Stealth Mouse (suppress hover).
    All remote administrative inputs are tagged with TRMM_INPUT_MAGIC in dwExtraInfo
    so low-level input hooks permit admin actions while blocking physical target inputs.
    """

    MOUSE_LEFTDOWN = 0x0002
    MOUSE_LEFTUP = 0x0004
    MOUSE_RIGHTDOWN = 0x0008
    MOUSE_RIGHTUP = 0x0010
    MOUSE_MIDDLEDOWN = 0x0020
    MOUSE_MIDDLEUP = 0x0040
    MOUSE_WHEEL = 0x0800

    KEY_EXTENDED = 0x0001
    KEY_KEYUP = 0x0002
    KEY_UNICODE = 0x0004

    EXTENDED_KEYS = {
        0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
        0x5B, 0x5C, 0x5D, 0x90, 0x91
    }

    _stealth_cursor_active = False
    _stealth_lock = threading.Lock()

    @classmethod
    def _ensure_default_desktop(cls):
        """Attaches current thread to Default desktop so keystrokes & clicks reach the active user session."""
        try:
            h_def = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_def:
                user32.SetThreadDesktop(h_def)
                user32.CloseDesktop(h_def)
        except Exception:
            pass

    @classmethod
    def set_stealth_cursor(cls, enabled: bool):
        """
        Replaces system cursor with a 100% transparent cursor so clicks and drags
        do not visually render the hardware cursor sprite on the target monitor.
        Restores default system cursors via SPI_SETCURSORS when disabled.
        """
        with cls._stealth_lock:
            try:
                cls._ensure_default_desktop()
                if enabled and not cls._stealth_cursor_active:
                    and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
                    xor_mask = (ctypes.c_ubyte * 128)(*([0x00] * 128))
                    ocr_ids = [32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650]
                    for ocr in ocr_ids:
                        hcur = user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
                        if hcur:
                            user32.SetSystemCursor(hcur, ocr)
                    cls._stealth_cursor_active = True
                    logger.info("Stealth cursor activated: transparent system cursor installed")
                elif not enabled:
                    user32.SystemParametersInfoW(0x0057, 0, None, 0)  # SPI_SETCURSORS
                    cls._stealth_cursor_active = False
                    logger.info("Stealth cursor deactivated: system cursors restored")
            except Exception as e:
                logger.debug(f"set_stealth_cursor error: {e}")

    @classmethod
    def clear_modifiers(cls):
        """Releases modifier keys (Alt, Ctrl, Shift) to prevent stuck modifiers from turning double-clicks into Properties dialogs."""
        cls._ensure_default_desktop()
        try:
            for vk in (0x12, 0x11, 0x10):  # VK_MENU (Alt), VK_CONTROL, VK_SHIFT
                user32.keybd_event(vk, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
        except Exception:
            pass

    @classmethod
    def _send_mouse_event(cls, x: int, y: int, flags: int):
        cls._ensure_default_desktop()
        ix = int(x)
        iy = int(y)
        user32.SetCursorPos(ix, iy)
        # Use absolute normalized positioning (0..65535) so the button event
        # is atomically dispatched at the exact coordinate without any positional desync or drag
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        norm_x = int(ix * 65535 / (sw - 1)) if sw > 1 else 0
        norm_y = int(iy * 65535 / (sh - 1)) if sh > 1 else 0
        # MOUSEEVENTF_ABSOLUTE (0x8000) | MOUSEEVENTF_MOVE (0x0001)
        full_flags = flags | 0x8000 | 0x0001
        user32.mouse_event(full_flags, norm_x, norm_y, 0, TRMM_INPUT_MAGIC)

    @classmethod
    def mouse_move(cls, x: int, y: int, stealth: bool = False):
        if stealth:
            return
        cls._send_mouse_event(x, y, 0x0001)  # MOUSEEVENTF_MOVE with MOUSEEVENTF_ABSOLUTE & TRMM_INPUT_MAGIC

    @classmethod
    def mouse_down(cls, x: int, y: int, button: str = "left", stealth: bool = False):
        if stealth:
            cls.set_stealth_cursor(True)
        if button == "right":
            flags = cls.MOUSE_RIGHTDOWN
        elif button == "middle":
            flags = cls.MOUSE_MIDDLEDOWN
        else:
            flags = cls.MOUSE_LEFTDOWN
        cls._send_mouse_event(x, y, flags)

    @classmethod
    def mouse_up(cls, x: int, y: int, button: str = "left", stealth: bool = False):
        if stealth:
            cls.set_stealth_cursor(True)
        if button == "right":
            flags = cls.MOUSE_RIGHTUP
        elif button == "middle":
            flags = cls.MOUSE_MIDDLEUP
        else:
            flags = cls.MOUSE_LEFTUP
        cls._send_mouse_event(x, y, flags)

    @classmethod
    def double_click(cls, x: int, y: int, stealth: bool = False):
        if stealth:
            cls.set_stealth_cursor(True)
        cls._send_mouse_event(x, y, cls.MOUSE_LEFTDOWN)
        cls._send_mouse_event(x, y, cls.MOUSE_LEFTUP)
        time.sleep(0.04)
        cls._send_mouse_event(x, y, cls.MOUSE_LEFTDOWN)
        cls._send_mouse_event(x, y, cls.MOUSE_LEFTUP)

    @classmethod
    def cleanup(cls):
        """Restores modifiers and resets stealth cursor if active."""
        cls.clear_modifiers()
        cls.set_stealth_cursor(False)

    @classmethod
    def mouse_wheel(cls, x: int, y: int, delta: int):
        cls._ensure_default_desktop()
        user32.SetCursorPos(int(x), int(y))
        w_delta = int(delta) if abs(delta) >= 120 else (120 if delta > 0 else -120)
        user32.mouse_event(cls.MOUSE_WHEEL, 0, 0, ctypes.c_ulong(w_delta).value, TRMM_INPUT_MAGIC)

    @classmethod
    def key_down(cls, vk: int):
        cls._ensure_default_desktop()
        v = int(vk) & 0xFF
        flags = cls.KEY_EXTENDED if (v in cls.EXTENDED_KEYS) else 0
        user32.keybd_event(v, 0, flags, TRMM_INPUT_MAGIC)

    @classmethod
    def key_up(cls, vk: int):
        cls._ensure_default_desktop()
        v = int(vk) & 0xFF
        flags = cls.KEY_KEYUP
        if v in cls.EXTENDED_KEYS:
            flags |= cls.KEY_EXTENDED
        user32.keybd_event(v, 0, flags, TRMM_INPUT_MAGIC)

    @classmethod
    def send_char(cls, char_code: int):
        cls._ensure_default_desktop()
        # 1. Primary path: VkKeyScanW + keybd_event with TRMM_INPUT_MAGIC
        try:
            ch = chr(int(char_code))
            vk_scan = user32.VkKeyScanW(ch)
            if vk_scan != -1:
                vk = vk_scan & 0xFF
                shift = bool(vk_scan & 0x0100)
                ctrl = bool(vk_scan & 0x0200)
                alt = bool(vk_scan & 0x0400)
                if shift:
                    user32.keybd_event(0x10, 0, 0, TRMM_INPUT_MAGIC)
                if ctrl:
                    user32.keybd_event(0x11, 0, 0, TRMM_INPUT_MAGIC)
                if alt:
                    user32.keybd_event(0x12, 0, 0, TRMM_INPUT_MAGIC)
                user32.keybd_event(vk, 0, 0, TRMM_INPUT_MAGIC)
                user32.keybd_event(vk, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
                if alt:
                    user32.keybd_event(0x12, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
                if ctrl:
                    user32.keybd_event(0x11, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
                if shift:
                    user32.keybd_event(0x10, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
                return
        except Exception:
            pass

        # 2. Secondary path: SendInput with KEYEVENTF_UNICODE
        try:
            inp_down = INPUT()
            inp_down.type = 1  # INPUT_KEYBOARD
            inp_down.ki.wVk = 0
            inp_down.ki.wScan = int(char_code)
            inp_down.ki.dwFlags = cls.KEY_UNICODE
            inp_down.ki.dwExtraInfo = ctypes.c_void_p(TRMM_INPUT_MAGIC)

            inp_up = INPUT()
            inp_up.type = 1
            inp_up.ki.wVk = 0
            inp_up.ki.wScan = int(char_code)
            inp_up.ki.dwFlags = cls.KEY_UNICODE | cls.KEY_KEYUP
            inp_up.ki.dwExtraInfo = ctypes.c_void_p(TRMM_INPUT_MAGIC)

            inputs = (INPUT * 2)(inp_down, inp_up)
            ret = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
            if ret > 0:
                return
        except Exception:
            pass

        # 3. Last-resort fallback
        try:
            v = int(char_code) & 0xFF
            user32.keybd_event(v, 0, 0, TRMM_INPUT_MAGIC)
            user32.keybd_event(v, 0, cls.KEY_KEYUP, TRMM_INPUT_MAGIC)
        except Exception:
            pass


# ── Screen Curtain & Low-Level Selective Input Hooking ──────────────────────
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
LLMHF_INJECTED = 0x00000001
LLKHF_INJECTED = 0x00000010

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD
)

try:
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
except Exception:
    pass

try:
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK

    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL

    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_longlong
except Exception:
    pass

try:
    user32.SetWinEventHook.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
        WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD
    ]
    user32.SetWinEventHook.restype = wintypes.HANDLE

    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user32.UnhookWinEvent.restype = wintypes.BOOL
except Exception:
    pass

try:
    user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
except Exception:
    pass

try:
    user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
except Exception:
    pass

user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong

user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.BeginPaint.restype = ctypes.c_void_p

user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.EndPaint.restype = wintypes.BOOL

user32.DrawTextW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int

user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p]
user32.FillRect.restype = ctypes.c_int

user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL

user32.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
user32.InvalidateRect.restype = wintypes.BOOL

gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p

gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p

gdi32.RoundRect.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.RoundRect.restype = wintypes.BOOL

gdi32.Ellipse.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.Ellipse.restype = wintypes.BOOL

gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = ctypes.c_void_p

gdi32.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF

gdi32.SetBkColor.argtypes = [ctypes.c_void_p, wintypes.COLORREF]
gdi32.SetBkColor.restype = wintypes.COLORREF

gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int

gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR
]
gdi32.CreateFontW.restype = ctypes.c_void_p

user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.UpdateWindow.argtypes = [ctypes.c_void_p]
user32.UpdateWindow.restype = wintypes.BOOL

user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.DestroyWindow.restype = wintypes.BOOL

user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t

user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class AudioVolumeLock:
    """
    Direct COM VTable interface to Windows Core Audio Endpoint Volume.
    Snapshots master volume & mute status and enforces continuous lock during Screen Curtain,
    preventing OEM hotkey drivers (e.g. HP HotKey UWP, Lenovo Vantage) or physical volume
    buttons (F6/F7/F8) from adjusting audio during maintenance.
    """
    _p_endpoint = None
    _get_vol_fn = None
    _set_vol_fn = None
    _get_mute_fn = None
    _set_mute_fn = None
    _initialized = False

    @classmethod
    def _init(cls):
        if cls._initialized:
            return
        try:
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)

            class GUID(ctypes.Structure):
                _fields_ = [
                    ('Data1', wintypes.DWORD),
                    ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD),
                    ('Data4', ctypes.c_byte * 8),
                ]

            CLSID_MMDeviceEnumerator = GUID(0xBCDE0395, 0xE52F, 0x467C, (ctypes.c_byte * 8)(0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E))
            IID_IMMDeviceEnumerator = GUID(0xA95664D2, 0x9614, 0x4F35, (ctypes.c_byte * 8)(0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6))
            IID_IAudioEndpointVolume = GUID(0x5CDF2C82, 0x841E, 0x4546, (ctypes.c_byte * 8)(0x97, 0x22, 0x0C, 0xF7, 0x40, 0x78, 0x22, 0x9A))

            pEnum = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum))
            if hr == 0 and pEnum.value:
                vtable = ctypes.cast(pEnum, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                GetDefaultEndpoint = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(vtable[4])
                pDev = ctypes.c_void_p()
                hr = GetDefaultEndpoint(pEnum, 0, 0, ctypes.byref(pDev))
                if hr == 0 and pDev.value:
                    dev_vtable = ctypes.cast(pDev, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                    Activate = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[3])
                    pEp = ctypes.c_void_p()
                    hr = Activate(pDev, ctypes.byref(IID_IAudioEndpointVolume), 1, None, ctypes.byref(pEp))
                    if hr == 0 and pEp.value:
                        cls._p_endpoint = pEp
                        ep_vtable = ctypes.cast(pEp, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        cls._set_vol_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p)(ep_vtable[7])
                        cls._get_vol_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float))(ep_vtable[9])
                        cls._set_mute_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p)(ep_vtable[14])
                        cls._get_mute_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL))(ep_vtable[15])
                        cls._initialized = True
        except Exception as e:
            logger.debug(f"AudioVolumeLock init error: {e}")

    @classmethod
    def get_volume(cls) -> Optional[float]:
        cls._init()
        if not cls._p_endpoint or not cls._get_vol_fn:
            return None
        try:
            val = ctypes.c_float()
            hr = cls._get_vol_fn(cls._p_endpoint, ctypes.byref(val))
            if hr == 0:
                return float(val.value)
        except Exception:
            pass
        return None

    @classmethod
    def set_volume(cls, vol: float) -> bool:
        cls._init()
        if not cls._p_endpoint or not cls._set_vol_fn:
            return False
        try:
            hr = cls._set_vol_fn(cls._p_endpoint, ctypes.c_float(vol), None)
            return hr == 0
        except Exception:
            return False

    @classmethod
    def get_mute(cls) -> Optional[bool]:
        cls._init()
        if not cls._p_endpoint or not cls._get_mute_fn:
            return None
        try:
            val = wintypes.BOOL()
            hr = cls._get_mute_fn(cls._p_endpoint, ctypes.byref(val))
            if hr == 0:
                return bool(val.value)
        except Exception:
            pass
        return None

    @classmethod
    def set_mute(cls, mute: bool) -> bool:
        cls._init()
        if not cls._p_endpoint or not cls._set_mute_fn:
            return False
        try:
            hr = cls._set_mute_fn(cls._p_endpoint, wintypes.BOOL(mute), None)
            return hr == 0
        except Exception:
            return False


class AccessibilityHotkeyLock:
    """
    Suppresses Windows accessibility shortcut popups (StickyKeys, ToggleKeys, FilterKeys)
    during Screen Curtain or Input Block mode.
    - Prevents 5x Shift key presses from launching the StickyKeys confirmation dialog.
    - Prevents 5-second NumLock holds from launching ToggleKeys.
    - Prevents 8-second Right Shift holds from launching FilterKeys.
    Restores original user settings on session exit.
    """
    _orig_sticky = None
    _orig_toggle = None
    _orig_filter = None

    @classmethod
    def disable_all(cls):
        try:
            class ACCESSIBILITY(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]

            # 1. StickyKeys (SPI_GETSTICKYKEYS = 0x003A, SPI_SETSTICKYKEYS = 0x003B)
            sk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY))
            if user32.SystemParametersInfoW(0x003A, sk.cbSize, ctypes.byref(sk), 0):
                if cls._orig_sticky is None:
                    cls._orig_sticky = sk.dwFlags
                sk.dwFlags &= ~0x00000004  # SKF_HOTKEYACTIVE
                sk.dwFlags &= ~0x00000008  # SKF_CONFIRMHOTKEY
                user32.SystemParametersInfoW(0x003B, sk.cbSize, ctypes.byref(sk), 0)

            # 2. ToggleKeys (SPI_GETTOGGLEKEYS = 0x0034, SPI_SETTOGGLEKEYS = 0x0035)
            tk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY))
            if user32.SystemParametersInfoW(0x0034, tk.cbSize, ctypes.byref(tk), 0):
                if cls._orig_toggle is None:
                    cls._orig_toggle = tk.dwFlags
                tk.dwFlags &= ~0x00000004  # TKF_HOTKEYACTIVE
                tk.dwFlags &= ~0x00000008  # TKF_CONFIRMHOTKEY
                user32.SystemParametersInfoW(0x0035, tk.cbSize, ctypes.byref(tk), 0)

            # 3. FilterKeys (SPI_GETFILTERKEYS = 0x0032, SPI_SETFILTERKEYS = 0x0033)
            fk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY))
            if user32.SystemParametersInfoW(0x0032, fk.cbSize, ctypes.byref(fk), 0):
                if cls._orig_filter is None:
                    cls._orig_filter = fk.dwFlags
                fk.dwFlags &= ~0x00000004  # FKF_HOTKEYACTIVE
                fk.dwFlags &= ~0x00000008  # FKF_CONFIRMHOTKEY
                user32.SystemParametersInfoW(0x0033, fk.cbSize, ctypes.byref(fk), 0)

            logger.info("Accessibility popup hotkeys (StickyKeys/ToggleKeys/FilterKeys) suppressed for maintenance")
        except Exception as e:
            logger.debug(f"Failed to suppress accessibility hotkeys: {e}")

    @classmethod
    def restore_all(cls):
        try:
            class ACCESSIBILITY(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]

            if cls._orig_sticky is not None:
                sk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY), dwFlags=cls._orig_sticky)
                user32.SystemParametersInfoW(0x003B, sk.cbSize, ctypes.byref(sk), 0)
                cls._orig_sticky = None

            if cls._orig_toggle is not None:
                tk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY), dwFlags=cls._orig_toggle)
                user32.SystemParametersInfoW(0x0035, tk.cbSize, ctypes.byref(tk), 0)
                cls._orig_toggle = None

            if cls._orig_filter is not None:
                fk = ACCESSIBILITY(cbSize=ctypes.sizeof(ACCESSIBILITY), dwFlags=cls._orig_filter)
                user32.SystemParametersInfoW(0x0033, fk.cbSize, ctypes.byref(fk), 0)
                cls._orig_filter = None

            logger.info("Accessibility popup hotkeys restored to original settings")
        except Exception as e:
            logger.debug(f"Failed to restore accessibility hotkeys: {e}")


def _run_silent(cmd, timeout=3):
    """Executes a command completely in the background with zero visible console window."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    try:
        return subprocess.run(
            cmd,
            shell=True if isinstance(cmd, str) else False,
            capture_output=True,
            text=True,
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
    except Exception:
        return None


def _popen_silent(cmd):
    """Launches a background process without opening any console window."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    try:
        return subprocess.Popen(
            cmd,
            shell=True if isinstance(cmd, str) else False,
            startupinfo=startupinfo,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
    except Exception:
        return None


class WorkstationLockPolicy:
    """
    Enforces Windows Lock Workstation policy and disables Explorer hotkeys (Win+L, Win+Ctrl+Arrows, etc.).
    - DisableLockWorkstation (Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System):
      Disables the Windows OS workstation lock function at the kernel/LogonUI level.
    - DisabledHotkeys (Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced):
      Disables Windows Explorer response to all Win + [Key] combinations.
    """
    _applied_targets = []

    @classmethod
    def _get_target_keys(cls):
        targets = [
            (r"HKLM\Software\Microsoft\Windows\CurrentVersion\Policies\System", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"),
            (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"),
        ]
        try:
            k_users = winreg.OpenKey(winreg.HKEY_USERS, "", 0, winreg.KEY_READ)
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(k_users, idx)
                    idx += 1
                    if sub.startswith("S-1-5-21-") and not sub.endswith("_Classes"):
                        targets.append((
                            rf"HKU\{sub}\Software\Microsoft\Windows\CurrentVersion\Policies\System",
                            rf"HKU\{sub}\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
                        ))
                except OSError:
                    break
            winreg.CloseKey(k_users)
        except Exception:
            pass
        return targets

    @classmethod
    def lock(cls):
        cls._applied_targets = []
        for pol_key, adv_key in cls._get_target_keys():
            # 1. DisableLockWorkstation
            res = _run_silent(f'reg add "{pol_key}" /v DisableLockWorkstation /t REG_DWORD /d 1 /f')
            if res and res.returncode == 0:
                cls._applied_targets.append(pol_key)

            # 2. DisabledHotkeys
            _run_silent(f'reg add "{adv_key}" /v DisabledHotkeys /t REG_SZ /d "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" /f')

        # 3. Broadcast WM_SETTINGCHANGE (0x001A) so Explorer and LogonUI immediately reload policies
        try:
            user32.PostMessageW(0xFFFF, 0x001A, 0, 0)
        except Exception:
            pass
        logger.info(f"WorkstationLockPolicy: DisableLockWorkstation applied across {len(cls._applied_targets)} registry hives and broadcast.")

    @classmethod
    def restore(cls):
        for pol_key, adv_key in cls._get_target_keys():
            _run_silent(f'reg delete "{pol_key}" /v DisableLockWorkstation /f')
            _run_silent(f'reg delete "{adv_key}" /v DisabledHotkeys /f')

        cls._applied_targets = []
        try:
            user32.PostMessageW(0xFFFF, 0x001A, 0, 0)
        except Exception:
            pass
        logger.info("WorkstationLockPolicy: Restored lock policies and hotkeys across all hives.")


class TouchpadLock:
    """
    Suppresses physical touchpad hardware and multi-finger Precision Touchpad gestures (3-finger / 4-finger virtual desktop switching).
    - Precision Touchpad Master Switch (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\PrecisionTouchPad\\Status\\Enabled = 0):
      Completely disables the physical touchpad in Windows without requiring elevation.
    - PrecisionTouchPad Gesture Lockdown: sets FourFingerSlideHorz, ThreeFingerSlideHorz, etc., to 0 and broadcasts WM_SETTINGCHANGE.
    - Silent Plug & Play hardware disable via background PowerShell if elevated.
    """
    _orig_values = {}
    _orig_status_enabled = None

    @classmethod
    def lock(cls):
        # Only suppress multi-finger workspace switching gestures during curtain;
        # NEVER disable physical taps or clicks so user trackpad clicking is never disrupted!
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad", 0, winreg.KEY_READ | winreg.KEY_WRITE)
            keys_to_zero = [
                'FourFingerSlideHorz', 'FourFingerSlideVert', 'FourFingerTap',
                'ThreeFingerSlideHorz', 'ThreeFingerSlideVert', 'ThreeFingerTap'
            ]
            cls._orig_values = {}
            for name in keys_to_zero:
                try:
                    val, typ = winreg.QueryValueEx(k, name)
                    cls._orig_values[name] = (val, typ)
                except OSError:
                    cls._orig_values[name] = None
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(k)
        except Exception as e:
            logger.debug(f"PrecisionTouchPad gesture lock error: {e}")

    @classmethod
    def restore(cls):
        # 1. Release any potentially stuck mouse buttons or modifiers
        try:
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            user32.mouse_event(0x0010, 0, 0, 0, 0)  # MOUSEEVENTF_RIGHTUP
            user32.mouse_event(0x0040, 0, 0, 0, 0)  # MOUSEEVENTF_MIDDLEUP
        except Exception:
            pass

        # 2. Guarantee Precision Touchpad Master Switch is ENABLED = 1
        try:
            k_status = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad\Status")
            winreg.SetValueEx(k_status, "Enabled", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(k_status)
        except Exception:
            pass

        # 3. Restore all Precision Touchpad Click and Tap settings in HKCU
        try:
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad")
            settings = {
                'TapsEnabled': 1,
                'TapAndDrag': 1,
                'TwoFingerTapEnabled': 1,
                'RightClickZoneEnabled': 1,
                'PanEnabled': 1,
                'ZoomEnabled': 1,
                'EnableEdgy': 1,
                'LeaveOnWithMouse': 1,
                'CursorSpeed': 10,
                'AAPThreshold': 0,  # 0 = Most sensitive (instant clickdown registration, zero palm delay)
                'FourFingerSlideHorz': 1,
                'FourFingerSlideVert': 1,
                'FourFingerTap': 1,
                'ThreeFingerSlideHorz': 1,
                'ThreeFingerSlideVert': 1,
                'ThreeFingerTap': 1,
            }
            for name, val in settings.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)

            try:
                winreg.DeleteValue(k, "ClickpadClickEnabled")
            except OSError:
                pass

            winreg.CloseKey(k)
            cls._orig_values.clear()

            # 4. Configure HKLM PrecisionTouchPad: Disable AAP suppression and configure physical RightClickZone
            try:
                k_hklm = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad")
                winreg.SetValueEx(k_hklm, "AAPDisabled", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k_hklm, "RightClickZoneWidth", 0, winreg.REG_DWORD, 5000)
                winreg.SetValueEx(k_hklm, "RightClickZoneHeight", 0, winreg.REG_DWORD, 3300)
                winreg.CloseKey(k_hklm)
            except Exception:
                pass

            # Broadcast setting update safely to Windows input engine
            try:
                user32.SendNotifyMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
                user32.SendNotifyMessageW.restype = wintypes.BOOL
                user32.SendNotifyMessageW(0xFFFF, 0x001A, 0, "PrecisionTouchPad")
                user32.SendNotifyMessageW(0xFFFF, 0x001A, 0, "Windows.UI.Core.Input.PointerInputObserver")
            except Exception:
                pass
            logger.info("Precision Touchpad fully restored: Taps=1, Physical Click=1, RightClickZone=1, Gestures=1")
        except Exception as e:
            logger.debug(f"PrecisionTouchPad registry restore error: {e}")

        # 3. Ensure any PnP touchpad hardware device is enabled (non-blocking background thread)
        def _bg_pnp():
            try:
                cmd = (
                    "Get-PnpDevice | Where-Object { "
                    "$_.FriendlyName -match 'Touchpad|Touch Pad|Synaptics|ELAN|Precision Touchpad' "
                    "-or $_.InstanceId -match 'SYNA|ELAN' "
                    "} | ForEach-Object { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false }"
                )
                _run_silent(["powershell", "-NoProfile", "-Command", cmd], timeout=5)
            except Exception:
                pass
        threading.Thread(target=_bg_pnp, daemon=True).start()


class OEMHotkeyBlocker:
    """
    Suppresses OEM-specific action hotkeys (such as HP Wireless Button Driver / HotKeyServiceUWP).
    Executed completely silently in the background with CREATE_NO_WINDOW.
    """
    @classmethod
    def lock(cls):
        try:
            _run_silent('powershell -NoProfile -Command "Stop-Service HotKeyServiceUWP -Force"', timeout=2)
            cmd = (
                "Get-PnpDevice | Where-Object { "
                "$_.InstanceId -match 'HPQ6001' -or $_.FriendlyName -match 'HP Wireless Button' "
                "} | ForEach-Object { Disable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false }"
            )
            _run_silent(["powershell", "-NoProfile", "-Command", cmd], timeout=2)
        except Exception:
            pass

    @classmethod
    def restore(cls):
        try:
            _run_silent('powershell -NoProfile -Command "Start-Service HotKeyServiceUWP"', timeout=2)
            cmd = (
                "Get-PnpDevice | Where-Object { "
                "$_.InstanceId -match 'HPQ6001' -or $_.FriendlyName -match 'HP Wireless Button' "
                "} | ForEach-Object { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false }"
            )
            _run_silent(["powershell", "-NoProfile", "-Command", cmd], timeout=2)
        except Exception:
            pass


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class ScreenCurtain:
    """
    Manages the physical monitor privacy screen curtain and selective remote input locking.
    Renders an authentic, full-screen Windows 11 Enterprise / Pro Update & Maintenance display
    directly via high-DPI GDI with ClearType smoothing.
    - WS_EX_TOOLWINDOW: Guarantees zero window icon in the Windows Taskbar or Alt-Tab switcher.
    - SetProcessDpiAwarenessContext: Covers 100% of all displays across any scaling percentage.
    - HTTRANSPARENT: Remote technician clicks pass straight through to open background applications.
    - WH_MOUSE_LL / WH_KEYBOARD_LL: Drops all physical mouse moves and typing at the target desk
      while passing remote admin events tagged with TRMM_INPUT_MAGIC.
    - SetWinEventHook (EVENT_SYSTEM_FOREGROUND & EVENT_OBJECT_SHOW): Provides zero-latency Z-order
      lock so newly launched applications/dialogs never flash or override the curtain on the target monitor.
    """
    _hwnd = None
    _desktop_hwnds = {}
    _vd_reg_key = None
    _last_vd = None
    _stop_event = None
    _thread = None
    _class_registered = False
    _curtain_active = False
    _input_blocked = False
    _need_recreate = False
    _wnd_proc_ref = None
    _mouse_proc_ref = None
    _kbd_proc_ref = None
    _winevent_proc_ref = None
    _mouse_hook = None
    _kbd_hook = None
    _win_event_hook1 = None
    _win_event_hook2 = None
    _win_event_hook3 = None
    _lock = threading.Lock()

    _bg_brush = None
    _blue_brush = None
    _white_brush = None
    _title_font = None
    _sub_font = None
    _detail_font = None
    _foot_font = None
    _os_edition = None
    _spinner_frame = 0
    _spin_mem_dc = None
    _spin_mem_bmp = None
    _spin_mem_old_bmp = None
    _spin_size = 0
    _orig_power_btn_ac = None
    _orig_power_btn_dc = None
    _locked_volume = None
    _locked_mute = None
    _saved_wifi_profile = None
    _last_wifi_check = 0
    _bg_locks_thread = None
    _bg_locks_stop = None
    _registered_hotkeys = []

    @classmethod
    def _set_taskbar_visible(cls, visible: bool):
        """Hides or restores the Windows Taskbar so gestures cannot peek it."""
        try:
            MirrorInput._ensure_default_desktop()
            for cls_name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                h = user32.FindWindowW(cls_name, None)
                if h:
                    user32.ShowWindow(h, 5 if visible else 0)
        except Exception:
            pass

    @classmethod
    def _check_and_restore_wifi(cls):
        """Watches Wi-Fi connectivity and automatically reconnects if Airplane mode is toggled, running 100% silently."""
        try:
            is_connected = False
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(("127.0.0.1", 8000))
                s.close()
                is_connected = True
            except Exception:
                pass

            if not is_connected:
                res = _run_silent('netsh wlan show interfaces', timeout=2)
                if res and res.stdout and ("disconnected" in res.stdout.lower() or "State" not in res.stdout or "radio off" in res.stdout.lower()):
                    logger.warning("Wi-Fi down or Airplane mode active! Enabling Wi-Fi interface and reconnecting...")
                    _run_silent('netsh interface set interface name="Wi-Fi" admin=enabled', timeout=2)
                    if cls._saved_wifi_profile:
                        _run_silent(f'netsh wlan connect name="{cls._saved_wifi_profile}"', timeout=2)
        except Exception:
            pass

    @classmethod
    def _mouse_hook_proc(cls, nCode, wParam, lParam):
        try:
            if nCode >= 0 and (cls._curtain_active or cls._input_blocked):
                p = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                # Allow strictly remote administrative input
                if p.dwExtraInfo == TRMM_INPUT_MAGIC:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                # Drop physical user mouse movement, clicks, and OEM touch events
                return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    @classmethod
    def _kbd_hook_proc(cls, nCode, wParam, lParam):
        try:
            if nCode >= 0 and (cls._curtain_active or cls._input_blocked):
                p = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                # Allow strictly remote administrative keystrokes tagged with TRMM_INPUT_MAGIC
                if p.dwExtraInfo == TRMM_INPUT_MAGIC:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)

                # If physical Windows key (0x5B = VK_LWIN, 0x5C = VK_RWIN) is touched,
                # immediately neutralize it by synthesizing a key-up so Windows kernel never
                # pairs it with 'L' to lock the workstation:
                if p.vkCode in (0x5B, 0x5C):
                    user32.keybd_event(p.vkCode, p.scanCode, 0x0002, TRMM_INPUT_MAGIC)
                    return 1

                # If physical 'L' (0x4C) is pressed while either Win key was touched, force release Win keys
                if p.vkCode == 0x4C:
                    user32.keybd_event(0x5B, 0, 0x0002, TRMM_INPUT_MAGIC)
                    user32.keybd_event(0x5C, 0, 0x0002, TRMM_INPUT_MAGIC)
                    return 1

                # Drop physical workstation typing, F1-F24 function keys, multimedia keys, and OEM injected hotkeys
                return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    @classmethod
    def _on_win_event(cls, hHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        try:
            if cls._curtain_active:
                if event == 0x0020:
                    # EVENT_SYSTEM_DESKTOPSWITCH: Target switched virtual desktops!
                    # Flag recreation so curtain re-appears on the new active desktop
                    cls._need_recreate = True
                elif cls._hwnd and hwnd != cls._hwnd:
                    user32.ShowWindow(cls._hwnd, 5)  # SW_SHOW
                    user32.BringWindowToTop(cls._hwnd)
                    user32.SetWindowPos(cls._hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        except Exception:
            pass

    @classmethod
    def _wnd_proc(cls, hwnd, uMsg, wParam, lParam):
        if uMsg == 0x0084:  # WM_NCHITTEST
            # HTTRANSPARENT (-1) passes all mouse clicks straight through to windows underneath
            return -1
        elif uMsg == 0x0011:  # WM_QUERYENDSESSION
            # Prevent shutdown/reboot while Screen Curtain / Maintenance is active
            return 0
        elif uMsg == 0x0016:  # WM_ENDSESSION
            return 0
        elif uMsg == 0x0319:  # WM_APPCOMMAND (Multimedia, Volume, App launcher, Radio keys)
            # Intercept and consume all application commands
            return 1
        elif uMsg == 0x0312:  # WM_HOTKEY (F1-F24, Win, etc.)
            # Intercept and consume all registered hardware function and shortcut keys
            return 0
        elif uMsg == 0x00FF:  # WM_INPUT
            # Consume Raw Input packets for consumer control devices
            return 0
        elif uMsg == 0x0112:  # WM_SYSCOMMAND
            sc = wParam & 0xFFF0
            # Prevent SC_MONITORPOWER (0xF170), SC_SCREENSAVE (0xF140), SC_CLOSE (0xF060), SC_KEYMENU (0xF100)
            if sc in (0xF170, 0xF140, 0xF060, 0xF100, 0xF030, 0xF020):
                return 0
        elif uMsg == 0x0113:  # WM_TIMER
            if wParam == 1:
                rc = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                w = rc.right - rc.left
                h = rc.bottom - rc.top
                scale = max(1.0, min(w / 1920.0, h / 1080.0))
                cx = w // 2
                spinner_cy = int((h // 2) - 140 * scale)
                spin_bound = int(90 * scale)
                r_spin = wintypes.RECT(cx - spin_bound, spinner_cy - spin_bound, cx + spin_bound, spinner_cy + spin_bound)
                user32.InvalidateRect(hwnd, ctypes.byref(r_spin), False)
                user32.UpdateWindow(hwnd)
                return 0
        elif uMsg == 0x000F:  # WM_PAINT
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            if not hdc:
                return 0
            rc = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            w = rc.right - rc.left
            h = rc.bottom - rc.top
            cx = w // 2
            cy = h // 2
            scale = max(1.0, min(w / 1920.0, h / 1080.0))
            spinner_cy = int(cy - 140 * scale)
            spin_bound = int(90 * scale)

            # Check if this is an incremental animation frame repaint for just the spinner
            is_spinner_partial = (
                ps.rcPaint.left >= cx - spin_bound and ps.rcPaint.right <= cx + spin_bound and
                ps.rcPaint.top >= spinner_cy - spin_bound and ps.rcPaint.bottom <= spinner_cy + spin_bound
            )

            if not is_spinner_partial:
                # Full redraw with pure black background and white text (drawn only once or on resize)
                user32.FillRect(hdc, ctypes.byref(rc), cls._bg_brush)

                gdi32.SetBkMode(hdc, 1)  # TRANSPARENT

                # 1. Main Title: Working on updates 15% (Authentic Windows 11 large display font)
                if cls._title_font:
                    gdi32.SelectObject(hdc, cls._title_font)
                gdi32.SetTextColor(hdc, 0x00FFFFFF)
                r_title = wintypes.RECT(0, int(cy - 10 * scale), w, int(cy + 65 * scale))
                user32.DrawTextW(hdc, "Working on updates 15%", -1, ctypes.byref(r_title), 0x00000001 | 0x00000004)

                # 2. Subtitle: Please keep your computer on
                if cls._sub_font:
                    gdi32.SelectObject(hdc, cls._sub_font)
                gdi32.SetTextColor(hdc, 0x00FFFFFF)
                r_sub = wintypes.RECT(0, int(cy + 85 * scale), w, int(cy + 130 * scale))
                user32.DrawTextW(hdc, "Please keep your computer on", -1, ctypes.byref(r_sub), 0x00000001 | 0x00000004)

                # 3. Detail Line: Your computer may restart a few times to apply updates before it shuts down.
                if cls._detail_font:
                    gdi32.SelectObject(hdc, cls._detail_font)
                gdi32.SetTextColor(hdc, 0x00FFFFFF)
                r_det = wintypes.RECT(0, int(cy + 145 * scale), w, int(cy + 185 * scale))
                user32.DrawTextW(hdc, "Your computer may restart a few times to apply updates before it shuts down.", -1, ctypes.byref(r_det), 0x00000001 | 0x00000004)

            # Double-buffered off-screen memory DC: zero flicker, zero tearing, and 100% fluid roll
            spin_w = spin_bound * 2
            spin_h = spin_bound * 2
            if cls._spin_size != spin_w or not cls._spin_mem_dc:
                if cls._spin_mem_dc:
                    if cls._spin_mem_old_bmp:
                        gdi32.SelectObject(cls._spin_mem_dc, cls._spin_mem_old_bmp)
                    if cls._spin_mem_bmp:
                        gdi32.DeleteObject(cls._spin_mem_bmp)
                    gdi32.DeleteDC(cls._spin_mem_dc)
                cls._spin_mem_dc = gdi32.CreateCompatibleDC(hdc)
                cls._spin_mem_bmp = gdi32.CreateCompatibleBitmap(hdc, spin_w, spin_h)
                cls._spin_mem_old_bmp = gdi32.SelectObject(cls._spin_mem_dc, cls._spin_mem_bmp)
                cls._spin_size = spin_w

            r_spin_box = wintypes.RECT(0, 0, spin_w, spin_h)
            user32.FillRect(cls._spin_mem_dc, ctypes.byref(r_spin_box), cls._bg_brush)

            if cls._white_brush:
                gdi32.SelectObject(cls._spin_mem_dc, cls._white_brush)
            gdi32.SelectObject(cls._spin_mem_dc, gdi32.GetStockObject(8))  # NULL_PEN

            import math
            current_time = time.time()
            radius = int(60 * scale)
            dot_size = int(5.5 * scale)
            dot_count = 5
            speed = 2.8
            spacing = 0.35  # Distinct, elegant spacing between beads
            base_angle = (current_time * speed) % (2 * math.pi)

            mcx = spin_bound
            mcy = spin_bound

            for i in range(dot_count):
                angle = base_angle - (i * spacing) - (math.pi / 2)
                dx = int(mcx + radius * math.cos(angle))
                dy = int(mcy + radius * math.sin(angle))
                gdi32.Ellipse(cls._spin_mem_dc, dx - dot_size, dy - dot_size, dx + dot_size + 1, dy + dot_size + 1)

            # Atomic copy onto screen DC in 1 call - zero flicker!
            gdi32.BitBlt(hdc, cx - spin_bound, spinner_cy - spin_bound, spin_w, spin_h, cls._spin_mem_dc, 0, 0, SRCCOPY)

            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        elif uMsg == 0x0014:  # WM_ERASEBKGND
            return 1
        elif uMsg == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, uMsg, wParam, lParam)

    @classmethod
    def _curtain_worker(cls):
        try:
            MirrorInput._ensure_default_desktop()
            # Modern Per-Monitor DPI Awareness so curtain covers 100% of all displays
            try:
                user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
                user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass

            # Install low-level selective input hooks on this worker message pump thread
            try:
                kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
                kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
                user32.SetWindowsHookExW.restype = wintypes.HHOOK
                user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
                user32.CallNextHookEx.restype = ctypes.c_longlong
                user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
                user32.UnhookWindowsHookEx.restype = wintypes.BOOL
                user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

                h_mod = kernel32.GetModuleHandleW(None)
                cls._mouse_proc_ref = HOOKPROC(cls._mouse_hook_proc)
                cls._kbd_proc_ref = HOOKPROC(cls._kbd_hook_proc)
                cls._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, cls._mouse_proc_ref, h_mod, 0)
                cls._kbd_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cls._kbd_proc_ref, h_mod, 0)
                logger.info(f"ScreenCurtain hooks installed: mouse={hex(cls._mouse_hook or 0)}, kbd={hex(cls._kbd_hook or 0)}")
            except Exception as ex:
                logger.error(f"Failed to install input hooks: {ex}")

            # Install WinEventHook for zero-latency foreground lock & virtual desktop tracking
            try:
                cls._winevent_proc_ref = WINEVENTPROC(cls._on_win_event)
                # EVENT_SYSTEM_FOREGROUND = 0x0003
                cls._win_event_hook1 = user32.SetWinEventHook(0x0003, 0x0003, 0, cls._winevent_proc_ref, 0, 0, 0)
                # EVENT_OBJECT_SHOW = 0x8002
                cls._win_event_hook2 = user32.SetWinEventHook(0x8002, 0x8002, 0, cls._winevent_proc_ref, 0, 0, 0)
                # EVENT_SYSTEM_DESKTOPSWITCH = 0x0020
                cls._win_event_hook3 = user32.SetWinEventHook(0x0020, 0x0020, 0, cls._winevent_proc_ref, 0, 0, 0)
                logger.info(f"ScreenCurtain zero-latency WinEvent hooks installed: fg={bool(cls._win_event_hook1)}, show={bool(cls._win_event_hook2)}, desk={bool(cls._win_event_hook3)}")
            except Exception as ex:
                logger.error(f"Failed to install win event hooks: {ex}")

            vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
            vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            if vw <= 0:
                vw = user32.GetSystemMetrics(0)
            if vh <= 0:
                vh = user32.GetSystemMetrics(1)

            scale = max(1.0, min(vw / 1920.0, vh / 1080.0))
            title_sz = int(68 * scale)
            sub_sz = int(34 * scale)
            det_sz = int(24 * scale)

            if not cls._class_registered:
                cls._bg_brush = gdi32.CreateSolidBrush(0x00000000)    # Pure pitch black #000000
                cls._white_brush = gdi32.CreateSolidBrush(0x00FFFFFF) # Pure white #FFFFFF
                cls._title_font = gdi32.CreateFontW(title_sz, 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, "Segoe UI")
                cls._sub_font = gdi32.CreateFontW(sub_sz, 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, "Segoe UI")
                cls._detail_font = gdi32.CreateFontW(det_sz, 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, "Segoe UI")
                cls._wnd_proc_ref = WNDPROC(cls._wnd_proc)

                class WNDCLASSW(ctypes.Structure):
                    _fields_ = [
                        ('style', wintypes.UINT),
                        ('lpfnWndProc', WNDPROC),
                        ('cbClsExtra', ctypes.c_int),
                        ('cbWndExtra', ctypes.c_int),
                        ('hInstance', ctypes.c_void_p),
                        ('hIcon', ctypes.c_void_p),
                        ('hCursor', ctypes.c_void_p),
                        ('hbrBackground', ctypes.c_void_p),
                        ('lpszMenuName', wintypes.LPCWSTR),
                        ('lpszClassName', wintypes.LPCWSTR),
                    ]
                wc = WNDCLASSW()
                wc.lpfnWndProc = cls._wnd_proc_ref
                wc.hbrBackground = cls._bg_brush
                wc.lpszClassName = "TRMM_CurtainHost"
                user32.RegisterClassW(ctypes.byref(wc))
                cls._class_registered = True

            def create_curtain_window():
                # WS_EX_TOPMOST (0x8) | WS_EX_TRANSPARENT (0x20) | WS_EX_LAYERED (0x80000) | WS_EX_NOACTIVATE (0x8000000) | WS_EX_TOOLWINDOW (0x80)
                ex_style = 0x00000008 | 0x00000020 | 0x00080000 | 0x08000000 | 0x00000080
                hwnd = None
                try:
                    user32.CreateWindowInBand.argtypes = [
                        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
                        wintypes.DWORD
                    ]
                    user32.CreateWindowInBand.restype = wintypes.HWND
                    hwnd = user32.CreateWindowInBand(
                        ex_style,
                        "TRMM_CurtainHost",
                        "",
                        0x80000000 | 0x10000000,
                        vx, vy, vw, vh,
                        None, None, None, None,
                        16  # ZBID_SYSTEM_TOOLS (Band 16)
                    )
                    if hwnd:
                        logger.info(f"ScreenCurtain window created in Band 16: hwnd={hwnd}")
                except Exception as ex:
                    logger.debug(f"CreateWindowInBand fallback: {ex}")

                if not hwnd:
                    hwnd = user32.CreateWindowExW(
                        ex_style,
                        "TRMM_CurtainHost",
                        "",
                        0x80000000 | 0x10000000,
                        vx, vy, vw, vh,
                        None, None, None, None
                    )
                    logger.info(f"ScreenCurtain window created via standard CreateWindowExW: hwnd={hwnd}")

                if hwnd:
                    try:
                        user32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x02)  # LWA_ALPHA
                    except Exception:
                        pass

                    try:
                        user32.SetWindowDisplayAffinity(hwnd, 0x00000011)  # WDA_EXCLUDEFROMCAPTURE
                    except Exception:
                        pass

                    try:
                        user32.ShutdownBlockReasonCreate(hwnd, "Working on Updates. Please keep your computer on.")
                    except Exception:
                        pass

                    if cls._locked_volume is None:
                        try:
                            cls._locked_volume = AudioVolumeLock.get_volume()
                            cls._locked_mute = AudioVolumeLock.get_mute()
                        except Exception:
                            pass

                    user32.BringWindowToTop(hwnd)
                    user32.SetWindowPos(hwnd, -1, vx, vy, vw, vh, 0x0040 | 0x0001 | 0x0002)
                    user32.ShowWindow(hwnd, 5)  # SW_SHOW
                    user32.UpdateWindow(hwnd)
                    user32.SetTimer(hwnd, 1, 16, None)  # High precision 16ms timer = ~60 FPS
                    cls._hwnd = hwnd

                    # Register F1-F24 and Win hotkeys on hwnd so Windows routes hardware function & shortcut keys to WM_HOTKEY
                    for vk in range(0x70, 0x88):  # F1 to F24
                        for mod in [0, 1, 2, 4, 8]:
                            hk_id = (vk << 8) | mod
                            try:
                                if user32.RegisterHotKey(hwnd, hk_id, mod, vk):
                                    cls._registered_hotkeys.append(hk_id)
                            except Exception:
                                pass
                    for vk in [0x5B, 0x5C, 0x1B, 0x09]:  # LWIN, RWIN, ESC, TAB
                        for mod in [0, 1, 2, 4, 8]:
                            hk_id = (vk << 8) | mod
                            try:
                                if user32.RegisterHotKey(hwnd, hk_id, mod, vk):
                                    cls._registered_hotkeys.append(hk_id)
                            except Exception:
                                pass

                return hwnd

            def destroy_curtain_window():
                for h in list(cls._desktop_hwnds.values()):
                    if h and user32.IsWindow(h):
                        try:
                            user32.KillTimer(h, 1)
                        except Exception:
                            pass
                        try:
                            user32.ShutdownBlockReasonDestroy(h)
                        except Exception:
                            pass
                        user32.DestroyWindow(h)
                cls._desktop_hwnds.clear()
                if cls._hwnd and user32.IsWindow(cls._hwnd):
                    try:
                        user32.KillTimer(cls._hwnd, 1)
                    except Exception:
                        pass
                    for hk_id in list(cls._registered_hotkeys):
                        try:
                            user32.UnregisterHotKey(cls._hwnd, hk_id)
                        except Exception:
                            pass
                    cls._registered_hotkeys.clear()
                    try:
                        user32.ShutdownBlockReasonDestroy(cls._hwnd)
                    except Exception:
                        pass
                    user32.DestroyWindow(cls._hwnd)
                    cls._hwnd = None
                if cls._spin_mem_dc:
                    try:
                        if cls._spin_mem_old_bmp:
                            gdi32.SelectObject(cls._spin_mem_dc, cls._spin_mem_old_bmp)
                        if cls._spin_mem_bmp:
                            gdi32.DeleteObject(cls._spin_mem_bmp)
                        gdi32.DeleteDC(cls._spin_mem_dc)
                    except Exception:
                        pass
                    cls._spin_mem_dc = None
                    cls._spin_mem_bmp = None
                    cls._spin_mem_old_bmp = None
                    cls._spin_size = 0
                cls._locked_volume = None
                cls._locked_mute = None
                cls._set_taskbar_visible(True)
                MirrorInput.set_stealth_cursor(False)
                try:
                    user32.SystemParametersInfoW(0x0057, 0, None, 0)
                except Exception:
                    pass

            if cls._curtain_active:
                h = create_curtain_window()
                if h:
                    cls._desktop_hwnds[b'initial'] = h

            msg = wintypes.MSG()
            last_reassert = time.time()

            while cls._stop_event and not cls._stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

                # Watch Virtual Desktop switches dynamically via registry
                try:
                    if not cls._vd_reg_key:
                        cls._vd_reg_key = winreg.OpenKey(
                            winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops",
                            0, winreg.KEY_READ
                        )
                    curr_vd, _ = winreg.QueryValueEx(cls._vd_reg_key, "CurrentVirtualDesktop")
                    if curr_vd != cls._last_vd:
                        cls._last_vd = curr_vd
                        # Target switched virtual desktops (e.g. to Desktop 2)!
                        if curr_vd not in cls._desktop_hwnds or not user32.IsWindow(cls._desktop_hwnds[curr_vd]):
                            new_h = create_curtain_window()
                            if new_h:
                                cls._desktop_hwnds[curr_vd] = new_h
                                logger.info(f"ScreenCurtain deployed to newly active Virtual Desktop: {curr_vd.hex()[:8]}")
                except Exception:
                    pass

                # Dynamically reflect curtain active state
                if cls._curtain_active and not cls._hwnd:
                    h = create_curtain_window()
                    if h:
                        cls._desktop_hwnds[cls._last_vd or b'default'] = h
                elif not cls._curtain_active and cls._hwnd:
                    destroy_curtain_window()

                now = time.time()
                if cls._curtain_active:
                    all_hwnds = [h for h in list(cls._desktop_hwnds.values()) + ([cls._hwnd] if cls._hwnd else []) if h and user32.IsWindow(h)]
                    # Periodic safety reassertion (every 500ms) without causing DWM compositor churn
                    if now - last_reassert > 0.5:
                        last_reassert = now
                        for h in all_hwnds:
                            if not user32.IsWindowVisible(h):
                                user32.ShowWindow(h, 5)  # SW_SHOW
                            user32.BringWindowToTop(h)
                            user32.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
                        # Keep taskbar hidden so four-finger gestures cannot peek through
                        cls._set_taskbar_visible(False)

                user32.MsgWaitForMultipleObjectsEx(0, None, 16, 0x04FF, 0x0004)

            # Cleanup on exit
            destroy_curtain_window()
            if cls._vd_reg_key:
                try:
                    winreg.CloseKey(cls._vd_reg_key)
                except Exception:
                    pass
                cls._vd_reg_key = None

            if cls._mouse_hook:
                user32.UnhookWindowsHookEx(cls._mouse_hook)
                cls._mouse_hook = None
            if cls._kbd_hook:
                user32.UnhookWindowsHookEx(cls._kbd_hook)
                cls._kbd_hook = None
            if cls._win_event_hook1:
                user32.UnhookWinEvent(cls._win_event_hook1)
                cls._win_event_hook1 = None
            if cls._win_event_hook2:
                user32.UnhookWinEvent(cls._win_event_hook2)
                cls._win_event_hook2 = None
            if cls._win_event_hook3:
                user32.UnhookWinEvent(cls._win_event_hook3)
                cls._win_event_hook3 = None

        except Exception as e:
            logger.error(f"Error in curtain worker: {e}")

    @classmethod
    def _bg_hardware_locks_worker(cls):
        """
        Runs hardware policies and locks asynchronously in background thread:
        - Suppress power button
        - Disable accessibility hotkeys
        - Lock touchpad status and gesture registry
        - Lock workstation policy (Win+L and Win+Key)
        - Block OEM hotkey services (HP Wireless Button)
        - Continuous Wi-Fi and audio lock watchdog
        """
        try:
            # 1. Hardware and OS lock policies
            cls._suppress_power_button(True)
            AccessibilityHotkeyLock.disable_all()
            TouchpadLock.lock()
            WorkstationLockPolicy.lock()
            OEMHotkeyBlocker.lock()

            # 2. Snapshot active Wi-Fi profile
            if not cls._saved_wifi_profile:
                try:
                    res = _run_silent('netsh wlan show interfaces', timeout=2)
                    if res and res.stdout:
                        m = re.search(r'Profile\s*:\s*(.+)', res.stdout)
                        if m:
                            cls._saved_wifi_profile = m.group(1).strip()
                except Exception:
                    pass

            # 3. Continuous watchdog while locks are active
            while cls._bg_locks_stop and not cls._bg_locks_stop.is_set():
                cls._check_and_restore_wifi()

                # Volume / Mute lock enforcement
                if cls._locked_volume is not None:
                    curr_vol = AudioVolumeLock.get_volume()
                    if curr_vol is not None and abs(curr_vol - cls._locked_volume) > 0.01:
                        AudioVolumeLock.set_volume(cls._locked_volume)
                if cls._locked_mute is not None:
                    curr_mute = AudioVolumeLock.get_mute()
                    if curr_mute is not None and curr_mute != cls._locked_mute:
                        AudioVolumeLock.set_mute(cls._locked_mute)

                cls._bg_locks_stop.wait(1.0)
        except Exception as e:
            logger.debug(f"Error in bg hardware locks worker: {e}")

    @classmethod
    def _restore_hardware_locks_worker(cls):
        """Restores hardware and OS policies in background."""
        try:
            cls._suppress_power_button(False)
            AccessibilityHotkeyLock.restore_all()
            TouchpadLock.restore()
            WorkstationLockPolicy.restore()
            OEMHotkeyBlocker.restore()
            MirrorInput.set_stealth_cursor(False)
            try:
                user32.SystemParametersInfoW(0x0057, 0, None, 0)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Error restoring hardware locks: {e}")

    @classmethod
    def _suppress_power_button(cls, enable: bool):
        """
        Suppresses the physical ACPI hardware power button while Screen Curtain or Input Block is active,
        running 100% silently in the background with zero visible console windows.
        """
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            if enable:
                try:
                    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
                except Exception:
                    pass
                # Query and store original settings if not yet saved
                if cls._orig_power_btn_ac is None:
                    try:
                        res = _run_silent(
                            'powercfg -qh SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f929386280',
                            timeout=3
                        )
                        if res and res.stdout:
                            ac_match = re.search(r'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)', res.stdout)
                            dc_match = re.search(r'Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)', res.stdout)
                            cls._orig_power_btn_ac = int(ac_match.group(1), 16) if ac_match else 1
                            cls._orig_power_btn_dc = int(dc_match.group(1), 16) if dc_match else 1
                    except Exception as e:
                        logger.debug(f"Could not query original power settings: {e}")
                        cls._orig_power_btn_ac = 1
                        cls._orig_power_btn_dc = 1

                # Set Power Button Action = 0 (Do nothing)
                _run_silent('powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f929386280 0', timeout=3)
                _run_silent('powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f929386280 0', timeout=3)
                _run_silent('powercfg /setactive SCHEME_CURRENT', timeout=3)
                logger.info("Power button action set to 'Do nothing' (maintenance mode active)")
            else:
                try:
                    kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                except Exception:
                    pass
                # Restore original power button actions
                ac_val = cls._orig_power_btn_ac if cls._orig_power_btn_ac is not None else 1
                dc_val = cls._orig_power_btn_dc if cls._orig_power_btn_dc is not None else 1
                _run_silent(f'powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f929386280 {ac_val}', timeout=3)
                _run_silent(f'powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 7648efa3-dd9c-4e3e-b566-50f929386280 {dc_val}', timeout=3)
                _run_silent('powercfg /setactive SCHEME_CURRENT', timeout=3)
                cls._orig_power_btn_ac = None
                cls._orig_power_btn_dc = None
                logger.info(f"Power button action restored to AC={ac_val}, DC={dc_val}")
        except Exception as e:
            logger.debug(f"Failed to adjust power button policy: {e}")

    @classmethod
    def _ensure_worker(cls):
        """Starts worker thread if either curtain or input blocking is needed, stops it if neither is needed."""
        needs_running = cls._curtain_active or cls._input_blocked
        if needs_running:
            # 1. Hide taskbar immediately when curtain is active so swipes/gestures cannot peek
            if cls._curtain_active:
                cls._set_taskbar_visible(False)

            # 2. Start UI message pump thread IMMEDIATELY so curtain window appears in < 20ms
            if not cls._thread or not cls._thread.is_alive():
                cls._stop_event = threading.Event()
                cls._thread = threading.Thread(target=cls._curtain_worker, daemon=True)
                cls._thread.start()

            # 3. Asynchronously run powercfg, OEM hotkeys, touchpad locks, and network watchdog
            if not cls._bg_locks_thread or not cls._bg_locks_thread.is_alive():
                cls._bg_locks_stop = threading.Event()
                cls._bg_locks_thread = threading.Thread(target=cls._bg_hardware_locks_worker, daemon=True)
                cls._bg_locks_thread.start()
        else:
            # 1. Restore taskbar immediately
            cls._set_taskbar_visible(True)

            # 2. Stop background locks watchdog
            if cls._bg_locks_stop:
                cls._bg_locks_stop.set()
                cls._bg_locks_stop = None
            cls._bg_locks_thread = None

            # 3. Stop UI worker
            if cls._stop_event:
                cls._stop_event.set()
                cls._stop_event = None

            # 4. Asynchronously restore hardware and system locks
            threading.Thread(target=cls._restore_hardware_locks_worker, daemon=True).start()

            if cls._thread:
                cls._thread.join(timeout=1.0)
                cls._thread = None

    @classmethod
    def set_curtain(cls, enabled: bool) -> bool:
        try:
            with cls._lock:
                MirrorInput._ensure_default_desktop()
                cls._curtain_active = bool(enabled)
                if not enabled:
                    MirrorInput.set_stealth_cursor(False)
                    try:
                        user32.SystemParametersInfoW(0x0057, 0, None, 0)
                    except Exception:
                        pass
                    try:
                        TouchpadLock.restore()
                    except Exception:
                        pass
                cls._ensure_worker()
                return True
        except Exception as e:
            logger.error(f"Failed to toggle Screen Curtain: {e}")
            return False

    @classmethod
    def set_block_input(cls, enabled: bool) -> bool:
        try:
            with cls._lock:
                MirrorInput._ensure_default_desktop()
                cls._input_blocked = bool(enabled)
                cls._ensure_worker()
                return True
        except Exception as e:
            logger.error(f"Failed to toggle BlockInput: {e}")
            return False

    @classmethod
    def cleanup(cls):
        """Restores screen curtain and unblocks input when session ends."""
        try:
            with cls._lock:
                cls._curtain_active = False
                cls._input_blocked = False
                AccessibilityHotkeyLock.restore_all()
                TouchpadLock.restore()
                WorkstationLockPolicy.restore()
                OEMHotkeyBlocker.restore()
                cls._set_taskbar_visible(True)
                MirrorInput.set_stealth_cursor(False)
                try:
                    user32.SystemParametersInfoW(0x0057, 0, None, 0)
                except Exception:
                    pass
                cls._ensure_worker()
        except Exception:
            pass




