@echo off
REM Builds a single-file Windows executable at dist\mpart.exe
python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name mpart ^
    --hidden-import pystray._win32 main.py
