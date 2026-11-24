"""LanChat core: serverless peer discovery, direct chat and public group chat
on a LAN, plus a bridge to IP Messenger (IPMsg) users.

LanChat protocol (all payloads are UTF-8 JSON; every packet carries
``"app": "lanchat"``, ``"v": 1`` and
``"from": {"id", "name", "group", "host", "tcp_port", "ipmsg_id"}``)

Discovery - UDP broadcast to DISCOVERY_PORT
    hello   {"reply": bool}   announce / heartbeat; reply=True asks peers to answer
    bye                       leaving the network

Messages - TCP to the peer's tcp_port, one packet per connection,
receiver answers b"ok" once the packet is processed
    presence      answer to a hello with reply=True
    dm            {"msg"}            direct message
    public        {"msg"}            message to the "Everyone" room
    group_msg     {"group", "msg"}   message to a group (carries group state)
    group_update  {"group"}          group created / joined / left / people added
    group_sync    {"groups"}         all known groups, sent when a peer comes online
    history_req   {"group"}          I just joined: please send me the group's history
    history       {"group_id", "records"}  answer to history_req (members only)
    opened        {"msg_id"}         we opened the sender's sealed message
    file_get      {"fid", "offset"}  download a file we were offered; the answer is a
                                     JSON line {"ok", "size"|"error"} then the raw bytes
    ipmsg_relay   {"to", "text", "group"}  please deliver this to an IPMsg user
                                     (sent by nodes whose IPMsg port is busy)

    msg   = {"id", "text", "ts", "sealed"?, "files"?: [{"fid", "name", "size"}],
             "author"?: {"id", "name"}}
            author = the IPMsg user a group message is relayed for
    group = {"id", "name", "public", "members": {peer_id: {"name", "ts", "in", "ipmsg"?}}}

Groups are public: every LanChat node keeps a copy of every group and anyone
can join. Membership is a last-writer-wins set: for every member the entry
with the newest ``ts`` wins, so concurrent changes converge everywhere without
a server. A node that receives older state than it has sends its copy back.

IPMsg users appear as peers with kind "ipmsg". They can be added to groups:
the sender of a group message delivers it to IPMsg members as
"[Group] Name: text", and an IPMsg user's reply is posted into the group.
"""

import collections
import getpass
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import uuid

from . import ipmsg

APP = "lanchat"
PROTO_VERSION = 1
DISCOVERY_PORT = 50505
HEARTBEAT_INTERVAL = 10
PEER_TIMEOUT = 35
IPMSG_REFRESH = 180         # re-broadcast IPMsg BR_ENTRY (like IPMsg's "Refresh")
IPMSG_TIMEOUT = 2 * IPMSG_REFRESH + 30
CONNECT_TIMEOUT = 3
MAX_PACKET = 1 << 20
MAX_TEXT = 64 * 1024
MAX_OUTBOX = 1000
HISTORY_LIMIT = 1000
EVERYONE = "everyone"

IPMSG_HELP = """LanChat commands:
/groups - list the group chats
/join NAME - join a group
/leave NAME - leave a group
/dm TEXT - send TEXT privately instead of to a group
To talk in a group, reply to its messages. If you are in several groups,
start your message with [Group name]."""

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PREFIX_RE = re.compile(r"^\s*\[([^\]\n]+)\]\s*")


def new_id():
    return uuid.uuid4().hex


def default_data_dir(profile=None):
    base = os.path.join(os.path.expanduser("~"), ".lanchat")
    return os.path.join(base, profile) if profile else base


