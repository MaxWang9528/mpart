# mpart

Split large files into smaller parts for services with upload size limits, then
rebuild the original on the other side.

## Usage

- **Split:** pick a file, set a part size (e.g. `2 GB`), pick an output folder.
  You get `name.part001`, `name.part002`, ... plus `name.mpart.json`.
  Upload all of them.
- **Join:** pick the `.mpart.json` manifest, or any part file. Every part is
  checked against its SHA-256 before the original file is restored.

Sizes are decimal (1 GB = 1,000,000,000 bytes), which keeps parts under limits
quoted as either GB or GiB.

Minimizing the window hides it to the system tray. Click the tray icon to bring
it back. Closing the window quits the app.

Parts are plain byte slices, so they can also be joined without mpart, e.g.
`copy /b name.part001+name.part002 name` on Windows or `cat name.part* > name`
on Linux and macOS.

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
