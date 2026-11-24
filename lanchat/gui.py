"""Tkinter desktop UI for LanChat: group chat with IP Messenger users,
laid out like IP Messenger.

App          main window: IPMsg-style user list (search, sort, refresh), the
             list of public group chats, "Start group chat" and "Send message".
GroupWindow  one window per group chat: members, chat log, message box.
RecvWindow   an incoming direct message pops up (Reply / Close / Quote).
SendWindow   send a direct message to one or more users.
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import dnd
from .core import EVERYONE, Node

FONT = ("TkDefaultFont", 10)
BOLD = ("TkDefaultFont", 10, "bold")


def fmt_time(ts, long=False):
    return time.strftime("%Y/%m/%d (%a) %H:%M:%S" if long else "%H:%M", time.localtime(ts))


def user_label(p):
    return p["name"] + ("  (away)" if p.get("away") else "")


def _ip_key(ip):
    try:
        return tuple(int(x) for x in (ip or "").split("."))
    except ValueError:
        return (999,)


def _text_box(master, height, readonly=False):
    box = ScrolledText(master, wrap="word", height=height, font=FONT, padx=6, pady=4,
                       relief="solid", borderwidth=1, undo=not readonly)
    if readonly:
        box.configure(state="disabled")
    return box


def _tree(master, columns, height, selectmode="extended"):
    """Treeview + vertical scrollbar in a frame. columns: (key, title, width)."""
    frame = ttk.Frame(master)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        selectmode=selectmode, height=height)
    for key, title, width in columns:
        tree.heading(key, text=title)
        tree.column(key, width=width, minwidth=40, stretch=True)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return frame, tree


def human_size(n):
    if n < 1024:
        return "%d bytes" % n
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)


def _unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s (%d)%s" % (base, i, ext))
        i += 1
    return path


def open_path(path):
    """Open a file or folder with the system's default program."""
    try:
        if os.name == "nt":
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        messagebox.showerror("LanChat", "Couldn't open %s:\n%s" % (path, exc))


def _bring_to_front(win):
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.after(1200, lambda: win.winfo_exists() and win.attributes("-topmost", False))


# ---------------------------------------------------------------- single instance


class InstanceServer:
    """Lets a second start of LanChat bring this one to the front instead
    (like IP Messenger: only one copy runs, it lives in the tray)."""

    def __init__(self, data_dir, on_show):
        self.path = os.path.join(data_dir, "instance.json")
        self.on_show = on_show
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self.running = True
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"port": self.sock.getsockname()[1], "pid": os.getpid()}, f)
        except OSError:
            pass
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    if conn.recv(16) == b"show":
                        self.on_show()
                        conn.sendall(b"ok")
                except OSError:
                    pass

    def close(self):
        self.running = False
        self.sock.close()
        try:
            with open(self.path, encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(self.path)
        except (OSError, ValueError):
            pass


def signal_running_instance(data_dir):
    """If LanChat already runs with this data dir, ask it to show itself."""
    try:
        with open(os.path.join(data_dir, "instance.json"), encoding="utf-8") as f:
            port = int(json.load(f)["port"])
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.settimeout(2)
            s.sendall(b"show")
            return s.recv(2) == b"ok"
    except (OSError, ValueError, KeyError, TypeError):
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_supported():
    return os.name == "nt" and getattr(sys, "frozen", False)


def get_autostart():
    if not autostart_supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "LanChat")
            return True
    except OSError:
        return False


def set_autostart(enabled):
    """Start LanChat (minimized to the tray) when the user logs on to Windows."""
    if not autostart_supported():
        return
    import winreg
    args = [a for a in sys.argv[1:] if a != "--minimized"]
    command = " ".join(['"%s"' % sys.executable] + ['"%s"' % a for a in args] + ["--minimized"])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, "LanChat", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "LanChat")
            except OSError:
                pass


# ---------------------------------------------------------------- user table


