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
- **Split / Join tabs:** the same operations with a folder chosen each time.
  Manual joins keep the parts.
- **Settings:** the Quick Split and Quick Join folders (empty means the folder
  mpart is in) and the part size. Settings are saved to
  `%APPDATA%\mpart\settings.json`.

Quick Split and Quick Join are also in the tray icon's right-click menu.
Minimizing the window hides it to the tray. Closing the window quits.

Windows doesn't support dropping files onto tray icons. Instead, drop files onto
`mpart.exe` or a shortcut to it (e.g. on the desktop): they are handed to the
running mpart, or mpart starts and splits them.

## Development

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Build a standalone executable

```
build.bat
```

This produces `dist\mpart.exe`, a single file with no Python install needed.
