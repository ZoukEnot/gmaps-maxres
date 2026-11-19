"""Simple window for gmaps_maxres: paste a Google Maps link, get a PNG."""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import gmaps_maxres

APP_TITLE = "Google Maps Max-Res Downloader"


def default_output_dir() -> Path:
    for candidate in (Path.home() / "Downloads", Path.home() / "Pictures", Path.home()):
        if candidate.is_dir():
            return candidate / "GMaps MaxRes"
    return Path.cwd()


def open_path(path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror(APP_TITLE, f"Could not open {path}:\n{exc}")


def show_in_folder(path: Path):
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        open_path(path.parent)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.result = None
        self.out_dir = default_output_dir()

        root.title(APP_TITLE)
        root.minsize(560, 400)
        try:
            ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
        except tk.TclError:
            pass

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root, padding=8)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        ttk.Label(
            frm,
            text="1. Open the place in Google Maps (satellite view) and copy the address from the browser.\n"
                 "2. Paste it below and press  Download.",
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", **pad)

        self.url_var = tk.StringVar()
        self.entry = ttk.Entry(frm, textvariable=self.url_var, font=("Segoe UI", 10))
        self.entry.grid(row=1, column=0, sticky="ew", padx=(12, 4), pady=6, ipady=3)
        self.entry.bind("<Return>", lambda _e: self.start())
        self.entry.bind("<Button-3>", self._context_menu)
        ttk.Button(frm, text="Paste", command=self.paste).grid(row=1, column=1, padx=4)
        self.go_btn = ttk.Button(frm, text="Download", command=self.start)
        self.go_btn.grid(row=1, column=2, padx=(4, 12))

        folder_row = ttk.Frame(frm)
        folder_row.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="Save to:").grid(row=0, column=0, sticky="w")
        self.dir_label = ttk.Label(folder_row, text=str(self.out_dir), foreground="#555")
        self.dir_label.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(folder_row, text="Change...", command=self.choose_dir).grid(row=0, column=2)

        self.stage_var = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.stage_var, font=("Segoe UI", 10, "bold")).grid(
            row=3, column=0, columnspan=3, sticky="w", **pad
        )
        self.bar = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.bar.grid(row=4, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 6))

        self.size_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.size_var, font=("Segoe UI", 11, "bold"), foreground="#1a5fb4").grid(
            row=5, column=0, columnspan=3, sticky="w", **pad
        )

        self.log = tk.Text(frm, height=8, state="disabled", wrap="word", font=("Consolas", 9),
                           background="#f6f6f6", relief="flat")
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", padx=12, pady=(0, 8))
        self.open_btn = ttk.Button(btns, text="Open image", state="disabled",
                                   command=lambda: open_path(self.result))
        self.open_btn.pack(side="left", padx=4)
        self.folder_btn = ttk.Button(btns, text="Show in folder", state="disabled",
                                     command=lambda: show_in_folder(self.result))
        self.folder_btn.pack(side="left", padx=4)

        self.entry.focus_set()
        self.root.after(100, self._poll)

    # --- UI helpers -------------------------------------------------------
    def _context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Paste", command=self.paste)
        menu.add_command(label="Clear", command=lambda: self.url_var.set(""))
        menu.tk_popup(event.x_root, event.y_root)

    def paste(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        self.url_var.set(text.strip())
        self.entry.icursor("end")

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=str(self.out_dir), title="Where to save the images")
        if d:
            self.out_dir = Path(d)
            self.dir_label.config(text=str(self.out_dir))

    def write_log(self, line: str):
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # --- work -------------------------------------------------------------
    def start(self):
        if self.worker is not None and self.worker.is_alive():
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "Paste a Google Maps link first.")
            return
        self.result = None
        self.size_var.set("")
        self.go_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.folder_btn.config(state="disabled")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.bar.config(mode="indeterminate")
        self.bar.start(12)
        self.worker = threading.Thread(target=self._run, args=(url, self.out_dir), daemon=True)
        self.worker.start()

    def _run(self, url, out_dir):
        put = self.events.put
        try:
            path = gmaps_maxres.generate(
                url,
                output_dir=out_dir,
                log=lambda s: put(("log", s)),
                stage=lambda s: put(("stage", s)),
                progress=lambda d, t: put(("progress", (d, t))),
                size=lambda w, h: put(("size", (w, h))),
            )
            put(("done", path))
        except gmaps_maxres.MapsError as exc:
            put(("error", str(exc)))
        except MemoryError:
            put(("error", "Not enough memory for an image this large. Zoom in further in Google Maps and try again."))
        except Exception as exc:  # show anything unexpected instead of silently dying
            put(("error", f"Unexpected error: {exc!r}"))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.write_log(payload)
                elif kind == "stage":
                    self.stage_var.set(payload)
                    if not payload.startswith("Downloading"):
                        self.bar.config(mode="indeterminate")
                        self.bar.start(12)
                elif kind == "progress":
                    done, total = payload
                    if str(self.bar.cget("mode")) != "determinate":
                        self.bar.stop()
                        self.bar.config(mode="determinate", maximum=total)
                    self.bar["value"] = done
                    self.stage_var.set(f"Downloading image pieces: {done} / {total}  ({done * 100 // total}%)")
                elif kind == "size":
                    w, h = payload
                    self.size_var.set(f"width: {w:.2f} m      height: {h:.2f} m")
                elif kind == "done":
                    self._finish()
                    self.result = Path(payload)
                    self.stage_var.set(f"Done! Saved {self.result.name}")
                    self.bar.config(mode="determinate", maximum=1, value=1)
                    self.open_btn.config(state="normal")
                    self.folder_btn.config(state="normal")
                elif kind == "error":
                    self._finish()
                    self.stage_var.set("Failed.")
                    self.bar.config(mode="determinate", value=0)
                    self.write_log("ERROR: " + payload)
                    messagebox.showerror(APP_TITLE, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self):
        self.bar.stop()
        self.go_btn.config(state="normal")


def _launched_by_double_click() -> bool:
    """True when our console window was created just for us (Explorer, a
    shortcut, Run dialog) rather than inherited from cmd / PowerShell."""
    try:
        import ctypes
        buf = (ctypes.c_uint32 * 16)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 16)
    except Exception:
        return False
    # A PyInstaller onefile exe is two processes (bootloader + python);
    # a terminal would add at least one more (cmd.exe, powershell.exe, ...).
    limit = 2 if getattr(sys, "frozen", False) else 1
    return 0 < n <= limit


def _relaunch_without_console() -> bool:
    """Start a console-less copy of ourselves to host the GUI, so no terminal
    window (conhost or Windows Terminal) stays open behind it."""
    env = dict(os.environ, GMAPS_MAXRES_GUI="1", PYINSTALLER_RESET_ENVIRONMENT="1")
    cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, env=env, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def run_gui():
    if sys.platform.startswith("win"):
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    # Any command-line arguments -> behave exactly like the CLI tool.
    if len(sys.argv) > 1:
        return gmaps_maxres.main()
    if (
        sys.platform.startswith("win")
        and os.environ.get("GMAPS_MAXRES_GUI") != "1"
        and _launched_by_double_click()
        and _relaunch_without_console()
    ):
        return None
    return run_gui()


if __name__ == "__main__":
    main()
