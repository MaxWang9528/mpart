#!/bin/sh
# Builds a macOS app bundle at dist/mpart.app
set -e
python3 -m pip install -r requirements-build.txt
python3 -m PyInstaller --noconfirm --windowed --name mpart \
    --hidden-import pystray._darwin main.py