class UserTable(ttk.Frame):
    """IPMsg-style user list: search box, count, sortable columns, scrollbar."""

    COLUMNS = (("name", "User", 170), ("group", "Group", 110),
               ("host", "Host", 130), ("ip", "IP address", 110))

    def __init__(self, master, height=12, on_activate=None, on_select=None):
        super().__init__(master)
        self.on_select = on_select
        self._peers = []
        self._sort = ("group", False)

        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Search:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        self.search_entry = ttk.Entry(top, textvariable=self.search, width=22)
        self.search_entry.pack(side="left", padx=(4, 8))
        self.count = ttk.Label(top, foreground="gray35")
        self.count.pack(side="left")
        self.toolbar = top  # callers add buttons on the right

        frame, self.tree = _tree(self, self.COLUMNS, height)
        frame.pack(fill="both", expand=True)
        for key, _, _ in self.COLUMNS:
            self.tree.heading(key, command=lambda k=key: self.sort_by(k))

        self.search_entry.bind("<Escape>", lambda e: self.search.set(""))
        self.search_entry.bind("<Down>", lambda e: self._focus_first())
        self.search_entry.bind("<Return>", lambda e: self._focus_first())
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select and self.on_select())
        if on_activate:
            self.tree.bind("<Double-1>", lambda e: on_activate())
            self.tree.bind("<Return>", lambda e: on_activate())

    def _focus_first(self):
        rows = self.tree.get_children()
        if rows:
            self.tree.focus_set()
            if not self.tree.selection():
                self.tree.selection_set(rows[0])
            self.tree.focus(self.tree.selection()[0])
        return "break"

    def set_peers(self, peers):
        self._peers = [p for p in peers if p["online"]]
        self.refresh()

    def sort_by(self, key):
        cur, rev = self._sort
        self._sort = (key, not rev if key == cur else False)
        self.refresh()

    def refresh(self):
        q = self.search.get().strip().lower()
        rows = [p for p in self._peers
                if not q or any(q in str(p.get(f) or "").lower()
                                for f in ("name", "group", "host", "ip", "user"))]
        key, rev = self._sort
        if key == "ip":
            rows.sort(key=lambda p: _ip_key(p["ip"]), reverse=rev)
        else:
            rows.sort(key=lambda p: (str(p.get(key) or "").lower(), p["name"].lower()), reverse=rev)

        # Update rows in place so selection and scroll position survive.
        wanted = [p["id"] for p in rows]
        for iid in set(self.tree.get_children()) - set(wanted):
            self.tree.delete(iid)
        for i, p in enumerate(rows):
            values = (user_label(p), p.get("group") or "", p.get("host") or "", p.get("ip") or "")
            if self.tree.exists(p["id"]):
                self.tree.item(p["id"], values=values)
                self.tree.move(p["id"], "", i)
            else:
                self.tree.insert("", i, iid=p["id"], values=values)

        for k, title, _ in self.COLUMNS:
            arrow = (" ▼" if rev else " ▲") if k == key else ""
            self.tree.heading(k, text=title + arrow)
        total = len(self._peers)
        self.count.configure(text="Users: %d" % total if not q else
                             "Users: %d of %d" % (len(rows), total))

    def selected_ids(self):
        return list(self.tree.selection())


# ---------------------------------------------------------------- dialogs


class PeerPicker(tk.Toplevel):
    """Modal: searchable multi-select user table."""

    def __init__(self, parent, title, peers, prompt):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.geometry("560x420")
        self.result = None
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=prompt).pack(anchor="w")
        self.table = UserTable(body, height=10, on_activate=self._ok)
        self.table.pack(fill="both", expand=True, pady=(4, 8))
        self.table.set_peers(peers)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        self.table.search_entry.focus_set()
        self.wait_visibility()
        self.grab_set()

    def _ok(self):
        ids = self.table.selected_ids()
        if not ids:
            messagebox.showwarning("LanChat", "Select at least one person.", parent=self)
            return
        self.result = ids
        self.destroy()


