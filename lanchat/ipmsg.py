"""IP Messenger (IPMsg) protocol support, so LanChat users and IPMsg users
see each other and can chat.

Spec: https://github.com/shirouzu/ipmsg  (protocol.txt, prot-eng.txt, src/ipmsg.h)

Packet:  "Ver:PacketNo:User:Host:Command:Extra"  over UDP port 2425.
Command: low 8 bits = mode, high 24 bits = option flags.
"""

import codecs
import collections
import os
import getpass
import hashlib
import random
import socket
import threading

IPMSG_PORT = 2425

# modes
NOOPERATION = 0x00
BR_ENTRY = 0x01
BR_EXIT = 0x02
ANSENTRY = 0x03
BR_ABSENCE = 0x04
SENDMSG = 0x20
RECVMSG = 0x21
READMSG = 0x30
ANSREADMSG = 0x32
GETINFO = 0x40
GETFILEDATA = 0x60      # over TCP: "packetID:fileID:offset" (hex) -> raw file bytes
RELEASEFILES = 0x61
GETDIRFILES = 0x62
SENDINFO = 0x41
GETABSENCEINFO = 0x50
SENDABSENCEINFO = 0x51

# options valid for every command
ABSENCEOPT = 0x00000100
FILEATTACHOPT = 0x00200000
ENCRYPTOPT = 0x00400000
UTF8OPT = 0x00800000
CAPUTF8OPT = 0x01000000

# options for SENDMSG (bit values overlap with the ones above)
SENDCHECKOPT = 0x00000100
SECRETOPT = 0x00000200
BROADCASTOPT = 0x00000400
AUTORETOPT = 0x00002000
RETRYOPT = 0x00004000
READCHECKOPT = 0x00100000

MAX_UDP = 32 * 1024
MAX_MSG_BYTES = 8000        # split longer texts into several IPMsg messages
ACK_TIMEOUT = 1.5
RETRIES = 3


def peer_id(user, host):
    """Stable LanChat id for an IPMsg user (same user@host -> same id)."""
    return hashlib.md5(("ipmsg:%s@%s" % (user, host)).encode("utf-8")).hexdigest()


FILE_REGULAR = 0x1     # fileattr low 8 bits; 0x2 = directory


def format_attachments(files):
    """SENDMSG attachment list: "fileID:name:size:mtime:attr:" per file
    (fileID decimal, the rest hex, ':' in names doubled), '\\a' after each."""
    return "".join("%d:%s:%x:%x:%x:\a" % (f["fid"], f["name"].replace(":", "::"), f["size"],
                                          int(f.get("mtime", 0)), FILE_REGULAR) for f in files)


def parse_attachments(text):
    """-> (regular files [{"fid", "name", "size", "mtime"}], number of folders skipped)"""
    files, folders = [], 0
    for entry in text.split("\a"):
        if entry.startswith(":"):
            entry = entry[1:]
        if not entry.strip():
            continue
        parts = [x.replace("\0", ":") for x in entry.replace("::", "\0").split(":")]
        try:
            fid, name, size, mtime, attr = (int(parts[0]), parts[1], int(parts[2], 16),
                                            int(parts[3], 16), int(parts[4], 16))
        except (IndexError, ValueError):
            continue
        if attr & 0xff == FILE_REGULAR:
            files.append({"fid": fid, "name": name, "size": size, "mtime": mtime})
        else:
            folders += 1
    return files, folders


def _clean(value):
    return value.replace(":", "_").replace("\0", "")


