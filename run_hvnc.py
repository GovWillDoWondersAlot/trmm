"""
Main entry point for running the Tactical RMM Python HVNC Server.

Usage:
    python run_hvnc.py [--host 0.0.0.0] [--port 8899] [--width 1280] [--height 720]
"""

import argparse
import sys
import uvicorn
import webbrowser
import threading
import time

def main():
    parser = argparse.ArgumentParser(description="TRMM Python HVNC (Hidden Virtual Workspace) Server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8899, help="Port to listen on (default: 8899)")
    parser.add_argument("--open-browser", action="store_true", help="Automatically open browser viewer on startup")
    args = parser.parse_args()

    print("=" * 65)
    print(" 🚀 Tactical RMM — Python Hidden Virtual Workspace (HVNC) Server")
    print(f" 🌐 Dashboard & Live Viewer : http://{args.host}:{args.port}")
    print(" 🛡️  Mode                   : Fully Isolated Win32 Desktop Session")
    print("=" * 65)

    if args.open_browser:
        def open_tab():
            time.sleep(1.5)
            webbrowser.open(f"http://{args.host}:{args.port}")
        threading.Thread(target=open_tab, daemon=True).start()

    uvicorn.run("hvnc.server:app", host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