class NameDialog(tk.Toplevel):
    """Ask for a new group's name."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.title("Start group chat")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        who = ", ".join(names[:6]) + (" and %d more" % (len(names) - 6) if len(names) > 6 else "")
        ttk.Label(body, text="Group chat with: " + (who or "nobody yet — others can join"),
                  wraplength=360).pack(anchor="w")
        ttk.Label(body, text="Group name:").pack(anchor="w", pady=(10, 2))
        self.entry = ttk.Entry(body, width=40)
        self.entry.pack(fill="x")
        ttk.Label(body, text="The group name is public: everyone on the LAN can see it and join.",
                  foreground="gray40").pack(anchor="w", pady=(6, 10))
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(row, text="Start", command=self._ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.entry.focus_set()
        self.wait_visibility()
        self.grab_set()

    def _ok(self):
        name = self.entry.get().strip()
        if not name:
            messagebox.showwarning("LanChat", "Please enter a group name.", parent=self)
            return
        self.result = name
        self.destroy()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("LanChat settings")
        self.transient(app.root)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="User name").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(body, text="Group name").grid(row=1, column=0, sticky="w", pady=3)
        self.name = ttk.Entry(body, width=30)
        self.group = ttk.Entry(body, width=30)
        self.name.insert(0, app.node.name)
        self.group.insert(0, app.node.group_name)
        self.name.grid(row=0, column=1, padx=(8, 0))
        self.group.grid(row=1, column=1, padx=(8, 0))
        ttk.Label(body, text="Shown to LanChat and IP Messenger users (Group = your department).",
                  foreground="gray40").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.popup = tk.BooleanVar(value=app.ui["popup_groups"])
        ttk.Checkbutton(body, text="Open a group chat window when a new message arrives",
                        variable=self.popup).grid(row=3, column=0, columnspan=2, sticky="w")
        self.autostart = tk.BooleanVar(value=get_autostart())
        if autostart_supported():
            ttk.Checkbutton(body, text="Start LanChat when Windows starts (in the tray)",
                            variable=self.autostart).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text=app.node.ipmsg_status()[1], foreground="gray40").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 8))
        row = ttk.Frame(body)
        row.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(row, text="OK", command=self._ok).pack(side="left", padx=6)
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.name.focus_set()

    def _ok(self):
        if not self.name.get().strip():
            messagebox.showwarning("LanChat", "Please enter a user name.", parent=self)
            return
        self.app.node.set_profile(self.name.get(), self.group.get())
        self.app.ui["popup_groups"] = self.popup.get()
        self.app.save_ui()
        if autostart_supported() and self.autostart.get() != get_autostart():
            try:
                set_autostart(self.autostart.get())
            except OSError as exc:
                messagebox.showerror("LanChat", "Couldn't change the start-up setting:\n%s" % exc,
                                     parent=self)
        self.app.update_status()
        self.destroy()


# ---------------------------------------------------------------- direct messages


class SendWindow(tk.Toplevel):
    """Send a direct message to one or more users, like IPMsg's send window."""

    def __init__(self, app, peer_ids, quote="", files=()):
        super().__init__(app.root)
        self.app, self.peer_ids = app, list(peer_ids)
        node = app.node
        names = [node.display_name(i) for i in self.peer_ids]
        self.title("Send to %s" % (names[0] if len(names) == 1 else "%d people" % len(names)))
        self.geometry("480x320")
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        if len(self.peer_ids) == 1:
            p = node.peer(self.peer_ids[0]) or {}
            to = "%s   %s   %s" % (names[0], p.get("group") or "", p.get("ip") or "")
        else:
            to = ", ".join(names)
        ttk.Label(body, text="To: " + to, font=BOLD, wraplength=450).pack(anchor="w", pady=(0, 6))
        self.text = _text_box(body, 8)
        self.text.pack(fill="both", expand=True)
        if quote:
            self.text.insert("1.0", quote)
        self.files = []
        frow = ttk.Frame(body)
        frow.pack(fill="x", pady=(6, 0))
        ttk.Button(frow, text="Attach files\u2026", command=self.attach).pack(side="left")
        self.clear_btn = ttk.Button(frow, text="Clear", width=6, command=self.clear_files)
        self.files_label = ttk.Label(frow, foreground="gray35", wraplength=330)
        self.files_label.pack(side="left", padx=8)
        self.can_drop = dnd.hook_dropfiles(self, self.add_files)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(6, 0))
        self.sealed = tk.BooleanVar()
        ttk.Checkbutton(row, text="Seal", variable=self.sealed).pack(side="left")
        ttk.Button(row, text="Send", command=self.send).pack(side="right")
        ttk.Label(row, text="Ctrl+Enter to send", foreground="gray50").pack(side="right", padx=8)
        self.text.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        self.bind("<Escape>", lambda e: self.destroy())
        self.text.focus_set()
        self.text.mark_set("insert", "end")
        self.add_files(files)

    def attach(self):
        self.add_files(filedialog.askopenfilenames(parent=self, title="Attach files"))

    def add_files(self, paths):
        """Attach files (from the dialog or dropped from Explorer)."""
        folders = [p for p in paths if os.path.isdir(p)]
        self.files.extend(p for p in paths if os.path.isfile(p) and p not in self.files)
        self._show_files()
        if folders:
            messagebox.showinfo("LanChat", "Folders can't be sent yet, only files:\n" +
                                "\n".join(folders), parent=self)

    def clear_files(self):
        self.files = []
        self._show_files()

    def _show_files(self):
        if self.files:
            self.files_label.configure(text="Attached: " + ", ".join(
                "%s (%s)" % (os.path.basename(f), human_size(os.path.getsize(f))) for f in self.files))
            self.clear_btn.pack(side="right")
        else:
            self.files_label.configure(text="or drop files here" if self.can_drop else "")
            self.clear_btn.pack_forget()

    def send(self):
        text = self.text.get("1.0", "end").strip()
        if not text and not self.files:
            return
        try:
            for pid in self.peer_ids:
                if self.files:
                    self.app.node.send_files("dm:" + pid, self.files, text, sealed=self.sealed.get())
                else:
                    self.app.node.send_text("dm:" + pid, text, sealed=self.sealed.get())
        except (ValueError, OSError) as exc:
            messagebox.showerror("LanChat", str(exc), parent=self)
            return
        names = [self.app.node.display_name(i) for i in self.peer_ids]
        self.app.flash("Sent to " + ", ".join(names[:5]) + ("…" if len(names) > 5 else ""))
        self.destroy()


