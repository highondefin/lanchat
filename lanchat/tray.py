"""Windows notification-area ("system tray") icon, using only ctypes.

Runs its own thread with a hidden window and message loop. User actions are
reported through ``on_event(name)`` from that thread ("open" or "quit"); the
caller should hand them to its UI thread (e.g. via a queue).
"""

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
WM_NULL, WM_DESTROY, WM_CLOSE = 0x0, 0x2, 0x10
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x202, 0x203, 0x205
NIN_BALLOONUSERCLICK = 0x405
WM_TRAY = 0x8000 + 1  # WM_APP + 1
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512
CMD_OPEN, CMD_QUIT = 1, 2

_sig = [
    (user32.DefWindowProcW, LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
    (user32.RegisterClassW, wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)]),
    (user32.CreateWindowExW, wintypes.HWND,
     [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]),
    (user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND]),
    (user32.GetMessageW, wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]),
    (user32.TranslateMessage, wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)]),
    (user32.DispatchMessageW, LRESULT, [ctypes.POINTER(wintypes.MSG)]),
    (user32.PostMessageW, wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
    (user32.PostQuitMessage, None, [ctypes.c_int]),
    (user32.LoadImageW, wintypes.HANDLE,
     [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]),
    (user32.LoadIconW, wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR]),
    (user32.CreatePopupMenu, wintypes.HMENU, []),
    (user32.AppendMenuW, wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]),
    (user32.SetMenuDefaultItem, wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, wintypes.UINT]),
    (user32.TrackPopupMenu, ctypes.c_int,
     [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]),
    (user32.DestroyMenu, wintypes.BOOL, [wintypes.HMENU]),
    (user32.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
    (user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND]),
    (user32.RegisterWindowMessageW, wintypes.UINT, [wintypes.LPCWSTR]),
    (shell32.Shell_NotifyIconW, wintypes.BOOL, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
    (kernel32.GetModuleHandleW, wintypes.HMODULE, [wintypes.LPCWSTR]),
]
for _fn, _res, _args in _sig:
    _fn.restype, _fn.argtypes = _res, _args


class Tray:
    def __init__(self, tooltip, icon_path, on_event):
        self.tooltip = tooltip
        self.icon_path = icon_path
        self.on_event = on_event
        self.hwnd = None
        self.ok = False
        # _proc already runs during CreateWindowExW, so these must exist first
        self._icon = None
        self._taskbar_created = None
        self._ready = threading.Event()
        self._thread = None

    def start(self):
        """Create the icon. Returns False if the tray isn't available."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.ok

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(timeout=3)

    def set_tooltip(self, text):
        self.tooltip = text
        if self.ok:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(NIF_TIP)))

    def balloon(self, title, text):
        """Show a notification bubble from the tray icon."""
        if not self.ok:
            return
        nid = self._data(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ------------------------------------------------------------ tray thread

    def _data(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = self.tooltip[:127]
        return nid

    def _add(self):
        return shell32.Shell_NotifyIconW(
            NIM_ADD, ctypes.byref(self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP)))

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._proc_ref = WNDPROC(self._proc)  # keep a reference: called from C
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc_ref
            wc.hInstance = hinst
            wc.lpszClassName = "LanChatTray%d" % id(self)
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError("RegisterClassW failed")
            self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "LanChat tray", 0, 0, 0, 0, 0,
                                               None, None, hinst, None)
            if not self.hwnd:
                raise OSError("CreateWindowExW failed")
            self._icon = None
            if self.icon_path:
                self._icon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                               LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not self._icon:
                self._icon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self.ok = bool(self._add())
        except Exception:
            self.ok = False
        finally:
            self._ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                self.on_event("open")
            elif lparam == WM_RBUTTONUP:
                self._menu()
            return 0
        if msg == self._taskbar_created:  # Explorer restarted: put the icon back
            self._add()
            return 0
        if msg == WM_CLOSE:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Open LanChat")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
        user32.SetMenuDefaultItem(menu, CMD_OPEN, False)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # so the menu closes when clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0,
                                    self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == CMD_OPEN:
            self.on_event("open")
        elif cmd == CMD_QUIT:
            self.on_event("quit")
