"""Release only currently held native input in the selected private X context."""

import ctypes
import ctypes.util


def release_held_input() -> bool:
    """Run inside VNC_HELPER confinement, with its exact DISPLAY/XAUTHORITY.

    No pointer movement, key presses, lock toggles, or trajectory synthesis.
    The caller bounds this separate process: an unreachable X server must not
    hold up helper termination. Xauthority protects reused display numbers.
    """
    xlib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xtest = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
    display_type = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XOpenDisplay.restype = display_type
    xlib.XDefaultRootWindow.argtypes = [display_type]
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    xlib.XQueryKeymap.argtypes = [display_type, ctypes.c_void_p]
    xlib.XQueryPointer.argtypes = [
        display_type,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        *([ctypes.POINTER(ctypes.c_int)] * 4),
        ctypes.POINTER(ctypes.c_uint),
    ]
    xtest.XTestFakeKeyEvent.argtypes = [display_type, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtest.XTestFakeButtonEvent.argtypes = [
        display_type,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xlib.XSync.argtypes = [display_type, ctypes.c_int]
    xlib.XCloseDisplay.argtypes = [display_type]
    display = xlib.XOpenDisplay(None)
    if not display:
        return False
    try:
        keys = (ctypes.c_ubyte * 32)()
        xlib.XQueryKeymap(display, keys)
        for keycode in range(8, 256):
            if keys[keycode // 8] & (1 << (keycode % 8)):
                xtest.XTestFakeKeyEvent(display, keycode, 0, 0)
        root = ctypes.c_ulong()
        child = ctypes.c_ulong()
        coordinates = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        if xlib.XQueryPointer(
            display,
            xlib.XDefaultRootWindow(display),
            ctypes.byref(root),
            ctypes.byref(child),
            *(ctypes.byref(value) for value in coordinates),
            ctypes.byref(mask),
        ):
            for button in range(1, 6):
                if mask.value & (1 << (button + 7)):
                    xtest.XTestFakeButtonEvent(display, button, 0, 0)
        xlib.XSync(display, 0)
        return True
    finally:
        xlib.XCloseDisplay(display)


if __name__ == "__main__":
    raise SystemExit(0 if release_held_input() else 1)