class RecvWindow(tk.Toplevel):
    """An incoming direct message, like IPMsg's receive window."""

    def __init__(self, app, conv, rec, offset=0):
        super().__init__(app.root)
        self.app, self.conv, self.rec = app, conv, rec
        node = app.node
        pid = rec["from_id"]
        p = node.peer(pid) or {}
        name = rec.get("from_name") or node.display_name(pid)
        self.title("Message from %s" % name)
        files = rec.get("files") or []
        self.geometry("460x%d+%d+%d" % (280 + min(len(files), 5) * 30 + (30 if files else 0),
                                        app.root.winfo_rootx() + 60 + offset,
                                        app.root.winfo_rooty() + 60 + offset))
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        who = name + ("   (%s)" % p["group"] if p.get("group") else "")
        if conv == EVERYONE:
            who += "   → to everyone"
        ttk.Label(body, text=who, font=BOLD).pack(anchor="w")
        where = "   ".join(x for x in (p.get("host"), p.get("ip")) if x)
        kind = "IPMsg" if p.get("kind") == "ipmsg" else "LanChat"
        ttk.Label(body, text="%s   (%s)" % (where, kind) if where else kind,
                  foreground="gray40").pack(anchor="w")
        ttk.Label(body, text=fmt_time(rec["ts"], True),
                  foreground="gray40").pack(anchor="w", pady=(0, 6))

        self.holder = ttk.Frame(body)
        self.holder.pack(fill="both", expand=True)
        if rec.get("sealed") and not rec.get("opened"):
            self.open_btn = ttk.Button(self.holder, text="Sealed message — click to open",
                                       command=self.open_sealed)
            self.open_btn.pack(expand=True, ipadx=20, ipady=20)
        else:
            self._show_text()

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(6, 0))
        self.quote = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Quote", variable=self.quote).pack(side="left")
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self.reply_btn = ttk.Button(row, text="Reply", command=self.reply)
        self.reply_btn.pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())
        _bring_to_front(self)
        self.reply_btn.focus_set()

    def _show_text(self):
        files = self.rec.get("files") or []
        if self.rec["text"] or not files:
            box = _text_box(self.holder, 7 if not files else 4, readonly=True)
            box.configure(state="normal")
            box.insert("1.0", self.rec["text"])
            box.configure(state="disabled")
            box.pack(fill="both", expand=True)
        if files:
            self._show_files(files)

    def _show_files(self, files):
        frame = ttk.Labelframe(self.holder, text="Attached file%s" % ("s" if len(files) > 1 else ""),
                               padding=4)
        frame.pack(fill="x", pady=(6, 0))
        for f in files:
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text="%s   (%s)" % (f["name"], human_size(f["size"]))).pack(side="left")
            status = ttk.Label(row, foreground="gray40", width=14)
            btn = ttk.Button(row, text="Save\u2026", width=8)
            btn.configure(command=lambda f=f, btn=btn, status=status: self.save(f, btn, status))
            btn.pack(side="right")
            status.pack(side="right", padx=4)
        if len(files) > 1:
            ttk.Button(frame, text="Save all\u2026", command=lambda: self.save_all(files)).pack(
                anchor="e", pady=(4, 0))

    def save(self, f, btn, status, dest=None):
        key = "%s:%d" % (self.rec["id"], f["fid"])
        saved = self.app.saved.get(key)
        if saved and os.path.exists(saved):
            open_path(saved)
            return
        dest = dest or filedialog.asksaveasfilename(parent=self, initialfile=f["name"],
                                                    title="Save %s" % f["name"])
        if not dest:
            return

        def update(text, done=False):
            status.configure(text=text)
            if done:
                btn.configure(text="Open")
        btn.configure(text="\u2026")
        self.app.start_download(self.conv, self.rec, f, dest, update)

    def save_all(self, files):
        folder = filedialog.askdirectory(parent=self, title="Save all files to")
        if not folder:
            return
        for child in self.holder.winfo_children():
            if isinstance(child, ttk.Labelframe):
                rows = [r for r in child.winfo_children() if isinstance(r, ttk.Frame)]
                for f, row in zip(files, rows):
                    btn = [w for w in row.winfo_children() if isinstance(w, ttk.Button)][0]
                    status = [w for w in row.winfo_children() if isinstance(w, ttk.Label)][1]
                    self.save(f, btn, status, dest=_unique_path(folder, f["name"]))

    def open_sealed(self):
        self.open_btn.destroy()
        self._show_text()
        self.app.node.open_sealed(self.conv, self.rec["id"])

    def reply(self):
        if self.rec.get("sealed") and not self.rec.get("opened"):
            self.open_sealed()
        quote = ""
        if self.quote.get():
            quote = "".join("> %s\n" % line for line in self.rec["text"].splitlines()) + "\n"
        SendWindow(self.app, [self.rec["from_id"]], quote)
        self.destroy()


# ---------------------------------------------------------------- group chat


