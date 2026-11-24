"""Drop files from Explorer onto a Tk window (Windows only, plain ctypes).

Tk has no drag and drop of its own. We mark the window as accepting files
(DragAcceptFiles) and subclass its window procedure to catch WM_DROPFILES.

The window procedure runs *inside* Tk's event loop, where calling back into
Tkinter crashes Python ("PyEval_RestoreThread: NULL tstate"). So it only
queues the paths; ``dispatch()``, called from a Tk timer, delivers them.
"""

import ctypes
import os
import queue
from ctypes import wintypes

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

_hooks = {}  # hwnd -> (new proc, old proc, callback): keeps the ctypes callbacks alive
_pending = queue.SimpleQueue()  # (hwnd, [paths]) waiting for dispatch()


def supported():
    return os.name == "nt"


def hook_dropfiles(widget, callback):
    """Call ``callback(list_of_paths)`` when files are dropped anywhere on
    ``widget``'s window (delivered by ``dispatch()``). False if unsupported."""
    if not supported():
        return False
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    set_proc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    set_proc.restype = ctypes.c_void_p
    set_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = LRESULT
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

    widget.update_idletasks()
    hwnd = widget.winfo_id()
    old = [None]

    def proc(h, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            paths = []
            count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
            for i in range(count):
                size = shell32.DragQueryFileW(wparam, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(size)
                shell32.DragQueryFileW(wparam, i, buf, size)
                paths.append(buf.value)
            shell32.DragFinish(wparam)
            _pending.put((h, paths))  # no Tkinter calls in here!
            return 0
        return user32.CallWindowProcW(old[0], h, msg, wparam, lparam)

    new = WNDPROC(proc)
    shell32.DragAcceptFiles(hwnd, True)
    old[0] = set_proc(hwnd, GWLP_WNDPROC, ctypes.cast(new, ctypes.c_void_p))
    _hooks[hwnd] = (new, old, callback)
    return True


def dispatch():
    """Deliver dropped files to their callbacks. Call from the Tk thread,
    e.g. from a widget.after() timer."""
    while True:
        try:
            hwnd, paths = _pending.get_nowait()
        except queue.Empty:
            return
        hook = _hooks.get(hwnd)
        if hook:
            try:
                hook[2](paths)
            except Exception:  # e.g. its window was closed meanwhile
                pass