def _valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _windows_broadcasts():
    """Broadcast address of every IPv4 adapter, via iphlpapi.GetIpAddrTable."""
    import ctypes
    from ctypes import wintypes

    class Row(ctypes.Structure):  # MIB_IPADDRROW
        _fields_ = [("addr", wintypes.DWORD), ("index", wintypes.DWORD),
                    ("mask", wintypes.DWORD), ("bcast", wintypes.DWORD),
                    ("reasm", wintypes.DWORD), ("unused", ctypes.c_ushort),
                    ("type", ctypes.c_ushort)]

    found = set()
    size = wintypes.ULONG(0)
    get_table = ctypes.windll.iphlpapi.GetIpAddrTable
    get_table(None, ctypes.byref(size), False)
    buf = ctypes.create_string_buffer(size.value)
    if get_table(buf, ctypes.byref(size), False) != 0:
        return found
    count = wintypes.DWORD.from_buffer(buf).value
    rows = (Row * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
    for row in rows:
        ip = struct.pack("<I", row.addr)     # DWORDs hold network byte order
        mask = struct.pack("<I", row.mask)
        if ip[0] in (0, 127) or ip[:2] == b"\xa9\xfe":  # unset, loopback, link-local
            continue
        found.add(socket.inet_ntoa(bytes(a | (~m & 0xff) for a, m in zip(ip, mask))))
    return found


def safe_filename(name):
    """A received file name, made safe to save (no folders, no odd characters)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    return name[:200] or "file"


def _clean_files(files):
    """Validate an attachment list from the network."""
    out = []
    for f in list(files)[:100]:
        try:
            out.append({"fid": int(f["fid"]), "name": safe_filename(f["name"]),
                        "size": max(0, int(f["size"]))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _interface_broadcasts():
    """Broadcast address of every IPv4 interface (Linux and Windows, best effort).

    Windows sends 255.255.255.255 out of one adapter only, so with VPN or
    VMware/VirtualBox adapters we must also use each subnet's own broadcast."""
    found = set()
    if os.name == "nt":
        try:
            return _windows_broadcasts()
        except Exception:
            return found
    if not sys.platform.startswith("linux"):
        return found
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, ifname in socket.if_nameindex():
                try:
                    req = struct.pack("256s", ifname[:15].encode())
                    res = fcntl.ioctl(s.fileno(), 0x8919, req)  # SIOCGIFBRDADDR
                except OSError:
                    continue
                addr = socket.inet_ntoa(res[20:24])
                if addr != "0.0.0.0":
                    found.add(addr)
    except Exception:
        pass
    return found


class Peer:
    FIELDS = ("name", "ip", "tcp_port", "ipmsg_id",          # LanChat peers
              "ipmsg_port", "user", "host", "group", "utf8", "encoding", "away")  # IPMsg

    def __init__(self, peer_id, kind):
        self.id = peer_id
        self.kind = kind
        for f in self.FIELDS:
            setattr(self, f, None)
        self.name = "?"
        self.last_seen = 0.0
        self.online = False

    def as_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d.update(id=self.id, kind=self.kind, online=self.online)
        return d


class Node:
    """One LanChat participant.

    ``on_event(kind, *args)`` is called from background threads with:
        ("peers",)                 peer list / online state changed
        ("groups",)                group list / membership changed
        ("message", conv, record)  a message was added to conversation ``conv``
        ("opened", peer_id)        someone opened a sealed message we sent
        ("history", conv)          older messages were merged into ``conv``
        ("transfer", key, done, total, status)  file download progress; status is
                                   "progress", "done" or "error: ..."; key = "<msg id>:<fid>"

    Conversation ids: ``"everyone"``, ``"dm:<peer_id>"``, ``"group:<group_id>"``.
    """

    def __init__(self, name=None, group=None, data_dir=None, discovery_port=DISCOVERY_PORT,
                 tcp_port=None, extra_broadcasts=(), on_event=None,
                 ipmsg_enabled=True, ipmsg_port=ipmsg.IPMSG_PORT, ipmsg_bind="",
                 ipmsg_targets=(), ipmsg_user=None, ipmsg_host=None, ipmsg_encoding="utf-8"):
        self.data_dir = data_dir or default_data_dir()
        os.makedirs(os.path.join(self.data_dir, "history"), exist_ok=True)
        self.discovery_port = discovery_port
        self._tcp_port_pref = discovery_port if tcp_port is None else tcp_port
        self.tcp_port = None
        self.extra_broadcasts = list(extra_broadcasts)
        self.on_event = on_event or (lambda *args: None)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._peers = {}
        self._history = {}
        self._seen = collections.OrderedDict()
        self._senders = {}
        self._last_group = {}   # IPMsg peer id -> group its replies go to

        cfg = self._load_json("config.json", {})
        self.id = cfg.get("id") if _valid_id(cfg.get("id")) else new_id()
        self.name = name or cfg.get("name") or getpass.getuser()
        self.group_name = group if group is not None else str(cfg.get("group", ""))
        self.hostname = socket.gethostname()
        self._save_config()
        self._groups = self._load_json("groups.json", {})
        self._outbox = collections.defaultdict(list)
        for pid, items in self._load_json("outbox.json", {}).items():
            # older versions stored raw TCP payload strings
            self._outbox[pid] = [i if isinstance(i, dict) else {"t": "tcp", "data": i}
                                 for i in items]

        shares = self._load_json("shares.json", {})
        self._next_fid = int(shares.get("next_fid", 1))
        self._shares = {int(k): v for k, v in shares.get("files", {}).items()}

        self.ipmsg_enabled = ipmsg_enabled
        self.bridge = ipmsg.Bridge(self, port=ipmsg_port, bind=ipmsg_bind, targets=ipmsg_targets,
                                   user=ipmsg_user, host=ipmsg_host, encoding=ipmsg_encoding)

    # ------------------------------------------------------------ lifecycle

    def start(self):
        self._stop.clear()
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":  # on Windows SO_REUSEADDR would allow port stealing
            self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._tcp.bind(("", self._tcp_port_pref))
        except OSError:  # e.g. a second instance on the same machine
            self._tcp.bind(("", 0))
        self._tcp.listen(32)
        self._tcp.settimeout(0.5)
        self.tcp_port = self._tcp.getsockname()[1]

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp.bind(("", self.discovery_port))
        self._udp.settimeout(0.5)

        for target in (self._udp_loop, self._tcp_loop, self._heartbeat_loop):
            self._spawn(target)
        if self.ipmsg_enabled:
            self.bridge.start()
        self._broadcast("hello", reply=True)

    def stop(self):
        if self._stop.is_set():
            return
        try:
            self._broadcast("bye")
            self.bridge.announce(ipmsg.BR_EXIT)
        except OSError:
            pass
        self._stop.set()
        for t in list(self._threads):
            t.join(timeout=2)
        self._threads.clear()
        with self._lock:
            self._senders.clear()
        for s in (self._tcp, self._udp):
            s.close()
        self.bridge.close()

    def _spawn(self, target, *args):
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._threads.append(t)
        return t

    # ------------------------------------------------------------ public API

    def ipmsg_status(self):
        """(active, message) for the IP Messenger bridge."""
        if not self.ipmsg_enabled:
            return False, "IPMsg off"
        if self.bridge.active:
            return True, "IPMsg on"
        return False, "IPMsg off: " + (self.bridge.error or "not started")

    def set_name(self, name):
        self.set_profile(name, self.group_name)

    def set_profile(self, name, group):
        """Change the name and group (the IPMsg "group" column) others see."""
        name = name.strip()
        if name:
            self.name = name[:60]
            self.group_name = group.strip()[:60]
            self._save_config()
            self._broadcast("hello", reply=False)
            self.bridge.announce(ipmsg.BR_ABSENCE)

    def refresh(self):
        """Like IPMsg's Refresh: forget who's online and ask everybody again."""
        with self._lock:
            for p in self._peers.values():
                p.online = False
        self.on_event("peers")
        self._broadcast("hello", reply=True)
        self.bridge.announce(ipmsg.BR_ENTRY)

    def open_sealed(self, conv, msg_id):
        """The user opened a sealed message: tell its sender."""
        with self._lock:
            rec = next((r for r in self._load_history(conv) if r.get("id") == msg_id), None)
            if not rec or not rec.get("sealed") or rec.get("opened"):
                return
            rec["opened"] = True
            pid = rec["from_id"]
            p = self._peers.get(pid)
            if rec.get("ipmsg_pkt"):
                if p and p.ip:
                    self.bridge.send_read((p.ip, p.ipmsg_port), rec["ipmsg_pkt"])
            else:
                self._send(pid, self._packet("opened", msg_id=msg_id))

    def peers(self):
        """Known people, LanChat and IPMsg (excluding LanChat users' own IPMsg presence)."""
        with self._lock:
            hidden = self._hidden_ids()
            return [p.as_dict() for p in self._peers.values() if p.id not in hidden]

    def peer(self, peer_id):
        with self._lock:
            p = self._peers.get(peer_id)
            return p.as_dict() if p else None

    def is_ipmsg(self, peer_id):
        with self._lock:
            p = self._peers.get(peer_id)
            if p:
                return p.kind == "ipmsg"
            return any(g["members"].get(peer_id, {}).get("ipmsg") for g in self._groups.values())

    def display_name(self, peer_id):
        if peer_id == self.id:
            return self.name
        with self._lock:
            p = self._peers.get(peer_id)
            if p:
                return p.name
            for g in self._groups.values():
                if peer_id in g["members"]:
                    return g["members"][peer_id]["name"]
        return "unknown"

    def groups(self):
        """All public groups that still have members. Each has ``joined`` set."""
        with self._lock:
            out = []
            for g in self._groups.values():
                if g.get("public", True) and self._active(g):
                    g = json.loads(json.dumps(g))
                    g["joined"] = self._is_member(g, self.id)
                    out.append(g)
            return out

    def group(self, group_id):
        with self._lock:
            g = self._groups.get(group_id)
            return json.loads(json.dumps(g)) if g else None

    def group_members(self, group_id):
        with self._lock:
            g = self._groups.get(group_id)
            return self._active(g) if g else []

    def history(self, conv):
        with self._lock:
            return list(self._load_history(conv))

    def send_files(self, conv, paths, text="", sealed=False):
        """Offer files to a person (``dm:``). The receiver downloads them from
        us while we're online; returns the history record."""
        text = text.strip()
        paths = [os.path.abspath(x) for x in paths]
        for path in paths:
            if not os.path.isfile(path):
                raise ValueError("%s is not a file (folders can't be sent yet)" % path)
        if len(text) > MAX_TEXT:
            raise ValueError("message too long")
        if not conv.startswith("dm:"):
            raise ValueError("files can only be sent to people, not to group chats")
        pid = conv[3:]
        files = self._share(paths, [pid])
        msg = {"id": new_id(), "text": text, "ts": time.time(), "files": files}
        if sealed:
            msg["sealed"] = True
        with self._lock:
            if self.is_ipmsg(pid):
                self._send_ipmsg(pid, text, files=files, sealed=sealed)
            else:
                self._send(pid, self._packet("dm", msg=msg))
            self._seen_id(msg["id"])
        record = {"id": msg["id"], "from_id": self.id, "from_name": self.name,
                  "text": text, "ts": msg["ts"], "files": files}
        if sealed:
            record["sealed"] = True
        self._add_history(conv, record)
        return record

    def download(self, conv, msg_id, fid, dest):
        """Save an attachment of a received message to ``dest``, in the
        background. Progress arrives as ("transfer", ...) events."""
        with self._lock:
            rec = next((r for r in self._load_history(conv) if r.get("id") == msg_id), None)
        f = next((f for f in (rec or {}).get("files", []) if f["fid"] == fid), None)
        if not f:
            raise ValueError("no such file")
        threading.Thread(target=self._download, args=(rec, f, dest), daemon=True).start()

    def conversations(self):
        """Every conversation with saved history (for the log viewer)."""
        out = []
        for fn in os.listdir(self._path("history")):
            if fn.endswith(".jsonl"):
                conv = fn[:-6].replace("_", ":", 1)
                if conv == EVERYONE or conv.split(":")[0] in ("dm", "group"):
                    out.append(conv)
        return out

    def send_text(self, conv, text, sealed=False):
        """Send to a conversation. ``sealed`` (direct messages only): the
        receiver must click to open it, and we're told when they do."""
        text = text.strip()
        if not text:
            return None
        if len(text) > MAX_TEXT:
            raise ValueError("message too long")
        if conv.startswith("group:"):
            gid = conv[6:]
            with self._lock:
                g = self._groups.get(gid)
                if not g or not self._is_member(g, self.id):
                    raise ValueError("join the group first")
            return self._post_group(gid, text, self.id, self.name)

        msg = {"id": new_id(), "text": text, "ts": time.time()}
        if sealed:
            msg["sealed"] = True
        with self._lock:
            if conv == EVERYONE:
                pkt = self._packet("public", msg=msg)
                for p in self._peers.values():
                    if p.online and p.kind == "lanchat":
                        self._send(p.id, pkt, keep=False)
            elif conv.startswith("dm:"):
                pid = conv[3:]
                if self.is_ipmsg(pid):
                    self._send_ipmsg(pid, text, sealed=sealed)
                else:
                    self._send(pid, self._packet("dm", msg=msg))
            else:
                raise ValueError("unknown conversation %r" % conv)
            self._seen_id(msg["id"])
        record = {"id": msg["id"], "from_id": self.id, "from_name": self.name,
                  "text": text, "ts": msg["ts"]}
        if sealed:
            record["sealed"] = True
        self._add_history(conv, record)
        return record

    def create_group(self, name, member_ids=()):
        name = re.sub(r"[\[\]\n]", "", name).strip()[:60] or "Group"
        now = time.time()
        with self._lock:
            members = {self.id: {"name": self.name, "ts": now, "in": True}}
            for mid in member_ids:  # everybody in the very first update
                if _valid_id(mid) and mid != self.id:
                    members[mid] = {"name": self.display_name(mid), "ts": now, "in": True}
                    if self.is_ipmsg(mid):
                        members[mid]["ipmsg"] = True
            g = {"id": new_id(), "name": name, "public": True, "members": members}
            self._merge_group(g, created=True)
            self._publish_group(g["id"])
        for mid in members:
            if mid != self.id and self.is_ipmsg(mid):
                self._welcome_ipmsg(mid, g["id"], "%s added you to" % self.name)
        return g["id"]

    def join_group(self, group_id):
        self._set_membership(group_id, [self.id], True)

    def leave_group(self, group_id):
        self._set_membership(group_id, [self.id], False)

    def add_members(self, group_id, member_ids):
        with self._lock:
            g = self._groups.get(group_id)
            if not g or not self._is_member(g, self.id):
                raise ValueError("join the group first")
            added = [m for m in member_ids
                     if _valid_id(m) and m != self.id and not self._is_member(g, m)]
            self._set_membership(group_id, added, True)  # one update for everybody
        for mid in added:
            if self.is_ipmsg(mid):
                self._welcome_ipmsg(mid, group_id, "%s added you to" % self.name)

    def remove_members(self, group_id, member_ids):
        """Take people out of a group (any member may do this)."""
        with self._lock:
            g = self._groups.get(group_id)
            if not g or not self._is_member(g, self.id):
                raise ValueError("join the group first")
            removed = [m for m in member_ids if m != self.id and self._is_member(g, m)]
            self._set_membership(group_id, removed, False)
            name = g["name"]
        for mid in removed:
            if self.is_ipmsg(mid):
                self._send_ipmsg(mid, "[%s] %s removed you from the LanChat group \u201c%s\u201d."
                                 % (name, self.name, name), keep=False)

    def find_group(self, name):
        """A group (with members) by name, case-insensitive, or None."""
        g = self._find_group(name)
        return json.loads(json.dumps(g)) if g else None

    # ------------------------------------------------------------ groups

    @staticmethod
    def _is_member(g, peer_id):
        e = g["members"].get(peer_id)
        return bool(e and e["in"])

    @staticmethod
    def _active(g):
        return [m for m, e in g["members"].items() if e["in"]]

    def _find_group(self, name):
        name = name.strip().lstrip("#").strip().lower()
        with self._lock:
            for g in self._groups.values():
                if g["name"].lower() == name and self._active(g):
                    return g
        return None

    def _set_membership(self, group_id, peer_ids, joined):
        """Add or remove several people in ONE group update, so nobody sees a
        half-applied change (e.g. a newcomer posting before the others are in)."""
        with self._lock:
            g = self._groups.get(group_id)
            if not g:
                return
            changes = {}
            for pid in peer_ids:
                if self._is_member(g, pid) == joined:
                    continue
                old = g["members"].get(pid)
                entry = {"name": self.display_name(pid), "in": joined,
                         "ts": max(time.time(), old["ts"] + 0.001) if old else time.time()}
                if self.is_ipmsg(pid):
                    entry["ipmsg"] = True
                changes[pid] = entry
            if not changes:
                return
            update = {"id": group_id, "name": g["name"], "public": g.get("public", True),
                      "members": changes}
            self._merge_group(update)
            # also tell whoever just left, so their copy is up to date
            self._publish_group(group_id, also=list(changes))

    def _publish_group(self, group_id, also=()):
        """Send a group's state to its members (queued if offline) and to every
        online LanChat user, so everybody sees the public group."""
        with self._lock:
            g = self._groups[group_id]
            pkt = self._packet("group_update", group=g)
            members = set(self._active(g)) | set(also)
            online = {p.id for p in self._peers.values() if p.online and p.kind == "lanchat"}
            for pid in members | online:
                if pid != self.id and not self.is_ipmsg(pid):
                    self._send(pid, pkt, keep=pid in members)

    def _knows_more(self, gid, incoming):
        """True if our copy of the group has entries newer than ``incoming``."""
        theirs = incoming.get("members", {})
        with self._lock:
            for mid, e in self._groups[gid]["members"].items():
                t = theirs.get(mid)
                try:
                    if t is None or e["ts"] > float(t["ts"]):
                        return True
                except (KeyError, TypeError, ValueError):
                    return True
        return False

    def _merge_group(self, incoming, created=False):
        """Merge a (possibly remote) group state. Returns the group id.
        created: we just created this group ourselves."""
        gid = incoming["id"]
        if not _valid_id(gid) or not isinstance(incoming.get("members"), dict):
            raise ValueError("bad group")
        with self._lock:
            cur = self._groups.get(gid)
            is_new = cur is None
            if is_new:
                name = re.sub(r"[\[\]\n]", "", str(incoming.get("name", "Group")))[:60]
                cur = {"id": gid, "name": name or "Group",
                       "public": bool(incoming.get("public", True)), "members": {}}
            before = set(self._active(cur))
            changed = is_new
            for mid, e in incoming["members"].items():
                if not _valid_id(mid):
                    continue
                entry = {"name": str(e["name"])[:100], "ts": float(e["ts"]), "in": bool(e["in"])}
                if e.get("ipmsg"):
                    entry["ipmsg"] = True
                old = cur["members"].get(mid)
                if old is None or entry["ts"] > old["ts"]:
                    cur["members"][mid] = entry
                    changed = True
            if not changed:
                return gid
            self._groups[gid] = cur
            self._save_json("groups.json", self._groups)
            after = set(self._active(cur))

        conv = "group:" + gid
        if self.id in after:
            if self.id not in before:
                others = sorted(self.display_name(m) for m in after if m != self.id)
                if created:
                    text = ("You created “%s”%s. Everyone on the LAN can see and join it."
                            % (cur["name"], " with " + ", ".join(others) if others else ""))
                else:
                    text = "You joined “%s”. Members: %s" % (
                        cur["name"], ", ".join(others) or "only you")
                self._add_history(conv, self._system_record(text))
                if others and not created:
                    self._request_history(gid)
            else:
                for m in sorted(after - before, key=self.display_name):
                    self._add_history(conv, self._system_record(self.display_name(m) + " joined"))
                for m in sorted(before - after, key=self.display_name):
                    self._add_history(conv, self._system_record(self.display_name(m) + " left"))
        elif self.id in before:
            self._add_history(conv, self._system_record("You left the group"))
        self.on_event("groups")
        return gid

    def _post_group(self, gid, text, author_id, author_name, files=None):
        """Deliver a message from ``author`` (us, or an IPMsg user we relay for)."""
        msg = {"id": new_id(), "text": text, "ts": time.time()}
        if files:
            msg["files"] = files
        if author_id != self.id:
            msg["author"] = {"id": author_id, "name": author_name}
        with self._lock:
            g = self._groups[gid]
            pkt = self._packet("group_msg", group=g, msg=msg)
            ipmsg_text = "[%s] %s: %s" % (g["name"], author_name, text)
            for mid in self._active(g):
                if mid in (self.id, author_id):
                    continue
                if self.is_ipmsg(mid):
                    self._send_ipmsg(mid, ipmsg_text, group=gid, files=files)
                else:
                    self._send(mid, pkt)
            self._seen_id(msg["id"])
            member = self._is_member(g, self.id)
        record = {"id": msg["id"], "from_id": author_id, "from_name": author_name,
                  "text": text, "ts": msg["ts"]}
        if files:
            record["files"] = files
        if member:
            self._add_history("group:" + gid, record)
        return record

    @staticmethod
    def _system_record(text):
        return {"id": new_id(), "system": True, "text": text, "ts": time.time()}

    # ------------------------------------------------------------ IPMsg users

    def _ipmsg_entry(self, user, host, addr, nick, group, utf8, away, encoding=None):
        self._update_peer(ipmsg.peer_id(user, host), "ipmsg", name=nick[:100], user=user,
                          host=host, ip=addr[0], ipmsg_port=addr[1], group=group,
                          utf8=utf8, encoding=encoding, away=away)

    def _ipmsg_exit(self, user, host):
        self._set_offline(ipmsg.peer_id(user, host))

    def _ipmsg_opened(self, user, host):
        self.on_event("opened", ipmsg.peer_id(user, host))

    def _ipmsg_message(self, user, host, addr, text, sealed=False, packet_no=None, files=None):
        pid = ipmsg.peer_id(user, host)
        with self._lock:
            known = pid in self._peers
        if known:
            self._update_peer(pid, "ipmsg", ip=addr[0], ipmsg_port=addr[1])
        else:  # never saw their BR_ENTRY
            self._update_peer(pid, "ipmsg", name=user, user=user, host=host,
                              ip=addr[0], ipmsg_port=addr[1], utf8=False, away=False)
        if pid in self._hidden_ids():
            return
        name = self.display_name(pid)
        if files:  # the IPMsg sender only lets its recipients download: keep it direct
            rec = {"id": new_id(), "from_id": pid, "from_name": name, "text": text,
                   "ts": time.time(), "files": _clean_files(files), "ipmsg_files_pkt": packet_no}
            if sealed:
                rec.update(sealed=True, opened=False, ipmsg_pkt=packet_no)
            self._add_history("dm:" + pid, rec)
            return
        if sealed:  # private by nature: never a group message or a command
            self._add_history("dm:" + pid, {"id": new_id(), "from_id": pid, "from_name": name,
                                            "text": text, "ts": time.time(), "sealed": True,
                                            "opened": False, "ipmsg_pkt": packet_no})
            return
        stripped = text.strip()
        if stripped.startswith("/") and self._ipmsg_command(pid, stripped):
            return
        gid, body = self._route_ipmsg(pid, text)
        if gid:
            self._post_group(gid, body, pid, name)
        else:
            self._add_history("dm:" + pid, {"id": new_id(), "from_id": pid, "from_name": name,
                                            "text": body, "ts": time.time()})

    def _route_ipmsg(self, pid, text):
        """Decide whether an IPMsg user's message is for a group. -> (gid|None, text)"""
        if text.strip().lower().startswith("/dm "):
            return None, text.strip()[4:].strip()
        with self._lock:
            mine = {g["id"]: g for g in self._groups.values() if self._is_member(g, pid)}
        if not mine:
            return None, text
        lines = text.split("\n")
        quoted = [ln for ln in lines if ln.lstrip().startswith(">")]
        body = "\n".join(ln for ln in lines if not ln.lstrip().startswith(">")).strip()
        by_name = {g["name"].lower(): gid for gid, g in mine.items()}
        m = _PREFIX_RE.match(body)
        if m and m.group(1).strip().lower() in by_name:
            return by_name[m.group(1).strip().lower()], body[m.end():].strip()
        for ln in quoted:  # IPMsg quotes the message being replied to
            m = re.search(r"\[([^\]\n]+)\]", ln)
            if m and m.group(1).strip().lower() in by_name:
                return by_name[m.group(1).strip().lower()], body or text.strip()
        last = self._last_group.get(pid)
        if last in mine:
            return last, body or text.strip()
        return None, text

    def _ipmsg_command(self, pid, text):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/help", "/?"):
            reply = IPMSG_HELP
        elif cmd == "/groups":
            lines = []
            for g in sorted(self.groups(), key=lambda g: g["name"].lower()):
                mark = " (you're in)" if self._is_member(g, pid) else ""
                lines.append("[%s] %d members%s" % (g["name"], len(self._active(g)), mark))
            reply = ("Group chats:\n" + "\n".join(lines) + "\n\n/join NAME to join") if lines \
                else "There are no group chats yet."
        elif cmd in ("/join", "/leave"):
            g = self._find_group(arg)
            if not g:
                reply = "No group called “%s”. Send /groups to see them." % arg.strip()
            elif cmd == "/join":
                if self._is_member(g, pid):
                    reply = "You are already in [%s]." % g["name"]
                else:
                    self._set_membership(g["id"], [pid], True)
                    self._welcome_ipmsg(pid, g["id"], "You joined")
                    return True
            else:
                self._set_membership(g["id"], [pid], False)
                if self._last_group.get(pid) == g["id"]:
                    del self._last_group[pid]
                reply = "You left [%s]." % g["name"]
        else:
            return False  # not a command: treat as a normal message
        self._send_ipmsg(pid, reply, keep=False)
        return True

    def _welcome_ipmsg(self, pid, gid, how):
        with self._lock:
            g = self._groups[gid]
            names = sorted(self.display_name(m) for m in self._active(g) if m != pid)
        text = ("[%s] %s the LanChat group “%s” (members: %s).\n"
                "Reply to group messages to talk to everyone. Send /help for commands."
                % (g["name"], how, g["name"], ", ".join(names)))
        self._send_ipmsg(pid, text, group=gid)

    def _hidden_ids(self):
        """IPMsg identities that belong to LanChat users (shown once, as LanChat)."""
        with self._lock:
            ids = {p.ipmsg_id for p in self._peers.values() if p.kind == "lanchat" and p.ipmsg_id}
        ids.add(self.bridge.id)
        return ids

    # ------------------------------------------------------------ incoming

    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                data, (ip, _) = self._udp.recvfrom(65535)
            except (socket.timeout, ConnectionResetError):
                # Windows reports an earlier "port unreachable" as a reset here
                continue
            except OSError:
                break
            try:
                pkt = json.loads(data.decode("utf-8"))
                if pkt.get("app") != APP:
                    continue
                pid = self._touch_lanchat(pkt["from"], ip)
                if not pid:
                    continue
                if pkt["type"] == "hello" and pkt.get("reply"):
                    self._send(pid, self._packet("presence"), keep=False)
                elif pkt["type"] == "bye":
                    self._set_offline(pid)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue

    def _tcp_loop(self):
        while not self._stop.is_set():
            try:
                conn, (ip, _) = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, ip), daemon=True).start()

    def _handle_conn(self, conn, ip):
        with conn:
            try:
                conn.settimeout(10)
                chunks, size = [], 0
                while size <= MAX_PACKET:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if size > MAX_PACKET:
                    return
                pkt = json.loads(b"".join(chunks).decode("utf-8"))
                if pkt.get("type") == "file_get":
                    self._serve_file(conn, pkt, ip)
                    return
                self._handle_packet(pkt, ip)
                conn.sendall(b"ok")
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass

    def _handle_packet(self, pkt, ip):
        if pkt.get("app") != APP:
            return
        pid = self._touch_lanchat(pkt["from"], ip)
        if not pid:
            return
        kind = pkt["type"]
        if kind == "group_update":
            gid = self._merge_group(pkt["group"])
            if self._knows_more(gid, pkt["group"]):
                # We hold changes the sender missed: spread the merged state.
                self._publish_group(gid, also=[pid])
            return
        if kind == "group_sync":
            newer = []
            for g in pkt["groups"]:
                try:
                    gid = self._merge_group(g)
                    if self._knows_more(gid, g):
                        newer.append(gid)
                except (ValueError, KeyError, TypeError):
                    continue
            if newer:
                with self._lock:
                    groups = [self._groups[gid] for gid in newer]
                    self._send(pid, self._packet("group_sync", groups=groups), keep=False)
            return
        if kind == "history_req":
            gid = self._merge_group(pkt["group"])
            with self._lock:
                g = self._groups[gid]
                if not (self._is_member(g, self.id) and self._is_member(g, pid)):
                    return  # only members get the history
                records = [r for r in self._load_history("group:" + gid) if not r.get("system")]
                while records and len(json.dumps(records)) > MAX_PACKET // 2:
                    records = records[len(records) // 4 + 1:]  # keep the newest
                self._send(pid, self._packet("history", group_id=gid, records=records), keep=False)
            return
        if kind == "history":
            gid = pkt["group_id"]
            with self._lock:
                g = self._groups.get(gid) if _valid_id(gid) else None
                ok = g and self._is_member(g, self.id) and self._is_member(g, pid)
            if ok and isinstance(pkt["records"], list):
                self._merge_history("group:" + gid, pkt["records"])
            return
        if kind == "opened":
            self.on_event("opened", pid)
            return
        if kind == "ipmsg_relay":
            to = pkt["to"]
            if _valid_id(to) and self.bridge.active:
                group = pkt.get("group") if _valid_id(pkt.get("group")) else None
                self._send_ipmsg(to, str(pkt["text"])[:MAX_TEXT], group=group)
            return
        if kind not in ("dm", "public", "group_msg"):
            return

        msg = pkt["msg"]
        with self._lock:
            if not _valid_id(msg["id"]) or not self._seen_id(msg["id"]):
                return
        author_id, author_name = pid, str(pkt["from"]["name"])
        if kind == "dm":
            conv = "dm:" + pid
        elif kind == "public":
            conv = EVERYONE
        else:
            gid = self._merge_group(pkt["group"])
            with self._lock:
                g = self._groups[gid]
                if self._knows_more(gid, pkt["group"]):
                    # Sender has a stale member list (maybe we even left).
                    self._send(pid, self._packet("group_update", group=g), keep=False)
                if not self._is_member(g, self.id):
                    return
            author = msg.get("author")
            if isinstance(author, dict) and _valid_id(author.get("id")):
                author_id, author_name = author["id"], str(author.get("name", "?"))[:100]
            conv = "group:" + gid
        text = str(msg["text"])[:MAX_TEXT]
        record = {"id": msg["id"], "from_id": author_id, "from_name": author_name,
                  "text": text, "ts": float(msg["ts"])}
        if msg.get("files"):
            record["files"] = _clean_files(msg["files"])
        if kind == "dm" and msg.get("sealed"):
            record.update(sealed=True, opened=False)
        self._add_history(conv, record)

    def _seen_id(self, msg_id):
        """Record a message id; False if it was already seen."""
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = True
        while len(self._seen) > 10000:
            self._seen.popitem(last=False)
        return True

    # ------------------------------------------------------------ peers

    def _touch_lanchat(self, frm, ip):
        """Update a LanChat peer from a packet's "from" field. Returns its id or None."""
        pid = frm["id"]
        if not _valid_id(pid) or pid == self.id:
            return None
        ipmsg_id = frm.get("ipmsg_id") if _valid_id(frm.get("ipmsg_id")) else None
        self._update_peer(pid, "lanchat", name=str(frm["name"])[:100] or "?", ip=ip,
                          tcp_port=int(frm["tcp_port"]), ipmsg_id=ipmsg_id,
                          host=str(frm.get("host") or "")[:100],
                          group=str(frm.get("group") or "")[:100])
        return pid

    def _update_peer(self, pid, kind, **fields):
        with self._lock:
            p = self._peers.get(pid)
            if p is None:
                p = self._peers[pid] = Peer(pid, kind)
            came_online = not p.online
            changed = came_online or any(getattr(p, k) != v for k, v in fields.items())
            for k, v in fields.items():
                setattr(p, k, v)
            p.last_seen = time.time()
            p.online = True
            if came_online:
                self._flush_outbox(pid)
                if kind == "lanchat":
                    if self._groups:
                        self._send(pid, self._packet("group_sync",
                                                     groups=list(self._groups.values())), keep=False)
                    if p.ipmsg_id and not self.bridge.active:
                        self._flush_ipmsg_outboxes()  # a relay became available
        if changed:
            self.on_event("peers")

    def _set_offline(self, pid):
        with self._lock:
            p = self._peers.get(pid)
            if not p or not p.online:
                return
            p.online = False
        self.on_event("peers")

    def _heartbeat_loop(self):
        beats = 0
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            beats += 1
            self._broadcast("hello", reply=False)
            if beats % (IPMSG_REFRESH // HEARTBEAT_INTERVAL) == 0:
                self.bridge.announce(ipmsg.BR_ENTRY)
            now = time.time()
            with self._lock:
                expired = [p.id for p in self._peers.values() if p.online and now - p.last_seen >
                           (PEER_TIMEOUT if p.kind == "lanchat" else IPMSG_TIMEOUT)]
            for pid in expired:
                self._set_offline(pid)

    # ------------------------------------------------------------ outgoing

    def _packet(self, kind, **fields):
        pkt = {"app": APP, "v": PROTO_VERSION, "type": kind,
               "from": {"id": self.id, "name": self.name, "group": self.group_name,
                        "host": self.hostname, "tcp_port": self.tcp_port,
                        "ipmsg_id": self.bridge.id if self.bridge.active else None}}
        pkt.update(fields)
        return pkt

    def _broadcast_targets(self):
        return {"255.255.255.255"} | _interface_broadcasts() | set(self.extra_broadcasts)

    def _broadcast(self, kind, **fields):
        data = json.dumps(self._packet(kind, **fields)).encode("utf-8")
        for addr in self._broadcast_targets():
            try:
                self._udp.sendto(data, (addr, self.discovery_port))
            except OSError:
                pass

    def _send(self, pid, pkt, keep=True):
        """Queue a LanChat packet. keep=True: hold it until the peer is reachable."""
        self._enqueue(pid, {"t": "tcp", "data": json.dumps(pkt)}, keep)

    def _send_ipmsg(self, pid, text, keep=True, group=None, sealed=False, files=None):
        """Queue a text message for an IPMsg user."""
        item = {"t": "ipmsg", "text": text}
        if files:
            item["files"] = files
        if sealed:
            item["sealed"] = True
        if group:
            item["group"] = group
            self._last_group[pid] = group  # their reply belongs to this group
        self._enqueue(pid, item, keep)

    def _enqueue(self, pid, item, keep):
        with self._lock:
            q = self._senders.get(pid)
            if q is None:
                q = self._senders[pid] = queue.Queue()
                self._spawn(self._sender_loop, pid, q)
        q.put((item, keep))

    def _sender_loop(self, pid, q):
        # One thread per peer keeps that peer's messages in order.
        while not self._stop.is_set():
            try:
                item, keep = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item["t"] == "ipmsg" and not self.bridge.active:
                ok = self._relay_ipmsg(pid, item)
            else:
                with self._lock:
                    p = self._peers.get(pid)
                    p = p.as_dict() if p and p.online else None
                ok = False
                if p:
                    if item["t"] == "tcp":
                        ok = self._deliver((p["ip"], p["tcp_port"]), item["data"])
                    else:
                        ok = self.bridge.send_message((p["ip"], p["ipmsg_port"]), item["text"],
                                                      bool(p["utf8"]), p["encoding"],
                                                      item.get("sealed", False),
                                                      item.get("files"))
                    if not ok:
                        self._set_offline(pid)
            if not ok and keep:
                with self._lock:
                    box = self._outbox[pid]
                    box.append(item)
                    del box[:-MAX_OUTBOX]
                    self._save_json("outbox.json", self._outbox)

    def _relay_ipmsg(self, pid, item):
        """Our IPMsg port is busy: ask a LanChat peer that has one to deliver."""
        with self._lock:
            relays = [(p.ip, p.tcp_port) for p in self._peers.values()
                      if p.kind == "lanchat" and p.online and p.ipmsg_id]
        text = item["text"]
        if item.get("files"):  # the relay can't serve our files: say what was sent
            text += "\n[file(s) sent in LanChat: %s]" % ", ".join(f["name"] for f in item["files"])
        pkt = json.dumps(self._packet("ipmsg_relay", to=pid, text=text,
                                      group=item.get("group")))
        return any(self._deliver(addr, pkt) for addr in relays)

    @staticmethod
    def _deliver(target, data):
        try:
            with socket.create_connection(target, timeout=CONNECT_TIMEOUT) as s:
                s.settimeout(10)
                s.sendall(data.encode("utf-8"))
                s.shutdown(socket.SHUT_WR)
                return s.recv(2) == b"ok"
        except OSError:
            return False

    def _flush_outbox(self, pid):
        with self._lock:
            pending = self._outbox.pop(pid, [])
            if not pending:
                return
            self._save_json("outbox.json", self._outbox)
            for item in pending:
                self._enqueue(pid, item, True)

    def _flush_ipmsg_outboxes(self):
        with self._lock:
            for pid, items in list(self._outbox.items()):
                if items and all(i["t"] == "ipmsg" for i in items):
                    self._flush_outbox(pid)

    # ------------------------------------------------------------ storage

    def _path(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def _load_json(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save_json(self, name, value):
        tmp = self._path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f)
        os.replace(tmp, self._path(name))

    def _save_config(self):
        self._save_json("config.json", {"id": self.id, "name": self.name,
                                        "group": self.group_name})

    def _history_file(self, conv):
        return self._path("history", conv.replace(":", "_") + ".jsonl")

    def _load_history(self, conv):
        if conv not in self._history:
            items = collections.deque(maxlen=HISTORY_LIMIT)
            try:
                with open(self._history_file(conv), encoding="utf-8") as f:
                    for line in f:
                        try:
                            items.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
            self._history[conv] = items
        return self._history[conv]

    # ------------------------------------------------------------ files

    def _share(self, paths, allowed):
        """Remember files we offer. allowed: list of peer ids, or "group:<id>"
        (whoever is a member when they download)."""
        files = []
        with self._lock:
            for path in paths:
                st = os.stat(path)
                fid = self._next_fid
                self._next_fid += 1
                name = os.path.basename(path)
                self._shares[fid] = {"path": path, "name": name, "size": st.st_size,
                                     "allowed": allowed, "ts": time.time()}
                files.append({"fid": fid, "name": name, "size": st.st_size,
                              "mtime": int(st.st_mtime)})
            self._save_json("shares.json", {"next_fid": self._next_fid,
                                            "files": {str(k): v for k, v in self._shares.items()}})
        return files

    def _shared_file(self, fid, requester):
        """(path, size) if ``requester`` may download file ``fid``, else None."""
        with self._lock:
            sh = self._shares.get(fid)
            if not sh:
                return None
            allowed = sh["allowed"]
            if isinstance(allowed, str):
                g = self._groups.get(allowed[6:]) if allowed.startswith("group:") else None
                ok = bool(g and self._is_member(g, requester))
            else:
                ok = requester in allowed
        if not ok:
            return None
        try:
            if os.path.getsize(sh["path"]) != sh["size"]:
                return None  # changed since it was sent
        except OSError:
            return None  # deleted or moved
        return sh["path"], sh["size"]

    def _serve_file(self, conn, pkt, ip):
        """A LanChat peer downloads one of our files."""
        pid = self._touch_lanchat(pkt["from"], ip)
        found = self._shared_file(int(pkt["fid"]), pid) if pid else None
        if not found:
            conn.sendall(b'{"ok": false, "error": "the file is no longer available"}\n')
            return
        path, size = found
        offset = max(0, int(pkt.get("offset", 0)))
        conn.settimeout(60)
        conn.sendall(json.dumps({"ok": True, "size": size}).encode() + b"\n")
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                conn.sendall(chunk)

    def _download(self, rec, f, dest):
        key = "%s:%d" % (rec["id"], f["fid"])
        total = f["size"]
        tmp = dest + ".part"
        last = [0.0]

        def progress(done):
            now = time.time()
            if now - last[0] > 0.2:
                last[0] = now
                self.on_event("transfer", key, done, total, "progress")
        try:
            with self._lock:
                p = self._peers.get(rec["from_id"])
                p = p.as_dict() if p else None
            if not p or not p["online"]:
                raise OSError("%s is offline" % rec.get("from_name", "the sender"))
            with open(tmp, "wb") as out:
                if rec.get("ipmsg_files_pkt") is not None:
                    ok = self.bridge.download((p["ip"], p["ipmsg_port"]), rec["ipmsg_files_pkt"],
                                              f["fid"], total, out, progress)
                else:
                    ok = self._fetch_lanchat((p["ip"], p["tcp_port"]), f["fid"], total, out, progress)
            if not ok:
                raise OSError("the transfer was interrupted")
            os.replace(tmp, dest)
            self.on_event("transfer", key, total, total, "done")
        except Exception as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.on_event("transfer", key, 0, total, "error: %s" % exc)

    def _fetch_lanchat(self, addr, fid, size, out, progress):
        request = json.dumps(self._packet("file_get", fid=fid, offset=0)).encode("utf-8")
        got = 0
        with socket.create_connection(addr, timeout=CONNECT_TIMEOUT) as s:
            s.settimeout(60)
            s.sendall(request)
            s.shutdown(socket.SHUT_WR)
            header = b""
            while not header.endswith(b"\n"):
                c = s.recv(1)
                if not c or len(header) > 4096:
                    raise OSError("no answer from the sender")
                header += c
            answer = json.loads(header.decode("utf-8"))
            if not answer.get("ok"):
                raise OSError(answer.get("error", "refused"))
            while got < size:
                chunk = s.recv(min(65536, size - got))
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                progress(got)
        return got == size

    def _request_history(self, gid):
        """Ask online members for a group's messages (we just joined)."""
        with self._lock:
            g = self._groups[gid]
            pkt = self._packet("history_req", group=g)
            for mid in self._active(g):
                p = self._peers.get(mid)
                if mid != self.id and p and p.online and p.kind == "lanchat":
                    self._send(mid, pkt, keep=False)

    def _merge_history(self, conv, records):
        """Merge messages from another member's history, keeping time order."""
        clean = []
        for r in records:
            try:
                if not (_valid_id(r["id"]) and _valid_id(r["from_id"])) or r.get("system"):
                    continue
                clean.append({"id": r["id"], "from_id": r["from_id"],
                              "from_name": str(r.get("from_name", "?"))[:100],
                              "text": str(r["text"])[:MAX_TEXT], "ts": float(r["ts"])})
                if r.get("files"):
                    clean[-1]["files"] = _clean_files(r["files"])
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            path = self._history_file(conv)
            existing = []
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            existing.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
            have = {r.get("id") for r in existing}
            new = [r for r in clean if r["id"] not in have]
            if not new:
                return
            for r in new:
                self._seen_id(r["id"])
                have.add(r["id"])
            merged = sorted(existing + new, key=lambda r: r.get("ts", 0))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in merged:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, path)
            self._history[conv] = collections.deque(merged, maxlen=HISTORY_LIMIT)
        self.on_event("history", conv)

    def _add_history(self, conv, record):
        with self._lock:
            self._load_history(conv).append(record)
            with open(self._history_file(conv), "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        self.on_event("message", conv, record)
