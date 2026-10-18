"""Small Tk GUI for mpart that minimizes to the system tray."""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import pystray
from PIL import Image, ImageDraw, ImageTk

from . import core, settings, single_instance

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


def _try_unlink(path):
    try:
        path.unlink()
        return True
    except OSError:
        return False


def reveal(path):
    """Open the system file manager with ``path`` selected."""
    if sys.platform == "win32":
        # Explorer needs the path glued to "/select,"; Windows paths can't contain quotes.
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


class App:
    def __init__(self, root, dnd_files_type=None):
        self.root = root
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self._last_pct = -1
        self._save_job = None

        root.title(APP_NAME)
        root.resizable(False, False)
        icon_img = make_icon_image()
        self._tk_icon = ImageTk.PhotoImage(icon_img)
        root.iconphoto(True, self._tk_icon)

        saved = settings.load()
        self.quick_split_dir = tk.StringVar(value=saved["quick_split_dir"])
        self.quick_join_dir = tk.StringVar(value=saved["quick_join_dir"])
        self.part_size = tk.StringVar(value=saved["part_size"])
        self.part_unit = tk.StringVar(value=saved["part_unit"] if saved["part_unit"] in UNITS else "GB")
        self.show_after_split = tk.BooleanVar(value=saved["show_after_split"])
        self.show_after_join = tk.BooleanVar(value=saved["show_after_join"])
        self._on_yes = None

        self._build_ui()
        for var in (
            self.quick_split_dir,
            self.quick_join_dir,
            self.part_size,
            self.part_unit,
            self.show_after_split,
            self.show_after_join,
        ):
            var.trace_add("write", lambda *_: self._schedule_save())
        if dnd_files_type:
            # The plain Tk root lacks the tkdnd methods, but its frame fills the window.
            for child in root.winfo_children():
                self._register_drop(child, dnd_files_type)

        self.tray = pystray.Icon(
            APP_NAME,
            icon_img,
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Show", lambda: self.events.put(("show",)), default=True),
                pystray.MenuItem("Quick Split…", lambda: self.events.put(("quick_split",))),
                pystray.MenuItem("Quick Join", lambda: self.events.put(("quick_join",))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self.events.put(("quit",))),
            ),
        )
        self.tray.run_detached()

        root.bind("<Unmap>", self._on_unmap)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if sys.platform == "darwin":
            # Dock icon click and Cmd+Q / Dock "Quit".
            root.createcommand("::tk::mac::ReopenApplication", self.show)
            root.createcommand("::tk::mac::Quit", self.quit)
        root.after(100, self._poll)

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        frame = ttk.Frame(self.root, padding=10)
        frame.grid(sticky="nsew")

        self.notebook = ttk.Notebook(frame)
        self.notebook.grid(row=0, column=0, columnspan=3, sticky="ew")

        # Quick tab
        quick = ttk.Frame(self.notebook, padding=8)
        quick.columnconfigure(0, weight=1)
        quick.columnconfigure(1, weight=1)
        self.notebook.add(quick, text="Quick")
        ttk.Label(
            quick,
            text="Drop files anywhere on this window to split them",
            relief="groove",
            anchor="center",
            padding=24,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.quick_split_btn = ttk.Button(quick, text="Quick Split…", command=self.quick_split)
        self.quick_split_btn.grid(row=1, column=0, sticky="ew", **pad)
        self.quick_join_btn = ttk.Button(quick, text="Quick Join", command=self.quick_join)
        self.quick_join_btn.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Checkbutton(quick, text="Show file after quick splitting", variable=self.show_after_split).grid(
            row=2, column=0, sticky="w", **pad
        )
        ttk.Checkbutton(quick, text="Show file after quick joining", variable=self.show_after_join).grid(
            row=2, column=1, sticky="w", **pad
        )
        self.quick_split_hint = tk.StringVar()
        self.quick_join_hint = tk.StringVar()
        ttk.Label(quick, textvariable=self.quick_split_hint, wraplength=230, foreground="gray").grid(
            row=3, column=0, sticky="nw", **pad
        )
        ttk.Label(quick, textvariable=self.quick_join_hint, wraplength=230, foreground="gray").grid(
            row=3, column=1, sticky="nw", **pad
        )
        self.quick_split_dir.trace_add("write", lambda *_: self._update_hints())
        self.quick_join_dir.trace_add("write", lambda *_: self._update_hints())
        self._update_hints()

        # Split tab
        split = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(split, text="Split")
        self.split_src = tk.StringVar()
        self.split_out = tk.StringVar()

        ttk.Label(split, text="File:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(split, textvariable=self.split_src, width=50).grid(row=0, column=1, columnspan=2, **pad)
        ttk.Button(split, text="Browse…", command=self._pick_split_src).grid(row=0, column=3, **pad)

        ttk.Label(split, text="Part size:").grid(row=1, column=0, sticky="w", **pad)
        self._part_size_widgets(split).grid(row=1, column=1, sticky="w", **pad)

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

        ttk.Label(join, text="Any part:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(join, textvariable=self.join_src, width=50).grid(row=0, column=1, **pad)
        ttk.Button(join, text="Browse…", command=self._pick_join_src).grid(row=0, column=2, **pad)

        ttk.Label(join, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(join, textvariable=self.join_out, width=50).grid(row=1, column=1, **pad)
        ttk.Button(join, text="Browse…", command=lambda: self._pick_dir(self.join_out)).grid(row=1, column=2, **pad)

        self.join_btn = ttk.Button(join, text="Join", command=self._start_join)
        self.join_btn.grid(row=2, column=2, sticky="e", **pad)

        # Settings tab
        prefs = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(prefs, text="Settings")

        ttk.Label(prefs, text="Quick Split folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(prefs, textvariable=self.quick_split_dir, width=44).grid(row=0, column=1, **pad)
        ttk.Button(prefs, text="Browse…", command=lambda: self._pick_dir(self.quick_split_dir)).grid(
            row=0, column=2, **pad
        )

        ttk.Label(prefs, text="Quick Join folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(prefs, textvariable=self.quick_join_dir, width=44).grid(row=1, column=1, **pad)
        ttk.Button(prefs, text="Browse…", command=lambda: self._pick_dir(self.quick_join_dir)).grid(
            row=1, column=2, **pad
        )

        ttk.Label(prefs, text="Part size:").grid(row=2, column=0, sticky="w", **pad)
        self._part_size_widgets(prefs).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(
            prefs,
            text=f"Leave a folder empty to use the app's folder:\n{settings.app_dir()}",
            foreground="gray",
        ).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        # Questions are asked in this bar, shown only while one is pending.
        self.ask_bar = ttk.Frame(frame)
        self.ask_bar.columnconfigure(0, weight=1)
        self.ask_text = tk.StringVar()
        ttk.Label(self.ask_bar, textvariable=self.ask_text, wraplength=380).grid(row=0, column=0, sticky="w")
        self.ask_yes_btn = ttk.Button(self.ask_bar, command=self._answer_yes)
        self.ask_yes_btn.grid(row=0, column=1, padx=(6, 0))
        ttk.Button(self.ask_bar, text="No", command=self._dismiss_ask).grid(row=0, column=2, padx=(6, 0))
        self.ask_bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.ask_bar.grid_remove()

        # Shared progress area
        self.progress = ttk.Progressbar(frame, maximum=1000, length=420)
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        self.cancel_btn = ttk.Button(frame, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(10, 2))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status).grid(row=3, column=0, columnspan=3, sticky="w")

        # Results and errors, newest at the bottom
        log_box = ttk.Frame(frame)
        log_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        log_box.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_box,
            width=1,  # take the window's width instead of widening it
            height=6,
            wrap="word",
            font="TkDefaultFont",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            state="disabled",
            takefocus=False,
        )
        self.log.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(log_box, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = scroll.set
        self.log.tag_configure("error", foreground="#b91c1c")

    def _part_size_widgets(self, parent):
        # Shared variables keep the Split tab and Settings tab in sync.
        box = ttk.Frame(parent)
        ttk.Entry(box, textvariable=self.part_size, width=10).pack(side="left")
        ttk.Combobox(box, textvariable=self.part_unit, values=list(UNITS), width=5, state="readonly").pack(
            side="left", padx=(6, 0)
        )
        return box

    def _update_hints(self):
        self.quick_split_hint.set(f"Pick files to split into:\n{self._dir(self.quick_split_dir)}")
        self.quick_join_hint.set(f"Join all parts in, then delete them:\n{self._dir(self.quick_join_dir)}")

    def _register_drop(self, widget, dnd_files_type):
        widget.drop_target_register(dnd_files_type)
        widget.dnd_bind("<<Drop>>", self._on_drop)
        for child in widget.winfo_children():
            self._register_drop(child, dnd_files_type)

    def _on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        # Defer so the drop returns to Explorer right away.
        self.root.after(10, lambda: self.quick_split_files(files))
        return event.action

    def _pick_split_src(self):
        path = filedialog.askopenfilename(title="Choose a file to split")
        if path:
            self.split_src.set(path)
            if not self.split_out.get():
                self.split_out.set(str(Path(path).parent))

    def _pick_join_src(self):
        path = filedialog.askopenfilename(
            title="Choose any part",
            filetypes=[("mpart parts", f"*{core.EXT}"), ("All files", "*.*")],
        )
        if path:
            self.join_src.set(path)
            if not self.join_out.get():
                self.join_out.set(str(Path(path).parent))

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="Choose a folder")
        if path:
            var.set(path)

    # ---- Settings ---------------------------------------------------------

    def _dir(self, var):
        return Path(var.get().strip() or settings.app_dir())

    def _part_size(self):
        try:
            size = int(float(self.part_size.get()) * UNITS[self.part_unit.get()])
        except (ValueError, OverflowError, KeyError):
            size = 0
        if size < core.MIN_PART_SIZE:
            self._log(f"Part size must be at least {core.format_size(core.MIN_PART_SIZE)}.", error=True)
            return None
        return size

    def _schedule_save(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, self._save_settings)

    def _save_settings(self):
        self._save_job = None
        settings.save(
            {
                "quick_split_dir": self.quick_split_dir.get().strip(),
                "quick_join_dir": self.quick_join_dir.get().strip(),
                "part_size": self.part_size.get().strip(),
                "part_unit": self.part_unit.get(),
                "show_after_split": self.show_after_split.get(),
                "show_after_join": self.show_after_join.get(),
            }
        )

    # ---- Jobs -------------------------------------------------------------

    def quick_split(self):
        if self._busy():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="Quick Split: choose file(s)")
        if paths:
            self.quick_split_files(paths)

    def quick_split_files(self, paths):
        self._split_files(paths, self._dir(self.quick_split_dir), self.show_after_split.get())

    def _start_split(self):
        src = self.split_src.get().strip()
        if not src:
            self._log("Choose a file to split.", error=True)
            return
        out = self.split_out.get().strip() or str(Path(src).parent)
        self._split_files([src], Path(out))

    def _split_files(self, paths, out_dir, show=False, confirmed=False):
        if self._busy():
            return
        files = [Path(p) for p in paths]
        bad = [p.name for p in files if not p.is_file() or p.suffix.lower() == core.EXT]
        if bad:
            self._log("These can't be split (folders, missing files or existing parts):\n" + "\n".join(bad), True)
            return
        part_size = self._part_size()
        if part_size is None:
            return
        stale = [old for f in files for old in core.existing_parts(f.name, out_dir)]
        if stale and not confirmed:
            self._ask(
                f"{len(stale)} existing part file(s) for the same name(s) are already in {out_dir}. Replace them?",
                "Replace",
                lambda: self._split_files(paths, out_dir, show, confirmed=True),
            )
            return

        def job(progress, status, cancel):
            for p in stale:
                p.unlink(missing_ok=True)
            total = sum(f.stat().st_size for f in files)
            base = 0
            lines = []
            first = None
            for i, f in enumerate(files, 1):
                size = f.stat().st_size
                status(f"Splitting {f.name} ({i}/{len(files)})…")
                parts = core.split_file(f, out_dir, part_size, lambda d, t: progress(base + d, total), cancel)
                first = first or parts[0]
                base += size
                lines.append(f"{f.name} → {len(parts)} part(s)")
            message = f"Split {len(files)} file(s) into {out_dir}\n" + "\n".join(lines)
            return message, False, first if show else None

        self._run(job)

    def quick_join(self):
        if self._busy():
            return
        folder = self._dir(self.quick_join_dir)
        show = self.show_after_join.get()

        def job(progress, status, cancel):
            status(f"Looking for parts in {folder}…")
            sets, problems = core.find_sets(folder)
            if not sets and not problems:
                return f"No {core.EXT} files found in {folder}.", True, None
            complete = [s for s in sets if s.complete]
            total = sum(s.size for s in complete)
            base = 0
            joined = []
            lines = []
            for i, s in enumerate(complete, 1):
                status(f"Joining {s.name} ({i}/{len(complete)})…")
                try:
                    out = core.join_set(
                        s, core.unique_path(folder / s.name), lambda d, t: progress(base + d, total), cancel
                    )
                except core.MpartError as e:
                    lines.append(f"✗ {e}")
                else:
                    joined.append(out)
                    kept = [p.name for p in s.paths if not _try_unlink(p)]
                    lines.append(f"✓ {out.name}" + (f" (couldn't delete {len(kept)} part(s))" if kept else ""))
                base += s.size
            lines += ["✗ " + s.describe_missing() for s in sets if not s.complete]
            lines += ["✗ " + p for p in problems]
            ok = len(joined) == len(sets) and not problems
            message = f"Joined {len(joined)} file(s) in {folder}\n" + "\n".join(lines)
            return message, not ok, joined[0] if show and joined else None

        self._run(job)

    def _start_join(self, confirmed=False):
        if self._busy():
            return
        src = self.join_src.get().strip()
        if not src:
            self._log("Choose any part of the file to join.", error=True)
            return
        try:
            part_set = core.find_set_for(src)
            if not part_set.complete:
                raise core.MpartError(part_set.describe_missing())
        except core.MpartError as e:
            self._log(str(e), error=True)
            return
        out_path = Path(self.join_out.get().strip() or Path(src).parent) / part_set.name
        if out_path.exists() and not confirmed:
            self._ask(f"{out_path.name} already exists. Overwrite it?", "Overwrite", lambda: self._start_join(True))
            return

        def job(progress, status, cancel):
            status(f"Joining {part_set.name}…")
            path = core.join_set(part_set, out_path, progress, cancel)
            return f"Rebuilt and verified {path}", False, None

        self._run(job)

    def _busy(self):
        if self.worker and self.worker.is_alive():
            self._log("A job is already running. Wait for it to finish or cancel it.", error=True)
            return True
        return False

    def _run(self, job):
        """Run ``job(progress, status, cancel) -> (message, is_error, path_to_show)`` on a worker thread."""
        self._dismiss_ask()
        self.cancel.clear()
        self._set_busy(True)
        self.progress["value"] = 0
        self._last_pct = -1

        def progress(done, total):
            self.events.put(("progress", done, total))

        def status(text):
            self.events.put(("status", text))

        def target():
            try:
                self.events.put(("finished", *job(progress, status, self.cancel)))
            except core.Cancelled:
                self.events.put(("finished", "Cancelled. Partial output was removed.", False, None))
            except core.MpartError as e:
                self.events.put(("finished", str(e), True, None))
            except OSError as e:
                self.events.put(("finished", f"{e.strerror or e}: {e.filename or ''}".strip(": "), True, None))
            except Exception as e:  # keep the GUI alive on unexpected errors
                self.events.put(("finished", f"Unexpected error: {e!r}", True, None))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self.split_btn, self.join_btn, self.quick_split_btn, self.quick_join_btn):
            btn["state"] = state
        self.cancel_btn["state"] = "normal" if busy else "disabled"

    def _finish(self, message, error, show):
        self._set_busy(False)
        first_line = message.splitlines()[0]
        # The tray tooltip carries the result while the window is hidden.
        self.tray.title = f"{APP_NAME}: {first_line}"[:100]
        self.progress["value"] = 0 if error else 1000
        self.status.set("Ready.")
        self._log(message, error)
        if show:
            try:
                reveal(show)
            except OSError as e:
                self._log(f"Couldn't open the file manager: {e.strerror or e}", error=True)

    # ---- Messages ---------------------------------------------------------

    def _log(self, message, error=False):
        """Add a message to the log at the bottom of the window."""
        self.log["state"] = "normal"
        if self.log.index("end-1c") != "1.0":
            self.log.insert("end", "\n")
        self.log.insert("end", message, ("error",) if error else ())
        self.log["state"] = "disabled"
        self.log.see("end")

    def _ask(self, question, yes_text, on_yes):
        """Ask a yes/no question inside the window; ``on_yes`` runs if confirmed."""
        self.ask_text.set(question)
        self.ask_yes_btn["text"] = yes_text
        self._on_yes = on_yes
        self.ask_bar.grid()

    def _answer_yes(self):
        on_yes = self._on_yes
        self._dismiss_ask()
        if on_yes:
            on_yes()

    def _dismiss_ask(self):
        self._on_yes = None
        self.ask_bar.grid_remove()

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
                elif kind == "status":
                    self.status.set(ev[1])
                elif kind == "finished":
                    self._finish(*ev[1:])
                elif kind == "show":
                    self.show()
                elif kind == "quick_split":
                    self.quick_split()
                elif kind == "quick_join":
                    self.quick_join()
                elif kind == "open":
                    # Another launch (e.g. files dropped onto mpart.exe) was forwarded here.
                    self.show()
                    if ev[1]:
                        self.quick_split_files(ev[1])
                elif kind == "quit":
                    if self.quit():
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

    def quit(self, confirmed=False):
        """Close the app. Returns False if it is waiting for the user to confirm."""
        if self.worker and self.worker.is_alive():
            if not confirmed:
                self.show()
                self._ask("A job is still running. Cancel it and quit?", "Quit", lambda: self.quit(True))
                return False
            self.cancel.set()
            self.worker.join(timeout=10)
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_settings()
        self.tray.stop()
        self.root.destroy()
        return True


def _enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _make_root():
    """A Tk root with drag-and-drop support when tkdnd is available."""
    root = tk.Tk()
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD

        TkinterDnD.require(root)
        return root, DND_FILES
    except Exception:
        return root, None


def main():
    # Files passed on the command line (e.g. dropped onto mpart.exe) get split.
    files = [str(Path(a).resolve()) for a in sys.argv[1:]]
    if single_instance.forward_to_running(files):
        return
    _enable_dpi_awareness()
    root, dnd_files_type = _make_root()
    app = App(root, dnd_files_type)
    single_instance.serve(lambda fs: app.events.put(("open", fs)))
    if files:
        app.events.put(("open", files))
    root.mainloop()


if __name__ == "__main__":
    main()
