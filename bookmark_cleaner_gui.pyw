"""
Bookmark Cleaner - a small Windows utility that finds and deletes dead links
in your browser bookmarks.

Supports Chrome, Edge, Brave, Vivaldi, Opera, Firefox, and exported bookmark .html files.
Uses only the Python standard library.
"""

import concurrent.futures as cf
import csv
import glob
import json
import os
import queue
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Bookmark Cleaner"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
WORKERS = 20
TIMEOUT = 15


# ==========================================================================
# Finding bookmark files
# ==========================================================================

def find_bookmark_sets():
    home = Path.home()
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        chromium = [("Chrome", local / "Google/Chrome/User Data"),
                    ("Edge", local / "Microsoft/Edge/User Data"),
                    ("Brave", local / "BraveSoftware/Brave-Browser/User Data"),
                    ("Vivaldi", local / "Vivaldi/User Data"),
                    ("Opera", roaming / "Opera Software/Opera Stable"),
                    ("Opera GX", roaming / "Opera Software/Opera GX Stable")]
        firefox = [roaming / "Mozilla/Firefox/Profiles"]
    elif sys.platform == "darwin":
        sup = home / "Library/Application Support"
        chromium = [("Chrome", sup / "Google/Chrome"), ("Edge", sup / "Microsoft Edge"),
                    ("Brave", sup / "BraveSoftware/Brave-Browser"), ("Vivaldi", sup / "Vivaldi"),
                    ("Opera", sup / "com.operasoftware.Opera")]
        firefox = [sup / "Firefox/Profiles"]
    else:
        cfg = home / ".config"
        chromium = [("Chrome", cfg / "google-chrome"), ("Chromium", cfg / "chromium"),
                    ("Edge", cfg / "microsoft-edge"), ("Brave", cfg / "BraveSoftware/Brave-Browser"),
                    ("Vivaldi", cfg / "vivaldi"), ("Opera", cfg / "opera")]
        firefox = [home / ".mozilla/firefox"]

    found = []
    for name, base in chromium:
        if not base.exists():
            continue
        for f in [base / "Bookmarks"] + [Path(p) for p in glob.glob(str(base / "*" / "Bookmarks"))]:
            if f.is_file():
                label = name if f.parent == base else f"{name} - {f.parent.name}"
                found.append((label, f))
    for base in firefox:
        for p in glob.glob(str(base / "*" / "places.sqlite")):
            f = Path(p)
            found.append((f"Firefox - {f.parent.name.split('.', 1)[-1]}", f))
    return found


def detect_kind(path):
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head.startswith(b"SQLite format 3"):
        return "firefox"
    text = head.decode("utf-8", "ignore").lstrip().lower()
    if text.startswith("{"):
        return "chromium"
    if "netscape-bookmark" in text or "<dl" in text or "<html" in text or "<!doctype" in text:
        return "html"
    raise ValueError("This doesn't look like a bookmarks file.\n\nPick a browser's 'Bookmarks' "
                     "file, Firefox's 'places.sqlite', or an exported bookmarks .html file.")


# (path fragments, display name, process names on Windows / macOS / Linux)
BROWSERS = [
    (("google/chrome", "google-chrome", "chromium"), "Chrome",
     {"win": ["chrome.exe"], "mac": ["Google Chrome"], "linux": ["chrome", "chromium", "chromium-browser"]}),
    (("microsoft/edge", "microsoft edge", "microsoft-edge"), "Edge",
     {"win": ["msedge.exe"], "mac": ["Microsoft Edge"], "linux": ["msedge"]}),
    (("brave",), "Brave",
     {"win": ["brave.exe"], "mac": ["Brave Browser"], "linux": ["brave"]}),
    (("vivaldi",), "Vivaldi",
     {"win": ["vivaldi.exe"], "mac": ["Vivaldi"], "linux": ["vivaldi-bin", "vivaldi"]}),
    (("opera",), "Opera",
     {"win": ["opera.exe"], "mac": ["Opera"], "linux": ["opera"]}),
    (("firefox", "mozilla"), "Firefox",
     {"win": ["firefox.exe"], "mac": ["firefox"], "linux": ["firefox", "firefox-bin"]}),
]


