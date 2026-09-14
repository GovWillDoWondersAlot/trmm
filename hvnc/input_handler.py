"""
Input Handler for HVNC.
Translates remote web mouse and keyboard events into Win32 messages dispatched strictly to windows on the hidden desktop.
Maintains 100% target endpoint invisibility (NO hardware cursor movement, ZERO leakage to the physical display desktop).
Features:
- In-desktop recursive child hit testing (resolving leaf controls like DirectUIHWND in File Explorer, Chrome_RenderWidgetHostHWND)
- Accurate titlebar window controls (Minimize, Maximize, Restore, Close) across both maximized and floating windows
- Cross-thread input attachment (AttachThreadInput) enabling reliable focus, active window state, and selection
- Activation message signaling (WM_MOUSEACTIVATE, WM_SETCURSOR) ensuring File Explorer hover highlights and file clicks work seamlessly
- 60Hz smooth drag and wheel scrolling support
"""

import ctypes
from ctypes import wintypes
import time
import logging
from typing import Optional, Tuple, Callable, Dict, Any, List

from .desktop import HiddenDesktop, user32, kernel32

logger = logging.getLogger("hvnc.input")

# Window Messages
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_SETCURSOR = 0x0020
WM_MOUSEACTIVATE = 0x0021
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112

SC_CLOSE = 0xF060
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102

SW_RESTORE = 9
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_SHOW = 5

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

# Hit-test codes
WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTCAPTION = 2
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTCLOSE = 20
SMTO_ABORTIFHUNG = 0x0002

user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL

user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM

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

user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND

user32.SetActiveWindow.argtypes = [wintypes.HWND]
user32.SetActiveWindow.restype = wintypes.HWND

user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL

user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long

user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL


def get_wnd_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_wnd_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def is_popup_window(hwnd: int) -> bool:
    """Returns True if window has WS_POPUP style (e.g. Chrome 3-dots menus, context menus, dropdowns)."""
    if not hwnd:
        return False
    try:
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        return bool(style & WS_POPUP)
    except Exception:
        return False


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class UIA_POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


ole32 = ctypes.windll.ole32
ole32.IIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
ole32.IIDFromString.restype = ctypes.c_long


def uia_click_point(x: int, y: int) -> bool:
    """
    Directly invokes or selects UI elements via native Windows UI Automation COM API.
    Essential for WinUI 3 / XAML Island navigation (e.g. Windows 11 Task Manager sidebar).
    """
    try:
        ole32.CoInitialize(None)
        CLSID_CUIAutomation = GUID()
        ole32.IIDFromString("{FF48DBA4-60EF-4201-AA87-54103EEF594E}", ctypes.byref(CLSID_CUIAutomation))
        IID_IUnknown = GUID()
        ole32.IIDFromString("{00000000-0000-0000-C000-000000000046}", ctypes.byref(IID_IUnknown))

        pUIA = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_CUIAutomation),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_IUnknown),
            ctypes.byref(pUIA)
        )
        if hr != 0 or not pUIA.value:
            return False

        vtbl = ctypes.cast(pUIA, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        # Method 7: ElementFromPoint(POINT pt, IUIAutomationElement **element)
        ElementFromPoint_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            UIA_POINT,
            ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[7])

        pElem = ctypes.c_void_p()
        hr_el = ElementFromPoint_fn(pUIA, UIA_POINT(x, y), ctypes.byref(pElem))
        success = False

        if hr_el == 0 and pElem.value:
            elem_vtbl = ctypes.cast(pElem, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            get_CurrentName = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))(elem_vtbl[23])
            el_name = ctypes.c_wchar_p()
            get_CurrentName(pElem, ctypes.byref(el_name))
            logger.info(f"uia_click_point({x}, {y}): Found element '{el_name.value}'")

            # Method 16: GetCurrentPattern(int patternId, IUnknown **patternObject)
            GetCurrentPattern = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p)
            )(elem_vtbl[16])

            # Try SelectionItem (10010), Invoke (10000), Toggle (10002)
            for pat_id in (10010, 10000, 10002):
                pPat = ctypes.c_void_p()
                hr_p = GetCurrentPattern(pElem, pat_id, ctypes.byref(pPat))
                if hr_p == 0 and pPat.value:
                    pat_vtbl = ctypes.cast(pPat, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
                    # Method 3 is Select() / Invoke() / Toggle()
                    action_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(pat_vtbl[3])
                    res_action = action_fn(pPat)
                    logger.info(f"uia_click_point({x}, {y}): Invoked pattern {pat_id} on '{el_name.value}', result: {res_action}")
                    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(pat_vtbl[2])(pPat)
                    if res_action == 0:
                        success = True
                        break

            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(elem_vtbl[2])(pElem)

        # Release pUIA
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(pUIA)
        return success, f"{el_name.value} (pat={pat_id if success else 'none'})"
    except Exception as ex:
        return False, str(ex)


class VARIANT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("bstrVal", ctypes.c_void_p), ("lVal", ctypes.c_long)]
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("u", _U),
        ("decVal_extra", ctypes.c_ulonglong),
    ]


oleaut32 = ctypes.windll.oleaut32
oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
oleaut32.SysAllocString.restype = ctypes.c_void_p


