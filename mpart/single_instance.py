"""Keep one running copy of mpart and forward new launches to it.

Launching mpart again (e.g. by dropping files onto mpart.exe or a shortcut)
sends the files to the copy that is already running instead of opening a
second window and tray icon.
"""

import getpass
import json
import os
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener

_AUTHKEY = b"mpart-single-instance"

if sys.platform == "win32":
    _FAMILY = "AF_PIPE"
    _ADDRESS = rf"\\.\pipe\mpart-{getpass.getuser()}"
else:
    _FAMILY = "AF_UNIX"
    _ADDRESS = os.path.join(tempfile.gettempdir(), f"mpart-{os.getuid()}.sock")


def forward_to_running(files):
    """Send ``files`` to a running instance. Returns False if none is running."""
    try:
        with Client(_ADDRESS, _FAMILY, authkey=_AUTHKEY) as conn:
            conn.send_bytes(json.dumps({"files": files}).encode("utf-8"))
        return True
    except (OSError, EOFError):
        return False


def serve(on_files):
    """Listen for later launches, calling ``on_files(files)`` from a background thread."""
    if _FAMILY == "AF_UNIX" and os.path.exists(_ADDRESS):
        os.unlink(_ADDRESS)  # stale socket from a crashed run; nobody answered above
    try:
        listener = Listener(_ADDRESS, _FAMILY, authkey=_AUTHKEY)
    except OSError:
        return

    def loop():
        while True:
            try:
                with listener.accept() as conn:
                    msg = json.loads(conn.recv_bytes(1 << 20))
                files = msg.get("files", [])
                if isinstance(files, list):
                    on_files([f for f in files if isinstance(f, str)])
            except Exception:
                continue  # ignore a bad or dropped connection

    threading.Thread(target=loop, daemon=True).start()
