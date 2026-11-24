"""Start LanChat:  python3 -m lanchat [--name NAME] [--cli] ..."""

import argparse
import sys
import threading
import time

from .core import DISCOVERY_PORT, EVERYONE, Node, default_data_dir


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="lanchat", description="Serverless LAN chat with group chat.")
    ap.add_argument("--name", help="your display name (remembered for next time)")
    ap.add_argument("--group", help="your group / department, shown in user lists (remembered)")
    ap.add_argument("--profile", help="separate identity/data, e.g. to run two copies on one PC")
    ap.add_argument("--data-dir", help="where to keep identity, groups and history")
    ap.add_argument("--port", type=int, default=DISCOVERY_PORT,
                    help="UDP discovery port, also the preferred TCP port (default %(default)s)")
    ap.add_argument("--broadcast", action="append", default=[], metavar="ADDR",
                    help="extra broadcast address, e.g. 192.168.1.255 (repeatable)")
    ap.add_argument("--no-ipmsg", action="store_true",
                    help="don't talk to IP Messenger users (UDP 2425)")
    ap.add_argument("--ipmsg-encoding", default="utf-8", metavar="ENC",
                    help="text encoding for old IPMsg clients without UTF-8 support, "
                         "e.g. cp932 (Japanese) or gbk (Chinese); default %(default)s")
    ap.add_argument("--minimized", action="store_true",
                    help="start hidden in the tray (Windows) / minimized")
    ap.add_argument("--cli", action="store_true", help="use the terminal interface instead of the GUI")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    kwargs = dict(name=args.name, group=args.group, data_dir=args.data_dir or default_data_dir(args.profile),
                  discovery_port=args.port, extra_broadcasts=args.broadcast,
                  ipmsg_enabled=not args.no_ipmsg, ipmsg_encoding=args.ipmsg_encoding)
    if args.cli:
        run_cli(**kwargs)
        return
    try:
        from .gui import run
    except ImportError as exc:  # tkinter missing (e.g. python3-tk not installed)
        sys.exit("GUI unavailable (%s). Install python3-tk or use --cli." % exc)
    run(minimized=args.minimized, **kwargs)


# ---------------------------------------------------------------- terminal UI

CLI_HELP = """Commands:
  /peers                     list people on the network (LanChat and IPMsg)
  /groups                    list all group chats ("*" = you're in it)
  /join #GROUP               join a group
  /to everyone|NAME|#GROUP   switch conversation (people by name, groups with #)
  /group NAME person,...     create a group with those people
  /add person,...            add people to the current group
  /leave                     leave the current group
  /name NEW                  change your name
  /history                   show this conversation again
  /quit                      exit
Anything else is sent to the current conversation."""


def run_cli(**kwargs):
    lock = threading.Lock()
    state = {"conv": EVERYONE}

    def label(conv):
        if conv == EVERYONE:
            return "#everyone"
        if conv.startswith("group:"):
            g = node.group(conv[6:])
            return "#" + (g["name"] if g else "?")
        return "@" + node.display_name(conv[3:])

    def fmt(rec, conv=None):
        stamp = time.strftime("%H:%M", time.localtime(rec["ts"]))
        where = "[%s] " % label(conv) if conv else ""
        if rec.get("system"):
            return "%s %s* %s" % (stamp, where, rec["text"])
        who = "you" if rec["from_id"] == node.id else rec.get("from_name", "?")
        return "%s %s%s: %s" % (stamp, where, who, rec["text"])

    def on_event(kind, *rest):
        if kind == "message":
            conv, rec = rest
            if rec.get("from_id") == node.id:
                return
            with lock:
                print("\r" + fmt(rec, None if conv == state["conv"] else conv))

    node = Node(on_event=on_event, **kwargs)
    node.start()
    print("LanChat - you are %r (port %s, %s). Type /help for commands."
          % (node.name, node.tcp_port, node.ipmsg_status()[1]))

    def find_peers(spec):
        found, peers = [], node.peers()
        for word in filter(None, (w.strip() for w in spec.split(","))):
            match = [p for p in peers if p["name"].lower() == word.lower() or p["id"].startswith(word)]
            if not match:
                print("  nobody called %r (see /peers)" % word)
                return None
            found.append(match[0]["id"])
        return found

    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        conv = state["conv"]
        try:
            if cmd == "/quit":
                break
            elif cmd == "/help":
                print(CLI_HELP)
            elif cmd == "/peers":
                for p in sorted(node.peers(), key=lambda p: p["name"].lower()):
                    print("  %s %-20s %-15s %s" % ("*" if p["online"] else " ", p["name"], p["ip"],
                                                   "IPMsg" if p["kind"] == "ipmsg" else ""))
            elif cmd == "/groups":
                for g in node.groups():
                    names = [node.display_name(m) for m, e in g["members"].items() if e["in"]]
                    print("  %s #%-20s %s" % ("*" if g["joined"] else " ", g["name"],
                                              ", ".join(sorted(names))))
            elif cmd == "/join":
                g = [g for g in node.groups() if g["name"].lower() == arg.lstrip("#").lower()]
                if not g:
                    print("  no such group (see /groups)")
                    continue
                node.join_group(g[0]["id"])
                state["conv"] = "group:" + g[0]["id"]
                print("  joined; now talking in %s" % label(state["conv"]))
            elif cmd == "/to":
                if arg.lower() in ("everyone", "#everyone"):
                    state["conv"] = EVERYONE
                elif arg.startswith("#"):
                    g = [g for g in node.groups() if g["name"].lower() == arg[1:].lower()]
                    if not g:
                        print("  no such group (see /groups)")
                        continue
                    state["conv"] = "group:" + g[0]["id"]
                else:
                    ids = find_peers(arg)
                    if not ids:
                        continue
                    state["conv"] = "dm:" + ids[0]
                print("  now talking in %s" % label(state["conv"]))
            elif cmd == "/group":
                name, _, people = arg.partition(" ")
                ids = find_peers(people)
                if not name or ids is None:
                    print("  usage: /group NAME person,person")
                    continue
                state["conv"] = "group:" + node.create_group(name, ids)
                print("  created %s, now talking there" % label(state["conv"]))
            elif cmd == "/add":
                ids = find_peers(arg)
                if ids and conv.startswith("group:"):
                    node.add_members(conv[6:], ids)
                elif not conv.startswith("group:"):
                    print("  switch to a group first")
            elif cmd == "/leave":
                if conv.startswith("group:"):
                    node.leave_group(conv[6:])
                    state["conv"] = EVERYONE
                    print("  left; now talking in #everyone")
            elif cmd == "/name":
                node.set_name(arg)
            elif cmd == "/history":
                for rec in node.history(conv):
                    print(fmt(rec))
            elif cmd.startswith("/"):
                print("  unknown command, try /help")
            else:
                node.send_text(conv, line)
        except ValueError as exc:
            print("  error:", exc)
    node.stop()


if __name__ == "__main__":
    main()