class GroupWindow(tk.Toplevel):
    """One group chat: members on the left, chat on the right, message box below."""

    def __init__(self, app, gid):
        super().__init__(app.root)
        self.app, self.node, self.gid = app, app.node, gid
        self.geometry("720x520")
        self.minsize(520, 360)
        self._shown = set()
        self._joined = None

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        self.header = ttk.Label(body, font=("TkDefaultFont", 13, "bold"))
        self.header.pack(anchor="w")
        self.subheader = ttk.Label(body, foreground="gray40")
        self.subheader.pack(anchor="w", pady=(0, 6))

        paned = ttk.PanedWindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        ttk.Label(left, text="Members", font=BOLD).pack(anchor="w")
        frame, self.members = _tree(left, (("name", "Name", 120), ("via", "Via", 60)), 10)
        frame.pack(fill="both", expand=True, pady=(2, 4))
        mrow = ttk.Frame(left)
        mrow.pack(fill="x")
        self.add_btn = ttk.Button(mrow, text="Add…", width=8, command=self.add_people)
        self.add_btn.pack(side="left")
        self.remove_btn = ttk.Button(mrow, text="Remove", width=8, command=self.remove_people)
        self.remove_btn.pack(side="left", padx=4)
        paned.add(left, weight=0)

        right = ttk.Frame(paned)
        self.text = _text_box(right, 14, readonly=True)
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("time", foreground="gray50")
        self.text.tag_configure("me", foreground="#1a5fb4", font=BOLD)
        self.text.tag_configure("other", foreground="#26a269", font=BOLD)
        self.text.tag_configure("ipmsg", foreground="#c64600", font=BOLD)
        self.text.tag_configure("system", foreground="gray45", font=("TkDefaultFont", 9, "italic"))
        self.text.tag_configure("notice", foreground="gray35", font=("TkDefaultFont", 11),
                                justify="center", spacing1=30)
        self.text.tag_configure("file", foreground="#1a5fb4", underline=True)
        self.text.tag_bind("file", "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind("file", "<Leave>", lambda e: self.text.configure(cursor=""))
        paned.add(right, weight=1)

        self.input = _text_box(body, 3)
        self.input.pack(fill="x", pady=(8, 0))
        self.transfer_label = ttk.Label(body, foreground="#1a5fb4")
        self.transfer_label.pack(anchor="w")
        self.input.bind("<Return>", self._on_enter)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(6, 0))
        self.join_btn = ttk.Button(row, text="Join group", command=self.toggle)
        self.join_btn.pack(side="left")
        ttk.Button(row, text="Close", command=self.close).pack(side="right")
        self.send_btn = ttk.Button(row, text="Send", command=self.send)
        self.send_btn.pack(side="right", padx=6)
        ttk.Label(row, text="Enter to send, Shift+Enter for a new line",
                  foreground="gray50").pack(side="right", padx=8)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.render()

    def joined(self):
        return self.node.id in self.node.group_members(self.gid)

    def render(self):
        """Redraw everything (after joining/leaving or when history arrived)."""
        joined = self._joined = self.joined()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._shown.clear()
        if joined:
            for rec in self.node.history("group:" + self.gid):
                self.append(rec, scroll=False)
        else:
            self.text.insert("end", "You're not in this group.\nJoin it to see the whole chat "
                                    "history and take part.\n", "notice")
        self.text.configure(state="disabled")
        self.text.see("end")
        for w in (self.send_btn, self.add_btn, self.remove_btn):
            w.state(["!disabled"] if joined else ["disabled"])
        self.input.configure(state="normal" if joined else "disabled",
                             background="white" if joined else "#eeeeee")
        self.join_btn.configure(text="Leave group" if joined else "Join group")
        self.update_members()
        if joined:
            self.input.focus_set()

    def update_members(self):
        g = self.node.group(self.gid) or {"name": "?", "members": {}}
        self.title("# %s — LanChat" % g["name"])
        self.header.configure(text="# " + g["name"])
        active = [m for m, e in g["members"].items() if e["in"]]
        n_ipmsg = sum(self.node.is_ipmsg(m) for m in active)
        self.subheader.configure(text="Public group chat  ·  %d member%s%s" % (
            len(active), "" if len(active) == 1 else "s",
            "  (%d via IPMsg)" % n_ipmsg if n_ipmsg else ""))
        rows = sorted(active, key=lambda m: (m != self.node.id, self.node.display_name(m).lower()))
        for iid in set(self.members.get_children()) - set(rows):
            self.members.delete(iid)
        for i, m in enumerate(rows):
            p = self.node.peer(m)
            online = m == self.node.id or bool(p and p["online"])
            name = "You" if m == self.node.id else self.node.display_name(m)
            values = (("● " if online else "○ ") + name,
                      "IPMsg" if self.node.is_ipmsg(m) else "LanChat")
            if self.members.exists(m):
                self.members.item(m, values=values)
                self.members.move(m, "", i)
            else:
                self.members.insert("", i, iid=m, values=values)
        if self._joined is not None and self.joined() != self._joined:
            self.render()  # membership changed elsewhere

    def append(self, rec, scroll=True):
        if rec["id"] in self._shown or not self._joined:
            return
        self._shown.add(rec["id"])
        state = self.text.cget("state")
        self.text.configure(state="normal")
        self.text.insert("end", fmt_time(rec["ts"]) + "  ", "time")
        if rec.get("system"):
            self.text.insert("end", rec["text"] + "\n", "system")
        else:
            mine = rec["from_id"] == self.node.id
            name = "You" if mine else rec.get("from_name") or self.node.display_name(rec["from_id"])
            tag = "me" if mine else ("ipmsg" if self.node.is_ipmsg(rec["from_id"]) else "other")
            self.text.insert("end", name + ": ", tag)
            files = rec.get("files") or []
            text = rec["text"] or ("sent %s" % ("a file" if len(files) == 1 else "%d files" % len(files))
                                   if files else "")
            self.text.insert("end", text + "\n")
            for f in files:
                link = "file:%s:%d" % (rec["id"], f["fid"])
                self.text.insert("end", "        ")
                self.text.insert("end", "\u2193 %s  (%s)" % (f["name"], human_size(f["size"])),
                                 ("file", link))
                self.text.insert("end", "\n")
                self.text.tag_bind(link, "<Button-1>", lambda e, rec=rec, f=f: self.file_clicked(rec, f))
        self.text.configure(state=state)
        if scroll:
            self.text.see("end")

    def file_clicked(self, rec, f):
        key = "%s:%d" % (rec["id"], f["fid"])
        if rec["from_id"] == self.node.id:  # our own file: show where it is
            sh = self.node._shares.get(f["fid"])
            if sh and os.path.exists(sh["path"]):
                open_path(os.path.dirname(sh["path"]))
            return
        saved = self.app.saved.get(key)
        if saved and os.path.exists(saved):
            open_path(saved)
            return
        dest = filedialog.asksaveasfilename(parent=self, initialfile=f["name"],
                                            title="Save %s" % f["name"])
        if dest:
            self.app.start_download("group:" + self.gid, rec, f, dest, lambda text, done=False:
                                    self.transfer_label.configure(text="%s: %s" % (f["name"], text)))

    def _on_enter(self, event):
        if event.state & 0x1:  # Shift: new line
            return None
        self.send()
        return "break"

    def send(self):
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        try:
            self.node.send_text("group:" + self.gid, text)
        except ValueError as exc:
            messagebox.showerror("LanChat", str(exc), parent=self)
            return
        self.input.delete("1.0", "end")

    def add_people(self):
        members = set(self.node.group_members(self.gid))
        candidates = [p for p in self.node.peers() if p["id"] not in members]
        dlg = PeerPicker(self, "Add people", candidates,
                         "Add people to the group (IPMsg users get the messages in IP Messenger):")
        self.wait_window(dlg)
        if dlg.result:
            self.node.add_members(self.gid, dlg.result)

    def remove_people(self):
        ids = [i for i in self.members.selection() if i != self.node.id]
        if not ids:
            messagebox.showinfo("LanChat", "Select the members to remove in the list "
                                           "(use “Leave group” to leave yourself).", parent=self)
            return
        names = ", ".join(self.node.display_name(i) for i in ids)
        if messagebox.askyesno("LanChat", "Remove %s from the group?" % names, parent=self):
            self.node.remove_members(self.gid, ids)

    def toggle(self):
        if not self.joined():
            self.node.join_group(self.gid)
        elif messagebox.askyesno("LanChat", "Leave this group?", parent=self):
            self.node.leave_group(self.gid)
        self.render()

    def close(self):
        self.app.group_windows.pop(self.gid, None)
        self.destroy()


