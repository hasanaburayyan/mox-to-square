"""Minimal Windows window: pick the Moxfield export, click Upload."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import default_root, load_settings
from .env_store import ENV_FILENAME, ENV_KEY, TOKEN_KEY, square_credentials, write_env
from .pipeline import UploadReport, upload


class SquareSettingsDialog(tk.Toplevel):
    """Paste the Square access token, pick the environment, test, save to .env."""

    def __init__(self, parent: tk.Tk, root_dir: Path, on_saved):
        super().__init__(parent)
        self.title("Square settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.root_dir = root_dir
        self.on_saved = on_saved
        token, env = square_credentials(root_dir)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Square access token", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, foreground="#666", wraplength=440, justify="left", text=(
            "From developer.squareup.com/apps: open your application, switch the toggle to Production, "
            "then Credentials > Production Access Token > Show. It needs ITEMS_READ, ITEMS_WRITE and "
            "INVENTORY_READ/WRITE. It is saved only in the .env file next to this app.")
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.token_var = tk.StringVar(value=token)
        self.show_var = tk.BooleanVar(value=False)
        self.token_entry = ttk.Entry(frm, textvariable=self.token_var, width=58, show="•")
        self.token_entry.grid(row=2, column=0, columnspan=2, sticky="we")
        ttk.Checkbutton(frm, text="show", variable=self.show_var, command=self._toggle).grid(row=2, column=2, padx=6)

        ttk.Label(frm, text="Environment").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.env_var = tk.StringVar(value=env if env in ("production", "sandbox") else "production")
        ttk.Combobox(frm, textvariable=self.env_var, values=["production", "sandbox"], state="readonly",
                     width=14).grid(row=4, column=0, sticky="w")

        self.result = ttk.Label(frm, text="", foreground="#666", wraplength=440, justify="left")
        self.result.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Test connection", command=self.test).pack(side="left")
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")
        self.token_entry.focus_set()

    def _toggle(self) -> None:
        self.token_entry.config(show="" if self.show_var.get() else "•")

    def _values(self) -> tuple[str, str]:
        return self.token_var.get().strip(), self.env_var.get().strip() or "production"

    def test(self) -> None:
        token, env = self._values()
        if not token:
            self.result.config(text="Paste a token first.", foreground="#b00")
            return
        self.result.config(text="Connecting…", foreground="#666")
        self.update_idletasks()

        def work():
            try:
                from .square_images import SquareClient
                locs = SquareClient(token, env).list_locations()
                names = ", ".join(l.get("name", "?") for l in locs) or "no locations"
                msg, color = f"Connected ({env}). Location(s): {names}", "#070"
            except Exception as exc:  # noqa: BLE001
                msg, color = f"Connection failed: {str(exc)[:300]}", "#b00"
            self.after(0, lambda: self.result.config(text=msg, foreground=color))

        threading.Thread(target=work, daemon=True).start()

    def save(self) -> None:
        token, env = self._values()
        if not token:
            self.result.config(text="Paste a token first.", foreground="#b00")
            return
        write_env(self.root_dir / ENV_FILENAME, {TOKEN_KEY: token, ENV_KEY: env})
        self.on_saved()
        self.destroy()


class BackfillDialog(tk.Toplevel):
    """Pick the Square category whose items get their missing images, then start."""

    ALL = "(every item in the library)"

    def __init__(self, parent: tk.Tk, client, default_category: str, on_start):
        super().__init__(parent)
        self.title("Add missing card images")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.on_start = on_start

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Which items should get images?", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, foreground="#666", wraplength=440, justify="left", text=(
            "Every item and variation in the chosen category that has no image yet gets its card scan, "
            "worked out from the SKU. Items that already have an image are left alone.")
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frm, text="Category").grid(row=2, column=0, sticky="w")
        self.cat_var = tk.StringVar(value=default_category or self.ALL)
        self.combo = ttk.Combobox(frm, textvariable=self.cat_var, values=["loading…"], state="readonly", width=44)
        self.combo.grid(row=3, column=0, columnspan=2, sticky="we")
        self.note = ttk.Label(frm, text="Loading categories from Square…", foreground="#666", wraplength=440)
        self.note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self.start, state="disabled")
        self.start_btn.pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)

        # The worker thread only puts on the queue; all widget changes happen on the Tk thread.
        self._q: queue.Queue = queue.Queue()
        self._default_category = default_category

        def work():
            try:
                from .backfill import category_names
                self._q.put(("ok", category_names(client)))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("error", str(exc)[:300]))
        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self) -> None:
        try:
            kind, payload = self._q.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(50, self._poll)
            return
        if kind == "ok":
            self._loaded(payload, self._default_category)
        else:
            self.note.config(text=f"Could not list categories: {payload}", foreground="#b00")

    def _loaded(self, names: list[str], default_category: str) -> None:
        values = [self.ALL] + names
        self.combo.config(values=values)
        match = next((n for n in names if n.lower() == (default_category or "").lower()), None)
        self.cat_var.set(match or self.ALL)
        self.note.config(text=f"{len(names)} categories in this Square account."
                         + ("" if match or not default_category else
                            f" The configured category {default_category!r} is not one of them."))
        self.start_btn.config(state="normal")

    def start(self) -> None:
        choice = self.cat_var.get()
        self.destroy()
        self.on_start("" if choice == self.ALL else choice)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"MTG Sync {__version__}  —  Moxfield → Square")
        self.root.geometry("760x520")
        self.project_root = default_root()
        self.csv_path: Path | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.busy = False

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        setup = ttk.Frame(frm)
        setup.pack(fill="x", pady=(0, 8))
        ttk.Button(setup, text="Square settings…", command=self.open_settings).pack(side="left")
        self.square_label = ttk.Label(setup, text="", foreground="#666")
        self.square_label.pack(side="left", padx=10)
        self.refresh_square_status()

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Button(row, text="Choose Moxfield export…", command=self.choose).pack(side="left")
        self.file_label = ttk.Label(row, text="no file selected", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        opts = ttk.Frame(frm)
        opts.pack(fill="x", pady=(8, 0))
        self.dry = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Dry run (check only, change nothing)", variable=self.dry).pack(side="left")
        ttk.Checkbutton(opts, text="Force (re-add a batch already uploaded)", variable=self.force).pack(side="left", padx=16)

        self.upload_btn = ttk.Button(frm, text="Upload to Square", command=self.start, state="disabled")
        self.upload_btn.pack(fill="x", pady=(10, 4))
        self.backfill_btn = ttk.Button(frm, text="Add missing card images to Square (for stock uploaded earlier)",
                                       command=self.start_backfill)
        self.backfill_btn.pack(fill="x", pady=(0, 10))

        self.status = ttk.Label(frm, text=f"Folder: {self.project_root}", foreground="#666")
        self.status.pack(fill="x")

        self.log = tk.Text(frm, height=18, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.root.after(100, self.drain)

    # --- actions -------------------------------------------------------------------------

    def open_settings(self) -> None:
        SquareSettingsDialog(self.root, self.project_root, self.refresh_square_status)

    def refresh_square_status(self) -> None:
        token, env = square_credentials(self.project_root)
        if token:
            self.square_label.config(text=f"Square: token saved ({env}, …{token[-4:]})", foreground="#070")
        else:
            self.square_label.config(text="Square: no token yet — click Square settings", foreground="#b00")

    def choose(self) -> None:
        path = filedialog.askopenfilename(title="Moxfield collection export",
                                          filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.csv_path = Path(path)
            self.file_label.config(text=self.csv_path.name, foreground="black")
            self.upload_btn.config(state="normal")

    def start(self) -> None:
        if self.busy or not self.csv_path:
            return
        if not square_credentials(self.project_root)[0]:
            messagebox.showwarning("Square settings", "Add your Square access token first (Square settings…).")
            return
        if not self.dry.get() and not messagebox.askokcancel(
                "Upload to Square", f"Add every card in {self.csv_path.name} to the Square library and stock?\n\n"
                "Nothing is deleted or decreased. Afterwards, clear the Moxfield collection."):
            return
        self.busy = True
        self.upload_btn.config(state="disabled")
        self.set_log("")
        self.status.config(text="Working…")
        threading.Thread(target=self.work, daemon=True).start()

    def work(self) -> None:
        try:
            settings = load_settings(root=self.project_root)
            report = upload(self.csv_path, settings, dry_run=self.dry.get(), force=self.force.get(),
                            log=self.q.put)
        except Exception as exc:  # noqa: BLE001
            report = UploadReport(errors=[f"{type(exc).__name__}: {exc}"])
            self.q.put("ERROR: " + str(exc))
        self.q.put(("__done__", report))  # type: ignore[arg-type]

    def start_backfill(self) -> None:
        if self.busy:
            return
        if not square_credentials(self.project_root)[0]:
            messagebox.showwarning("Square settings", "Add your Square access token first (Square settings…).")
            return
        from .pipeline import make_client
        settings = load_settings(root=self.project_root)
        try:
            client = make_client(settings)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Square", str(exc))
            return
        BackfillDialog(self.root, client, str(settings.get("square.category", "") or ""),
                       lambda category: self._run_backfill(client, category))

    def _run_backfill(self, client, category: str) -> None:
        dry = self.dry.get()
        where = f"the category {category!r}" if category else "every item in the library"
        if not dry and not messagebox.askokcancel(
                "Add missing images",
                f"Attach card images to every item and variation in {where} that has none yet?\n\n"
                "Nothing else is changed; items and variations that already have an image are left alone."):
            return
        self.busy = True
        self.upload_btn.config(state="disabled")
        self.backfill_btn.config(state="disabled")
        self.set_log("")
        self.status.config(text="Working…")
        threading.Thread(target=self.work_backfill, args=(client, category, dry), daemon=True).start()

    def work_backfill(self, client, category: str, dry: bool) -> None:
        from .backfill import BackfillReport, run_backfill
        try:
            settings = load_settings(root=self.project_root)
            report = run_backfill(settings, client, category=category, dry_run=dry, log=self.q.put)
        except Exception as exc:  # noqa: BLE001
            report = BackfillReport(dry_run=dry, errors=[f"{type(exc).__name__}: {exc}"])
            self.q.put("ERROR: " + str(exc))
        self.q.put(("__backfill_done__", report))  # type: ignore[arg-type]

    # --- ui plumbing -----------------------------------------------------------------------

    def drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                if isinstance(msg, tuple) and msg[0] == "__done__":
                    self.finish(msg[1])
                elif isinstance(msg, tuple) and msg[0] == "__backfill_done__":
                    self.finish_backfill(msg[1])
                else:
                    self.append(str(msg))
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def finish_backfill(self, r) -> None:
        self.busy = False
        self.backfill_btn.config(state="normal")
        if self.csv_path:
            self.upload_btn.config(state="normal")
        summary = (f"{r.variations_matched} of {r.variations_seen} variations matched to a printing.\n"
                   f"Item images uploaded: {r.item_images.uploaded}   already had one: {r.item_images.already_done}\n"
                   f"Printing scans uploaded: {r.variation_images.uploaded}   "
                   f"variations given an image: {r.variation_images.attached}\n"
                   f"Skipped: {len(r.skipped)} (see log)")
        if r.ok and r.dry_run:
            self.status.config(text="Dry run finished. Nothing changed.")
            messagebox.showinfo("Dry run", "Would do:\n\n" + summary)
        elif r.ok:
            self.status.config(text="Done.")
            messagebox.showinfo("Images added", summary)
        else:
            self.status.config(text="Finished with errors.")
            messagebox.showerror("Adding images failed", "\n".join(r.errors)[:1500])

    def finish(self, r: UploadReport) -> None:
        self.busy = False
        self.upload_btn.config(state="normal")
        self.backfill_btn.config(state="normal")
        if r.dry_run and r.ok:
            self.status.config(text="Dry run finished. Nothing changed.")
            messagebox.showinfo("Dry run", f"{r.items_total} items, {r.variations_total} variations, "
                                f"{r.quantity_total} cards would be added.\n"
                                f"{r.unmatched} rows unmatched, {len(r.warnings)} warnings (see log).")
        elif r.ok:
            self.status.config(text="Done.")
            messagebox.showinfo("Upload complete",
                                f"Added {r.quantity_total} cards to stock.\n"
                                f"New items: {r.items_created}   New variations: {r.variations_created}\n"
                                f"Images uploaded: {r.images_uploaded}\n"
                                f"Unmatched rows: {r.unmatched}   Warnings: {len(r.warnings)}\n\n"
                                "Now clear the Moxfield collection so the next export only holds new stock.")
        else:
            self.status.config(text="Finished with errors.")
            messagebox.showerror("Upload failed", "\n".join(r.errors)[:1500])

    def append(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def set_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.config(state="disabled")


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0