def _os_key():
    if sys.platform.startswith("win"):
        return "win"
    return "mac" if sys.platform == "darwin" else "linux"


def browser_for(path, kind):
    """Return (process names, display name) for the browser that owns this file."""
    p = str(path).lower().replace("\\", "/")
    for frags, name, procs in BROWSERS:
        if (kind == "firefox" and name == "Firefox") or (kind != "firefox" and any(f in p for f in frags)):
            return procs[_os_key()], name
    return [], "your browser"


def is_running(procs):
    for proc in procs:
        try:
            if _os_key() == "win":
                out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {proc}", "/NH"],
                                     capture_output=True, text=True, creationflags=0x08000000).stdout
                if proc.lower() in out.lower():
                    return True
            elif subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0:
                return True
        except Exception:
            pass
    return False


# ==========================================================================
# Reading bookmarks -> list of {key, title, url, folder}
# ==========================================================================

def read_chromium(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []

    def walk(node, folder):
        if node.get("type") == "url":
            out.append({"key": node.get("id"), "title": node.get("name", ""),
                        "url": node.get("url", ""), "folder": folder})
        sub = f"{folder}/{node.get('name', '')}".strip("/") if node.get("type") == "folder" else folder
        for child in node.get("children", []):
            walk(child, sub)

    for root in data.get("roots", {}).values():
        if isinstance(root, dict):
            walk(root, "")
    return out


def read_firefox(path):
    tmpdir = tempfile.mkdtemp()
    try:
        for suffix in ("", "-wal"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmpdir) / ("places.sqlite" + suffix))
        con = sqlite3.connect(Path(tmpdir) / "places.sqlite")
        folders = {r[0]: (r[1] or "", r[2]) for r in
                   con.execute("SELECT id, title, parent FROM moz_bookmarks WHERE type = 2")}

        def folder_path(fid):
            parts, seen = [], set()
            while fid in folders and folders[fid][0] and fid not in seen:
                seen.add(fid)
                parts.append(folders[fid][0])
                fid = folders[fid][1]
            return "/".join(reversed(parts))

        out = [{"key": r[0], "title": r[1] or "", "url": r[2], "folder": folder_path(r[3])}
               for r in con.execute("SELECT b.id, b.title, p.url, b.parent FROM moz_bookmarks b "
                                    "JOIN moz_places p ON b.fk = p.id WHERE b.type = 1")]
        con.close()
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class _NetscapeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.stack = [], []
        self.pending_folder = None
        self._in_h3 = False
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self._in_h3, self.pending_folder = True, ""
        elif tag == "dl":
            self.stack.append(self.pending_folder or "")
            self.pending_folder = None
        elif tag == "a" and a.get("href"):
            self._cur = {"key": self.getpos()[0], "title": "", "url": a["href"],
                         "folder": "/".join(s for s in self.stack if s)}

    def handle_endtag(self, tag):
        if tag == "h3":
            self._in_h3 = False
        elif tag == "dl" and self.stack:
            self.stack.pop()
        elif tag == "a" and self._cur:
            self.items.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._in_h3:
            self.pending_folder += data
        elif self._cur:
            self._cur["title"] += data


def read_html(path):
    p = _NetscapeParser()
    p.feed(Path(path).read_text(encoding="utf-8", errors="replace"))
    return p.items


READERS = {"chromium": read_chromium, "firefox": read_firefox, "html": read_html}


# ==========================================================================
# Checking links
# ==========================================================================

CTX = ssl.create_default_context()


