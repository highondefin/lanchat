"""End-to-end tests: several nodes talk to each other over real sockets.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import queue
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat import ipmsg  # noqa: E402
from lanchat.core import EVERYONE, Node  # noqa: E402


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def texts(node, conv):
    return [m["text"] for m in node.history(conv) if not m.get("system")]


def joined_ids(node):
    return [g["id"] for g in node.groups() if g["joined"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lanchat-test-")
        # Private ports so tests don't see real LanChat / IPMsg users.
        self.port = random.randint(40000, 44000)
        self.ipmsg_port = random.randint(44001, 48000)
        self.nodes = []
        self.fakes = []

    def tearDown(self):
        for n in self.nodes:
            n.stop()
        for f in self.fakes:
            f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name, **kw):
        kw.setdefault("ipmsg_enabled", False)
        n = Node(name=name, data_dir=os.path.join(self.tmp, name), discovery_port=self.port, **kw)
        n.start()
        self.nodes.append(n)
        return n

    def online(self, node, *others):
        ids = {o.id for o in others}
        return wait_for(lambda: ids <= {p["id"] for p in node.peers() if p["online"]})


class LanChatTest(Base):
    def test_discovery_and_everyone_room(self):
        a, b, c = self.make("alice"), self.make("bob"), self.make("carol")
        self.assertTrue(self.online(a, b, c))
        self.assertTrue(self.online(b, a, c))
        a.send_text(EVERYONE, "hi all")
        self.assertTrue(wait_for(lambda: texts(b, EVERYONE) == ["hi all"]))
        self.assertTrue(wait_for(lambda: texts(c, EVERYONE) == ["hi all"]))

    def test_direct_message(self):
        a, b, c = self.make("alice"), self.make("bob"), self.make("carol")
        self.assertTrue(self.online(a, b))
        a.send_text("dm:" + b.id, "psst")
        self.assertTrue(wait_for(lambda: texts(b, "dm:" + a.id) == ["psst"]))
        time.sleep(0.3)
        self.assertEqual(texts(c, "dm:" + a.id), [])

    def test_group_chat_add_and_leave(self):
        a, b, c, d = (self.make(n) for n in ("alice", "bob", "carol", "dave"))
        self.assertTrue(self.online(a, b, c, d))

        gid = a.create_group("Project X", [b.id, c.id])
        conv = "group:" + gid
        for n in (b, c):
            self.assertTrue(wait_for(lambda n=n: joined_ids(n) == [gid]))
        # Public: dave sees the group but is not in it and gets no messages.
        self.assertTrue(wait_for(lambda: [g["id"] for g in d.groups()] == [gid]))
        self.assertEqual(joined_ids(d), [])

        b.send_text(conv, "hello group")
        for n in (a, c):
            self.assertTrue(wait_for(lambda n=n: texts(n, conv) == ["hello group"]))
        time.sleep(0.3)
        self.assertEqual(texts(d, conv), [])
        with self.assertRaises(ValueError):
            d.send_text(conv, "not a member")

        # Any member can add people; newcomers see new messages.
        c.add_members(gid, [d.id])
        members = {a.id, b.id, c.id, d.id}
        for n in (a, b, c, d):
            self.assertTrue(wait_for(lambda n=n: set(n.group_members(gid)) == members))
        d.send_text(conv, "thanks for adding me")
        for n in (a, b, c):
            self.assertTrue(wait_for(lambda n=n: "thanks for adding me" in texts(n, conv)))

        # Leaving removes you everywhere and stops delivery to you.
        b.leave_group(gid)
        self.assertEqual(joined_ids(b), [])
        self.assertEqual([g["id"] for g in b.groups()], [gid])  # still listed (public)
        for n in (a, c, d):
            self.assertTrue(wait_for(lambda n=n: b.id not in n.group_members(gid)))
        a.send_text(conv, "after bob left")
        self.assertTrue(wait_for(lambda: "after bob left" in texts(d, conv)))
        time.sleep(0.3)
        self.assertNotIn("after bob left", texts(b, conv))
        self.assertTrue(any(m.get("system") and m["text"] == "bob left" for m in a.history(conv)))

    def test_anyone_can_join_public_group(self):
        a, b = self.make("alice"), self.make("bob")
        self.assertTrue(self.online(a, b))
        gid = a.create_group("Lunch")
        self.assertTrue(wait_for(lambda: [g["id"] for g in b.groups()] == [gid]))
        b.join_group(gid)
        self.assertTrue(wait_for(lambda: set(a.group_members(gid)) == {a.id, b.id}))
        b.send_text("group:" + gid, "count me in")
        self.assertTrue(wait_for(lambda: texts(a, "group:" + gid) == ["count me in"]))

    def test_sealed_message_and_read_notice(self):
        a, b = self.make("alice"), self.make("bob")
        self.assertTrue(self.online(a, b))
        opened = []
        a.on_event = lambda kind, *rest: kind == "opened" and opened.append(rest[0])
        rec = a.send_text("dm:" + b.id, "secret", sealed=True)
        self.assertTrue(wait_for(lambda: texts(b, "dm:" + a.id) == ["secret"]))
        got = b.history("dm:" + a.id)[0]
        self.assertTrue(got["sealed"] and not got["opened"])
        b.open_sealed("dm:" + a.id, rec["id"])
        self.assertTrue(wait_for(lambda: opened == [b.id]))

    def test_profile_and_refresh(self):
        a, b = self.make("alice"), self.make("bob")
        self.assertTrue(self.online(a, b) and self.online(b, a))
        a.set_profile("Alice A", "Sales")
        self.assertTrue(wait_for(lambda: b.peer(a.id)["group"] == "Sales"))
        p = b.peer(a.id)
        self.assertEqual((p["name"], p["host"]), ("Alice A", socket.gethostname()))
        b.refresh()  # everyone is asked again and comes back
        self.assertTrue(self.online(b, a))

    def test_joining_member_gets_full_history(self):
        a, b, c, d = (self.make(n) for n in ("alice", "bob", "carol", "dave"))
        self.assertTrue(self.online(a, b, c, d))
        gid = a.create_group("History", [b.id])
        conv = "group:" + gid
        self.assertTrue(wait_for(lambda: gid in joined_ids(b)))
        for i, (n, text) in enumerate([(a, "one"), (b, "two"), (a, "three")]):
            n.send_text(conv, text)
            self.assertTrue(wait_for(lambda t=text: t in texts(a, conv) and t in texts(b, conv)))
        self.assertEqual(texts(c, conv), [])
        # carol joins by herself: she gets everything said before, in order
        c.join_group(gid)
        self.assertTrue(wait_for(lambda: texts(c, conv) == ["one", "two", "three"]))
        # dave is added by bob: same
        b.add_members(gid, [d.id])
        self.assertTrue(wait_for(lambda: texts(d, conv) == ["one", "two", "three"]))
        # and new messages still arrive exactly once, after the history
        a.send_text(conv, "four")
        self.assertTrue(wait_for(lambda: texts(c, conv) == ["one", "two", "three", "four"]))
        time.sleep(0.3)
        self.assertEqual(texts(d, conv), ["one", "two", "three", "four"])

    def test_remove_member(self):
        a, b, c = (self.make(n) for n in ("alice", "bob", "carol"))
        self.assertTrue(self.online(a, b, c))
        gid = a.create_group("Team", [b.id, c.id])
        self.assertTrue(wait_for(lambda: gid in joined_ids(c)))
        b.remove_members(gid, [c.id])
        self.assertTrue(wait_for(lambda: gid not in joined_ids(c)))
        self.assertTrue(wait_for(lambda: set(a.group_members(gid)) == {a.id, b.id}))
        a.send_text("group:" + gid, "without carol")
        self.assertTrue(wait_for(lambda: "without carol" in texts(b, "group:" + gid)))
        time.sleep(0.3)
        self.assertNotIn("without carol", texts(c, "group:" + gid))

    def _tmpfile(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _save(self, node, conv, rec_id, fid):
        dest = os.path.join(self.tmp, "saved-%s-%s-%d" % (node.name, rec_id[:6], fid))
        done = []
        node.on_event = lambda kind, *rest: kind == "transfer" and rest[3] != "progress" \
            and done.append(rest[3])
        node.download(conv, rec_id, fid, dest)
        self.assertTrue(wait_for(lambda: done, timeout=15))
        return done[0], dest

    def test_send_file_direct(self):
        a, b, c = (self.make(n) for n in ("alice", "bob", "carol"))
        self.assertTrue(self.online(a, b, c))
        data = os.urandom(300000)  # several chunks
        a.send_files("dm:" + b.id, [self._tmpfile("report.bin", data)], "here's the report")
        conv = "dm:" + a.id
        self.assertTrue(wait_for(lambda: b.history(conv) and b.history(conv)[-1].get("files")))
        rec = b.history(conv)[-1]
        self.assertEqual((rec["text"], rec["files"][0]["name"], rec["files"][0]["size"]),
                         ("here's the report", "report.bin", 300000))
        status, dest = self._save(b, conv, rec["id"], rec["files"][0]["fid"])
        self.assertEqual(status, "done")
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)
        # carol was not a recipient: alice refuses her
        with self.assertRaises(OSError):
            c._fetch_lanchat(("127.0.0.1", a.tcp_port), rec["files"][0]["fid"], 300000,
                             open(os.devnull, "wb"), lambda n: None)

    def test_no_files_in_group_chats(self):
        a = self.make("alice")
        gid = a.create_group("Chat only")
        with self.assertRaises(ValueError):
            a.send_files("group:" + gid, [self._tmpfile("x.txt", b"x")])

    def test_late_comer_sees_existing_groups(self):
        a = self.make("alice")
        gid = a.create_group("Already here")
        b = self.make("bob")  # starts after the group was created
        self.assertTrue(wait_for(lambda: [g["id"] for g in b.groups()] == [gid]))

    def test_offline_member_gets_messages_later(self):
        a, b = self.make("alice"), self.make("bob")
        self.assertTrue(self.online(a, b))
        gid = a.create_group("Team", [b.id])
        conv = "group:" + gid
        self.assertTrue(wait_for(lambda: joined_ids(b)))

        b.stop()
        self.nodes.remove(b)
        self.assertTrue(wait_for(lambda: not a.peer(b.id)["online"]))
        a.send_text(conv, "while you were away")

        b2 = self.make("bob")  # same data dir -> same identity and groups
        self.assertEqual(b2.id, b.id)
        self.assertTrue(wait_for(lambda: "while you were away" in texts(b2, conv)))

    def test_concurrent_adds_merge(self):
        a, b, c, d = (self.make(n) for n in ("alice", "bob", "carol", "dave"))
        self.assertTrue(self.online(a, b, c, d))
        gid = a.create_group("Merge", [b.id])
        self.assertTrue(wait_for(lambda: joined_ids(b)))
        a.add_members(gid, [c.id])  # both add someone at the same time
        b.add_members(gid, [d.id])
        members = {a.id, b.id, c.id, d.id}
        for n in (a, b, c, d):
            self.assertTrue(wait_for(lambda n=n: set(n.group_members(gid)) == members))


# ---------------------------------------------------------------- IPMsg


class FakeIpmsg:
    """A minimal IP Messenger client speaking the real wire format."""

    def __init__(self, addr, port, user="taro", host="taro-pc", nick="Taro"):
        self.addr, self.port = addr, port
        self.user, self.host, self.nick = user, host, nick
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((addr, port))
        self.sock.settimeout(0.2)
        self.running = True
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.bind((addr, port))
        self.tcp.listen(4)
        self.tcp.settimeout(0.2)
        self.shared = {}                # fileID -> bytes we offer
        self.attachments = []           # (packet_no, [(fid, name, size)]) offered to us
        threading.Thread(target=self._serve, daemon=True).start()
        self.packet_no = 1000
        self.received = queue.Queue()   # message texts
        self.sendmsg_opts = []          # option bits of every SENDMSG received
        self.reads = []                 # READMSG packets (they opened our sealed message)
        self.entries = []
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def close(self):
        self.running = False
        self.sock.close()
        self.tcp.close()

    def _serve(self):  # answer GETFILEDATA like IP Messenger does
        while self.running:
            try:
                conn, _ = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                req = conn.recv(4096).split(b":", 5)
                if int(req[4]) & 0xff == ipmsg.GETFILEDATA:
                    pkt, fid, off = (int(x, 16) for x in req[5].split(b":")[:3])
                    conn.sendall(self.shared[fid][off:])

    def send_file(self, to, text, fid, name, data):
        self.shared[fid] = data
        attach = "%d:%s:%x:%x:1:\a" % (fid, name.replace(":", "::"), len(data), 1700000000)
        self.send(to, ipmsg.SENDMSG | ipmsg.SENDCHECKOPT | ipmsg.UTF8OPT | ipmsg.FILEATTACHOPT,
                  text.encode() + b"\0" + attach.encode() + b"\0")
        return self.packet_no

    def fetch(self, to, packet_no, fid, size):
        """Download an attachment from LanChat exactly as IP Messenger would."""
        with socket.create_connection((to, self.port), timeout=5) as s:
            self.packet_no += 1
            s.sendall(("1:%d:%s:%s:%d:%x:%x:0" % (self.packet_no, self.user, self.host,
                                                  ipmsg.GETFILEDATA, packet_no, fid)).encode())
            data = b""
            while len(data) < size:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return data

    def send(self, to, command, extra=b""):
        self.packet_no += 1
        head = ("1:%d:%s:%s:%d:" % (self.packet_no, self.user, self.host, command)).encode()
        self.sock.sendto(head + extra, (to, self.port))

    def entry(self, to):
        extra = (b"Taro\0Sales\0\n" +
                 ("UN:%s\nHN:%s\nNN:%s\nGN:Sales\n" % (self.user, self.host, self.nick)).encode())
        self.send(to, ipmsg.BR_ENTRY | ipmsg.CAPUTF8OPT, extra)

    def message(self, to, text, utf8=True, sealed=False):
        seal = ipmsg.SECRETOPT if sealed else 0
        if utf8:
            self.send(to, ipmsg.SENDMSG | ipmsg.SENDCHECKOPT | ipmsg.UTF8OPT | seal,
                      text.encode("utf-8") + b"\0")
            return self.packet_no
        else:
            self.send(to, ipmsg.SENDMSG | ipmsg.SENDCHECKOPT, text.encode("cp932") + b"\0")

    def _loop(self):
        while self.running:
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            ver, pno, user, host, cmd, extra = data.split(b":", 5)
            cmd = int(cmd)
            mode, opt = cmd & 0xff, cmd & 0xffffff00
            if mode == ipmsg.SENDMSG:
                if opt & ipmsg.SENDCHECKOPT:
                    self.send(src[0], ipmsg.RECVMSG, pno)
                self.sendmsg_opts.append(opt)
                enc = "utf-8" if opt & ipmsg.UTF8OPT else "cp932"
                if opt & ipmsg.FILEATTACHOPT:
                    files = []
                    for entry in extra.split(b"\0")[1].decode(enc).split("\a"):
                        if entry.strip(":"):
                            f = entry.lstrip(":").split(":")
                            files.append((int(f[0]), f[1], int(f[2], 16)))
                    self.attachments.append((int(pno), files))
                self.received.put(extra.split(b"\0")[0].decode(enc))
            elif mode == ipmsg.READMSG:
                self.reads.append(extra.split(b"\0")[0].decode())
            elif mode in (ipmsg.ANSENTRY, ipmsg.BR_ENTRY):
                self.entries.append((mode, extra, src))

    def wait_text(self, pred, timeout=8.0):
        end = time.time() + timeout
        seen = []
        while time.time() < end:
            try:
                t = self.received.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(t)
            if pred(t):
                return t
        raise AssertionError("no matching IPMsg message; got %r" % seen)


class IpmsgTest(Base):
    A, B, C, TARO = "127.0.0.2", "127.0.0.4", "127.0.0.5", "127.0.0.3"

    def bridge_node(self, name, addr, others):
        return self.make(name, ipmsg_enabled=True, ipmsg_port=self.ipmsg_port, ipmsg_bind=addr,
                         ipmsg_targets=others, ipmsg_user=name, ipmsg_host=name + "-pc")

    def setUp(self):
        super().setUp()
        self.taro = FakeIpmsg(self.TARO, self.ipmsg_port)
        self.fakes.append(self.taro)
        self.a = self.bridge_node("alice", self.A, [self.B, self.TARO])
        self.b = self.bridge_node("bob", self.B, [self.A, self.TARO])
        self.assertTrue(self.a.bridge.active and self.b.bridge.active)
        for n in (self.a, self.b):
            self.taro.entry(n.bridge.bind_addr)
        self.taro_id = ipmsg.peer_id("taro", "taro-pc")
        for n in (self.a, self.b):
            self.assertTrue(wait_for(lambda n=n: (n.peer(self.taro_id) or {}).get("online")))

    def test_ipmsg_users_listed_once(self):
        a = self.a
        self.assertTrue(self.online(a, self.b))
        # alice answered taro's BR_ENTRY with an ANSENTRY carrying her name (UTF-8 ext.)
        self.assertTrue(wait_for(lambda: any(m == ipmsg.ANSENTRY and b"NN:alice" in e
                                             for m, e, _ in self.taro.entries)))
        p = a.peer(self.taro_id)
        self.assertEqual((p["kind"], p["name"], p["group"]), ("ipmsg", "Taro", "Sales"))
        # bob's own IPMsg presence is hidden: bob appears once, as a LanChat user
        self.assertTrue(wait_for(lambda: a.peer(self.b.bridge.id) is not None))
        names = sorted((p["name"], p["kind"]) for p in a.peers())
        self.assertEqual(names, [("Taro", "ipmsg"), ("bob", "lanchat")])

    def test_direct_chat_with_ipmsg_user(self):
        a = self.a
        a.send_text("dm:" + self.taro_id, "hello Taro")
        self.taro.wait_text(lambda t: t == "hello Taro")
        self.taro.message(self.A, "こんにちは", utf8=False)  # legacy CP932 client
        self.assertTrue(wait_for(lambda: texts(a, "dm:" + self.taro_id) == ["hello Taro", "こんにちは"]))

    def test_sealed_messages_with_ipmsg(self):
        a, taro = self.a, self.taro
        conv = "dm:" + self.taro_id
        # Taro sends a sealed message; READMSG goes back only when alice opens it.
        pkt = taro.message(self.A, "for your eyes only", sealed=True)
        self.assertTrue(wait_for(lambda: texts(a, conv) == ["for your eyes only"]))
        rec = a.history(conv)[0]
        self.assertTrue(rec["sealed"])
        time.sleep(0.3)
        self.assertEqual(taro.reads, [])
        a.open_sealed(conv, rec["id"])
        self.assertTrue(wait_for(lambda: taro.reads == [str(pkt)]))
        # Alice sends a sealed one: SECRETOPT set; Taro's READMSG -> "opened" event.
        opened = []
        a.on_event = lambda kind, *rest: kind == "opened" and opened.append(rest[0])
        a.send_text(conv, "sealed reply", sealed=True)
        taro.wait_text(lambda t: t == "sealed reply")
        self.assertTrue(taro.sendmsg_opts[-1] & ipmsg.SECRETOPT)
        taro.send(self.A, ipmsg.READMSG, b"1")
        self.assertTrue(wait_for(lambda: opened == [self.taro_id]))

    def test_files_with_ipmsg(self):
        a, taro = self.a, self.taro
        conv = "dm:" + self.taro_id
        # Taro attaches a file in IP Messenger; alice saves it (GETFILEDATA over TCP)
        data = os.urandom(150000)
        taro.send_file(self.A, "minutes attached", 0, "minutes:v2.txt", data)
        self.assertTrue(wait_for(lambda: a.history(conv) and a.history(conv)[-1].get("files")))
        rec = a.history(conv)[-1]
        self.assertEqual(rec["files"][0]["name"], "minutes_v2.txt")  # ':' made safe
        dest = os.path.join(self.tmp, "from-taro")
        done = []
        a.on_event = lambda kind, *rest: kind == "transfer" and rest[3] != "progress" \
            and done.append(rest[3])
        a.download(conv, rec["id"], 0, dest)
        self.assertTrue(wait_for(lambda: done, timeout=15))
        self.assertEqual(done[0], "done")
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)
        # alice sends Taro a file; IP Messenger downloads it from alice's TCP 2425
        path = os.path.join(self.tmp, "budget.xlsx")
        with open(path, "wb") as f:
            f.write(b"x" * 70000)
        a.send_files(conv, [path], "the budget")
        taro.wait_text(lambda t: t == "the budget")
        pkt, files = taro.attachments[-1]
        self.assertEqual([(f[1], f[2]) for f in files], [("budget.xlsx", 70000)])
        self.assertEqual(taro.fetch(self.A, pkt, files[0][0], 70000), b"x" * 70000)

    def test_ipmsg_user_in_group(self):
        a, b, taro = self.a, self.b, self.taro
        self.assertTrue(self.online(a, b))
        gid = a.create_group("Team", [b.id, self.taro_id])
        conv = "group:" + gid
        taro.wait_text(lambda t: t.startswith("[Team] alice added you"))
        self.assertTrue(wait_for(lambda: set(b.group_members(gid)) == {a.id, b.id, self.taro_id}))

        # Group messages reach the IPMsg user, tagged with group and author.
        b.send_text(conv, "hi team")
        taro.wait_text(lambda t: t == "[Team] bob: hi team")

        # Taro hits "reply" in IPMsg (to bob, quoting): it lands in the group.
        taro.message(self.B, "> [Team] bob: hi team\nhello everyone")
        for n in (a, b):
            self.assertTrue(wait_for(lambda n=n: "hello everyone" in texts(n, conv)))
        rec = [m for m in a.history(conv) if m["text"] == "hello everyone"][0]
        self.assertEqual((rec["from_id"], rec["from_name"]), (self.taro_id, "Taro"))

        # Without a quote it goes to the last group that user heard from.
        taro.message(self.A, "no quote this time")
        self.assertTrue(wait_for(lambda: "no quote this time" in texts(b, conv)))
        # /dm keeps it private.
        taro.message(self.A, "/dm just for alice")
        self.assertTrue(wait_for(lambda: "just for alice" in texts(a, "dm:" + self.taro_id)))
        self.assertNotIn("just for alice", texts(b, conv))

        # Commands: list groups, leave.
        taro.message(self.A, "/groups")
        taro.wait_text(lambda t: "[Team] 3 members (you're in)" in t)
        taro.message(self.A, "/leave team")
        taro.wait_text(lambda t: t == "You left [Team].")
        self.assertTrue(wait_for(lambda: self.taro_id not in b.group_members(gid)))

    def test_ipmsg_user_joins_by_command(self):
        a, taro = self.a, self.taro
        gid = a.create_group("Lunch")
        taro.message(self.A, "/join lunch")
        taro.wait_text(lambda t: t.startswith("[Lunch] You joined"))
        self.assertIn(self.taro_id, a.group_members(gid))
        taro.message(self.A, "sushi?")
        self.assertTrue(wait_for(lambda: texts(a, "group:" + gid) == ["sushi?"]))

    def test_relay_when_ipmsg_port_busy(self):
        a, taro = self.a, self.taro
        # carol's IPMsg port is taken (e.g. IP Messenger runs on her PC)
        c = self.bridge_node("carol", self.TARO, [])
        self.assertFalse(c.bridge.active)
        self.assertTrue(self.online(c, a))
        gid = a.create_group("Team", [c.id, self.taro_id])
        taro.wait_text(lambda t: t.startswith("[Team] alice added you"))
        self.assertTrue(wait_for(lambda: gid in joined_ids(c)))
        c.send_text("group:" + gid, "via a relay")
        taro.wait_text(lambda t: t == "[Team] carol: via a relay")


if __name__ == "__main__":
    unittest.main()
