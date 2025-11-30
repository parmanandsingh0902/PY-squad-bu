# file_organizer_gui.py
"""
File Organizer - Tkinter GUI
This program organizes files by:
- Extension type (Images, Documents, Videos, etc.)
- OR modification date (month/day)
Includes:
- Move or Copy mode
- Subfolder organization
- Undo feature
- Live progress bar
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import shutil, os, json
from datetime import datetime
import threading, queue
import sys

# Log file name stored in the destination directory
LOG_FILENAME = "organizer_log.json"

# Copy chunk size used for smoother % updates
CHUNK_SIZE = 1024 * 512  # 512 KB

# --------------------------------------------------------
# EXTENSION CATEGORY MAPPING
# --------------------------------------------------------
def ext_folder_name(ext: str):
    """
    Returns a folder name based on file extension.
    Groups similar file types together (images, videos, etc.).
    """
    mapping = {
        'jpg':'Images','jpeg':'Images','png':'Images','gif':'Images','bmp':'Images','svg':'Images',
        'pdf':'Documents','docx':'Documents','doc':'Documents','txt':'Documents','md':'Documents',
        'ppt':'Presentations','pptx':'Presentations',
        'xlsx':'Spreadsheets','xls':'Spreadsheets','csv':'Spreadsheets',
        'mp3':'Audio','wav':'Audio','flac':'Audio',
        'mp4':'Videos','mkv':'Videos','mov':'Videos','avi':'Videos',
        'zip':'Archives','rar':'Archives','7z':'Archives','tar':'Archives','gz':'Archives',
        'py':'Code','js':'Code','html':'Code','css':'Code','java':'Code','c':'Code','cpp':'Code',
    }
    return mapping.get(ext.lower(), ext.upper() if ext else "NO_EXT")

# --------------------------------------------------------
# DATE HELPERS (FOR SUBFOLDERS)
# --------------------------------------------------------
def get_mod_monthfolder(path: Path):
    """Return 'YYYY-MM' based on file modification month."""
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m")

def get_mod_dayfolder(path: Path):
    """Return 'YYYY-MM-DD' based on modification date."""
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")

# --------------------------------------------------------
# LIST ALL FILES IN TARGET DIRECTORY
# --------------------------------------------------------
def build_file_list(target: Path, recursive: bool=True, ignore_dirs=None):
    """
    Scans target folder and collects all files.
    Skips destination directory to avoid copying loops.
    """
    ignore_dirs = set(p.resolve() for p in (ignore_dirs or []))
    files = []

    if recursive:
        for p in target.rglob('*'):
            if p.is_file():
                if any(str(p.resolve()).startswith(str(d)) for d in ignore_dirs):
                    continue
                files.append(p)
    else:
        for p in target.iterdir():
            if p.is_file():
                files.append(p)

    return files

# --------------------------------------------------------
# LOG FUNCTIONS (USED FOR UNDO FEATURE)
# --------------------------------------------------------
def make_log_entry(moves, mode, timestamp=None):
    """Create structured JSON entry for copied/moved files."""
    if timestamp is None:
        timestamp = datetime.now().isoformat()
    return {"timestamp": timestamp, "mode": mode, "moves": [{"src": str(src), "dst": str(dst)} for src, dst in moves]}

def read_log(log_path: Path):
    """Load undo log. If missing or invalid, return empty list."""
    if not log_path.exists():
        return []
    try:
        return json.load(log_path.open('r', encoding='utf-8'))
    except:
        return []

def write_log_entry(log_path: Path, entry):
    """Append new undo entry to the log file."""
    data = read_log(log_path)
    data.append(entry)
    json.dump(data, log_path.open('w', encoding='utf-8'), indent=2)

def undo_last(log_path: Path, output_fn=print):
    """
    Undo the last operation by moving files back to original paths.
    Used for move/copy undo.
    """
    data = read_log(log_path)
    if not data:
        output_fn("No log entries to undo.")
        return 0

    last = data.pop()
    moves = last.get("moves", [])
    undone = 0

    for m in reversed(moves):
        src = Path(m['src'])   # original location
        dst = Path(m['dst'])   # current file location

        if dst.exists():
            try:
                src.parent.mkdir(parents=True, exist_ok=True)

                # Avoid overwriting existing files
                candidate = src
                i = 1
                while candidate.exists():
                    candidate = src.parent / f"{src.stem} (restored {i}){src.suffix}"
                    i += 1

                shutil.move(str(dst), str(candidate))
                undone += 1
                output_fn(f"Restored: {dst} -> {candidate}")

            except Exception as e:
                output_fn(f"Failed to restore {dst}: {e}")
        else:
            output_fn(f"File missing (skipped): {dst}")

    # Save updated log
    write_log_entry(log_path, data)
    output_fn(f"Undo completed. Restored {undone} files.")
    return undone

# --------------------------------------------------------
# COPY ENGINE (WITH PROGRESS + CANCEL SUPPORT)
# --------------------------------------------------------
def safe_copy_stream(src: Path, dst: Path, cancel_event, progress_fn=None):
    """
    Copy large files smoothly using chunk streaming.
    Shows progress and supports cancelling.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # If file exists, auto-rename to avoid overwrite
    candidate = dst
    i = 1
    while candidate.exists():
        candidate = dst.parent / f"{dst.stem} ({i}){dst.suffix}"
        i += 1

    total = src.stat().st_size
    copied = 0

    temp = candidate.with_suffix(candidate.suffix + ".part")

    try:
        with src.open('rb') as rf, temp.open('wb') as wf:
            while True:
                if cancel_event.is_set():
                    # Clean cancellation of partial file
                    temp.unlink(missing_ok=True)
                    return None

                buf = rf.read(CHUNK_SIZE)
                if not buf:
                    break

                wf.write(buf)
                copied += len(buf)

                if progress_fn:
                    progress_fn(copied, total, str(src), str(candidate))

        temp.replace(candidate)
        return candidate

    except Exception:
        temp.unlink(missing_ok=True)
        raise

