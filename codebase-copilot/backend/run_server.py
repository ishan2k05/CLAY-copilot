#!/usr/bin/env python3
"""
CLAY Copilot - Standalone Server Runner
Starts the full FastAPI backend programmatically with Uvicorn.
Used as the compilation entrypoint for PyInstaller.
"""
import sys
import os
import argparse
import multiprocessing

# Critical on Windows: Prevents PyInstaller spawned worker infinite fork loops
if sys.platform == "win32":
    multiprocessing.freeze_support()

# Ensure frozen bundle paths are in sys.path
if getattr(sys, "frozen", False):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
else:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

import uvicorn

def main():
    parser = argparse.ArgumentParser(description="CLAY Copilot Backend Server")
    parser.add_argument("--host", default=os.environ.get("CLAY_HOST", "127.0.0.1"), help="Host to bind")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLAY_PORT", "8765")), help="Port to bind")
    parser.add_argument("--log-level", default="info", help="Logging level")
    args, unknown = parser.parse_known_args()

    print(f"[CLAY Runner] Starting CLAY Copilot FastAPI Server on http://{args.host}:{args.port}")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        workers=1,
        reload=False
    )

if __name__ == "__main__":
    main()