class Bridge:
    """Speaks IPMsg on behalf of a LanChat Node.

    Calls back into the node with:
        node._ipmsg_entry(user, host, addr, nick, group, utf8, away, encoding)
        node._ipmsg_exit(user, host)
        node._ipmsg_message(user, host, addr, text, sealed, packet_no)
        node._ipmsg_opened(user, host)        a sealed message of ours was opened
    """

    def __init__(self, node, port=IPMSG_PORT, bind="", targets=(), user=None, host=None,
                 encoding="utf-8"):
        self.node = node
        self.port = port
        self.bind_addr = bind
        self.targets = set(targets)
        self.user = _clean(user or getpass.getuser())
        self.host = _clean(host or socket.gethostname())
        self.encoding = encoding
        self.id = peer_id(self.user, self.host)
        self.sock = None
        self.error = None
        self._lock = threading.Lock()
        self._packet_no = random.randint(10 ** 8, 10 ** 9)
        self._acks = {}
        self._seen = collections.OrderedDict()

    @property
    def active(self):
        return self.sock is not None

    # ------------------------------------------------------------ lifecycle

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No SO_REUSEADDR: if IP Messenger runs on this PC we must not
            # steal its packets; the bridge just stays off.
            s.bind((self.bind_addr, self.port))
        except OSError:
            s.close()
            self.error = "UDP port %d is in use (is IP Messenger running on this PC?)" % self.port
            return False
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.5)
        self.sock = s
        self.error = None
        self.node._spawn(self._loop)
        # IPMsg users download our attachments over TCP on the same port.
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":  # Linux: allow a restart while old connections linger
                t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind((self.bind_addr, self.port))
            t.listen(16)
            t.settimeout(0.5)
            self.tcp = t
            self.node._spawn(self._tcp_loop)
        except OSError:
            t.close()
            self.tcp = None
        self.announce(BR_ENTRY)
        return True

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        if getattr(self, "tcp", None):
            self.tcp.close()
            self.tcp = None

    # ------------------------------------------------------------ sending

    def _next_packet_no(self):
        with self._lock:
            self._packet_no += 1
            return self._packet_no

    def _header(self, command, packet_no):
        return ("1:%d:%s:%s:%d:" % (packet_no, self.user, self.host, command)).encode(
            self.encoding, "replace")

    def _sendto(self, data, addr):
        try:
            self.sock.sendto(data, addr)
            return True
        except (OSError, AttributeError):
            return False

    def _send_command(self, addr, command, extra=b""):
        self._sendto(self._header(command, self._next_packet_no()) + extra, addr)

    def _entry_extra(self):
        nick = _clean(self.node.name)
        group = _clean(self.node.group_name)
        legacy = (nick.encode(self.encoding, "replace") + b"\0" +
                  group.encode(self.encoding, "replace") + b"\0\n")
        ext = "UN:%s\nHN:%s\nNN:%s\nGN:%s\n" % (self.user, self.host, nick, group)
        return legacy + ext.encode("utf-8")

    def announce(self, mode=BR_ENTRY):
        """Broadcast BR_ENTRY (hello / refresh), BR_ABSENCE (name change) or BR_EXIT."""
        if not self.sock:
            return
        command = mode | CAPUTF8OPT if mode != BR_EXIT else mode
        data = self._header(command, self._next_packet_no()) + self._entry_extra()
        for addr in self.node._broadcast_targets() | self.targets:
            self._sendto(data, (addr, self.port))

    def send_read(self, addr, packet_no):
        """Tell the sender we opened their sealed message."""
        self._send_command(addr, READMSG, str(packet_no).encode("ascii"))

    def send_message(self, addr, text, utf8, encoding=None, sealed=False, files=None):
        """Send a message and wait for the receipt. Blocking; True if acknowledged.

        utf8: the peer announced CAPUTF8OPT. encoding: what the peer said it
        uses (iptux does), otherwise our configured legacy encoding."""
        if not self.sock:
            return False
        enc = "utf-8" if utf8 else (encoding or self.encoding)
        flags = SENDCHECKOPT | (UTF8OPT if utf8 else 0) | (SECRETOPT if sealed else 0)
        chunks = list(self._chunks(text, enc))
        for i, chunk in enumerate(chunks):
            body = chunk.encode(enc, "replace") + b"\0"
            if files and i == len(chunks) - 1:  # attachments ride on the last part
                body += format_attachments(files).encode(enc, "replace") + b"\0"
                flags |= FILEATTACHOPT
            packet_no = self._next_packet_no()
            event = self._acks[str(packet_no)] = threading.Event()
            try:
                for attempt in range(RETRIES + 1):
                    command = SENDMSG | flags | (RETRYOPT if attempt else 0)
                    self._sendto(self._header(command, packet_no) + body, addr)
                    if event.wait(ACK_TIMEOUT):
                        break
                else:
                    return False
            finally:
                self._acks.pop(str(packet_no), None)
        return True

    @staticmethod
    def _chunks(text, enc):
        chunk = []
        size = 0
        for ch in text:
            n = len(ch.encode(enc, "replace"))
            if size + n > MAX_MSG_BYTES:
                yield "".join(chunk)
                chunk, size = [], 0
            chunk.append(ch)
            size += n
        yield "".join(chunk)

    # ------------------------------------------------------------ receiving

    def _decode(self, raw, utf8=False):
        if utf8:
            return raw.decode("utf-8", "replace")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # the spec's legacy encoding is CP932 (Japanese Windows)
            fallback = self.encoding if self.encoding.replace("-", "").lower() != "utf8" else "cp932"
            return raw.decode(fallback, "replace")

    def _loop(self):
        while not self.node._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(MAX_UDP + 1024)
            except (socket.timeout, ConnectionResetError):
                # Windows reports an earlier "port unreachable" as a reset here
                continue
            except (OSError, AttributeError):
                break
            try:
                self._handle(data, addr)
            except (ValueError, IndexError, UnicodeError):
                continue

    def _parse_entry(self, extra):
        """-> (nickname, group, encoding or None)"""
        parts = extra.split(b"\0")
        nick = self._decode(parts[0]).strip()
        group = self._decode(parts[1]).strip() if len(parts) > 1 else ""
        encoding = None
        if len(parts) > 3 and not parts[2].startswith(b"\n"):
            # iptux: "nick\0group\0icon\0encoding\0"
            try:
                encoding = codecs.lookup(parts[3].decode("ascii").strip()).name
            except (LookupError, UnicodeDecodeError):
                pass
        if len(parts) > 2 and parts[2].startswith(b"\n"):
            for line in parts[2].decode("utf-8", "replace").split("\n"):
                key, _, value = line.partition(":")
                if key == "NN" and value:
                    nick = value
                elif key == "GN":
                    group = value
        return nick, group, encoding

    def _handle(self, data, addr):
        parts = data.split(b":", 5)
        if len(parts) < 5:
            return
        extra = parts[5] if len(parts) == 6 else b""
        packet_no = parts[1].decode("ascii").strip()
        user, host = self._decode(parts[2]), self._decode(parts[3])
        command = int(parts[4])
        if user == self.user and host == self.host:
            return  # our own broadcast
        mode, opt = command & 0xff, command & 0xffffff00

        if mode in (BR_ENTRY, ANSENTRY, BR_ABSENCE):
            nick, group, encoding = self._parse_entry(extra)
            # iptux sets the absence bit on every entry packet; ignore it there
            away = bool(opt & ABSENCEOPT) and b"iptux" not in parts[0]
            self.node._ipmsg_entry(user, host, addr, nick or user, group,
                                   bool(opt & CAPUTF8OPT), away, encoding)
            if mode == BR_ENTRY:
                self._send_command(addr, ANSENTRY | CAPUTF8OPT, self._entry_extra())
        elif mode == BR_EXIT:
            self.node._ipmsg_exit(user, host)
        elif mode == SENDMSG:
            if opt & SENDCHECKOPT and not opt & (BROADCASTOPT | AUTORETOPT):
                self._send_command(addr, RECVMSG, packet_no.encode("ascii"))
            key = (user, host, packet_no)
            if key in self._seen:
                return  # a retry of something we already have
            self._seen[key] = True
            while len(self._seen) > 2000:
                self._seen.popitem(last=False)
            files = []
            pieces = extra.split(b"\0")
            if opt & ENCRYPTOPT:
                text = "[encrypted message — LanChat can't read encrypted IPMsg messages]"
            else:
                text = self._decode(pieces[0], bool(opt & UTF8OPT)).replace("\r\n", "\n")
                if opt & FILEATTACHOPT and len(pieces) > 1:
                    files, folders = parse_attachments(self._decode(pieces[1], bool(opt & UTF8OPT)))
                    if folders:
                        text += "\n[%d folder(s) attached \u2014 LanChat can only receive files]" % folders
            # sealed: READMSG is sent later, when the user opens it
            self.node._ipmsg_message(user, host, addr, text, bool(opt & SECRETOPT), packet_no,
                                     files)
        elif mode == RECVMSG:
            event = self._acks.get(extra.split(b"\0")[0].decode("ascii", "replace").strip())
            if event:
                event.set()
        elif mode == READMSG:
            if opt & READCHECKOPT:
                self._send_command(addr, ANSREADMSG, extra.split(b"\0")[0])
            self.node._ipmsg_opened(user, host)
        elif mode == GETINFO:
            self._send_command(addr, SENDINFO, b"LanChat")
        elif mode == GETABSENCEINFO:
            self._send_command(addr, SENDABSENCEINFO, b"Not absence mode")

    # ------------------------------------------------------------ file transfer

    def download(self, addr, packet_no, fid, size, out, progress=None):
        """Fetch an IPMsg attachment: GETFILEDATA over TCP, then raw bytes.
        Writes into the open file ``out``; True when all ``size`` bytes arrived."""
        request = self._header(GETFILEDATA, self._next_packet_no()) + \
            ("%x:%x:%x" % (int(packet_no), int(fid), 0)).encode("ascii")
        got = 0
        with socket.create_connection(addr, timeout=10) as s:
            s.settimeout(60)
            s.sendall(request)
            while got < size:
                chunk = s.recv(min(65536, size - got))
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got)
        return got == size

    def _tcp_loop(self):
        while not self.node._stop.is_set():
            try:
                conn, addr = self.tcp.accept()
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                break
            threading.Thread(target=self._serve_file, args=(conn, addr), daemon=True).start()

    def _serve_file(self, conn, addr):
        """An IPMsg user downloads one of our attachments."""
        with conn:
            try:
                conn.settimeout(10)
                data = conn.recv(4096)
                parts = data.split(b":", 5)
                if len(parts) < 6 or int(parts[4]) & 0xff != GETFILEDATA:
                    return  # GETDIRFILES (folders) etc. are not offered
                user, host = self._decode(parts[2]), self._decode(parts[3])
                packet_id, fid, offset = (int(x, 16) for x in
                                          parts[5].split(b"\0")[0].split(b":")[:3])
                found = self.node._shared_file(fid, peer_id(user, host))
                if not found:
                    return
                path, size = found
                conn.settimeout(60)
                with open(path, "rb") as f:
                    f.seek(offset)
                    remaining = size - offset
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        conn.sendall(chunk)
                        remaining -= len(chunk)
            except (OSError, ValueError, IndexError):
                pass
