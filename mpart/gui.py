"""Small Tk GUI for mpart that minimizes to the system tray."""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray
from PIL import Image, ImageDraw, ImageTk

from . import core

APP_NAME = "mpart"
# Decimal units: services quoting "2 GB" limits are safe with 2 * 10**9 bytes.
UNITS = {"MB": 10**6, "GB": 10**9}


def make_icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((4 * s, 4 * s, 60 * s, 60 * s), radius=10 * s, fill=(37, 99, 235))
    # Three stacked "parts" of a file.
    for i in range(3):
        top = (14 + i * 14) * s
        d.rounded_rectangle((14 * s, top, 50 * s, top + 9 * s), radius=2 * s, fill="white")
    return img


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self._last_pct = -1

        root.title(APP_NAME)
        root.resizable(False, False)
        icon_img = make_icon_image()
        self._tk_icon = ImageTk.PhotoImage(icon_img)
        root.iconphoto(True, self._tk_icon)

        self._build_ui()

        self.tray = pystray.Icon(
            APP_NAME,
            icon_img,
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Show", lambda: self.events.put(("show",)), default=True),
                pystray.MenuItem("Quit", lambda: self.events.put(("quit",))),
            ),
        )
        self.tray.run_detached()

        root.bind("<Unmap>", self._on_unmap)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(100, self._poll)

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        frame = ttk.Frame(self.root, padding=10)
        frame.grid(sticky="nsew")

        self.notebook = ttk.Notebook(frame)
        self.notebook.grid(row=0, column=0, columnspan=3, sticky="ew")

        # Split tab
        split = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(split, text="Split")
        self.split_src = tk.StringVar()
        self.split_out = tk.StringVar()
        self.split_size = tk.StringVar(value="2")
        self.split_unit = tk.StringVar(value="GB")

        ttk.Label(split, text="File:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(split, textvariable=self.split_src, width=50).grid(row=0, column=1, columnspan=2, **pad)
        ttk.Button(split, text="Browse…", command=self._pick_split_src).grid(row=0, column=3, **pad)

        ttk.Label(split, text="Part size:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(split, textvariable=self.split_size, width=10).grid(row=1, column=1, sticky="w", **pad)
        ttk.Combobox(split, textvariable=self.split_unit, values=list(UNITS), width=5, state="readonly").grid(
            row=1, column=2, sticky="w", **pad
        )

        ttk.Label(split, text="Output folder:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(split, textvariable=self.split_out, width=50).grid(row=2, column=1, columnspan=2, **pad)
        ttk.Button(split, text="Browse…", command=lambda: self._pick_dir(self.split_out)).grid(row=2, column=3, **pad)

        self.split_btn = ttk.Button(split, text="Split", command=self._start_split)
        self.split_btn.grid(row=3, column=3, sticky="e", **pad)

        # Join tab
        join = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(join, text="Join")
        self.join_src = tk.StringVar()
        self.join_out = tk.StringVar()

        ttk.Label(join, text="Manifest or part:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(join, textvariable=self.join_src, width=50).grid(row=0, column=1, **pad)
        ttk.Button(join, text="Browse…", command=self._pick_join_src).grid(row=0, column=2, **pad)

        ttk.Label(join, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(join, textvariable=self.join_out, width=50).grid(row=1, column=1, **pad)
        ttk.Button(join, text="Browse…", command=lambda: self._pick_dir(self.join_out)).grid(row=1, column=2, **pad)

        self.join_btn = ttk.Button(join, text="Join", command=self._start_join)
        self.join_btn.grid(row=2, column=2, sticky="e", **pad)

        # Shared progress area
        self.progress = ttk.Progressbar(frame, maximum=1000, length=420)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        self.cancel_btn = ttk.Button(frame, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.grid(row=1, column=2, sticky="e", padx=(6, 0), pady=(10, 2))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status).grid(row=2, column=0, columnspan=3, sticky="w")

    def _pick_split_src(self):
        path = filedialog.askopenfilename(title="Choose a file to split")
        if path:
            self.split_src.set(path)
            if not self.split_out.get():
                self.split_out.set(str(Path(path).parent))

    def _pick_join_src(self):
        path = filedialog.askopenfilename(
            title="Choose the manifest or any part",
            filetypes=[("mpart files", "*.mpart.json *.part*"), ("All files", "*.*")],
        )
        if path:
            self.join_src.set(path)
            if not self.join_out.get():
                self.join_out.set(str(Path(path).parent))

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            var.set(path)

    # ---- Jobs -------------------------------------------------------------

    def _start_split(self):
        src = Path(self.split_src.get().strip())
        if not src.is_file():
            messagebox.showerror(APP_NAME, "Choose a file to split.")
            return
        try:
            part_size = int(float(self.split_size.get()) * UNITS[self.split_unit.get()])
            if part_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Part size must be a positive number.")
            return
        out_dir = Path(self.split_out.get().strip() or src.parent)
        if core.manifest_path_for(src, out_dir).exists() and not messagebox.askyesno(
            APP_NAME, "Parts for this file already exist in the output folder. Overwrite them?"
        ):
            return
        count = core.part_count(src.stat().st_size, part_size)
        self._run(
            f"Splitting {src.name} into {count} part(s)…",
            lambda p, c: core.split_file(src, out_dir, part_size, p, c),
            lambda mpath: f"Split into {count} part(s) in {mpath.parent}.\n"
            f"Send all parts plus {mpath.name}.",
        )

    def _start_join(self):
        src = self.join_src.get().strip()
        if not src:
            messagebox.showerror(APP_NAME, "Choose the manifest or any part file.")
            return
        try:
            manifest = core.find_manifest(src)
            out_dir = self.join_out.get().strip() or None
            out_path = core.output_path_for(manifest, out_dir)
        except core.MpartError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        if out_path.exists() and not messagebox.askyesno(APP_NAME, f"{out_path.name} already exists. Overwrite it?"):
            return
        self._run(
            f"Joining {out_path.name}…",
            lambda p, c: core.join_file(manifest, out_dir, p, c),
            lambda path: f"Rebuilt and verified {path}.",
        )

    def _run(self, status, job, describe):
        self.cancel.clear()
        self._set_busy(True)
        self.status.set(status)
        self.progress["value"] = 0
        self._last_pct = -1

        def progress(done, total):
            self.events.put(("progress", done, total))

        def target():
            try:
                result = job(progress, self.cancel)
                self.events.put(("done", describe(result)))
            except core.Cancelled:
                self.events.put(("cancelled",))
            except core.MpartError as e:
                self.events.put(("error", str(e)))
            except OSError as e:
                self.events.put(("error", f"{e.strerror or e}: {e.filename or ''}".strip(": ")))
            except Exception as e:  # keep the GUI alive on unexpected errors
                self.events.put(("error", f"Unexpected error: {e!r}"))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.split_btn["state"] = state
        self.join_btn["state"] = state
        self.cancel_btn["state"] = "normal" if busy else "disabled"

    def _finish(self, title, message, error=False):
        self._set_busy(False)
        self.tray.title = APP_NAME
        self.status.set(message.splitlines()[0])
        if self.root.state() == "withdrawn":
            self.tray.notify(message, title)
        elif error:
            messagebox.showerror(APP_NAME, message)
        else:
            messagebox.showinfo(APP_NAME, message)

    # ---- Event loop / tray ------------------------------------------------

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    _, done, total = ev
                    frac = done / total if total else 1.0
                    self.progress["value"] = frac * 1000
                    pct = int(frac * 100)
                    if pct != self._last_pct:
                        self._last_pct = pct
                        self.tray.title = f"{APP_NAME} – {pct}%"
                elif kind == "done":
                    self.progress["value"] = 1000
                    self._finish("Done", ev[1])
                elif kind == "cancelled":
                    self.progress["value"] = 0
                    self._finish("Cancelled", "Cancelled. Partial output was removed.")
                elif kind == "error":
                    self._finish("Error", ev[1], error=True)
                elif kind == "show":
                    self.show()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_unmap(self, event):
        # Minimizing the window hides it to the tray instead of the taskbar.
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        if self.worker and self.worker.is_alive():
            self.show()
            if not messagebox.askyesno(APP_NAME, "A job is still running. Cancel it and quit?"):
                return
            self.cancel.set()
            self.worker.join(timeout=10)
        self.tray.stop()
        self.root.destroy()


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def main():
    _enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
