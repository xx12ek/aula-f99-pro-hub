"""让原生窗口和深色界面协调：深色标题栏、标题栏配色、程序图标。"""
import ctypes
import os
import time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
dwm = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_WINDOW_CORNER_PREFERENCE = 33

WM_SETICON = 0x0080
ICON_BIG, ICON_SMALL = 1, 0
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010


def _attr(hwnd, attr, value):
    v = ctypes.c_int(value)
    dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))


def apply(hwnd, icon_path=None):
    if not hwnd:
        return
    try:
        _attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        _attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, 2)  # 圆角
        _attr(hwnd, DWMWA_CAPTION_COLOR, 0x00100C08)   # COLORREF #080c10
        _attr(hwnd, DWMWA_TEXT_COLOR, 0x00F8F1EE)      # 浅色文字
        _attr(hwnd, DWMWA_BORDER_COLOR, 0x00241B16)    # 细边框
    except Exception:
        pass
    if icon_path and os.path.exists(icon_path):
        for size, kind in ((32, ICON_BIG), (16, ICON_SMALL)):
            h = user32.LoadImageW(None, icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                user32.SendMessageW(hwnd, WM_SETICON, kind, h)


def watch(title="AULA Hub", icon_path=None, timeout=15):
    """等窗口出现后套用外观。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            time.sleep(0.25)
            apply(hwnd, icon_path)
            return
        time.sleep(0.2)