# ---------------------------------------------------------------- log


class LogWindow(tk.Toplevel):
    """Message log: every saved conversation, searchable."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.node = app, app.node
        self.title("LanChat log")
        self.geometry("760x480")
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)
        left = ttk.Frame(paned)
        paned.add(left, weight=0)
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.fill())
        ttk.Entry(left, textvariable=self.search).pack(fill="x", pady=(0, 4))
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lf, width=26, exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda e: self.show())
        self.text = _text_box(paned, 20, readonly=True)
        paned.add(self.text, weight=1)
        self.text.tag_configure("head", font=BOLD)
        self.text.tag_configure("time", foreground="gray50")
        self.rows = []
        self.fill()

    def label(self, conv):
        if conv == EVERYONE:
            return "Everyone"
        if conv.startswith("group:"):
            g = self.node.group(conv[6:])
            return "# " + (g["name"] if g else "?")
        return self.node.display_name(conv[3:])

    def fill(self):
        q = self.search.get().strip().lower()
        convs = []
        for conv in self.node.conversations():
            hist = self.node.history(conv)
            if not hist:
                continue
            label = self.label(conv)
            if q and q not in label.lower() and not any(q in r["text"].lower() for r in hist):
                continue
            convs.append((hist[-1]["ts"], conv, label))
        convs.sort(reverse=True)
        self.rows = [c for _, c, _ in convs]
        self.lb.delete(0, "end")
        for _, _, label in convs:
            self.lb.insert("end", label)

    def show(self):
        sel = self.lb.curselection()
        if not sel:
            return
        conv = self.rows[sel[0]]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for rec in self.node.history(conv):
            self.text.insert("end", fmt_time(rec["ts"], True) + "  ", "time")
            if rec.get("system"):
                self.text.insert("end", rec["text"] + "\n\n")
                continue
            mine = rec["from_id"] == self.node.id
            who = "You" if mine else rec.get("from_name") or "?"
            self.text.insert("end", who + ("  (sealed)" if rec.get("sealed") else "") + "\n", "head")
            if rec["text"]:
                self.text.insert("end", rec["text"] + "\n")
            for f in rec.get("files") or []:
                self.text.insert("end", "[file] %s  (%s)\n" % (f["name"], human_size(f["size"])))
            self.text.insert("end", "\n")
        self.text.configure(state="disabled")
        self.text.see("end")


# ---------------------------------------------------------------- main window


class App:
    def __init__(self, root, minimized=False, **node_kwargs):
        self.root = root
        self.events = queue.Queue()
        self.node = Node(on_event=lambda *e: self.events.put(e), **node_kwargs)
        self.ui = {"popup_groups": True}
        self.ui.update(self.node._load_json("ui.json", {}))
        self.group_windows = {}
        self.unread = {}
        self.transfers = {}   # "<msg id>:<fid>" -> (callback, destination)
        self.saved = {}       # "<msg id>:<fid>" -> where it was saved
        self._popups = 0
        self._flash_job = None

        self._build()
        try:
            self.node.start()
        except OSError as exc:
            messagebox.showerror("LanChat", "Could not open network ports:\n%s" % exc)
            root.destroy()
            raise SystemExit(1)
        self.instance = InstanceServer(self.node.data_dir,
                                       lambda: self.events.put(("tray", "open")))
        self.tray = None
        if os.name == "nt":
            from .tray import Tray
            icon = os.path.join(os.path.dirname(__file__), "icon.ico")
            tray = Tray("LanChat", icon, lambda name: self.events.put(("tray", name)))
            if tray.start():
                self.tray = tray
        self.refresh_users()
        self.refresh_groups()
        self.update_status()
        # Like IPMsg: closing the window keeps LanChat running in the tray.
        root.protocol("WM_DELETE_WINDOW", self.hide if self.tray else self.close)
        if minimized:
            if self.tray:
                self.root.withdraw()
            else:
                self.root.iconify()
        root.after(100, self._poll)

    def save_ui(self):
        try:
            self.node._save_json("ui.json", self.ui)
        except OSError:
            pass

    def _build(self):
        root = self.root
        root.title("LanChat")
        root.geometry("700x660")
        root.minsize(540, 480)
        try:
            self._icon = tk.PhotoImage(file=os.path.join(os.path.dirname(__file__), "icon.png"))
            root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)

        users = ttk.Frame(paned)
        self.table = UserTable(users, height=12, on_activate=self.send_message,
                               on_select=self._update_selection)
        self.table.pack(fill="both", expand=True)
        ttk.Button(self.table.toolbar, text="Refresh", command=self.do_refresh).pack(side="right")
        paned.add(users, weight=3)

        groups = ttk.Labelframe(paned, text="Group chats  (double-click to open; anyone can join)",
                                padding=4)
        frame, self.groups = _tree(groups, (("name", "Group", 240), ("members", "Members", 140),
                                            ("status", "Status", 150)), 5, selectmode="browse")
        frame.pack(fill="both", expand=True)
        self.groups.tag_configure("notjoined", foreground="gray45")
        self.groups.tag_configure("unread", foreground="#c01c28")
        self.groups.bind("<Double-1>", lambda e: self.open_group())
        self.groups.bind("<Return>", lambda e: self.open_group())
        paned.add(groups, weight=2)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 0))
        self.selection_label = ttk.Label(actions, foreground="gray35")
        self.selection_label.pack(side="left")
        ttk.Button(actions, text="Send message", command=self.send_message).pack(side="right")
        ttk.Button(actions, text="Start group chat", command=self.start_group_chat).pack(
            side="right", padx=6)

        status = ttk.Frame(root, padding=(8, 0, 8, 6))
        status.pack(fill="x", side="bottom")
        self.status = ttk.Label(status, foreground="gray40")
        self.status.pack(side="left")
        ttk.Button(status, text="Settings", command=lambda: SettingsDialog(self)).pack(side="right")
        ttk.Button(status, text="Log", command=lambda: LogWindow(self)).pack(side="right", padx=4)

        root.bind("<Control-f>", lambda e: self.table.search_entry.focus_set())
        root.bind("<F5>", lambda e: self.do_refresh())
        dnd.hook_dropfiles(root, self.files_dropped)
        self._update_selection()
        self.table.search_entry.focus_set()

    # ------------------------------------------------------------ users

    def refresh_users(self):
        self.table.set_peers(self.node.peers())
        self._update_selection()

    def do_refresh(self):
        self.node.refresh()
        self.flash("Refreshing…")

    def _update_selection(self):
        n = len(self.table.selected_ids())
        self.selection_label.configure(
            text="Select people above (Ctrl/Shift-click for several)" if not n else
            "%d selected" % n)

    def send_message(self):
        ids = self.table.selected_ids()
        if not ids:
            messagebox.showinfo("LanChat", "Select one or more users in the list first.")
            return "break"
        SendWindow(self, ids)
        return "break"

    def files_dropped(self, paths):
        """Files dropped on the main window: send them to the selected people."""
        ids = self.table.selected_ids()
        if not ids:
            messagebox.showinfo("LanChat", "Select who should get the files in the list first, "
                                           "then drop the files again.")
            return
        SendWindow(self, ids, files=paths)

    def start_group_chat(self):
        ids = self.table.selected_ids()
        dlg = NameDialog(self.root, [self.node.display_name(i) for i in ids])
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        existing = self.node.find_group(dlg.result)
        if existing:
            if messagebox.askyesno("LanChat", "A group called “%s” already exists.\n"
                                              "Open it instead?" % existing["name"]):
                win = self.open_group(existing["id"])
                if ids and self.node.id in self.node.group_members(existing["id"]):
                    self.node.add_members(existing["id"], ids)
                win.render()
            return
        self.open_group(self.node.create_group(dlg.result, ids))

    # ------------------------------------------------------------ groups

    def refresh_groups(self):
        groups = sorted(self.node.groups(), key=lambda g: (not g["joined"], g["name"].lower()))
        wanted = [g["id"] for g in groups]
        for iid in set(self.groups.get_children()) - set(wanted):
            self.groups.delete(iid)
        for i, g in enumerate(groups):
            active = [m for m, e in g["members"].items() if e["in"]]
            n_ipmsg = sum(self.node.is_ipmsg(m) for m in active)
            members = "%d%s" % (len(active), "  (%d IPMsg)" % n_ipmsg if n_ipmsg else "")
            unread = self.unread.get(g["id"], 0)
            status = ("Joined" if g["joined"] else "Not joined") + \
                ("  ·  %d new" % unread if unread else "")
            tags = ("unread",) if unread else (() if g["joined"] else ("notjoined",))
            values = ("# " + g["name"], members, status)
            if self.groups.exists(g["id"]):
                self.groups.item(g["id"], values=values, tags=tags)
                self.groups.move(g["id"], "", i)
            else:
                self.groups.insert("", i, iid=g["id"], values=values, tags=tags)
        for w in self.group_windows.values():
            w.update_members()
        total = sum(self.unread.values())
        self.root.title("LanChat  (%d new)" % total if total else "LanChat")

    def open_group(self, gid=None):
        if gid is None:
            sel = self.groups.selection()
            if not sel:
                return None
            gid = sel[0]
        self.unread.pop(gid, None)
        win = self.group_windows.get(gid)
        if win is None:
            win = self.group_windows[gid] = GroupWindow(self, gid)
        _bring_to_front(win)
        self.refresh_groups()
        return win

    # ------------------------------------------------------------ files

    def start_download(self, conv, rec, f, dest, update):
        key = "%s:%d" % (rec["id"], f["fid"])
        self.transfers[key] = (update, dest)
        update("starting\u2026")
        try:
            self.node.download(conv, rec["id"], f["fid"], dest)
        except ValueError as exc:
            update("failed: %s" % exc)

    def _on_transfer(self, key, done, total, status):
        update, dest = self.transfers.get(key, (None, None))
        if status == "done":
            self.saved[key] = dest
            self.transfers.pop(key, None)
            self.flash("Saved %s" % dest)
            text, finished = "saved \u2713", True
        elif status == "progress":
            text, finished = "%d%%" % (100 * done // total if total else 100), False
        else:
            self.transfers.pop(key, None)
            text, finished = status.replace("error: ", "failed: "), False
        if update:
            try:
                update(text, finished)
            except tk.TclError:
                pass  # its window was closed

    # ------------------------------------------------------------ status

    def update_status(self):
        peers = [p for p in self.node.peers() if p["online"]]
        n_ipmsg = sum(p["kind"] == "ipmsg" for p in peers)
        me = self.node.name + (" (%s)" % self.node.group_name if self.node.group_name else "")
        self.status.configure(text="%s  \u00b7  %d online, %d via IPMsg  \u00b7  %s"
                              % (me, len(peers), n_ipmsg, self.node.ipmsg_status()[1]))
        if getattr(self, "tray", None):
            unread = sum(self.unread.values())
            self.tray.set_tooltip("LanChat \u2014 %s\n%d online%s" % (
                self.node.name, len(peers), ", %d new" % unread if unread else ""))

    def flash(self, text):
        self.status.configure(text=text, foreground="#1a5fb4")
        if self._flash_job:
            self.root.after_cancel(self._flash_job)

        def restore():
            self.status.configure(foreground="gray40")
            self.update_status()
        self._flash_job = self.root.after(3000, restore)

    def _notice(self, text):
        win = tk.Toplevel(self.root)
        win.title("LanChat")
        win.resizable(False, False)
        ttk.Label(win, text=text, padding=16).pack()
        ttk.Button(win, text="OK", command=win.destroy).pack(pady=(0, 12))
        win.bind("<Escape>", lambda e: win.destroy())
        _bring_to_front(win)

    # ------------------------------------------------------------ events

    def _poll(self):
        peers_dirty = groups_dirty = False
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "message":
                    self._on_message(event[1], event[2])
                elif kind == "transfer":
                    self._on_transfer(*event[1:])
                elif kind == "history":
                    win = self.group_windows.get(event[1][6:])
                    if win is not None:
                        win.render()
                elif kind == "groups":
                    groups_dirty = True
                elif kind == "tray":
                    if event[1] == "open":
                        self.show()
                    else:
                        self.close()
                        return
                elif kind == "opened":
                    self._notice("%s opened your sealed message.\n%s" % (
                        self.node.display_name(event[1]), fmt_time(time.time(), True)))
                else:
                    peers_dirty = True
        except queue.Empty:
            pass
        dnd.dispatch()  # files dropped from Explorer
        if peers_dirty:
            self.refresh_users()
            self.update_status()
            for w in self.group_windows.values():
                w.update_members()
        if groups_dirty:
            self.refresh_groups()
        self.root.after(150, self._poll)

    def _on_message(self, conv, rec):
        mine = rec.get("from_id") == self.node.id
        new = not mine and not rec.get("system")
        if conv.startswith("group:"):
            gid = conv[6:]
            win = self.group_windows.get(gid)
            if win is not None:
                win.append(rec)
                if new and self.root.focus_get() is None:
                    self.root.bell()
            elif new:
                if self.ui["popup_groups"]:
                    self.open_group(gid)  # pops up with the new message
                else:
                    self.unread[gid] = self.unread.get(gid, 0) + 1
                    self.refresh_groups()
                    self.update_status()
                    if self.tray and self.root.state() == "withdrawn":
                        g = self.node.group(gid) or {"name": "?"}
                        self.tray.balloon("# " + g["name"], "%s: %s" % (
                            rec.get("from_name", "?"), rec["text"]))
                self.root.bell()
            return
        if not new:
            return
        RecvWindow(self, conv, rec, offset=(self._popups % 8) * 24)
        self._popups += 1
        self.root.bell()

    def hide(self):
        """Close button: keep running in the tray, like IP Messenger."""
        self.root.withdraw()
        if not self.ui.get("tray_hint_shown"):
            self.tray.balloon("LanChat is still running",
                              "You'll still get messages. Click this icon to open LanChat, "
                              "right-click it to quit.")
            self.ui["tray_hint_shown"] = True
            self.save_ui()

    def show(self):
        self.root.deiconify()
        _bring_to_front(self.root)
        self.root.focus_force()

    def close(self):
        if self.tray:
            self.tray.stop()
        self.instance.close()
        self.node.stop()
        self.root.destroy()


def run(minimized=False, **node_kwargs):
    from .core import default_data_dir
    data_dir = node_kwargs.get("data_dir") or default_data_dir()
    if signal_running_instance(data_dir):
        return  # the running LanChat came to the front instead
    root = tk.Tk()
    App(root, minimized=minimized, **node_kwargs)
    root.mainloop()
