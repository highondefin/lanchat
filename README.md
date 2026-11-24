# LanChat

Serverless chat for a local network with **public group chat**, which also
talks to **IP Messenger (IPMsg)** users.
Pure Python 3 standard library — nothing to install.

- Works like IP Messenger: a searchable user list, messages pop up with
  Reply/Quote, sealed messages with a read notice
- Finds everyone on the LAN automatically: LanChat users *and* IPMsg / iptux users
- **Group chats**: public, so everybody sees every group and can join with one click
- IPMsg users can be in groups too (see below)
- Messages to people who are offline are queued and delivered when they come back
- History, groups and your identity are saved in `~/.lanchat/`
- Desktop GUI (Tkinter) or a terminal mode (`--cli`)

## Run

```bash
cd lanchat                          # the folder containing this README
python3 -m lanchat                  # GUI
python3 -m lanchat --name Alice     # set your display name (remembered)
python3 -m lanchat --cli            # terminal interface (type /help)
```

On Windows use `py -m lanchat`. On Debian/Ubuntu the GUI needs
`sudo apt install python3-tk` if Tkinter is missing.

### Windows .exe

`dist/LanChat.exe` is a single file that needs no Python: copy it to the PC and
double-click it. On the first start, Windows Firewall asks whether LanChat may
use the network: tick **Private networks** and click **Allow access**. Quit IP
Messenger on that PC first, so LanChat can use port 2425 and see your IPMsg users.
Settings are saved in `%USERPROFILE%\.lanchat`. The exe accepts the same options
as below, e.g. a shortcut to `LanChat.exe --name Alice`; `--cli` doesn't work in
the exe because it has no console.

**Building the exe**

- On Windows with Python 3.8+ from python.org: run `build_windows.bat`; the
  result is `dist\LanChat.exe`.
- Or let GitHub build it: every push runs the *Test and build* workflow
  (Actions tab). Open the latest run and download the **LanChat-windows**
  artifact. You can also start it by hand with *Run workflow*.

## Using it

LanChat is for **group chats that include IP Messenger users**. It looks and
works like IP Messenger.

**Main window**
- **User list** of everyone on the LAN (LanChat and IPMsg users): type in
  *Search* (name, group, host or IP) to filter, click a column to sort,
  *Refresh* (F5) to ask everyone again. Ctrl+F jumps to the search box.
- **Group chats**: every group on the LAN (groups are public). Double-click
  one to open its window; *Status* shows whether you've joined it.
- **Start group chat**: select people (Ctrl/Shift-click), click it, give the
  group a name. A new window opens for the group.
- **Send message**: an ordinary IPMsg-style message to the selected people.
  Incoming messages pop up with *Reply* and *Quote*; *Seal* works as in IPMsg.
- **Log**: all past messages, searchable. **Settings**: your user name and
  group (department), and whether group windows pop up on new messages.

**Tray (Windows)**: like IP Messenger, LanChat keeps running in the
notification area. Closing the window only hides it: messages and group chats
still pop up. Click the tray icon to open LanChat, right-click it → *Quit* to
exit. Starting LanChat again just brings the running one to the front.
*Settings → Start LanChat when Windows starts* runs it in the tray at log-on
(`--minimized` starts it hidden). If the tray icon is hidden by Windows, drag
it from the "^" overflow area onto the taskbar to keep it visible.

**Files**
- *Send message* → **Attach files…**: send files to one or more people (with an
  optional message; *Seal* works too). Group chats are for messages only.
- **Drag and drop (Windows):** drop files from Explorer onto the send window to
  attach them, or select people in the main window and drop files onto it to
  open a send window with the files attached. (Doesn't work if LanChat runs
  "as administrator": Windows blocks drops from Explorer into elevated programs.)
- Received files are listed in the message window with **Save…** (and *Save
  all…*). Progress is shown; afterwards the button opens the file.
- Files are downloaded directly from the sender's PC, so the sender must be
  online (LanChat running, also fine in the tray).
- Works with IP Messenger both ways: IPMsg users see LanChat files as normal
  IPMsg attachments, and files they send arrive in LanChat. Folders can't be
  sent or received yet.

**Group chat window**
- Members on the left (*Add…*, *Remove*), the chat on the right, the message
  box below: Enter sends, Shift+Enter starts a new line.
- **Join group**: you then get the group's **whole earlier history** from the
  members who are online, and all new messages. **Leave group** to stop.
- IPMsg members don't need LanChat: they get every message in IP Messenger as
  `[Group] Name: text`, and their replies are posted into the group.

To try it on **one** computer, give each copy its own profile:

```bash
python3 -m lanchat --profile alice --name Alice &
python3 -m lanchat --profile bob   --name Bob &
```

### Options

| Option | |
|---|---|
| `--name NAME` | display name |
| `--group G` | your group / department |
| `--profile P` / `--data-dir DIR` | separate identity and data |
| `--broadcast ADDR` | extra broadcast address, e.g. `192.168.1.255` (repeatable) |
| `--no-ipmsg` | don't talk to IPMsg users |
| `--minimized` | start hidden in the tray |
| `--ipmsg-encoding ENC` | encoding for old IPMsg clients without UTF-8, e.g. `cp932`, `gbk` |
| `--port N` | LanChat port (default 50505) |

### Firewall

Allow on every machine:
- **UDP 50505** and **TCP 50505**: LanChat
- **UDP 2425** and **TCP 2425**: IP Messenger (TCP is for file transfer)

## Working with IP Messenger users

- IPMsg users and LanChat users are in the same list; the status bar counts how
  many are on IPMsg, and a message window says "(IPMsg)" or "(LanChat)".
  LanChat users show up in IPMsg with their name and group from *Settings*.
- Messages, sealed messages and read notices are normal IPMsg messaging on
  their side.
- **Groups:** add IPMsg users with *Add people* in a group window. They receive group messages as
  ordinary IPMsg messages:

  ```
  [Weekend trip] Carol: Who's in for hiking on Saturday?
  ```

  When they **reply**, the reply is posted into the group for everyone. If
  they're in several groups, the group is chosen from the quoted text, or
  from a `[Group name]` at the start of their message, or else it's the group
  they last heard from.
- IPMsg users can type commands in a message to any LanChat user:
  `/groups`, `/join NAME`, `/leave NAME`, `/dm text` (a private message instead
  of a group message), `/help`.
- **IPMsg and LanChat on the same PC:** only one program can use port 2425.
  LanChat notices this, shows "IPMsg off" in the status bar, and passes its
  IPMsg messages through another LanChat user's computer. To see IPMsg users in
  LanChat itself, quit IP Messenger on that PC.
- Not supported yet: sending or receiving folders, encrypted IPMsg messages and files.
  Turn encryption off for LanChat users in IP Messenger if messages come
  through as "[encrypted message]".

Tested against iptux 0.7.6, and against the protocol spec of H. Shirouzu's IP
Messenger (`protocol.txt` / `ipmsg.h`, including the UTF-8 entry extension).

## How it works

| What | How |
|---|---|
| Discovery | UDP broadcast `hello` on start and every 10 s, `bye` on exit; peers silent for 35 s are shown offline |
| Messages | JSON over TCP, one connection per message, receiver acknowledges; one sender thread per peer keeps order |
| Offline delivery | Undelivered messages go to an outbox (`outbox.json`) and are flushed when the peer is back |
| Groups | No server: every node stores every public group. Each member entry has a timestamp and the newest wins, so concurrent joins, leaves and adds converge. When a node sees older state than it has, it sends its copy back. Groups sync to anyone who comes online |
| IPMsg | `BR_ENTRY`/`ANSENTRY`/`BR_EXIT` on UDP 2425 (with `CAPUTF8OPT` + UTF-8 extension), `SENDMSG`+`SENDCHECKOPT` with retries until `RECVMSG`; re-broadcasts `BR_ENTRY` every 3 min like IPMsg's *Refresh* |

Packet formats are documented at the top of [`lanchat/core.py`](lanchat/core.py)
and [`lanchat/ipmsg.py`](lanchat/ipmsg.py).

```
lanchat/core.py      LanChat networking, groups, storage (no UI code)
lanchat/ipmsg.py     IP Messenger protocol bridge
lanchat/gui.py       Tkinter interface
lanchat/tray.py      Windows tray icon (ctypes)
lanchat/dnd.py       drag and drop from Explorer (ctypes)
lanchat/__main__.py  command line + terminal interface
tests/test_core.py   end-to-end tests: real sockets, several nodes, a fake IPMsg client
packaging/           exe entry point and icon; build_windows.bat builds the exe
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Limitations

- **No encryption or authentication.** Anyone on the LAN can read traffic or
  pretend to be someone, just as with plain IPMsg. That's fine for a trusted
  office or home network.
- All groups are public for now (private groups are planned; the `public`
  flag is already in the group format).
- A new member gets the earlier history only from members who are online at
  that moment; IPMsg members only get new messages.
- Offline users are not shown in the list (as in IPMsg); messages already sent to them are still queued.
- Group membership ordering relies on roughly synchronised clocks.