# --------------------------------------------------------
# MAIN ORGANIZATION LOGIC
# --------------------------------------------------------
def organizer_run(target_dir: Path, method: str, move: bool, dry_run: bool,
                  filters, recursive: bool, dest_base: Path,
                  sub_by_date: bool, date_granularity: str,
                  output_fn, progress_fn, cancel_event):
    """
    Main engine:
    - Scans target folder
    - Decides destination path
    - Copies or moves files
    - Logs operations for undo
    """
    filters = set(x.lower().strip() for x in (filters or []))
    target_dir = target_dir.resolve()

    # Default destination = target folder
    if dest_base is None:
        dest_base = target_dir
    else:
        dest_base = dest_base.resolve()
        dest_base.mkdir(parents=True, exist_ok=True)

    files = build_file_list(target_dir, recursive=recursive, ignore_dirs=[dest_base])
    moves = []

    for f in files:
        if cancel_event.is_set():
            output_fn("Operation canceled by user.")
            break

        if f.name == LOG_FILENAME:
            continue

        ext = f.suffix[1:].lower()

        if filters and ext not in filters:
            continue

        # Decide destination folder
        if method == 'extension':
            base_folder = ext_folder_name(ext)
            if sub_by_date:
                date_sub = get_mod_dayfolder(f) if date_granularity == 'YYYY-MM-DD' else get_mod_monthfolder(f)
                dst = dest_base / base_folder / date_sub / f.name
            else:
                dst = dest_base / base_folder / f.name

        elif method == 'date':
            date_folder = get_mod_dayfolder(f) if date_granularity == 'YYYY-MM-DD' else get_mod_monthfolder(f)
            if sub_by_date:
                dst = dest_base / date_folder / ext_folder_name(ext) / f.name
            else:
                dst = dest_base / date_folder / f.name

        else:
            dst = dest_base / f.name  # fallback

        # DRY RUN (only show actions)
        if dry_run:
            output_fn(f"[DRY] {f} -> {dst}")
            moves.append((str(f), str(dst)))
            continue

        try:
            result = safe_copy_stream(f, dst, cancel_event, progress_fn)

            if result is None:
                output_fn("Copy cancelled during file.")
                break

            output_fn(f"COPIED: {f} -> {result}")
            moves.append((str(f), str(result)))

            if move:
                try:
                    f.unlink()
                    output_fn(f"REMOVED SOURCE (move): {f}")
                except Exception as e:
                    output_fn(f"Could not remove source: {e}")

        except Exception as e:
            output_fn(f"Error copying {f}: {e}")

    # Save log entry for undo feature
    if not dry_run and moves and not cancel_event.is_set():
        entry = make_log_entry(moves, "move" if move else "copy")
        write_log_entry(dest_base / LOG_FILENAME, entry)
        output_fn(f"Logged {len(moves)} operations.")

    if not cancel_event.is_set():
        output_fn("--- Done ---")