def _request(url, method):
    req = urllib.request.Request(url, method=method, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return r.status


def _classify_error(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "DEAD", "Page not found (404)"
        if e.code == 410:
            return "DEAD", "Page removed (410)"
        if e.code in (401, 403):
            return "UNSURE", f"Site blocked the check ({e.code})"
        if e.code == 429:
            return "UNSURE", "Site said 'too many requests' (429)"
        if e.code >= 500:
            return "UNSURE", f"Site having problems ({e.code})"
        return "UNSURE", f"Error {e.code}"
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return "DEAD", "Website no longer exists"
    if isinstance(reason, ConnectionRefusedError):
        return "DEAD", "Website refused to connect"
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(e, (socket.timeout, TimeoutError)):
        return "UNSURE", "Took too long to answer"
    if isinstance(reason, ssl.SSLError):
        return "UNSURE", "Security certificate problem"
    return "UNSURE", f"Couldn't connect ({type(reason).__name__})"


def check_url(url):
    if not url.lower().startswith(("http://", "https://")):
        return "SKIPPED", "Not a web link"
    try:
        code = _request(url, "HEAD")
        return "OK", f"Working ({code})"
    except Exception:
        pass  # many sites mishandle HEAD; try a normal GET
    result = ("UNSURE", "Unknown")
    for _ in range(2):
        try:
            code = _request(url, "GET")
            return "OK", f"Working ({code})"
        except Exception as e:
            result = _classify_error(e)
            if isinstance(e, urllib.error.HTTPError):
                break
            time.sleep(2)
    return result


def internet_ok():
    for test in ("https://www.google.com", "https://www.cloudflare.com", "https://www.wikipedia.org"):
        try:
            _request(test, "HEAD")
            return True
        except Exception:
            continue
    return False


# ==========================================================================
# Deleting
# ==========================================================================

def make_backup(path):
    dst = Path(f"{path}.backup-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, dst)
    return dst


def delete_chromium(path, doomed):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    removed = 0

    def prune(node):
        nonlocal removed
        if "children" in node:
            keep = []
            for c in node["children"]:
                if c.get("type") == "url" and doomed.get(c.get("id")) == c.get("url"):
                    removed += 1
                else:
                    prune(c)
                    keep.append(c)
            node["children"] = keep

    for root in data.get("roots", {}).values():
        if isinstance(root, dict):
            prune(root)
    data.pop("checksum", None)  # the browser recalculates it
    bak = make_backup(path)
    Path(path).write_text(json.dumps(data, indent=3, ensure_ascii=False), encoding="utf-8")
    return removed, bak


def delete_firefox(path, doomed):
    try:
        con = sqlite3.connect(path, timeout=1)
        con.execute("BEGIN EXCLUSIVE")
        con.rollback()
    except sqlite3.OperationalError:
        raise RuntimeError("Firefox still has your bookmarks open.\n\nClose Firefox completely and try again.")
    bak = make_backup(path)
    ids, removed = list(doomed), 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        cur = con.execute(f"DELETE FROM moz_bookmarks WHERE type = 1 AND id IN "
                          f"({','.join('?' * len(chunk))})", chunk)
        removed += cur.rowcount
    con.commit()
    con.close()
    return removed, bak


def delete_html(path, doomed, out_path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    keep, removed = [], 0
    for n, line in enumerate(lines, 1):
        if n in doomed and "<a" in line.lower():
            removed += 1
        else:
            keep.append(line)
    Path(out_path).write_text("".join(keep), encoding="utf-8")
    return removed, None


# ==========================================================================
# The window
# ==========================================================================

def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


TICK, UNTICK = "☑", "☐"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x620")
        self.minsize(760, 460)
        try:
            self.iconbitmap(resource("bookmark_cleaner.ico"))
        except Exception:
            pass

        self.sets = find_bookmark_sets()
        self.path = None
        self.kind = None
        self.rows = {}          # tree item id -> row dict
        self.ticked = set()
        self.q = queue.Queue()
        self.scanning = False

        style = ttk.Style(self)
        style.configure("Treeview", rowheight=24)
        style.configure("Big.TButton", padding=(14, 6))

        # --- Step 1: choose bookmarks
        top = ttk.Frame(self, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        ttk.Label(top, text="1.  Choose your bookmarks", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(6, 0))
        self.choice = tk.StringVar()
        names = [label for label, _ in self.sets]
        self.combo = ttk.Combobox(row, textvariable=self.choice, values=names, state="readonly", width=60)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self.on_pick_set)
        ttk.Button(row, text="Browse for a file…", command=self.on_browse).pack(side="left", padx=(8, 0))
        self.path_lbl = ttk.Label(top, text="", foreground="#666")
        self.path_lbl.pack(anchor="w", pady=(4, 0))

        if self.sets:
            self.combo.current(0)
            self.on_pick_set()
        else:
            self.combo.set("No browsers found automatically - use Browse")

        # --- Step 2: check
        mid = ttk.Frame(self, padding=(14, 10, 14, 4))
        mid.pack(fill="x")
        ttk.Label(mid, text="2.  Check the links", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        row2 = ttk.Frame(mid)
        row2.pack(fill="x", pady=(6, 0))
        self.scan_btn = ttk.Button(row2, text="Check links", style="Big.TButton", command=self.on_scan)
        self.scan_btn.pack(side="left")
        self.progress = ttk.Progressbar(row2, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        self.status = ttk.Label(mid, text="Pick your bookmarks, then click Check links.")
        self.status.pack(anchor="w", pady=(6, 0))

        # --- Step 3: results
        res = ttk.Frame(self, padding=(14, 10, 14, 4))
        res.pack(fill="both", expand=True)
        ttk.Label(res, text="3.  Review and delete", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(res, foreground="#555",
                  text="Ticked links will be deleted. Dead links are ticked for you; 'Unsure' links "
                       "usually still work, so check them first. Double-click any link to open it.").pack(anchor="w")
        tf = ttk.Frame(res)
        tf.pack(fill="both", expand=True, pady=(6, 0))
        cols = ("del", "status", "title", "reason", "folder", "url")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, label, w, stretch in [("del", "Delete", 60, False), ("status", "Status", 80, False),
                                     ("title", "Bookmark", 230, True), ("reason", "Why", 200, False),
                                     ("folder", "Folder", 150, False), ("url", "Link", 260, True)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=stretch, anchor="center" if c == "del" else "w")
        self.tree.tag_configure("DEAD", foreground="#b03a2e")
        self.tree.tag_configure("UNSURE", foreground="#b9770e")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<space>", self.on_space)

        # --- Bottom buttons
        bot = ttk.Frame(self, padding=(14, 6, 14, 12))
        bot.pack(fill="x")
        ttk.Button(bot, text="Tick all dead", command=lambda: self.tick_status("DEAD")).pack(side="left")
        ttk.Button(bot, text="Tick all unsure", command=lambda: self.tick_status("UNSURE")).pack(side="left", padx=6)
        ttk.Button(bot, text="Untick all", command=self.untick_all).pack(side="left")
        ttk.Button(bot, text="Save report…", command=self.on_save_report).pack(side="left", padx=6)
        self.del_btn = ttk.Button(bot, text="Delete ticked bookmarks", style="Big.TButton",
                                  command=self.on_delete, state="disabled")
        self.del_btn.pack(side="right")
        self.count_lbl = ttk.Label(bot, text="")
        self.count_lbl.pack(side="right", padx=10)

        self.all_rows = []
        self.after(100, self.poll)

    # ----- choosing
    def set_path(self, path):
        try:
            self.kind = detect_kind(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return False
        self.path = Path(path)
        self.path_lbl.config(text=str(self.path))
        self.clear_results()
        return True

    def on_pick_set(self, _=None):
        i = self.combo.current()
        if i >= 0 and i < len(self.sets):
            self.set_path(self.sets[i][1])

    def on_browse(self):
        f = filedialog.askopenfilename(
            title="Choose a bookmarks file",
            filetypes=[("Bookmark files", "Bookmarks *.html *.htm places.sqlite"),
                       ("Exported bookmarks", "*.html *.htm"), ("All files", "*.*")])
        if f and self.set_path(f):
            self.combo.set(Path(f).name)

    # ----- scanning
    def clear_results(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.ticked.clear()
        self.all_rows = []
        self.progress["value"] = 0
        self.update_count()

    def on_scan(self):
        if self.scanning:
            return
        if not self.path:
            messagebox.showinfo(APP_NAME, "Choose your bookmarks first.")
            return
        try:
            bookmarks = READERS[self.kind](self.path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't read the bookmarks:\n\n{e}")
            return
        if not bookmarks:
            messagebox.showinfo(APP_NAME, "There are no bookmarks in that file.")
            return
        self.clear_results()
        self.scanning = True
        self.scan_btn.config(state="disabled")
        self.del_btn.config(state="disabled")
        self.status.config(text="Checking your internet connection…")
        threading.Thread(target=self.scan_worker, args=(bookmarks,), daemon=True).start()

    def scan_worker(self, bookmarks):
        if not internet_ok():
            self.q.put(("error", "Can't reach the internet right now, so every link would look dead.\n\n"
                                 "Check your connection and try again."))
            return
        urls = sorted({b["url"] for b in bookmarks})
        self.q.put(("start", len(urls), len(bookmarks)))
        results = {}
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(check_url, u): u for u in urls}
            for n, fut in enumerate(cf.as_completed(futs), 1):
                try:
                    results[futs[fut]] = fut.result()
                except Exception as e:
                    results[futs[fut]] = ("UNSURE", str(e)[:60])
                self.q.put(("progress", n))
        rows = [dict(b, status=results[b["url"]][0], reason=results[b["url"]][1]) for b in bookmarks]
        self.q.put(("done", rows))

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "start":
                    self.progress.config(maximum=msg[1], value=0)
                    self.total = msg[1]
                    self.status.config(text=f"Checking {msg[1]} links from {msg[2]} bookmarks…")
                elif msg[0] == "progress":
                    self.progress["value"] = msg[1]
                    self.status.config(text=f"Checked {msg[1]} of {self.total} links…")
                elif msg[0] == "done":
                    self.show_results(msg[1])
                elif msg[0] == "error":
                    self.scanning = False
                    self.scan_btn.config(state="normal")
                    self.status.config(text="Stopped.")
                    messagebox.showwarning(APP_NAME, msg[1])
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def show_results(self, rows):
        self.scanning = False
        self.scan_btn.config(state="normal")
        self.all_rows = rows
        order = {"DEAD": 0, "UNSURE": 1}
        shown = sorted([r for r in rows if r["status"] in order],
                       key=lambda r: (order[r["status"]], r["folder"].lower(), r["title"].lower()))
        for r in shown:
            tick = r["status"] == "DEAD"
            iid = self.tree.insert("", "end", tags=(r["status"],), values=(
                TICK if tick else UNTICK, r["status"].title(), r["title"] or "(no name)",
                r["reason"], r["folder"], r["url"]))
            self.rows[iid] = r
            if tick:
                self.ticked.add(iid)
        c = {s: sum(1 for r in rows if r["status"] == s) for s in ("DEAD", "UNSURE", "OK", "SKIPPED")}
        self.status.config(text=f"Done.  {c['DEAD']} dead,  {c['UNSURE']} unsure,  {c['OK']} working"
                                + (f",  {c['SKIPPED']} skipped (not web links)" if c["SKIPPED"] else "") + ".")
        self.update_count()
        if not shown:
            messagebox.showinfo(APP_NAME, "Good news: all your links are working.")

    # ----- ticking
    def toggle(self, iid):
        if iid not in self.rows:
            return
        if iid in self.ticked:
            self.ticked.discard(iid)
            self.tree.set(iid, "del", UNTICK)
        else:
            self.ticked.add(iid)
            self.tree.set(iid, "del", TICK)
        self.update_count()

    def on_click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            self.toggle(self.tree.identify_row(e.y))
            return "break"

    def on_space(self, _):
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def on_double(self, e):
        iid = self.tree.identify_row(e.y)
        if iid in self.rows and self.tree.identify_column(e.x) != "#1":
            webbrowser.open(self.rows[iid]["url"])

    def tick_status(self, status):
        for iid, r in self.rows.items():
            if r["status"] == status:
                self.ticked.add(iid)
                self.tree.set(iid, "del", TICK)
        self.update_count()

    def untick_all(self):
        for iid in list(self.ticked):
            self.tree.set(iid, "del", UNTICK)
        self.ticked.clear()
        self.update_count()

    def update_count(self):
        n = len(self.ticked)
        self.count_lbl.config(text=f"{n} ticked" if self.rows else "")
        self.del_btn.config(state="normal" if n and not self.scanning else "disabled")

    # ----- report
    def on_save_report(self):
        if not self.all_rows:
            messagebox.showinfo(APP_NAME, "Check your links first.")
            return
        f = filedialog.asksaveasfilename(title="Save report", defaultextension=".csv",
                                         initialfile=f"bookmark_report_{datetime.now():%Y-%m-%d}.csv",
                                         filetypes=[("Spreadsheet (CSV)", "*.csv")])
        if not f:
            return
        with open(f, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Status", "Why", "Bookmark", "Link", "Folder"])
            for r in self.all_rows:
                w.writerow([r["status"], r["reason"], r["title"], r["url"], r["folder"]])
        messagebox.showinfo(APP_NAME, f"Report saved:\n{f}\n\nIt opens in Excel or Google Sheets.")

    # ----- deleting
    def on_delete(self):
        doomed_rows = [self.rows[i] for i in self.ticked]
        if not doomed_rows:
            return
        n = len(doomed_rows)
        out_path = None

        if self.kind == "html":
            out_path = filedialog.asksaveasfilename(
                title="Save the cleaned bookmarks as", defaultextension=".html",
                initialfile=self.path.stem + "_cleaned.html", filetypes=[("Bookmarks file", "*.html")])
            if not out_path:
                return
        else:
            procs, bname = browser_for(self.path, self.kind)
            if is_running(procs):
                messagebox.showwarning(APP_NAME,
                    f"{bname} is still open.\n\nPlease close {bname} completely (check the little "
                    f"icons by the clock, or quit it from the menu bar on a Mac), then click Delete again.\n\n{bname} saves its bookmarks "
                    f"when it closes, which would undo the clean-up.")
                return
            if not messagebox.askyesno(APP_NAME,
                    f"Delete {n} bookmark{'s' if n != 1 else ''}?\n\n"
                    f"A backup of your bookmarks is saved first, so this can be undone.\n\n"
                    f"Make sure {bname} is closed. If you sync bookmarks across devices, turn bookmark "
                    f"sync off first or the links may come back.", icon="warning"):
                return

        try:
            keys = {r["key"]: r["url"] for r in doomed_rows}
            if self.kind == "chromium":
                removed, bak = delete_chromium(self.path, keys)
            elif self.kind == "firefox":
                removed, bak = delete_firefox(self.path, keys)
            else:
                removed, bak = delete_html(self.path, keys, out_path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Nothing was deleted.\n\n{e}")
            return

        for iid in list(self.ticked):
            self.tree.delete(iid)
            self.rows.pop(iid, None)
        self.ticked.clear()
        self.update_count()
        self.status.config(text=f"Deleted {removed} bookmarks.")
        if self.kind == "html":
            messagebox.showinfo(APP_NAME, f"Removed {removed} bookmarks.\n\nCleaned file saved as:\n{out_path}\n\n"
                                          "Import it into your browser to use it.")
        else:
            messagebox.showinfo(APP_NAME, f"Deleted {removed} bookmarks. You can open your browser again.\n\n"
                                          f"Backup saved as:\n{bak}\n\nTo undo: close the browser, delete "
                                          f"'{self.path.name}' and rename the backup to '{self.path.name}'.")


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-res screens
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
