"""
Window-by-Window Compositor for HVNC.
Captures visible windows on the hidden Win32 desktop and composites them onto a virtual canvas.
"""

import os
import time
import ctypes
from ctypes import wintypes
import io
import struct
import logging
from typing import List, Tuple, Optional, Dict
from PIL import Image, ImageDraw, ImageFont

from .desktop import HiddenDesktop, user32, kernel32, gdi32, BITMAPINFO, BITMAPINFOHEADER

logger = logging.getLogger("hvnc.compositor")

# PrintWindow flags
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

# WM_PRINT flags
WM_PRINT = 0x0317
WM_PRINTCLIENT = 0x0318
PRF_CHECKVISIBLE = 0x00000001
PRF_NONCLIENT = 0x00000002
PRF_CLIENT = 0x00000004
PRF_ERASEBKGND = 0x00000008
PRF_CHILDREN = 0x00000010
PRF_OWNED = 0x00000020

# GDI Prototypes
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL

user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC

user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL

SMTO_ABORTIFHUNG = 0x0002
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_ulong),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM

# CreateDIBSection configuration using c_void_p for seamless cross-module struct compatibility
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

shell32 = ctypes.windll.shell32

class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ('hIcon', wintypes.HICON),
        ('iIcon', ctypes.c_int),
        ('dwAttributes', wintypes.DWORD),
        ('szDisplayName', wintypes.WCHAR * 260),
        ('szTypeName', wintypes.WCHAR * 80),
    ]

user32.DrawIconEx.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HICON,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.HBRUSH,
    wintypes.UINT,
]
user32.DrawIconEx.restype = wintypes.BOOL

user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL

# TrueType font caching
_font_cache: Dict[str, ImageFont.FreeTypeFont] = {}

def _get_font(size: int = 12, bold: bool = False, semibold: bool = False) -> ImageFont.ImageFont:
    key = f"{size}_{'b' if bold else ('sb' if semibold else 'r')}"
    if key in _font_cache:
        return _font_cache[key]

    font_file = "segoeuib.ttf" if bold else ("segoeuisb.ttf" if semibold else "segoeui.ttf")
    font_path = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", font_file)
    try:
        font = ImageFont.truetype(font_path, size)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", size)
        except Exception:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font

def draw_vector_wifi(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20, color: Tuple[int, int, int] = (241, 245, 249)):
    """Draws a clean Wi-Fi icon: 3 concentric arcs and a center dot."""
    cx, cy = x + size // 2, y + size - 3
    draw.arc([(cx - 10, cy - 16), (cx + 10, cy + 4)], start=220, end=320, fill=color, width=2)
    draw.arc([(cx - 6, cy - 11), (cx + 6, cy + 1)], start=220, end=320, fill=color, width=2)
    draw.arc([(cx - 3, cy - 6), (cx + 3, cy)], start=220, end=320, fill=color, width=2)
    draw.ellipse([(cx - 2, cy - 2), (cx + 2, cy + 2)], fill=color)

def draw_vector_speaker(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20, color: Tuple[int, int, int] = (241, 245, 249)):
    """Draws a clean speaker with sound waves."""
    draw.rectangle([(x + 2, y + 6), (x + 6, y + 14)], fill=color)
    draw.polygon([(x + 6, y + 6), (x + 11, y + 2), (x + 11, y + 18), (x + 6, y + 14)], fill=color)
    draw.arc([(x + 7, y + 4), (x + 15, y + 16)], start=300, end=60, fill=color, width=2)
    draw.arc([(x + 10, y + 1), (x + 20, y + 19)], start=305, end=55, fill=color, width=2)

def draw_vector_battery(draw: ImageDraw.ImageDraw, x: int, y: int, width: int = 24, height: int = 14, color: Tuple[int, int, int] = (241, 245, 249), fill_level: float = 0.9):
    """Draws a clean horizontal battery with green charge fill and terminal cap."""
    draw.rounded_rectangle([(x, y), (x + width - 3, y + height)], radius=3, outline=color, width=2)
    draw.rounded_rectangle([(x + width - 2, y + 3), (x + width, y + height - 3)], radius=1, fill=color)
    inner_w = int((width - 7) * fill_level)
    if inner_w > 0:
        draw.rounded_rectangle([(x + 2, y + 2), (x + 2 + inner_w, y + height - 2)], radius=2, fill=(34, 197, 94))