# --------------------------------------------------------
# TKINTER GRAPHICAL USER INTERFACE
# --------------------------------------------------------
class FileOrganizerGUI(tk.Tk):
    """Main Tkinter window for controlling program."""

    def __init__(self):
        super().__init__()
        self.title("File Organizer")
        self.geometry("980x760")

        # User-configurable options
        self.target_dir = tk.StringVar()
        self.dest_dir = tk.StringVar()
        self.method = tk.StringVar(value='extension')
        self.move_mode = tk.BooleanVar(value=True)
        self.dry_run = tk.BooleanVar(value=False)
        self.ext_filters = tk.StringVar()
        self.recursive = tk.BooleanVar(value=True)
        self.sub_by_date = tk.BooleanVar(value=False)
        self.date_granularity = tk.StringVar(value='YYYY-MM')

        # For worker thread communication
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        self._build_widgets()
        self._periodic_check()

    # --------------------------------------------------------
    # GUI LAYOUT (ALL BUTTONS, INPUTS & TEXT AREA)
    # --------------------------------------------------------
    def _build_widgets(self):
        frm = ttk.Frame(self)
        frm.grid(sticky='nsew', padx=10, pady=10)
        frm.columnconfigure(1, weight=1)

        # Folder selection
        ttk.Label(frm, text="Target folder:").grid(row=0, column=0, sticky='w')
        ttk.Entry(frm, textvariable=self.target_dir).grid(row=0, column=1, sticky='ew')
        ttk.Button(frm, text="Browse", command=self.browse_target).grid(row=0, column=2)

        ttk.Label(frm, text="Destination (optional):").grid(row=1, column=0, sticky='w')
        ttk.Entry(frm, textvariable=self.dest_dir).grid(row=1, column=1, sticky='ew')
        ttk.Button(frm, text="Browse", command=self.browse_dest).grid(row=1, column=2)

        # Method selection
        mfrm = ttk.LabelFrame(frm, text="Organize by")
        mfrm.grid(row=2, column=0, columnspan=3, sticky='ew', pady=5)
        ttk.Radiobutton(mfrm, text="Extension", variable=self.method, value='extension').grid(row=0, column=0)
        ttk.Radiobutton(mfrm, text="Date", variable=self.method, value='date').grid(row=0, column=1)

        # Options
        ttk.Checkbutton(frm, text="Move files (else copy)", variable=self.move_mode).grid(row=3, column=0, sticky='w')
        ttk.Checkbutton(frm, text="Dry-run only", variable=self.dry_run).grid(row=3, column=1, sticky='w')
        ttk.Checkbutton(frm, text="Recursive scan", variable=self.recursive).grid(row=3, column=2, sticky='w')

        ttk.Label(frm, text="Extensions (comma/space, blank = all):").grid(row=4, column=0, columnspan=3, sticky='w')
        ttk.Entry(frm, textvariable=self.ext_filters).grid(row=5, column=0, columnspan=3, sticky='ew')

        # Sub-folder options
        optfrm = ttk.Frame(frm)
        optfrm.grid(row=6, column=0, columnspan=3, sticky='ew')
        ttk.Checkbutton(optfrm, text="Sub-organize by date", variable=self.sub_by_date).grid(row=0, column=0)
        ttk.Label(optfrm, text="Date granularity:").grid(row=0, column=1)
        gran = ttk.Combobox(optfrm, values=['YYYY-MM','YYYY-MM-DD'], state='readonly',
                            textvariable=self.date_granularity, width=12)
        gran.grid(row=0, column=2)

        # Action buttons
        arow = ttk.Frame(frm)
        arow.grid(row=7, column=0, columnspan=3, pady=8, sticky='ew')
        arow.columnconfigure((0,1,2,3,4), weight=1)

        ttk.Button(arow, text='Run', command=self.on_run).grid(row=0, column=0, sticky='ew')
        ttk.Button(arow, text='Dry-Run', command=self.on_dryrun).grid(row=0, column=1, sticky='ew')
        ttk.Button(arow, text='Cancel', command=self.on_cancel).grid(row=0, column=2, sticky='ew')
        ttk.Button(arow, text='Undo Last', command=self.on_undo).grid(row=0, column=3, sticky='ew')
        ttk.Button(arow, text='Save Log As...', command=self.save_log).grid(row=0, column=4, sticky='ew')

        # Progress display
        progfrm = ttk.Frame(self)
        progfrm.grid(row=8, column=0, sticky='ew', padx=10, pady=5)
        progfrm.columnconfigure(1, weight=1)

        ttk.Label(progfrm, text="Current file:").grid(row=0, column=0)
        self.current_file_label = ttk.Label(progfrm, text="(idle)")
        self.current_file_label.grid(row=0, column=1, sticky='w')

        self.progress_bar = ttk.Progressbar(progfrm, mode='determinate')
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky='ew')

        self.progress_percent_label = ttk.Label(progfrm, text="")
        self.progress_percent_label.grid(row=2, column=0, columnspan=2, sticky='e')

        # Output log
        outfrm = ttk.LabelFrame(self, text="Output Log")
        outfrm.grid(row=9, column=0, sticky='nsew', padx=10, pady=10)
        outfrm.rowconfigure(0, weight=1)
        outfrm.columnconfigure(0, weight=1)

        self.output_text = tk.Text(outfrm, wrap='none')
        self.output_text.grid(row=0, column=0, sticky='nsew')

        # Scrollbars
        yscroll = ttk.Scrollbar(outfrm, orient='vertical', command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky='ns')

        xscroll = ttk.Scrollbar(outfrm, orient='horizontal', command=self.output_text.xview)
        self.output_text.configure(xscrollcommand=xscroll.set)
        xscroll.grid(row=1, column=0, sticky='ew', columnspan=2)

    # --------------------------------------------------------
    # FILE CHOOSER BUTTONS
    # --------------------------------------------------------
    def browse_target(self):
        d = filedialog.askdirectory()
        if d:
            self.target_dir.set(d)

    def browse_dest(self):
        d = filedialog.askdirectory()
        if d:
            self.dest_dir.set(d)

    # --------------------------------------------------------
    # THREAD-SAFE UI UPDATE HELPERS
    # --------------------------------------------------------
    def _append_output(self, text):
        self.queue.put({'type': 'text', 'text': text})

    def _update_progress(self, copied, total, src, dst):
        self.queue.put({'type': 'progress', 'copied': copied, 'total': total, 'src': src, 'dst': dst})

    def _periodic_check(self):
        """
        Periodically check the queue for progress updates or log messages.
        """
        try:
            while True:
                item = self.queue.get_nowait()

                if item.get('type') == 'progress':
                    copied = item['copied']
                    total = item['total']
                    fname = Path(item['src']).name
                    percent = int((copied / total) * 100) if total > 0 else 0

                    self.progress_bar['maximum'] = total
                    self.progress_bar['value'] = copied
                    self.current_file_label.config(text=fname)
                    self.progress_percent_label.config(text=f"{percent}%")

                else:
                    self.output_text.insert('end', item['text'] + '\n')
                    self.output_text.see('end')

        except queue.Empty:
            pass

        self.after(100, self._periodic_check)

    # --------------------------------------------------------
    # BUTTON ACTION HANDLERS
    # --------------------------------------------------------
    def _start_worker(self, target_dir, method, move_mode, dry_run,
                      filters, recursive, dest_dir,
                      sub_by_date, date_granularity):
        """Starts the background worker for organizing files."""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Another operation is running.")
            return

        self.output_text.delete('1.0', 'end')
        self.cancel_event.clear()

        args = (
            Path(target_dir), method, move_mode, dry_run,
            filters, recursive, Path(dest_dir) if dest_dir else None,
            sub_by_date, date_granularity,
            self._append_output, self._update_progress, self.cancel_event
        )

        self.worker = threading.Thread(target=organizer_run, args=args, daemon=True)
        self.worker.start()

    def on_run(self):
        """Run the organizer in normal mode."""
        target = self.target_dir.get().strip()
        if not target:
            messagebox.showerror("Error", "Please select a target folder.")
            return
        filters = self._parse_filters(self.ext_filters.get())
        self._start_worker(
            target, self.method.get(), self.move_mode.get(),
            False, filters, self.recursive.get(),
            self.dest_dir.get().strip() or None,
            self.sub_by_date.get(), self.date_granularity.get()
        )

    def on_dryrun(self):
        """Preview actions without modifying files."""
        target = self.target_dir.get().strip()
        if not target:
            messagebox.showerror("Error", "Please select a target folder.")
            return
        filters = self._parse_filters(self.ext_filters.get())
        self._start_worker(
            target, self.method.get(), self.move_mode.get(),
            True, filters, self.recursive.get(),
            self.dest_dir.get().strip() or None,
            self.sub_by_date.get(), self.date_granularity.get()
        )

    def on_cancel(self):
        """Request cancellation of the ongoing operation."""
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self._append_output("Cancel requested…")
        else:
            self._append_output("No task running.")

    def on_undo(self):
        """Undo the last move/copy operation."""
        dest = self.dest_dir.get().strip() or self.target_dir.get().strip()
        if not dest:
            messagebox.showerror("Error", "Select destination or target folder.")
            return

        log_path = Path(dest) / LOG_FILENAME
        if not log_path.exists():
            messagebox.showerror("No log", "No undo log found.")
            return

        def _undo():
            count = undo_last(log_path, output_fn=lambda s: self.queue.put({'type':'text','text':s}))
            self.queue.put({'type': 'text', 'text': f"Undo complete ({count} restored)."})

        threading.Thread(target=_undo, daemon=True).start()

    def save_log(self):
        """Save text output window content to file."""
        dest = filedialog.asksaveasfilename(
            defaultextension='.txt',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')]
        )
        if dest:
            try:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(self.output_text.get('1.0', 'end'))
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save: {e}")

    def _parse_filters(self, s: str):
        """Convert user extension list ('jpg, png') → ['jpg', 'png']"""
        if not s.strip():
            return []
        return [p.strip().lstrip('.') for p in s.replace(',', ' ').split() if p.strip()]

# --------------------------------------------------------
# START GUI
# --------------------------------------------------------
if __name__ == '__main__':
    app = FileOrganizerGUI()
    app.mainloop()