def uia_select_tab_name(top_hwnd: int, name_str: str, target_hwnd: Optional[int] = None) -> bool:
    """
    Finds a UI element by Name (e.g. 'Performance', 'Processes', 'Services')
    under the specified window handle using UI Automation, and invokes its SelectionItem or Invoke pattern.
    Tries target_hwnd (e.g. DesktopWindowContentBridge) first for instant XAML traversal,
    then falls back to top_hwnd.
    """
    try:
        ole32.CoInitialize(None)
        CLSID_CUIAutomation = GUID()
        ole32.IIDFromString("{FF48DBA4-60EF-4201-AA87-54103EEF594E}", ctypes.byref(CLSID_CUIAutomation))
        IID_IUnknown = GUID()
        ole32.IIDFromString("{00000000-0000-0000-C000-000000000046}", ctypes.byref(IID_IUnknown))

        pUIA = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_CUIAutomation),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_IUnknown),
            ctypes.byref(pUIA)
        )
        if hr != 0 or not pUIA.value:
            return False

        vtbl = ctypes.cast(pUIA, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        # Method 6: ElementFromHandle(HWND hwnd, IUIAutomationElement **element)
        ElementFromHandle = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            wintypes.HWND,
            ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[6])

        # Method 23 on IUIAutomation: CreatePropertyCondition(int propertyId, VARIANT value, IUIAutomationCondition **newCondition)
        CreatePropertyCondition = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            VARIANT,
            ctypes.POINTER(ctypes.c_void_p)
        )(vtbl[23])

        var = VARIANT()
        var.vt = 8  # VT_BSTR
        var.u.bstrVal = oleaut32.SysAllocString(name_str)
        pCond = ctypes.c_void_p()
        CreatePropertyCondition(pUIA, 30005, var, ctypes.byref(pCond))  # UIA_NamePropertyId = 30005

        hwnds_to_try = []
        if target_hwnd and target_hwnd != top_hwnd:
            hwnds_to_try.append(target_hwnd)
        hwnds_to_try.append(top_hwnd)

        success = False
        for h in hwnds_to_try:
            pRoot = ctypes.c_void_p()
            hr_root = ElementFromHandle(pUIA, h, ctypes.byref(pRoot))
            if hr_root != 0 or not pRoot.value:
                continue

            root_vtbl = ctypes.cast(pRoot, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            FindFirst = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p)
            )(root_vtbl[5])

            pFound = ctypes.c_void_p()
            hr_find = FindFirst(pRoot, 4, pCond, ctypes.byref(pFound))  # 4 = TreeScope_Descendants

            if hr_find == 0 and pFound.value:
                found_vtbl = ctypes.cast(pFound, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
                GetCurrentPattern = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p)
                )(found_vtbl[16])

                for pat_id in (10010, 10000, 10002):  # SelectionItem (10010), Invoke (10000), Toggle (10002)
                    pPat = ctypes.c_void_p()
                    if GetCurrentPattern(pFound, pat_id, ctypes.byref(pPat)) == 0 and pPat.value:
                        pat_vtbl = ctypes.cast(pPat, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
                        action_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(pat_vtbl[3])
                        res_action = action_fn(pPat)
                        logger.info(f"uia_select_tab_name('{name_str}') on hwnd 0x{h:X}: Pattern {pat_id} action returned {res_action}")
                        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(pat_vtbl[2])(pPat)
                        if res_action == 0:
                            success = True
                            break

                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(found_vtbl[2])(pFound)

            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(root_vtbl[2])(pRoot)
            if success:
                break

        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(pUIA)
        return success
    except Exception as ex:
        logger.warning(f"uia_select_tab_name('{name_str}') failed: {ex}")
        return False


class InputHandler:
    """Dispatches mouse and keyboard events to windows on the hidden desktop."""

    def __init__(
        self,
        desktop: HiddenDesktop,
        compositor: Optional[Any] = None,
        spawner_callback: Optional[Callable[[str, Optional[str]], None]] = None
    ):
        self.desktop = desktop
        self.compositor = compositor
        self.spawner_callback = spawner_callback
        self.last_focused_hwnd: Optional[int] = None
        self._cached_windows = []
        self._last_enum_time = 0.0
        self.is_lbutton_down = False
        self.drag_target: Optional[int] = None
        self.titlebar_action_down = False
        self.dragged_icon_idx: Optional[int] = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.tm_active_tab = 0
        self._minimized_for_show_desktop = set()
        self.dragged_window_hwnd: Optional[int] = None
        self.window_drag_offset_x = 0
        self.window_drag_offset_y = 0

    def _handle_task_manager_navigation(self, top_hwnd: int, x: int, y: int, target_hwnd: Optional[int] = None) -> Optional[int]:
        """
        Translates physical sidebar clicks into reliable tab navigation for Windows 11 Task Manager.
        Combines UIAutomation, SysTabControl32 message notification, and synthetic Ctrl+Tab cycle commands.
        Returns target tab index if in sidebar, else None.
        """
        r = wintypes.RECT()
        user32.GetWindowRect(top_hwnd, ctypes.byref(r))
        x_rel = x - r.left
        y_rel = y - r.top

        # Sidebar navigation is located on the left 220px below top title/search bar
        if x_rel > 220 or y_rel < 130:
            return None

        # Determine target tab based on vertical position
        target_idx = 0
        if y_rel < 195:
            target_idx = 0  # Processes
        elif y_rel < 255:
            target_idx = 1  # Performance
        elif y_rel < 310:
            target_idx = 2  # App history
        elif y_rel < 365:
            target_idx = 3  # Startup apps
        elif y_rel < 420:
            target_idx = 4  # Users
        elif y_rel < 475:
            target_idx = 5  # Details
        elif y_rel < 540:
            target_idx = 6  # Services
        else:
            target_idx = 7  # Settings

        tab_names = [
            "Processes",
            "Performance",
            "App history",
            "Startup apps",
            "Users",
            "Details",
            "Services",
            "Settings",
        ]
        target_name = tab_names[target_idx]
        logger.info(f"Task Manager sidebar clicked: ({x}, {y}) -> tab {target_idx} ('{target_name}', active: {self.tm_active_tab})")

        # 1. UI Automation: Direct named element selection (Primary)
        if uia_select_tab_name(top_hwnd, target_name, target_hwnd=target_hwnd):
            logger.info(f"Successfully selected Task Manager tab '{target_name}' via UIA Name matching")
            self.tm_active_tab = target_idx
            return target_idx

        # Also try point hit-test
        uia_click_point(x, y)

        # 2. Attach thread input and focus sidebar bridge
        bridge_hwnd = None
        def _enum_bridge(ch, lp):
            nonlocal bridge_hwnd
            if "DesktopWindowContentBridge" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                cr = wintypes.RECT()
                user32.GetWindowRect(ch, ctypes.byref(cr))
                if cr.left <= r.left + 30:
                    bridge_hwnd = ch
                    return False
            return True
        user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_bridge), 0)
        target_wnd = bridge_hwnd or top_hwnd

        try:
            tm_tid = user32.GetWindowThreadProcessId(top_hwnd, None)
            my_tid = kernel32.GetCurrentThreadId()
            if tm_tid and tm_tid != my_tid:
                user32.AttachThreadInput(my_tid, tm_tid, True)

            user32.SetForegroundWindow(top_hwnd)
            user32.SetActiveWindow(top_hwnd)
            user32.SetFocus(target_wnd)
        except Exception:
            pass

        # 3. SysTabControl32 direct selection & parent notification
        tab_ctrl = None
        def _enum_tab(ch, lp):
            nonlocal tab_ctrl
            if get_wnd_class(ch) == "SysTabControl32":
                tab_ctrl = ch
                return False
            return True
        user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_tab), 0)

        if tab_ctrl:
            try:
                user32.SendMessageW(tab_ctrl, 0x130C, target_idx, 0)  # TCM_SETCURSEL
                ctrl_id = user32.GetDlgCtrlID(tab_ctrl)
                class NMHDR(ctypes.Structure):
                    _fields_ = [("hwndFrom", wintypes.HWND), ("idFrom", wintypes.UINT), ("code", wintypes.UINT)]
                nm = NMHDR(tab_ctrl, ctrl_id, 0xFFFFFDDF)  # TCN_SELCHANGE (-551)
                parent_hwnd = user32.GetParent(tab_ctrl) or top_hwnd
                user32.SendMessageW(parent_hwnd, 0x004E, ctrl_id, ctypes.byref(nm))
            except Exception:
                pass

        # 4. Direct Keyboard Navigation (VK_HOME -> VK_DOWN * N -> VK_SPACE)
        # Guarantees deterministic section selection regardless of prior state
        def _send_key(w, vk):
            scan = user32.MapVirtualKeyW(vk, 0)
            lp_d = 1 | (scan << 16)
            lp_u = 1 | (scan << 16) | (1 << 30) | (1 << 31)
            user32.PostMessageW(w, WM_KEYDOWN, vk, lp_d)
            user32.PostMessageW(w, WM_KEYUP, vk, lp_u)

        targets = [target_wnd]
        if top_hwnd != target_wnd:
            targets.append(top_hwnd)

        for w in targets:
            _send_key(w, 0x24)  # VK_HOME (jump to first item: Processes)
            time.sleep(0.02)
            for _ in range(target_idx):
                _send_key(w, 0x28)  # VK_DOWN to target item
                time.sleep(0.02)
            _send_key(w, 0x20)  # VK_SPACE (select active item)
            _send_key(w, 0x0D)  # VK_RETURN (confirm selection)

        # 5. Synthetic Ctrl + Tab / Ctrl + Shift + Tab cycling as secondary fallback
        delta = target_idx - self.tm_active_tab
        if delta != 0:
            steps = abs(delta)
            is_forward = delta > 0
            try:
                scan_ctrl = user32.MapVirtualKeyW(0x11, 0)
                scan_shift = user32.MapVirtualKeyW(0x10, 0)
                scan_tab = user32.MapVirtualKeyW(0x09, 0)

                lp_ctrl_down = 1 | (scan_ctrl << 16)
                lp_ctrl_up = 1 | (scan_ctrl << 16) | (1 << 30) | (1 << 31)
                lp_shift_down = 1 | (scan_shift << 16)
                lp_shift_up = 1 | (scan_shift << 16) | (1 << 30) | (1 << 31)
                lp_tab_down = 1 | (scan_tab << 16)
                lp_tab_up = 1 | (scan_tab << 16) | (1 << 30) | (1 << 31)

                for w in targets:
                    user32.PostMessageW(w, WM_KEYDOWN, 0x11, lp_ctrl_down)
                    if not is_forward:
                        user32.PostMessageW(w, WM_KEYDOWN, 0x10, lp_shift_down)
                    for _ in range(steps):
                        user32.PostMessageW(w, WM_KEYDOWN, 0x09, lp_tab_down)
                        user32.PostMessageW(w, WM_KEYUP, 0x09, lp_tab_up)
                        time.sleep(0.03)
                    if not is_forward:
                        user32.PostMessageW(w, WM_KEYUP, 0x10, lp_shift_up)
                    user32.PostMessageW(w, WM_KEYUP, 0x11, lp_ctrl_up)
            except Exception:
                pass

        self.tm_active_tab = target_idx
        return target_idx

    def _find_target_window(self, x: int, y: int, force_refresh: bool = False) -> Tuple[Optional[int], Optional[int], int, int]:
        """
        Finds the top-level window and exact leaf target window under (x, y) on the hidden desktop.
        Strictly stays within the hidden desktop window tree (no calls to WindowFromPoint).
        Returns (top_level_hwnd, target_child_hwnd, client_x, client_y).
        """
        now = time.time()
        if force_refresh or (now - self._last_enum_time > 0.25) or not self._cached_windows:
            self._cached_windows = self.desktop.enumerate_windows()
            self._last_enum_time = now
        windows = self._cached_windows
        top_hwnd = None
        top_class = ""

        # 1. Identify top-level window in Z-order covering (x, y), prioritizing popups and smaller foreground windows
        covering_windows = [
            w for w in windows
            if w[3][0] <= x <= w[3][2] and w[3][1] <= y <= w[3][3]
        ]
        if not covering_windows:
            return None, None, x, y

        def sort_key(w):
            is_pop = is_popup_window(w[0])
            area = (w[3][2] - w[3][0]) * (w[3][3] - w[3][1])
            return (0 if is_pop else 1, area)

        covering_windows.sort(key=sort_key)
        top_hwnd = covering_windows[0][0]
        top_class = covering_windows[0][2]

        # 2. Search children of top_hwnd on the hidden desktop to find deepest leaf
        target = top_hwnd
        if top_class not in ("ConsoleWindowClass",) and not is_popup_window(top_hwnd):
            matching_children: List[Tuple[int, int, str]] = []

            def enum_child_proc(hwnd: int, lparam: int) -> bool:
                if user32.IsWindowVisible(hwnd):
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.left <= x <= r.right and r.top <= y <= r.bottom:
                        area = (r.right - r.left) * (r.bottom - r.top)
                        c_name = get_wnd_class(hwnd)
                        matching_children.append((area, hwnd, c_name))
                return True

            cb = WNDENUMPROC(enum_child_proc)
            user32.EnumChildWindows(top_hwnd, cb, 0)

            if matching_children:
                # Smallest area is deepest leaf candidate
                matching_children.sort(key=lambda item: item[0])
                leaf_candidate = matching_children[0][1]
                leaf_class = matching_children[0][2]

                # Chrome/Edge/Firefox: Chromium Aura & Gecko require input directed at the top-level container window (Chrome_WidgetWin_1).
                # Targeting child Intermediate D3D Window or RenderWidgetHostHWND drops events or offsets coordinates by ~87px.
                if top_class in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
                    target = top_hwnd
                # File Explorer: resolve SysTreeView32 (side nav), DesktopChildSiteBridge (tabs & address bar), or DirectUIHWND
                elif top_class == "CabinetWClass":
                    tree_ctrls = [m for m in matching_children if m[2] == "SysTreeView32"]
                    if tree_ctrls:
                        target = tree_ctrls[0][1]
                    else:
                        bridge_ctrls = [m for m in matching_children if "DesktopChildSiteBridge" in m[2]]
                        # Address bar / toolbar area: top ~270px
                        if bridge_ctrls and y < 270:
                            target = bridge_ctrls[0][1]
                        else:
                            # Include ToolbarWindow32 (address bar, search, breadcrumbs, nav buttons)
                            interactive = [
                                m for m in matching_children
                                if m[2] not in ("CtrlNotifySink", "SHELLDLL_DefView", "DUIViewWndClassName",
                                                "ShellTabWindowClass", "CabinetWClass", "Static",
                                                "NamespaceTreeControl", "TITLE_BAR_SCAFFOLDING_WINDOW_CLASS")
                            ]
                            if interactive:
                                target = interactive[0][1]
                            else:
                                target = leaf_candidate
                # Task Manager: prioritize Windows.UI.Input.InputSite.WindowClass (the modern XAML input router), or DesktopWindowContentBridge
                elif top_class in ("TaskManagerWindow", "TaskManager"):
                    input_sites = [m for m in matching_children if "InputSite" in m[2]]
                    if input_sites:
                        target = input_sites[0][1]
                    else:
                        interactive = [
                            m for m in matching_children
                            if m[2] not in ("NativeHWNDHost", "GlassWindow", "FilterControlGlassWindow")
                        ]
                        target = interactive[0][1] if interactive else leaf_candidate
                else:
                    target = leaf_candidate

        # Convert screen (x, y) to target client coordinates
        pt_client = wintypes.POINT(x, y)
        user32.ScreenToClient(target, ctypes.byref(pt_client))

        return top_hwnd, target, pt_client.x, pt_client.y

    def _check_titlebar_action(self, top_hwnd: int, x: int, y: int) -> Optional[str]:
        """
        Determines if (x, y) falls on a titlebar button (close, maximize/restore, minimize).
        Uses native WM_NCHITTEST for standard windows; pure pixel geometry for WinUI3 (Task Manager)
        which always returns HTCLIENT from WM_NCHITTEST.
        """
        if is_popup_window(top_hwnd):
            return None

        rect = wintypes.RECT()
        user32.GetWindowRect(top_hwnd, ctypes.byref(rect))
        sw = user32.GetSystemMetrics(0) or 1920
        is_zoomed = bool(user32.IsZoomed(top_hwnd)) or (rect.left < 0 and rect.top < 0) or (rect.left <= 0 and rect.right >= sw)
        is_task_manager = get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager")

        vis_right = sw if is_zoomed else (rect.right - (0 if rect.left < 0 else 0))
        top_y = 0 if is_zoomed else rect.top
        # Task Manager titlebar is ~48px; standard windows ~40px
        btn_height = 48 if is_task_manager else 42

        # For non-TM windows: use native WM_NCHITTEST first (most accurate)
        if not is_task_manager:
            try:
                lParam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
                res = ctypes.c_ulong(0)
                user32.SendMessageTimeoutW(top_hwnd, WM_NCHITTEST, 0, lParam, SMTO_ABORTIFHUNG, 25, ctypes.byref(res))
                hit = res.value
                if hit == HTCLOSE:
                    return "close"
                elif hit == HTMAXBUTTON:
                    return "restore" if is_zoomed else "maximize"
                elif hit == HTMINBUTTON:
                    return "minimize"
            except Exception:
                pass

        # Pixel-Calibrated Geometry (always used for TM; fallback for others)
        if not (top_y <= y <= top_y + btn_height):
            return None

        # Windows 11 caption button widths: Chrome/Edge are ~56px each; TM/native ~46px
        is_chrome = "Chrome" in get_wnd_class(top_hwnd) or "Edge" in get_wnd_class(top_hwnd)
        close_w = 56 if is_chrome else 46
        max_w = 56 if is_chrome else 46
        min_w = 56 if is_chrome else 46
        if vis_right - close_w <= x <= vis_right + 2:
            return "close"
        elif vis_right - close_w - max_w <= x < vis_right - close_w:
            return "restore" if is_zoomed else "maximize"
        elif vis_right - close_w - max_w - min_w <= x < vis_right - close_w - max_w:
            return "minimize"

        return None

    def handle_taskbar_action(self, action: str, hwnd: int) -> bool:
        """Executes window management operations (restore, minimize, maximize, close)."""
        try:
            self.desktop.attach_current_thread()
            if action == "restore":
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
                self.last_focused_hwnd = hwnd
                return True
            elif action == "minimize":
                user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
                user32.ShowWindow(hwnd, SW_MINIMIZE)
                return True
            elif action == "maximize":
                user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MAXIMIZE, 0)
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
                return True
            elif action == "close":
                user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True
        except Exception as e:
            logger.warning(f"Error handling taskbar action '{action}' on hwnd 0x{hwnd:X}: {e}")
        return False

    def _toggle_show_desktop(self):
        """Toggles minimizing all windows (Show Desktop) or restoring them."""
        windows = self.desktop.enumerate_windows(include_minimized=True)
        non_min = [w[0] for w in windows if not (w[4] if len(w) == 5 else False) and w[2] != "Shell_TrayWnd" and not is_popup_window(w[0])]
        if non_min:
            self._minimized_for_show_desktop = set(non_min)
            for hwnd in non_min:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif self._minimized_for_show_desktop:
            for hwnd in self._minimized_for_show_desktop:
                user32.ShowWindow(hwnd, SW_RESTORE)
            self._minimized_for_show_desktop.clear()

    def _handle_taskbar_click(self, x: int, y: int, tb_top: int, button: str = "left") -> Dict[str, Any]:
        """Handles clicks on the virtual taskbar: Start button, Search, pinned apps, window tabs, and right-side tray buttons."""
        diag = {"status": "taskbar_click", "action": "taskbar", "button": button}
        screen_w = (self.compositor.width if self.compositor else user32.GetSystemMetrics(0)) or 1920

        # 1. Windows Start button (8px to 52px)
        if 8 <= x <= 52:
            logger.info("Start button clicked on virtual taskbar.")
            diag["taskbar_action"] = "toggle_start_menu"
            return diag

        # 2. Windows Search pill (54px to 182px)
        if 54 <= x <= 182:
            logger.info("Search pill clicked on virtual taskbar.")
            diag["taskbar_action"] = "toggle_start_menu"
            return diag

        # 3. Pinned Quick Launch Icons
        # File Explorer (184 to 228)
        if 184 <= x <= 228:
            if self.spawner_callback:
                self.spawner_callback("explorer", None)
            diag["taskbar_action"] = "launch_explorer"
            return diag

        # Microsoft Edge (230 to 274)
        if 230 <= x <= 274:
            if self.spawner_callback:
                self.spawner_callback("edge", None)
            diag["taskbar_action"] = "launch_edge"
            return diag

        # Google Chrome (276 to 322)
        if 276 <= x <= 322:
            if self.spawner_callback:
                self.spawner_callback("chrome", None)
            diag["taskbar_action"] = "launch_chrome"
            return diag

        # 4. Right-side System Tray Buttons
        # Show Desktop Peek (far right edge)
        if x >= screen_w - 6:
            self._toggle_show_desktop()
            diag["taskbar_action"] = "show_desktop"
            return diag

        # Notification Bell
        if screen_w - 40 <= x < screen_w - 6:
            diag["taskbar_action"] = "open_notifications"
            return diag

        # Clock & Date / Calendar
        if screen_w - 152 <= x < screen_w - 40:
            diag["taskbar_action"] = "open_calendar"
            return diag

        # Quick Settings Pill (Wi-Fi, Volume, Battery)
        if screen_w - 254 <= x < screen_w - 152:
            diag["taskbar_action"] = "open_quick_settings"
            return diag

        # Language Badge
        if screen_w - 302 <= x < screen_w - 254:
            diag["taskbar_action"] = "toggle_language"
            return diag

        # System Tray Overflow Chevron
        if screen_w - 342 <= x < screen_w - 302:
            diag["taskbar_action"] = "toggle_system_tray"
            return diag

        # 5. Open Windows Tabs on Taskbar (starts at cur_x = 328)
        cur_x = 328
        windows = self.desktop.enumerate_windows(include_minimized=True)
        for w in windows:
            hwnd, title, class_name, _, is_min = w if len(w) == 5 else (*w, False)
            if class_name == "Shell_TrayWnd" or is_popup_window(hwnd):
                continue
            disp_title = (title or class_name or "Application")[:22]
            btn_w = min(180, max(100, len(disp_title) * 8 + 30))
            if cur_x <= x <= cur_x + btn_w:
                diag["target_hwnd"] = hwnd
                diag["target_title"] = disp_title
                diag["is_minimized"] = is_min

                if button == "right":
                    diag["taskbar_action"] = "context_menu"
                    return diag
                else:
                    if is_min or hwnd != self.last_focused_hwnd:
                        self.handle_taskbar_action("restore", hwnd)
                        diag["taskbar_action"] = "restored"
                    else:
                        self.handle_taskbar_action("minimize", hwnd)
                        diag["taskbar_action"] = "minimized"
                    return diag
            cur_x += btn_w + 6
        return diag

    def handle_mouse_move(self, x: int, y: int):
        """Sends WM_MOUSEMOVE with window drag, icon drag, and ultra-fast non-blocking event routing."""
        self.desktop.attach_current_thread()

        # 1. Window move dragging
        if self.is_lbutton_down and self.dragged_window_hwnd:
            new_x = x - self.window_drag_offset_x
            new_y = max(0, y - self.window_drag_offset_y)
            user32.SetWindowPos(self.dragged_window_hwnd, 0, new_x, new_y, 0, 0, 0x0001 | 0x0004 | 0x0010)
            return

        # 2. Desktop icon dragging
        if self.is_lbutton_down and self.dragged_icon_idx is not None and self.compositor:
            icons = getattr(self.compositor, "desktop_icons", [])
            idx = self.dragged_icon_idx
            if 0 <= idx < len(icons):
                item = list(icons[idx])
                w = item[4][2] - item[4][0]
                h = item[4][3] - item[4][1]
                new_x1 = max(10, min(self.compositor.width - w - 10, x - self.drag_offset_x))
                new_y1 = max(10, min(self.compositor.height - h - 60, y - self.drag_offset_y))
                item[4] = (new_x1, new_y1, new_x1 + w, new_y1 + h)
                icons[idx] = tuple(item)
                return

        # 3. Fast-path in-window drag (scrollbar, text selection, controls)
        if self.is_lbutton_down and self.drag_target:
            pt_client = wintypes.POINT(x, y)
            user32.ScreenToClient(self.drag_target, ctypes.byref(pt_client))
            l_param = ((pt_client.y & 0xFFFF) << 16) | (pt_client.x & 0xFFFF)
            user32.PostMessageW(self.drag_target, WM_MOUSEMOVE, MK_LBUTTON, l_param)
            return

        # 4. Standard hover mouse move
        top_hwnd, hwnd, cx, cy = self._find_target_window(x, y)
        target = hwnd or top_hwnd
        if target:
            pt_client = wintypes.POINT(x, y)
            user32.ScreenToClient(target, ctypes.byref(pt_client))
            l_param = ((pt_client.y & 0xFFFF) << 16) | (pt_client.x & 0xFFFF)
            user32.PostMessageW(target, WM_MOUSEMOVE, 0, l_param)

    def handle_mouse_down(self, x: int, y: int, button: str = "left") -> Dict[str, Any]:
        """Sends mouse down with top-right window buttons (Close, Minimize, Maximize), desktop icon drag, & returns diagnostics."""
        self.desktop.attach_current_thread()
        if button == "left":
            self.is_lbutton_down = True

        diag = {
            "action": "mousedown",
            "button": button,
            "x": x,
            "y": y,
            "target_hwnd": None,
            "target_class": None,
            "target_title": None,
            "top_hwnd": None,
            "client_x": x,
            "client_y": y,
            "status": "missed",
        }

        top_hwnd, hwnd, cx, cy = self._find_target_window(x, y, force_refresh=True)

        # Desktop background clicks (when no window is under cursor)
        if not top_hwnd:
            if button == "left":
                # Check for desktop icon drag start
                if self.compositor and hasattr(self.compositor, "desktop_icons"):
                    for idx, item in enumerate(self.compositor.desktop_icons):
                        if len(item) >= 5:
                            bx1, by1, bx2, by2 = item[4]
                            if bx1 <= x <= bx2 and by1 <= y <= by2:
                                self.dragged_icon_idx = idx
                                self.drag_offset_x = x - bx1
                                self.drag_offset_y = y - by1
                                diag["status"] = "icon_drag_start"
                                diag["icon_name"] = item[0]
                                return diag
            elif button == "right":
                diag["status"] = "desktop_context_menu"
                diag["desktop_action"] = "show_desktop_menu"
                return diag
            return diag

        if not top_hwnd:
            return diag

        is_popup = is_popup_window(top_hwnd)

        top_tid = user32.GetWindowThreadProcessId(top_hwnd, None) if top_hwnd else 0
        target_tid = user32.GetWindowThreadProcessId(hwnd or top_hwnd, None) if (hwnd or top_hwnd) else 0
        diag.update({
            "top_hwnd": top_hwnd,
            "target_hwnd": hwnd or top_hwnd,
            "target_class": get_wnd_class(hwnd or top_hwnd),
            "target_title": get_wnd_title(top_hwnd),
            "client_x": cx,
            "client_y": cy,
            "is_popup": is_popup,
            "top_tid": top_tid,
            "target_tid": target_tid,
            "my_tid": kernel32.GetCurrentThreadId(),
        })

        is_task_manager = get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager") if top_hwnd else False

        # 2. Window Titlebar Buttons (Close, Maximize/Restore, Minimize)
        if button == "left" and not is_popup:
            tb_action = self._check_titlebar_action(top_hwnd, x, y)
            if tb_action:
                self.titlebar_action_down = True
                self.drag_target = None
                if tb_action == "close":
                    if is_task_manager:
                        # Task Manager: use SendMessage for WM_SYSCOMMAND to ensure WinUI3 processes it
                        user32.SendMessageW(top_hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
                    else:
                        user32.SendMessageTimeoutW(top_hwnd, WM_SYSCOMMAND, SC_CLOSE, 0, SMTO_ABORTIFHUNG, 35, ctypes.byref(ctypes.c_ulong()))
                        user32.PostMessageW(top_hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
                        user32.PostMessageW(top_hwnd, WM_CLOSE, 0, 0)
                    diag["status"] = "window_close"
                elif tb_action == "maximize":
                    if is_task_manager:
                        user32.ShowWindow(top_hwnd, SW_MAXIMIZE)
                    else:
                        user32.SendMessageTimeoutW(top_hwnd, WM_SYSCOMMAND, SC_MAXIMIZE, 0, SMTO_ABORTIFHUNG, 35, ctypes.byref(ctypes.c_ulong()))
                        user32.PostMessageW(top_hwnd, WM_SYSCOMMAND, SC_MAXIMIZE, 0)
                        user32.ShowWindow(top_hwnd, SW_MAXIMIZE)
                        user32.SetWindowPos(top_hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020)
                        user32.BringWindowToTop(top_hwnd)
                    diag["status"] = "window_maximize"
                elif tb_action == "restore":
                    if is_task_manager:
                        user32.ShowWindow(top_hwnd, SW_RESTORE)
                    else:
                        user32.SendMessageTimeoutW(top_hwnd, WM_SYSCOMMAND, SC_RESTORE, 0, SMTO_ABORTIFHUNG, 35, ctypes.byref(ctypes.c_ulong()))
                        user32.PostMessageW(top_hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
                        user32.ShowWindow(top_hwnd, SW_RESTORE)
                        user32.SetWindowPos(top_hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020)
                        user32.BringWindowToTop(top_hwnd)
                    diag["status"] = "window_restore"
                elif tb_action == "minimize":
                    if is_task_manager:
                        user32.ShowWindow(top_hwnd, SW_MINIMIZE)
                    else:
                        user32.SendMessageTimeoutW(top_hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0, SMTO_ABORTIFHUNG, 35, ctypes.byref(ctypes.c_ulong()))
                        user32.PostMessageW(top_hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
                        user32.ShowWindow(top_hwnd, SW_MINIMIZE)
                    diag["status"] = "window_minimize"
                return diag

        # 3. Window titlebar dragging (moving floating windows)
        rect = wintypes.RECT()
        user32.GetWindowRect(top_hwnd, ctypes.byref(rect))
        sw = user32.GetSystemMetrics(0) or 1920
        is_zoomed = bool(user32.IsZoomed(top_hwnd)) or (rect.left < 0 and rect.top < 0) or (rect.left <= 0 and rect.right >= sw)
        if button == "left" and not is_zoomed and not is_popup:
            if rect.top <= y <= rect.top + 38 and rect.left <= x <= rect.right:
                self.dragged_window_hwnd = top_hwnd
                self.window_drag_offset_x = x - rect.left
                self.window_drag_offset_y = y - rect.top

        # 4. Standard Window Client Clicks vs. Popup Menu Clicks
        target = hwnd or top_hwnd
        if button == "left":
            self.drag_target = target

        if is_popup:
            pass
        else:
            # Only change window activation if clicking on a different top-level window (eliminates hairline blinking!)
            if top_hwnd != self.last_focused_hwnd:
                self.last_focused_hwnd = top_hwnd
                try:
                    top_tid = user32.GetWindowThreadProcessId(top_hwnd, None)
                    my_tid = kernel32.GetCurrentThreadId()
                    if top_tid and top_tid != my_tid:
                        user32.AttachThreadInput(my_tid, top_tid, True)

                    user32.SetWindowPos(top_hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002)
                    user32.BringWindowToTop(top_hwnd)
                    user32.SetForegroundWindow(top_hwnd)
                    user32.SetActiveWindow(top_hwnd)
                except Exception:
                    pass

            try:
                user32.SetFocus(target)
            except Exception:
                pass

            # Send WM_MOUSEACTIVATE with timeout so DirectUI and File Explorer accept item clicks
            try:
                sm_res = ctypes.c_ulong()
                user32.SendMessageTimeoutW(top_hwnd, WM_MOUSEACTIVATE, top_hwnd, (WM_LBUTTONDOWN << 16) | HTCLIENT, SMTO_ABORTIFHUNG, 20, ctypes.byref(sm_res))
                user32.SendMessageTimeoutW(target, WM_SETCURSOR, target, (WM_LBUTTONDOWN << 16) | HTCLIENT, SMTO_ABORTIFHUNG, 20, ctypes.byref(sm_res))
            except Exception:
                pass

        l_param = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        # Precursor WM_MOUSEMOVE: establishes pointer position, updates Blink hover cache, and triggers pointerover/mouseenter on DOM elements
        user32.PostMessageW(target, WM_MOUSEMOVE, 0, l_param)
        if top_hwnd and top_hwnd != target:
            pt_top = wintypes.POINT(x, y)
            user32.ScreenToClient(top_hwnd, ctypes.byref(pt_top))
            l_param_top = ((pt_top.y & 0xFFFF) << 16) | (pt_top.x & 0xFFFF)
            user32.PostMessageW(top_hwnd, WM_MOUSEMOVE, 0, l_param_top)

        if button == "left":
            res = user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, l_param)
            if top_hwnd and top_hwnd != target:
                pt_top = wintypes.POINT(x, y)
                user32.ScreenToClient(top_hwnd, ctypes.byref(pt_top))
                l_param_top = ((pt_top.y & 0xFFFF) << 16) | (pt_top.x & 0xFFFF)
                user32.PostMessageW(top_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, l_param_top)

            # Modern WinUI 3 / XAML Island Pointer Message dispatching
            if top_hwnd and ("TaskManager" in get_wnd_class(top_hwnd) or "DesktopWindowContentBridge" in get_wnd_class(target)):
                l_param_screen = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                w_ptr_down = 1 | ((0x0004 | 0x2000 | 0x0010) << 16)
                # Send pointer events to InputSite child only (avoid flooding all children)
                input_site_hwnd = None
                def _enum_site_down(ch, lp):
                    nonlocal input_site_hwnd
                    if "InputSite" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                        input_site_hwnd = ch
                        user32.PostMessageW(ch, 0x0246, w_ptr_down, l_param_screen)
                        return False
                    return True
                user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_site_down), 0)
                if not input_site_hwnd:
                    user32.PostMessageW(target, 0x0246, w_ptr_down, l_param_screen)
                diag["pointer_down_dispatched"] = True

                # Windows 11 Task Manager sidebar navigation handler (only for sidebar zone clicks)
                if is_task_manager:
                    r_tm = wintypes.RECT()
                    user32.GetWindowRect(top_hwnd, ctypes.byref(r_tm))
                    x_rel = x - r_tm.left
                    if x_rel <= 220:  # Only invoke sidebar handler for left-side clicks
                        nav_idx = self._handle_task_manager_navigation(top_hwnd, x, y, target_hwnd=target)
                        if nav_idx is not None:
                            diag["tm_sidebar_nav"] = nav_idx
                    else:
                        # For non-sidebar clicks (buttons, search, list items), focus the InputSite
                        if input_site_hwnd:
                            try:
                                tm_tid = user32.GetWindowThreadProcessId(top_hwnd, None)
                                my_tid = kernel32.GetCurrentThreadId()
                                if tm_tid and tm_tid != my_tid:
                                    user32.AttachThreadInput(my_tid, tm_tid, True)
                                user32.SetForegroundWindow(top_hwnd)
                                user32.SetActiveWindow(top_hwnd)
                                user32.SetFocus(input_site_hwnd)
                            except Exception:
                                pass

            diag["status"] = f"post_lbuttondown_{res}"
        elif button == "right":
            res = user32.PostMessageW(target, WM_RBUTTONDOWN, MK_RBUTTON, l_param)
            if top_hwnd and top_hwnd != target and get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager"):
                pt_top = wintypes.POINT(x, y)
                user32.ScreenToClient(top_hwnd, ctypes.byref(pt_top))
                l_param_top = ((pt_top.y & 0xFFFF) << 16) | (pt_top.x & 0xFFFF)
                user32.PostMessageW(top_hwnd, WM_RBUTTONDOWN, MK_RBUTTON, l_param_top)
            diag["status"] = f"post_rbuttondown_{res}"

            # Task Manager right-click on mousedown: send complete RDown+RUp+ContextMenu
            # sequence immediately so it arrives while focus is on TM. Don't wait for mouseup.
            if top_hwnd and get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager"):
                l_param_screen = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                def _enum_input_rclick(ch, lp):
                    if "InputSite" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                        # Full sequence: down, up, contextmenu
                        user32.PostMessageW(ch, WM_RBUTTONDOWN, MK_RBUTTON, l_param_screen)
                        user32.PostMessageW(ch, WM_RBUTTONUP, 0, l_param_screen)
                        user32.PostMessageW(ch, 0x007B, ch, l_param_screen)  # WM_CONTEXTMENU
                        return False
                    return True
                user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_input_rclick), 0)
                user32.PostMessageW(top_hwnd, WM_RBUTTONDOWN, MK_RBUTTON, l_param_screen)
                user32.PostMessageW(top_hwnd, WM_RBUTTONUP, 0, l_param_screen)
                user32.PostMessageW(top_hwnd, 0x007B, top_hwnd, l_param_screen)
                diag["tm_contextmenu_dispatched"] = True
                diag["status"] = f"post_rbuttondown_{res}"

        return diag

    def handle_mouse_up(self, x: int, y: int, button: str = "left"):
        """Sends WM_LBUTTONUP or WM_RBUTTONUP."""
        self.desktop.attach_current_thread()
        if button == "left":
            self.dragged_icon_idx = None
            self.dragged_window_hwnd = None
            self.is_lbutton_down = False
            if self.titlebar_action_down:
                self.titlebar_action_down = False
                self.drag_target = None
                return

        top_hwnd, hwnd, cx, cy = self._find_target_window(x, y)
        if top_hwnd and button == "left":
            if self._check_titlebar_action(top_hwnd, x, y):
                self.drag_target = None
                return

        target = hwnd or top_hwnd
        if target:
            pt_client = wintypes.POINT(x, y)
            user32.ScreenToClient(target, ctypes.byref(pt_client))
            l_param = ((pt_client.y & 0xFFFF) << 16) | (pt_client.x & 0xFFFF)
            if button == "left":
                user32.PostMessageW(target, WM_LBUTTONUP, 0, l_param)
                if top_hwnd and top_hwnd != target and get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager"):
                    pt_top = wintypes.POINT(x, y)
                    user32.ScreenToClient(top_hwnd, ctypes.byref(pt_top))
                    l_param_top = ((pt_top.y & 0xFFFF) << 16) | (pt_top.x & 0xFFFF)
                    user32.PostMessageW(top_hwnd, WM_LBUTTONUP, 0, l_param_top)

                # Modern WinUI 3 / XAML Island Pointer Up dispatching
                if top_hwnd and ("TaskManager" in get_wnd_class(top_hwnd) or "DesktopWindowContentBridge" in get_wnd_class(target)):
                    l_param_screen = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                    w_ptr_up = 1 | (0x2000 << 16)
                    # Only send to the first visible InputSite to avoid event flooding
                    input_site_up = None
                    def _enum_site_up(ch, lp):
                        nonlocal input_site_up
                        if "InputSite" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                            input_site_up = ch
                            user32.PostMessageW(ch, 0x0247, w_ptr_up, l_param_screen)
                            return False
                        return True
                    user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_site_up), 0)
                    if not input_site_up:
                        user32.PostMessageW(target, 0x0247, w_ptr_up, l_param_screen)

                    # Focus InputSite on pointer release for WinUI3
                    is_tm_up = get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager")
                    if is_tm_up and input_site_up:
                        try:
                            tm_tid_up = user32.GetWindowThreadProcessId(top_hwnd, None)
                            my_tid_up = kernel32.GetCurrentThreadId()
                            if tm_tid_up and tm_tid_up != my_tid_up:
                                user32.AttachThreadInput(my_tid_up, tm_tid_up, True)
                            user32.SetForegroundWindow(top_hwnd)
                            user32.SetActiveWindow(top_hwnd)
                            user32.SetFocus(input_site_up)
                        except Exception:
                            pass

                if self.drag_target and self.drag_target != target:
                    user32.PostMessageW(self.drag_target, WM_LBUTTONUP, 0, l_param)
            elif button == "right":
                user32.PostMessageW(target, WM_RBUTTONUP, 0, l_param)
                l_param_screen = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                user32.PostMessageW(target, 0x007B, target, l_param_screen)
                if top_hwnd and top_hwnd != target:
                    pt_top = wintypes.POINT(x, y)
                    user32.ScreenToClient(top_hwnd, ctypes.byref(pt_top))
                    l_param_top = ((pt_top.y & 0xFFFF) << 16) | (pt_top.x & 0xFFFF)
                    user32.PostMessageW(top_hwnd, WM_RBUTTONUP, 0, l_param_top)
                    user32.PostMessageW(top_hwnd, 0x007B, top_hwnd, l_param_screen)

                # Task Manager: also dispatch to InputSite for WinUI3 context menu support
                if top_hwnd and get_wnd_class(top_hwnd) in ("TaskManagerWindow", "TaskManager"):
                    def _enum_input_ctx_up(ch, lp):
                        if "InputSite" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                            user32.PostMessageW(ch, WM_RBUTTONUP, 0, l_param_screen)
                            user32.PostMessageW(ch, 0x007B, ch, l_param_screen)
                            return False
                        return True
                    user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_input_ctx_up), 0)

        self.drag_target = None
        self.dragged_window_hwnd = None

    def handle_double_click(self, x: int, y: int):
        """Sends WM_LBUTTONDBLCLK to open files/folders or launches desktop icons."""
        self.desktop.attach_current_thread()
        top_hwnd, hwnd, cx, cy = self._find_target_window(x, y, force_refresh=True)

        # Check if clicking on desktop background (no window above), launch desktop icon if hit
        if not top_hwnd and self.compositor and hasattr(self.compositor, "desktop_icons"):
            for item in getattr(self.compositor, "desktop_icons", []):
                if len(item) >= 5:
                    name, path, is_dir, ext, (bx1, by1, bx2, by2) = item
                    if bx1 <= x <= bx2 and by1 <= y <= by2:
                        logger.info(f"Desktop icon double-clicked: '{name}' -> '{path}'")
                        if self.spawner_callback:
                            self.spawner_callback("custom_file", path)
                        return

        target = hwnd or top_hwnd
        if target:
            try:
                top_tid = user32.GetWindowThreadProcessId(top_hwnd or target, None)
                my_tid = kernel32.GetCurrentThreadId()
                if top_tid and top_tid != my_tid:
                    user32.AttachThreadInput(my_tid, top_tid, True)

                user32.BringWindowToTop(top_hwnd or target)
                user32.SetForegroundWindow(top_hwnd or target)
                user32.SetActiveWindow(top_hwnd or target)
                user32.SetFocus(target)
                sm_res = ctypes.c_ulong()
                user32.SendMessageTimeoutW(top_hwnd or target, WM_MOUSEACTIVATE, top_hwnd or target, (WM_LBUTTONDBLCLK << 16) | HTCLIENT, SMTO_ABORTIFHUNG, 20, ctypes.byref(sm_res))
                user32.SendMessageTimeoutW(target, WM_SETCURSOR, target, (WM_LBUTTONDBLCLK << 16) | HTCLIENT, SMTO_ABORTIFHUNG, 20, ctypes.byref(sm_res))
            except Exception:
                pass
            pt_client = wintypes.POINT(x, y)
            user32.ScreenToClient(target, ctypes.byref(pt_client))
            l_param = ((pt_client.y & 0xFFFF) << 16) | (pt_client.x & 0xFFFF)
            user32.PostMessageW(target, WM_LBUTTONDBLCLK, MK_LBUTTON, l_param)

    def handle_mouse_wheel(self, x: int, y: int, delta: int) -> Dict[str, Any]:
        """Sends WM_MOUSEWHEEL to leaf control. For Task Manager / DirectUIHWND uses key-only scroll (no WM_MOUSEWHEEL flood)."""
        self.desktop.attach_current_thread()
        top_hwnd, hwnd, cx, cy = self._find_target_window(x, y)
        target = hwnd or top_hwnd or self.last_focused_hwnd

        diag = {
            "action": "wheel",
            "delta": delta,
            "x": x,
            "y": y,
            "target_hwnd": target,
            "target_class": get_wnd_class(target) if target else None,
        }

        if not target:
            return diag

        try:
            top_tid = user32.GetWindowThreadProcessId(top_hwnd or target, None)
            my_tid = kernel32.GetCurrentThreadId()
            if top_tid and top_tid != my_tid:
                user32.AttachThreadInput(my_tid, top_tid, True)
            user32.SetFocus(top_hwnd or target)
        except Exception:
            pass

        t_class = get_wnd_class(target)
        top_class = get_wnd_class(top_hwnd) if top_hwnd else ""
        l_param = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        
        # Native Windows WHEEL_DELTA: preserve proportional delta from client machine / trackpad
        actual_delta = int(delta)
        w_param = (actual_delta & 0xFFFF) << 16
        sm_res = ctypes.c_ulong()

        # --- Task Manager Special Zone-Aware Scroll Handling ---
        # Task Manager has two distinct scroll areas:
        # 1. Left sidebar (Navigation Pane): x_rel < 220px -> Keys navigate tabs (InputSite).
        # 2. Main content area (Processes/Details/Services table): x_rel >= 220px ->
        #    Target is the table (DirectUIHWND); focus it and dispatch WM_MOUSEWHEEL, WM_VSCROLL, & arrow keys directly!
        if top_class in ("TaskManagerWindow", "TaskManager"):
            r_tm = wintypes.RECT()
            user32.GetWindowRect(top_hwnd, ctypes.byref(r_tm))
            x_rel = x - r_tm.left

            # Case A: Left sidebar zone (x_rel < 220)
            if x_rel < 220:
                best = None
                def _find_site(ch, lp):
                    nonlocal best
                    if "InputSite" in get_wnd_class(ch) and user32.IsWindowVisible(ch):
                        best = ch
                        return False
                    return True
                user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_find_site), 0)
                sidebar_wnd = best or target
                vk = 0x28 if actual_delta < 0 else 0x26
                scan = user32.MapVirtualKeyW(vk, 0)
                user32.SetFocus(sidebar_wnd)
                user32.PostMessageW(sidebar_wnd, WM_KEYDOWN, vk, 1 | (scan << 16))
                user32.PostMessageW(sidebar_wnd, WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))
                diag["tm_sidebar_scroll"] = True
                diag["scroll_wnd"] = sidebar_wnd
                return diag

            # Case B: Main table zone (x_rel >= 220) -> Target the table control (DirectUIHWND) directly!
            table_wnd = target or top_hwnd
            user32.SetFocus(table_wnd)

            # Send WM_MOUSEWHEEL with screen coordinates
            user32.SendMessageTimeoutW(table_wnd, WM_MOUSEWHEEL, w_param, l_param, SMTO_ABORTIFHUNG, 35, ctypes.byref(sm_res))
            user32.PostMessageW(table_wnd, WM_MOUSEWHEEL, w_param, l_param)
            if top_hwnd != table_wnd:
                user32.PostMessageW(top_hwnd, WM_MOUSEWHEEL, w_param, l_param)

            # Send WM_VSCROLL (SB_LINEDOWN=1 if delta < 0 else SB_LINEUP=0)
            vscroll_cmd = 1 if actual_delta < 0 else 0
            user32.PostMessageW(table_wnd, 0x0115, vscroll_cmd, 0)

            # Send discrete arrow key to scroll list items without touching side panel
            vk = 0x28 if actual_delta < 0 else 0x26
            scan = user32.MapVirtualKeyW(vk, 0)
            user32.PostMessageW(table_wnd, WM_KEYDOWN, vk, 1 | (scan << 16))
            user32.PostMessageW(table_wnd, WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))

            diag["tm_table_scroll"] = True
            diag["scroll_wnd"] = table_wnd
            return diag

        # --- Standard Windows (File Explorer, Chrome, Edge, Notepad, etc.) ---
        # 1. Set focus to target child control
        user32.SetFocus(target)

        # 2. Standard WM_MOUSEWHEEL via SendMessage + PostMessage with screen coordinates
        user32.SendMessageTimeoutW(target, WM_MOUSEWHEEL, w_param, l_param, SMTO_ABORTIFHUNG, 35, ctypes.byref(sm_res))
        res1 = user32.PostMessageW(target, WM_MOUSEWHEEL, w_param, l_param)
        diag["post_result"] = bool(res1)

        # 3. For list/tree controls (File Explorer DirectUIHWND, SysTreeView32, SysListView32, ScrollBar): also send WM_VSCROLL
        vscroll_cmd = 0 if actual_delta > 0 else 1  # SB_LINEUP=0, SB_LINEDOWN=1
        if t_class in ("DirectUIHWND", "SysTreeView32", "SysListView32", "ScrollBar") or "Tree" in t_class or "View" in t_class:
            user32.PostMessageW(target, 0x0115, vscroll_cmd, 0)

        # 4. Also forward WM_MOUSEWHEEL to top-level window if target is a child
        if top_hwnd and top_hwnd != target:
            user32.SendMessageTimeoutW(top_hwnd, WM_MOUSEWHEEL, w_param, l_param, SMTO_ABORTIFHUNG, 35, ctypes.byref(sm_res))
            user32.PostMessageW(top_hwnd, WM_MOUSEWHEEL, w_param, l_param)

        return diag

    def handle_key_down(self, vk_code: int):
        """Sends WM_KEYDOWN with hardware scancode."""
        self.desktop.attach_current_thread()
        target_hwnd = self.last_focused_hwnd
        if not target_hwnd or not user32.IsWindowVisible(target_hwnd):
            wins = self.desktop.enumerate_windows()
            if wins:
                target_hwnd = wins[0][0]
                self.last_focused_hwnd = target_hwnd

        if target_hwnd:
            try:
                top_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
                my_tid = kernel32.GetCurrentThreadId()
                if top_tid and top_tid != my_tid:
                    user32.AttachThreadInput(my_tid, top_tid, True)
            except Exception:
                pass

            scan = user32.MapVirtualKeyW(vk_code, 0)
            lparam = 1 | (scan << 16)
            user32.PostMessageW(target_hwnd, WM_KEYDOWN, vk_code, lparam)

    def handle_key_up(self, vk_code: int):
        """Sends WM_KEYUP with hardware scancode and transition state."""
        self.desktop.attach_current_thread()
        target_hwnd = self.last_focused_hwnd
        if not target_hwnd or not user32.IsWindowVisible(target_hwnd):
            wins = self.desktop.enumerate_windows()
            if wins:
                target_hwnd = wins[0][0]
                self.last_focused_hwnd = target_hwnd

        if target_hwnd:
            scan = user32.MapVirtualKeyW(vk_code, 0)
            lparam = 1 | (scan << 16) | (1 << 30) | (1 << 31)
            user32.PostMessageW(target_hwnd, WM_KEYUP, vk_code, lparam)

    def handle_char(self, char_code: int):
        """Sends WM_CHAR for clean single-character input without duplicate typing."""
        self.desktop.attach_current_thread()
        target_hwnd = self.last_focused_hwnd
        if not target_hwnd or not user32.IsWindowVisible(target_hwnd):
            wins = self.desktop.enumerate_windows()
            if wins:
                target_hwnd = wins[0][0]
                self.last_focused_hwnd = target_hwnd

        if target_hwnd:
            user32.PostMessageW(target_hwnd, WM_CHAR, char_code, 1)