def draw_vector_bell(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 18, color: Tuple[int, int, int] = (148, 163, 184)):
    """Draws a clean notification bell."""
    cx = x + size // 2
    draw.arc([(cx - 6, y + 2), (cx + 6, y + 12)], start=180, end=0, fill=color, width=2)
    draw.polygon([(cx - 7, y + 7), (cx + 7, y + 7), (cx + 8, y + 13), (cx - 8, y + 13)], fill=color)
    draw.ellipse([(cx - 2, y + 14), (cx + 2, y + 17)], fill=color)

def draw_vector_chevron_up(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 14, color: Tuple[int, int, int] = (148, 163, 184)):
    """Draws a clean chevron ^."""
    cx = x + size // 2
    draw.line([(cx - 5, y + 9), (cx, y + 4)], fill=color, width=2)
    draw.line([(cx, y + 4), (cx + 5, y + 9)], fill=color, width=2)

def create_fallback_icon(icon_type: str, size: int = 64) -> Image.Image:
    """Creates a high-definition 64x64 Fluent-style RGBA icon."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = size / 48.0
    if icon_type == 'folder':
        draw.rounded_rectangle([(int(4*scale), int(10*scale)), (int(24*scale), int(20*scale))], radius=4, fill=(0, 120, 215))
        draw.rounded_rectangle([(int(4*scale), int(16*scale)), (int(44*scale), int(42*scale))], radius=5, fill=(255, 185, 0), outline=(218, 140, 0), width=2)
        draw.rectangle([(int(8*scale), int(22*scale)), (int(40*scale), int(38*scale))], fill=(255, 205, 50))
    elif icon_type == 'computer':
        draw.rounded_rectangle([(int(4*scale), int(6*scale)), (int(44*scale), int(34*scale))], radius=5, fill=(30, 41, 59), outline=(56, 189, 248), width=2)
        draw.rectangle([(int(8*scale), int(10*scale)), (int(40*scale), int(30*scale))], fill=(14, 116, 144))
        draw.rectangle([(int(21*scale), int(34*scale)), (int(27*scale), int(40*scale))], fill=(71, 85, 105))
        draw.rounded_rectangle([(int(14*scale), int(40*scale)), (int(34*scale), int(44*scale))], radius=3, fill=(100, 116, 139))
    elif icon_type == 'recycle':
        draw.rounded_rectangle([(int(10*scale), int(10*scale)), (int(38*scale), int(42*scale))], radius=5, fill=(15, 23, 42, 200), outline=(56, 189, 248), width=2)
        draw.line([(int(16*scale), int(16*scale)), (int(16*scale), int(36*scale))], fill=(56, 189, 248), width=2)
        draw.line([(int(24*scale), int(16*scale)), (int(24*scale), int(36*scale))], fill=(56, 189, 248), width=2)
        draw.line([(int(32*scale), int(16*scale)), (int(32*scale), int(36*scale))], fill=(56, 189, 248), width=2)
        draw.rounded_rectangle([(int(8*scale), int(6*scale)), (int(40*scale), int(10*scale))], radius=3, fill=(56, 189, 248))
    else:
        draw.rounded_rectangle([(int(8*scale), int(4*scale)), (int(40*scale), int(44*scale))], radius=4, fill=(248, 250, 252), outline=(148, 163, 184), width=2)
        draw.rectangle([(int(14*scale), int(12*scale)), (int(34*scale), int(16*scale))], fill=(56, 189, 248))
        draw.rectangle([(int(14*scale), int(20*scale)), (int(34*scale), int(23*scale))], fill=(148, 163, 184))
        draw.rectangle([(int(14*scale), int(27*scale)), (int(34*scale), int(30*scale))], fill=(148, 163, 184))
        draw.rectangle([(int(14*scale), int(34*scale)), (int(28*scale), int(37*scale))], fill=(148, 163, 184))
    return img

def get_file_icon(path: str, size: int = 64) -> Optional[Image.Image]:
    """Extracts a real Windows file/folder icon as 32-bit RGBA Image using SHGetFileInfoW."""
    try:
        shfi = SHFILEINFOW()
        res = shell32.SHGetFileInfoW(path, 0, ctypes.byref(shfi), ctypes.sizeof(shfi), 0x100)
        if not res or not shfi.hIcon:
            return None

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bmi = bytearray(40)
        struct.pack_into('<IiiHHIIIIII', bmi, 0, 40, size, -size, 1, 32, 0, size*size*4, 0, 0, 0, 0)
        p_bits = ctypes.c_void_p()
        h_bmp = gdi32.CreateDIBSection(hdc_mem, (ctypes.c_char * 40).from_buffer(bmi), 0, ctypes.byref(p_bits), None, 0)
        h_old = gdi32.SelectObject(hdc_mem, h_bmp)
        user32.DrawIconEx(hdc_mem, 0, 0, shfi.hIcon, size, size, 0, None, 0x0003)
        buf = (ctypes.c_byte * (size * size * 4)).from_address(p_bits.value)
        raw = bytes(buf)
        gdi32.SelectObject(hdc_mem, h_old)
        gdi32.DeleteObject(h_bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)
        user32.DestroyIcon(shfi.hIcon)
        return Image.frombuffer('RGBA', (size, size), raw, 'raw', 'BGRA', 0, 1).copy()
    except Exception:
        return None


def init_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


init_dpi_awareness()


GWL_STYLE = -16
WS_POPUP = 0x80000000
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long


def is_popup_window(hwnd: int) -> bool:
    try:
        return bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_POPUP)
    except Exception:
        return False


class WindowCompositor:
    """Renders all windows on the hidden desktop into a single composite frame with full window visibility."""

    def __init__(self, desktop: HiddenDesktop, width: Optional[int] = None, height: Optional[int] = None):
        self.desktop = desktop
        init_dpi_awareness()
        if width is None or height is None:
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            width = sw if sw > 0 else 1920
            height = sh if sh > 0 else 1080
        self.width = width
        self.height = height
        self.cursor_pos = (width // 2, height // 2)
        # Persistent reusable DIB cache: hwnd -> (hdc_mem, h_bmp, p_bits, w, h)
        self._window_buffers = {}
        # Desktop wallpaper & icon replication
        self._wallpaper_cached: Optional[Image.Image] = None
        self._wallpaper_size: Tuple[int, int] = (0, 0)
        self._desktop_icons_cache = []
        self._desktop_icons_time: float = 0.0
        self.desktop_icons = []
        self._icon_cache = {}
        self._last_frame_bytes: Optional[bytes] = None
        self._last_frame_time: float = 0.0

    def set_cursor_pos(self, x: int, y: int):
        self.cursor_pos = (max(0, min(self.width, x)), max(0, min(self.height, y)))

    def _get_wallpaper(self) -> Image.Image:
        """Retrieves and caches the target machine's actual active wallpaper."""
        if self._wallpaper_cached is not None and self._wallpaper_size == (self.width, self.height):
            return self._wallpaper_cached

        appdata = os.environ.get("APPDATA", "")
        windir = os.environ.get("WINDIR", "C:\\Windows")
        candidates = [
            os.path.join(appdata, "Microsoft", "Windows", "Themes", "TranscodedWallpaper"),
            os.path.join(appdata, "Microsoft", "Windows", "Themes", "CachedFiles"),
            os.path.join(windir, "Web", "Wallpaper", "Windows", "img0.jpg"),
            os.path.join(windir, "Web", "4K", "Wallpaper", "Windows", "img0_3840x2160.jpg"),
        ]

        wp_img = None
        for c in candidates:
            try:
                if os.path.isfile(c):
                    wp_img = Image.open(c).convert("RGB")
                    break
                elif os.path.isdir(c):
                    files = [f for f in os.listdir(c) if not f.startswith(".")]
                    if files:
                        wp_img = Image.open(os.path.join(c, files[0])).convert("RGB")
                        break
            except Exception:
                pass

        if wp_img:
            wp_img = wp_img.resize((self.width, self.height), Image.Resampling.BILINEAR)
        else:
            wp_img = Image.new("RGB", (self.width, self.height), color=(15, 23, 42))

        self._wallpaper_cached = wp_img
        self._wallpaper_size = (self.width, self.height)
        return self._wallpaper_cached

    def _get_desktop_icons(self, force_refresh: bool = False):
        """Enumerates user desktop items and computes layout positions."""
        if self.desktop_icons and not force_refresh:
            return self.desktop_icons

        now = time.time()

        userprofile = os.environ.get("USERPROFILE", "")
        public = os.environ.get("PUBLIC", "C:\\Users\\Public")
        desktop_paths = [
            os.path.join(userprofile, "Desktop"),
            os.path.join(public, "Desktop")
        ]

        raw_items = [
            ("Recycle Bin", "shell:RecycleBinFolder", False, "recycle"),
            ("This PC", "shell:MyComputerFolder", False, "computer"),
        ]

        for dp in desktop_paths:
            if os.path.exists(dp):
                try:
                    for name in os.listdir(dp):
                        if name.lower() == "desktop.ini":
                            continue
                        p = os.path.join(dp, name)
                        is_dir = os.path.isdir(p)
                        ext = os.path.splitext(name)[1].lower()
                        clean_name = os.path.splitext(name)[0][:18]
                        raw_items.append((clean_name, p, is_dir, ext))
                except Exception:
                    pass

        # Calculate grid positions with prominent 64x64 icons in 104x116 cells
        col_x = 24
        row_y = 24
        item_w = 104
        item_h = 116
        max_y = self.height - 54 - item_h

        icons_layout = []
        for name, path, is_dir, ext in raw_items[:48]:
            if row_y > max_y:
                row_y = 24
                col_x += item_w + 18
            bbox = (col_x, row_y, col_x + item_w, row_y + item_h)
            icons_layout.append((name, path, is_dir, ext, bbox))
            row_y += item_h

        self._desktop_icons_cache = icons_layout
        self._desktop_icons_time = now
        self.desktop_icons = icons_layout
        return icons_layout

    def _get_window_dc(self, hwnd: int, width: int, height: int):
        """Retrieves or creates a cached offscreen DIB section for the specific window."""
        if hwnd in self._window_buffers:
            hdc_mem, h_bmp, p_bits, cached_w, cached_h = self._window_buffers[hwnd]
            if cached_w == width and cached_h == height:
                return hdc_mem, p_bits
            # Window dimensions changed: release previous buffer
            self._release_window_dc(hwnd)

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        p_bits = ctypes.c_void_p()
        h_bmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(p_bits), None, 0)
        user32.ReleaseDC(0, hdc_screen)

        if not h_bmp or not p_bits:
            gdi32.DeleteDC(hdc_mem)
            return None, None

        gdi32.SelectObject(hdc_mem, h_bmp)
        self._window_buffers[hwnd] = (hdc_mem, h_bmp, p_bits, width, height)
        return hdc_mem, p_bits

    def _release_window_dc(self, hwnd: int):
        """Releases the cached GDI resources for a specific window."""
        if hwnd in self._window_buffers:
            hdc_mem, h_bmp, p_bits, w, h = self._window_buffers.pop(hwnd)
            try:
                gdi32.DeleteObject(h_bmp)
                gdi32.DeleteDC(hdc_mem)
            except Exception:
                pass

    def _cleanup_stale_buffers(self, active_hwnds: set):
        """Frees GDI buffers for windows that are closed or no longer visible."""
        stale = [h for h in self._window_buffers if h not in active_hwnds]
        for h in stale:
            self._release_window_dc(h)

    def capture_window_bitmap(self, hwnd: int, width: int, height: int) -> Optional[Image.Image]:
        """Captures an individual window using PrintWindow / WM_PRINT into a PIL Image."""
        if width <= 0 or height <= 0 or width > 4096 or height > 4096:
            return None

        hdc_mem, p_bits = self._get_window_dc(hwnd, width, height)
        if not hdc_mem or not p_bits:
            return None

        # Attempt PW_RENDERFULLCONTENT first (modern Windows 8.1 / 10 / 11)
        captured = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not captured:
            # Fallback to standard PrintWindow
            captured = user32.PrintWindow(hwnd, hdc_mem, 0)

        if not captured:
            # Fallback to WM_PRINT with safety timeout (max 35ms, aborts if hung)
            print_flags = PRF_CLIENT | PRF_NONCLIENT | PRF_CHILDREN | PRF_ERASEBKGND
            sm_res = ctypes.c_ulong()
            user32.SendMessageTimeoutW(hwnd, WM_PRINT, hdc_mem, print_flags, SMTO_ABORTIFHUNG, 35, ctypes.byref(sm_res))

        # Convert raw BGRX memory buffer directly to RGB PIL Image
        buf_size = width * height * 4
        raw_bytes = ctypes.string_at(p_bits.value, buf_size)

        try:
            img = Image.frombytes("RGB", (width, height), raw_bytes, "raw", "BGRX")
            return img
        except Exception as e:
            logger.debug(f"Image conversion failed for hwnd 0x{hwnd:X}: {e}")
            return None

    def render_frame(self, is_active: bool = False, target_width: int = 0, quality: Optional[int] = None, byte_budget: int = 0) -> bytes:
        """
        Renders the composed desktop with all open windows, corners, and taskbar.
        Returns JPEG encoded bytes. Supports dynamic resolution downscaling and quality tuning for low-latency streaming.
        """
        all_windows = self.desktop.enumerate_windows(include_minimized=True)

        # Accommodate larger windows if genuine bounds exceed current dimensions
        for w in all_windows:
            hwnd, title, class_name, (left, top, right, bottom), is_min = w if len(w) == 5 else (*w, False)
            if not is_min:
                if 0 < right <= 3840 and right > self.width + 12:
                    self.width = right
                if 0 < bottom <= 2160 and bottom > self.height + 12:
                    self.height = bottom

        # 1. Start with the target machine's actual wallpaper
        canvas = self._get_wallpaper().copy()
        draw = ImageDraw.Draw(canvas)

        # 2. Draw authentic, prominent 64x64 desktop icons on the wallpaper
        desktop_icons = self._get_desktop_icons()
        lbl_font = _get_font(13)
        icon_sz = 64
        item_w = 104

        for name, path, is_dir, ext, (bx, by, rx, ry) in desktop_icons:
            icon_img = self._icon_cache.get(path)
            if icon_img is None:
                if os.path.exists(path):
                    icon_img = get_file_icon(path, icon_sz)
                if not icon_img:
                    icon_type = 'folder' if is_dir else ('recycle' if ext == 'recycle' else ('computer' if ext == 'computer' else 'doc'))
                    icon_img = create_fallback_icon(icon_type, icon_sz)
                self._icon_cache[path] = icon_img

            # Paste 64x64 icon centered in 104px column
            icon_x = bx + (item_w - icon_sz) // 2
            icon_y = by + 6
            if icon_img.mode == 'RGBA':
                canvas.paste(icon_img, (icon_x, icon_y), icon_img)
            else:
                canvas.paste(icon_img, (icon_x, icon_y))

            # Word-wrapped label with shadow
            words = name.split()
            lines = []
            cur_line = ""
            for w in words:
                if not cur_line:
                    cur_line = w
                elif len(cur_line) + len(w) + 1 <= 15:
                    cur_line += " " + w
                else:
                    lines.append(cur_line)
                    cur_line = w
            if cur_line:
                lines.append(cur_line)
            if not lines:
                lines = [name[:15]]

            cur_ly = by + icon_sz + 10
            for line in lines[:2]:
                bbox = lbl_font.getbbox(line)
                tw = bbox[2] - bbox[0]
                tx = bx + max(0, (item_w - tw) // 2)
                # 8-direction black halo for maximum legibility on light or dark wallpapers
                for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
                    draw.text((tx + dx, cur_ly + dy), line, font=lbl_font, fill=(0, 0, 0))
                draw.text((tx, cur_ly), line, font=lbl_font, fill=(255, 255, 255))
                cur_ly += 17

        has_native_taskbar = False
        visible_to_render = []
        taskbar_apps = []

        for w in all_windows:
            hwnd, title, class_name, (left, top, right, bottom), is_min = w if len(w) == 5 else (*w, False)
            if class_name == "Shell_TrayWnd":
                has_native_taskbar = True

            is_popup = is_popup_window(hwnd)

            # Application tabs for virtual taskbar: include active and minimized windows, skip popups
            if class_name != "Shell_TrayWnd" and not is_popup:
                disp_title = (title or class_name or "Application")[:22]
                taskbar_apps.append((hwnd, disp_title, class_name, is_min))

            if is_min:
                continue

            w_dim = right - left
            h_dim = bottom - top

            if w_dim < 8 or h_dim < 8:
                continue

            if right < 0 or bottom < 0 or left >= self.width or top >= self.height:
                continue

            visible_to_render.append((hwnd, title, class_name, (left, top, right, bottom), w_dim, h_dim))

            # Full coverage check
            if left <= 0 and top <= 0 and right >= self.width and bottom >= self.height:
                break

            if len(visible_to_render) >= 8:
                break

        # Render windows bottom-to-top so frontmost window is on top
        tb_height = 54
        tb_top = self.height - tb_height

        for hwnd, title, class_name, (left, top, right, bottom), w_dim, h_dim in reversed(visible_to_render):
            win_img = self.capture_window_bitmap(hwnd, w_dim, h_dim)
            if not win_img:
                continue

            # Check if window is maximized (Windows 11 bounds are typically -8, -8 to sw+8, sh+8)
            is_maximized = (-16 <= left <= 0 and -16 <= top <= 0 and right >= self.width)
            paste_x = left
            paste_y = top

            if is_maximized:
                # Maximized: crop off the invisible aero margin (-8, -8) so titlebar buttons are flush at (0, 0)
                crop_x = abs(left) if left < 0 else 0
                crop_y = abs(top) if top < 0 else 0
                max_avail_h = self.height
                crop_w = min(win_img.width - crop_x, self.width)
                crop_h = min(win_img.height - crop_y, max_avail_h)
                if crop_w > 0 and crop_h > 0:
                    win_img = win_img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
                    paste_x = 0
                    paste_y = 0
                canvas.paste(win_img, (paste_x, paste_y))
            else:
                # Floating/Restored window: render all 4 corners with sleek subtle Fluent border and drop-shadow
                canvas.paste(win_img, (paste_x, paste_y))
                # Draw subtle modern accent outline on all 4 corners of floating window so edges are crystal clear
                draw.rectangle([(paste_x, paste_y), (paste_x + win_img.width - 1, paste_y + win_img.height - 1)], outline=(51, 65, 85), width=1)

        # Clean up offscreen DIB buffers for any windows that closed or became hidden
        active_set = {w[0] for w in visible_to_render}
        self._cleanup_stale_buffers(active_set)

        # Dynamic resolution downscaling if target_width requested and active canvas exceeds it
        if target_width > 0 and canvas.width > target_width:
            ratio = target_width / float(canvas.width)
            target_height = max(100, int(canvas.height * ratio))
            canvas = canvas.resize((target_width, target_height), Image.Resampling.BILINEAR)

        # Adaptive JPEG compression: quality 48 during active motion/drag (~35KB), 65 when idle (~95KB)
        final_quality = quality if quality is not None and quality > 0 else (48 if is_active else 65)

        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=final_quality, subsampling=2, optimize=False)
        frame_bytes = output.getvalue()
        self._last_frame_bytes = frame_bytes
        self._last_frame_time = time.time()
        return frame_bytes
