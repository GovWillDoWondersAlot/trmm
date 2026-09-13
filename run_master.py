"""
Tactical RMM — Master Admin Hub Launcher.

Starts the central Web Dashboard, Agent Generator, and Reverse WebSocket Hub.

Usage:
    python run_master.py [--host 0.0.0.0] [--port 8000] [--open-browser]
"""

import argparse
import sys
import os
import uvicorn
import webbrowser
import threading
import time

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

def main():
    parser = argparse.ArgumentParser(description="Tactical RMM Master Hub Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host IP to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--open-browser", action="store_true", help="Auto open browser to dashboard")
    args = parser.parse_args()

    # Ensure UTF-8 output encoding on Windows console
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    print("=" * 70)
    print(" [*] Tactical RMM -- Master Admin Management & Reverse HVNC Hub")
    print(f" [*] Master Dashboard URL : http://localhost:{args.port} (or http://{args.host}:{args.port})")
    print(f" [*] Agent WebSocket Hub  : ws://localhost:{args.port}/ws/agent/<agent_id>")
    print("=" * 70)

    if args.open_browser:
        def open_tab():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{args.port}")
        threading.Thread(target=open_tab, daemon=True).start()

    uvicorn.run("master_hub.app:app", host=args.host, port=args.port, log_level="info", reload=True, reload_excludes=["build/*", "dist/*", "generated_agents/*", "scratch/*", "*.zip", "*.exe", "*.bat", "*.spec", "*.log"])

if __name__ == "__main__":
    main()
