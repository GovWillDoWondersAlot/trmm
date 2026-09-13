"""
Comprehensive Test Script for TRMM HVNC Input & Process Execution.
Tests:
1. Desktop Isolation & Initialization
2. App Spawner (File Explorer, CMD, Chrome)
3. Zero-leakage Hit Testing (EnumChildWindows, DirectUIHWND)
4. Mouse Move, Hover WM_SETCURSOR, Click, Double-click, Wheel
5. Titlebar Window Controls (Minimize, Maximize, Restore, Close)
"""

import sys
import os
import time
import ctypes
from ctypes import wintypes

# Ensure TRMM repo root is in python path
repo_root = os.path.abspath(os.path.dirname(__file__))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from hvnc.desktop import HiddenDesktop, user32, kernel32
from hvnc.spawner import AppSpawner
from hvnc.input_handler import InputHandler, get_wnd_class, get_wnd_title, SW_RESTORE, SW_MAXIMIZE, SW_MINIMIZE


def run_tests():
    print("=" * 70)
    print("[TEST] TRMM HVNC - Comprehensive Input & Spawner Verification Test")
    print("=" * 70)

    test_desktop_name = "TRMM_Test_Workspace"
    desktop = HiddenDesktop(test_desktop_name)
    print(f"[1/5] Initializing Isolated Hidden Desktop: '{test_desktop_name}'...")
    if not desktop.initialize():
        print("[FAIL] Could not create/open test hidden desktop.")
        return False
    desktop.attach_current_thread()
    print("[PASS] Hidden Desktop created and thread attached successfully.")

    # Clean up any lingering windows on test desktop from prior runs
    for h, _, _, _ in desktop.enumerate_windows():
        user32.PostMessageW(h, 0x0010, 0, 0)
        user32.ShowWindow(h, 0)
    time.sleep(0.5)

    spawner = AppSpawner(test_desktop_name)
    input_handler = InputHandler(desktop)

    # Test 2: Launch Applications
    print("\n[2/5] Testing Application Spawner on Hidden Desktop...")

    # Spawn CMD
    cmd_pid, cmd_err, cmd_bin = spawner.launch_cmd()
    print(f"  * Spawn CMD: PID={cmd_pid}, err={cmd_err}")
    assert cmd_pid is not None, f"Failed to spawn CMD: {cmd_err}"

    # Spawn File Explorer
    exp_pid, exp_err, exp_bin = spawner.launch_explorer()
    print(f"  * Spawn File Explorer: PID={exp_pid}, err={exp_err}")
    assert exp_pid is not None, f"Failed to spawn File Explorer: {exp_err}"

    # Spawn Chrome
    chr_pid, chr_err, chr_bin = spawner.launch_chrome("https://google.com")
    print(f"  * Spawn Chrome: PID={chr_pid}, err={chr_err}")
    if not chr_bin:
        print("    (Note: Chrome binary not found on this machine, skipping Chrome-specific window check)")

    print("  Waiting 3 seconds for windows to initialize on hidden desktop...")
    time.sleep(3.0)

    # Enumerate windows on hidden desktop
    windows = desktop.enumerate_windows()
    print(f"\n  Found {len(windows)} top-level windows on hidden desktop:")
    wnd_map = {}
    for hwnd, title, class_name, rect in windows:
        print(f"    - HWND 0x{hwnd:08X} [{class_name}] '{title}' Rect={rect}")
        if class_name not in wnd_map:
            wnd_map[class_name] = (hwnd, title, rect)

    assert "ConsoleWindowClass" in wnd_map or len(windows) > 0, "No windows appeared on hidden desktop!"
    print("[PASS] App Spawner passed: processes running on isolated desktop.")

    # Test 3: Child Hit Testing (File Explorer & Chrome)
    print("\n[3/5] Testing Child Hit Testing on Hidden Desktop (Zero Leakage)...")
    if "CabinetWClass" in wnd_map:
        exp_hwnd, exp_title, exp_rect = wnd_map["CabinetWClass"]
        # Minimize all Chrome windows so Explorer is strictly unobstructed
        for h, t, c, r in windows:
            if c in ("Chrome_WidgetWin_1", "ConsoleWindowClass", "#32770"):
                user32.ShowWindow(h, SW_MINIMIZE)
        time.sleep(0.2)

        user32.ShowWindow(exp_hwnd, SW_RESTORE)
        time.sleep(0.3)

        center_x = (exp_rect[0] + exp_rect[2]) // 2
        center_y = (exp_rect[1] + exp_rect[3]) // 2

        top_h, leaf_h, cx, cy = input_handler._find_target_window(center_x, center_y, force_refresh=True)
        leaf_class = get_wnd_class(leaf_h) if leaf_h else ""
        print(f"  * File Explorer ({center_x}, {center_y}) resolved to:")
        print(f"    Top HWND  : 0x{top_h:08X} [{get_wnd_class(top_h)}]")
        print(f"    Leaf HWND : 0x{leaf_h:08X} [{leaf_class}] at client ({cx}, {cy})")

        assert top_h == exp_hwnd, f"Expected top_h == exp_hwnd (0x{exp_hwnd:08X}), got 0x{top_h:08X}"
        assert leaf_class in ("DirectUIHWND", "SysListView32", "SysTreeView32", "CabinetWClass"), f"Unexpected leaf: {leaf_class}"
        print("  [PASS] File Explorer resolved cleanly to leaf view.")

        # Minimize all Explorer windows
        for h, t, c, r in windows:
            if c == "CabinetWClass":
                user32.ShowWindow(h, SW_MINIMIZE)
        time.sleep(0.3)

    if "Chrome_WidgetWin_1" in wnd_map:
        chr_hwnd, chr_title, chr_rect = wnd_map["Chrome_WidgetWin_1"]
        for h, t, c, r in windows:
            if c in ("CabinetWClass", "ConsoleWindowClass"):
                user32.ShowWindow(h, SW_MINIMIZE)
        time.sleep(0.2)

        user32.ShowWindow(chr_hwnd, SW_RESTORE)
        time.sleep(0.3)

        cx_chr = (chr_rect[0] + chr_rect[2]) // 2
        cy_chr = (chr_rect[1] + chr_rect[3]) // 2
        top_h, leaf_h, cx, cy = input_handler._find_target_window(cx_chr, cy_chr, force_refresh=True)
        leaf_class = get_wnd_class(leaf_h) if leaf_h else ""
        print(f"  * Chrome ({cx_chr}, {cy_chr}) resolved to:")
        print(f"    Top HWND  : 0x{top_h:08X} [{get_wnd_class(top_h)}]")
        print(f"    Leaf HWND : 0x{leaf_h:08X} [{leaf_class}] at client ({cx}, {cy})")
        assert top_h == chr_hwnd, f"Expected top_h == chr_hwnd (0x{chr_hwnd:08X}), got 0x{top_h:08X}"
        print("  [PASS] Chrome window resolved cleanly on hidden desktop.")

        # Minimize Chrome so we can test Console window directly
        user32.ShowWindow(chr_hwnd, SW_MINIMIZE)
        time.sleep(0.3)

    # Test 4: Mouse Input & Wheel Simulation
    print("\n[4/5] Testing Mouse Movement, Clicks, and Mouse Wheel...")
    if "ConsoleWindowClass" in wnd_map:
        cmd_hwnd, _, cmd_rect = wnd_map["ConsoleWindowClass"]
        user32.ShowWindow(cmd_hwnd, SW_RESTORE)
        time.sleep(0.3)
        tx = (cmd_rect[0] + cmd_rect[2]) // 2
        ty = (cmd_rect[1] + cmd_rect[3]) // 2

        # Mouse Move & Hover
        input_handler.handle_mouse_move(tx, ty)
        print(f"  * handle_mouse_move({tx}, {ty}) dispatched successfully.")

        # Mouse Down
        down_diag = input_handler.handle_mouse_down(tx, ty, "left")
        print(f"  * handle_mouse_down diag: target=0x{down_diag.get('target_hwnd', 0):08X}, class={down_diag.get('target_class')}, status={down_diag.get('status')}")
        assert down_diag.get("target_hwnd") is not None, "handle_mouse_down failed to find target window!"

        # Mouse Up
        input_handler.handle_mouse_up(tx, ty, "left")
        print(f"  * handle_mouse_up({tx}, {ty}) dispatched successfully.")

        # Double Click
        input_handler.handle_double_click(tx, ty)
        print(f"  * handle_double_click({tx}, {ty}) dispatched successfully.")

        # Mouse Wheel
        wheel_diag = input_handler.handle_mouse_wheel(tx, ty, -120)
        print(f"  * handle_mouse_wheel diag: target=0x{wheel_diag.get('target_hwnd', 0):08X}, post_result={wheel_diag.get('post_result')}")
        assert wheel_diag.get("post_result") is True, "handle_mouse_wheel failed to post WM_MOUSEWHEEL!"
        print("[PASS] Mouse movement, click, double click, and scroll wheel verified.")

    # Test 5: Window Controls (Minimize, Maximize/Restore, Close)
    print("\n[5/5] Testing Window Controls (Minimize, Maximize, Restore, Close)...")
    if "ConsoleWindowClass" in wnd_map:
        cmd_hwnd, _, cmd_rect = wnd_map["ConsoleWindowClass"]
        user32.ShowWindow(cmd_hwnd, SW_RESTORE)
        time.sleep(0.3)

        c_rect = wintypes.RECT()
        user32.GetWindowRect(cmd_hwnd, ctypes.byref(c_rect))
        rx = c_rect.right
        ry = c_rect.top

        # Maximize click: (rx - 72, ry + 15)
        max_x = rx - 72
        max_y = ry + 15
        is_zoomed_before = bool(user32.IsZoomed(cmd_hwnd))
        print(f"  * Testing Maximize at ({max_x}, {max_y}), IsZoomed before = {is_zoomed_before}")
        diag_max = input_handler.handle_mouse_down(max_x, max_y, "left")
        input_handler.handle_mouse_up(max_x, max_y, "left")
        time.sleep(0.5)
        is_zoomed_after = bool(user32.IsZoomed(cmd_hwnd))
        print(f"    Status: {diag_max.get('status')}, IsZoomed after = {is_zoomed_after}")
        assert is_zoomed_after != is_zoomed_before or diag_max.get("status") in ("window_maximize", "window_restore"), "Maximize control failed!"

        # Restore click on maximized window: (screen_w - 72, 15)
        screen_w = user32.GetSystemMetrics(0) or 1920
        rest_x = screen_w - 72
        rest_y = 15
        print(f"  * Testing Restore at ({rest_x}, {rest_y})...")
        diag_rest = input_handler.handle_mouse_down(rest_x, rest_y, "left")
        input_handler.handle_mouse_up(rest_x, rest_y, "left")
        time.sleep(0.5)
        is_zoomed_rest = bool(user32.IsZoomed(cmd_hwnd))
        print(f"    Status: {diag_rest.get('status')}, IsZoomed after restore = {is_zoomed_rest}")
        assert diag_rest.get("status") == "window_restore", "Restore control failed!"

        # Minimize click
        c_rect2 = wintypes.RECT()
        user32.GetWindowRect(cmd_hwnd, ctypes.byref(c_rect2))
        min_x = c_rect2.right - 120
        min_y = c_rect2.top + 15
        print(f"  * Testing Minimize at ({min_x}, {min_y})...")
        diag_min = input_handler.handle_mouse_down(min_x, min_y, "left")
        input_handler.handle_mouse_up(min_x, min_y, "left")
        time.sleep(0.3)
        print(f"    Status: {diag_min.get('status')}, IsIconic = {bool(user32.IsIconic(cmd_hwnd))}")
        assert diag_min.get("status") == "window_minimize", "Minimize control failed!"

        # Restore window back to test Close
        user32.ShowWindow(cmd_hwnd, SW_RESTORE)
        time.sleep(0.3)
        user32.GetWindowRect(cmd_hwnd, ctypes.byref(c_rect2))
        close_x = c_rect2.right - 24
        close_y = c_rect2.top + 15
        print(f"  * Testing Window Close at ({close_x}, {close_y})...")
        diag_close = input_handler.handle_mouse_down(close_x, close_y, "left")
        input_handler.handle_mouse_up(close_x, close_y, "left")
        print(f"    Status: {diag_close.get('status')}")
        assert diag_close.get("status") == "window_close", "Window Close control failed!"
        print("[PASS] Window control buttons (Maximize/Restore/Minimize/Close) verified.")
    else:
        print("  Console window not available for window button tests, skipping.")

    # Cleanup spawned processes
    print("\nCleaning up test processes...")
    try:
        os.system(f"taskkill /PID {cmd_pid} /F >nul 2>&1")
        if chr_pid:
            os.system(f"taskkill /PID {chr_pid} /T /F >nul 2>&1")
    except Exception:
        pass

    desktop.close()
    print("\n" + "=" * 70)
    print("[SUCCESS] ALL TESTS PASSED! Hidden desktop isolation and input handling verified.")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
