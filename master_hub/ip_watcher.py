import time
import json
import os
import subprocess
from pathlib import Path

from .utils.aws_meta import get_public_ip

CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config():
    if CONFIG_PATH.is_file():
        try:
            import yaml
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
    return {}

def save_config(data):
    try:
        import yaml
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
    except Exception:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def restart_service():
    subprocess.run(["systemctl", "restart", "trmm"], capture_output=True)

def watch_ip(interval=60):
    cfg = load_config()
    last_ip = cfg.get("public_ip")
    while True:
        ip = get_public_ip()
        if ip and ip != last_ip:
            cfg["public_ip"] = ip
            save_config(cfg)
            restart_service()
            last_ip = ip
        time.sleep(interval)

if __name__ == "__main__":
    watch_ip()
