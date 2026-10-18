# mpart

Split large files into smaller parts for services with upload size limits, then
rebuild the original on the other side.

## Parts

Splitting `movie.mkv` produces `movie.mkv.001.mpart`, `movie.mkv.002.mpart`, ...
Nothing else is needed: each part starts with a small header recording which
file it belongs to, its position, and a SHA-256 checksum. On join, mpart checks
that no part is missing, truncated or corrupted before restoring the original.
Parts are matched by their headers, so they still join if a download renames
them.

Each part, header included, is at most the chosen part size. Sizes are decimal
(1 GB = 1,000,000,000 bytes), which keeps parts under limits quoted as either GB
or GiB.

## Usage

- **Quick Split:** choose file(s) and they are split into the Quick Split
  folder. Dropping files anywhere on the window does the same.
- **Quick Join:** rebuilds every complete set of parts in the Quick Join folder
  into that same folder, then deletes the parts. Incomplete sets are reported
  and left alone.
- **Show file after quick splitting / joining:** when ticked, the file manager
  opens with the result selected (the first part, or the first rebuilt file).
- **Split / Join tabs:** the same operations with a folder chosen each time.
  Manual joins keep the parts.
- **Settings:** the Quick Split and Quick Join folders (empty means the folder
  mpart is in) and the part size. These and the checkboxes are saved to
  `%APPDATA%\mpart\settings.json` on Windows and
  `~/Library/Application Support/mpart/settings.json` on macOS.

mpart never opens message boxes. Results and errors appear in the log at the
bottom of the window, and questions (such as replacing existing parts) appear
above the progress bar with buttons to answer them.

Quick Split and Quick Join are also in the tray icon's menu (the menu bar on
macOS). Minimizing the window hides it to the tray; hover over the tray icon to
see the last result. Closing the window quits.

Tray icons can't accept dropped files. On Windows, drop files onto `mpart.exe`
or a shortcut to it (e.g. on the desktop) instead: they are handed to the
running mpart, or mpart starts and splits them. On macOS, drop files onto the
window.

## Development

Windows:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

macOS:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Build a standalone app

PyInstaller can only build for the system it runs on, so build the Windows exe
on Windows and the macOS app on a Mac.

### Windows

From the project folder:

```
.venv\Scripts\activate
build.bat
```

This produces `dist\mpart.exe`, a single file with no Python install needed.
Windows SmartScreen may warn about an unsigned exe the first time it runs; click
"More info" → "Run anyway".

### macOS

1. Install Python 3.10 or newer with Tk. The installer from
   [python.org](https://www.python.org/downloads/macos/) includes Tk. With
   Homebrew, run `brew install python python-tk`.
2. From the project folder, create the virtual environment (see Development
   above), then:

   ```
   source .venv/bin/activate
   ./build.sh
   ```

This produces `dist/mpart.app`. Drag it into Applications or anywhere else.
Leaving the Quick folders empty in Settings means the folder that contains
`mpart.app`, so set them to something like `~/Downloads` if the app lives in
Applications.

The app only runs on the same kind of Mac it was built on (Apple Silicon or
Intel). It isn't signed, so on another Mac it opens with a warning: right-click
it and choose Open, or run `xattr -dr com.apple.quarantine mpart.app` once.
