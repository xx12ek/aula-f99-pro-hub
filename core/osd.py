"""屏幕上的提示条 —— Win11 原生 OSD 的像素级复刻（纯 Win32 实现）。

为什么要分两轨
--------------
真机穷举实测（2026-10-22，见 `tmp/osd_which.py` / `tmp/scan_inject.py`）：

    物理媒体键穿过低级钩子放行  ->  Windows 自己弹原生 OSD，音量真变
    WASAPI 直接改音量           ->  音变，OSD 不弹
    SendInput / keybd_event     ->  音变，OSD 不弹
    扫描码注入                   ->  音变，OSD 不弹
    WM_APPCOMMAND 广播           ->  音变（而且直接拉满），OSD 不弹
    WMI 改屏幕亮度               ->  亮度变，OSD 不弹

也就是说：**系统 OSD 只认真实硬件按键**，程序化改值一律不弹。所以：

  * 音量 / 静音 —— 旋钮那一格绑成它原生语义时，钩子直接放行，让 Windows
    画它自己的条。那是最漂亮的，也是用户要的「以系统那个为优先」。
    这一路本模块完全不参与。
  * 键盘背光 / 灯效 / 速度 / 屏幕亮度 —— 系统根本没有对应 OSD，只能自绘。
    形态照 Win11 抄，尽量看不出是仿的。

为什么不用 tkinter
------------------
第一版是 tkinter + `-transparentcolor` 洋红哨兵色抠背景，真机截图
（`shots/osd_live.png`）露出两个硬伤：

  1. 抗锯齿会在边缘产生「洋红→胶囊色」的中间色，逐一精确匹配抠不掉，
     结果胶囊外面挂了一圈洋红描边，非常难看。
  2. 位图缩放后边缘发糊，跟 Win11 的锐利边缘完全不像。

改用 Win32 `UpdateLayeredWindow` + 预乘 alpha 的 32bpp DIB：
真正逐像素透明，圆角边缘干净，也不需要任何 tkinter 依赖。

形态（96 dpi 逻辑像素，运行时按 DPI 缩放）
----------------------------------------
Win11 那条提示是一个 **170x45 逻辑像素的圆角矩形**（圆角只有 6，不是
胶囊）：纯色底，左边一个图标，右边一根 4 物理像素的细条，条在**垂直正中**、
左端从 27% 处让开图标，填充用**系统强调色**，**没有任何文字和数字**。

以上尺寸不是估的：`tmp/osd_layout.txt` 是从原生条截图上逐像素量出来的
（物理 255x68 / 圆角 9 / 条 165x6 / 条左端距左 68 / 右距右 22 / 底边距 63），
再除以 1.5 的 DPI 缩放得到逻辑值。早先那版 300x60 做成了胶囊、填充用固定
白色，跟原生差得很远。

实现要点
--------
1. 图标与进度条都用 PIL 以 4 倍超采样绘制再 LANCZOS 缩回，边缘才锐。
2. 像素必须是**预乘 alpha**（`UpdateLayeredWindow` 的 `AC_SRC_ALPHA` 要求），
   否则半透明边缘会泛白。
3. 窗口 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW |
   WS_EX_NOACTIVATE` + `HWND_TOPMOST`：浮在最上层、鼠标穿透、不抢焦点、
   不出现在 Alt+Tab。
4. 反复 show 只重置隐藏计时，不重建窗口（转旋钮很频繁，重建会闪）。
5. 深/浅色跟**任务栏实际像素**走，注册表兜底 —— 实测本机注册表写着浅色
   但任务栏是深色，只信注册表会画错。
"""
from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes as wt

from PIL import Image, ImageDraw

# ── 形态（96 dpi 下的逻辑像素，运行时乘 scale）──────────────────────
# 全部来自对 Win11 原生条的逐像素实测（2026-10-23，tmp/osd_layout.txt）：
# 截到的是笔记本亮度键按下的那一帧，物理 255x68、背景 #DADBDB、
# 圆角只有约 9 物理像素 —— 所以它是**圆角矩形**，不是胶囊。
W_LOGICAL = 170          # 255 物理 / 1.5
H_LOGICAL = 45           # 68 物理 / 1.5
RADIUS_LOGICAL = 5       # 实测圆角 7~8 物理 / 1.5
# 图标 bbox 实测 20x20 物理（y 23..42, x 25..44），中心 (34.5, 32.5)。
# 胶囊高 67、中线 33.5 —— 所以图标**比中线高 1 物理**，跟进度条同轴（条中心 32.5）。
ICON_LOGICAL = 13.33     # 实测太阳外廓 20 物理 / 1.5
ICON_CX_LOGICAL = 23.0   # 实测圆心 x = 34.5 物理 / 1.5
BAR_X_LOGICAL = 45       # 进度条左端到胶囊左边缘（= 68 物理 / 1.5）
BAR_RIGHT_LOGICAL = 15   # 进度条右端到胶囊右边缘（= 22 物理 / 1.5）
BAR_H_LOGICAL = 4        # 6 物理 / 1.5
# 图标与进度条都**比几何中线高 1 物理**（实测两者中心 y 都是 32.5，胶囊中线 33.5）。
# 这个 1 像素的偏移肉眼看不出，但对齐后误差能从 4.7 降到 4.3 左右。
CENTER_DY_LOGICAL = -0.67
MARGIN_BOTTOM_LOGICAL = 42   # 63 物理 / 1.5

FADE_MS = 1600           # 停留多久开始隐藏
TICK_MS = 40

# ── 配色（Win11 实测取色）──────────────────────────────────────────
# 浅色档的 bg / track 是从原生条截图上直接量的均值，不是猜的：
#   bg    mean [218 219 219] = #DADBDB
#   track mean [143 143 142] = #8F8F8E
# 填充色**不是**固定色，是系统强调色（本机 #D00050 洋红），由 accent() 取。
THEMES = {
    True: {"bg": "#2b2b2b", "fg": "#ffffff", "track": "#5a5a5a"},   # 深色
    False: {"bg": "#DADBDB", "fg": "#1a1a1a", "track": "#8F8F8E"},  # 浅色
}
_DEFAULT_ACCENT = "#D00050"


def accent() -> str:
    """系统强调色（就是原生条里进度条填充的那条洋红）。

    注册表里存着**好几套**强调色，实测只有一个是 OSD 真用的
    （`tmp/accent_probe.py`，目标 = 原生条截图的众数色 #D00050）：

        Accent\\StartColorMenu   #D30050   d= 3.00   <- 就是它（差 3 是取整）
        DWM\\ColorizationColor   #EA005E   d=29.53   <- 偏亮，OSD 不用
        DWM\\AccentColor         #EA005E   d=29.53
        Accent\\AccentColorMenu  #EA005E   d=29.53
        AccentPalette[4]        #EA005E   d=29.53

    注意字节序：`StartColorMenu` 是 0xAABBGGRR，不是 AARRGGBB。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion"
                r"\Explorer\Accent") as k:
            v, _ = winreg.QueryValueEx(k, "StartColorMenu")
            v = int(v)
            return "#%02X%02X%02X" % (v & 255, (v >> 8) & 255, (v >> 16) & 255)
    except Exception:                            # noqa: BLE001
        pass
    try:                                          # 退路：DWM 的（偏亮一点）
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\DWM") as k:
            v, _ = winreg.QueryValueEx(k, "ColorizationColor")
            v = int(v) & 0xFFFFFF
            return "#%02X%02X%02X" % ((v >> 16) & 255, (v >> 8) & 255, v & 255)
    except Exception:                            # noqa: BLE001
        return _DEFAULT_ACCENT


# ── Win32 ───────────────────────────────────────────────────────────
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
HWND_TOPMOST = -1
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte),
                ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wt.DWORD * 3)]


# DefWindowProcW 必须声明原型：不声明时 ctypes 会把 LPARAM 按 int 传，
# 高位消息（如 WM_DPICHANGED）的 lParam 超过 32 位就抛 OverflowError，
# 窗口过程里一抛异常，后续消息全部漏处理。
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

_user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
_user32.UpdateLayeredWindow.restype = wt.BOOL
_user32.CreateWindowExW.restype = wt.HWND
_user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.UINT]
_gdi32.CreateDIBSection.restype = wt.HBITMAP
_gdi32.CreateDIBSection.argtypes = [
    wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wt.HDC]


# ── 主题判定 ────────────────────────────────────────────────────────

def _registry_is_light() -> bool:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            v, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
            return bool(v)
    except Exception:                            # noqa: BLE001
        return False


def detect_dark() -> bool:
    """判断该用深色还是浅色。

    优先采样**任务栏真实像素**：本机注册表写着 SystemUsesLightTheme=1，
    但任务栏实际是深灰 —— 只信注册表会画出跟系统完全不搭的浅色条。
    """
    try:
        from PIL import ImageGrab
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        # 取任务栏中间一小块（避开左右两端的图标）
        box = (sw // 2 - 60, sh - 12, sw // 2 + 60, sh - 4)
        im = ImageGrab.grab(bbox=box).convert("L")
        px = list(im.getdata())
        if not px:
            return not _registry_is_light()
        avg = sum(px) / float(len(px))
        return avg < 128          # 平均偏黑 -> 深色
    except Exception:                            # noqa: BLE001
        return not _registry_is_light()


def dpi_scale() -> float:
    """当前系统的 DPI 缩放（96 dpi = 1.0）。"""
    try:
        # 进程若不是 DPI aware，这两个 API 都会返回 96，所以先声明一次
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:                        # noqa: BLE001
            pass
        hdc = _user32.GetDC(None)
        try:
            dpi = _gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        finally:
            _user32.ReleaseDC(None, hdc)
        if dpi and dpi > 0:
            return max(0.75, min(3.0, dpi / 96.0))
    except Exception:                            # noqa: BLE001
        pass
    return 1.0


# ── 图标绘制（都在 0..size 的方框里）──────────────────────────────

def _draw_speaker(d, x0, y0, size, fill, muted=False, waves=2):
    """喇叭 + 声波弧；muted=True 时改用叉号。"""
    u = size / 22.0
    cx = x0 + size / 2
    cy = y0 + size / 2
    body_x = cx - 7 * u
    d.polygon(
        [(body_x, cy - 3.2 * u), (body_x + 3.4 * u, cy - 3.2 * u),
         (body_x + 7.4 * u, cy - 7.4 * u), (body_x + 7.4 * u, cy + 7.4 * u),
         (body_x + 3.4 * u, cy + 3.2 * u), (body_x, cy + 3.2 * u)],
        fill=fill)
    arc_x = body_x + 5.6 * u
    if muted:
        r = 4.4 * u
        sx = arc_x + r * 0.7
        w = max(2, int(round(1.9 * u)))
        d.line([(sx, cy - r), (sx + 2 * r, cy + r)], fill=fill, width=w)
        d.line([(sx + 2 * r, cy - r), (sx, cy + r)], fill=fill, width=w)
        return
    for i in range(waves):
        rr = (4.6 + i * 4.4) * u
        d.arc([arc_x - rr, cy - rr, arc_x + rr, cy + rr],
              start=-52, end=52, fill=fill, width=max(2, int(round(1.7 * u))))


def _draw_sun(d, x0, y0, size, fill):
    """太阳：**空心**圆 + 8 个分离的点状光芒（原生条就是这个形态）。

    放大 8 倍看原生条（tmp/osd_measure/cap_after_8x.png）：中间是一圈
    空心圆环，周围是 8 个小点，不是实心圆 + 线状光芒。
    """
    import math
    cx = x0 + size / 2
    cy = y0 + size / 2
    # 比例全部来自原生条的逐像素实测（tmp/osd_sun_measure.py）：
    #   太阳外框 19 物理，圆环命中半径 5..6、光点命中半径 8..9
    r = size * 0.289            # 环中心半径
    ring = max(1.0, size * 0.079)
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=fill, width=max(1, int(round(ring))))
    dot_r = max(0.6, size * 0.053)
    for i in range(8):
        a = math.radians(i * 45.0)
        ca, sa = math.cos(a), math.sin(a)
        px = cx + ca * size * 0.447
        py = cy + sa * size * 0.447
        d.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r], fill=fill)


def _draw_bulb(d, x0, y0, size, fill):
    """灯泡：**空心**圆头 + 底座两道横线（键盘背光）。

    空心而不是实心：尺寸只有 19 物理像素，实心圆在这个尺寸上糊成一个黑点，
    和空心圆环的太阳放在一起也不成系列。线宽沿用太阳的环宽比例。
    """
    u = size / 22.0
    cx = x0 + size / 2
    cy = y0 + size / 2 - 1.6 * u
    r = 4.6 * u
    w = max(1, int(round(max(1.0, size * 0.079))))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=w)
    for i, dy in enumerate((5.2, 7.4)):
        half = (2.9 - i * 0.8) * u
        d.line([(cx - half, cy + dy * u), (cx + half, cy + dy * u)],
               fill=fill, width=w)


def _draw_sparkle(d, x0, y0, size, fill):
    """四角星：灯效。"""
    u = size / 22.0
    cx = x0 + size / 2
    cy = y0 + size / 2
    big, small = 9.2 * u, 3.0 * u
    d.polygon(
        [(cx, cy - big), (cx + small, cy - small), (cx + big, cy),
         (cx + small, cy + small), (cx, cy + big), (cx - small, cy + small),
         (cx - big, cy), (cx - small, cy - small)], fill=fill)


def _draw_wind(d, x0, y0, size, fill):
    """速度：一个尖角箭头 + 两条拖尾线。

    原来画三道横线，在 19 像素上看起来就是汉堡菜单图标，认不出是速度。
    """
    u = size / 22.0
    cx = x0 + size / 2
    cy = y0 + size / 2
    w = max(1, int(round(max(1.0, size * 0.079))))
    # 箭头（指向右）
    ax = cx + 8.4 * u
    d.line([(cx - 1.2 * u, cy - 5.6 * u), (ax, cy), (cx - 1.2 * u, cy + 5.6 * u)],
           fill=fill, width=w, joint="curve")
    # 两条拖尾
    for dy in (-3.4, 3.4):
        d.line([(cx - 8.6 * u, cy + dy * u), (cx - 4.2 * u, cy + dy * u)],
               fill=fill, width=w)


_ICONS = {
    "volume": lambda d, x, y, s, f: _draw_speaker(d, x, y, s, f, waves=2),
    "mute": lambda d, x, y, s, f: _draw_speaker(d, x, y, s, f, muted=True),
    "light": _draw_bulb,
    "screen": _draw_sun,
    "effect": _draw_sparkle,
    "speed": _draw_wind,
    # 长按旋钮切模式：两个模式各用一个和它用途同族的图标，用户一眼就知道
    # 现在切到了「声音」还是「亮度」。单一个 "mode" kind 画不出区别。
    "mode_sound": lambda d, x, y, s, f: _draw_speaker(d, x, y, s, f, waves=2),
    "mode_bright": _draw_sun,
}


def render(kind: str, value: float, total: float,
           scale: float = 1.0, dark: bool = True) -> Image.Image:
    """画一张 RGBA 的 OSD 位图。value<0 表示只有图标、没有进度条。"""
    th = dict(THEMES[bool(dark)])
    th["fill"] = accent()                     # 填充 = 系统强调色
    w = int(round(W_LOGICAL * scale))
    h = int(round(H_LOGICAL * scale))
    icon_sz = ICON_LOGICAL * scale
    icon_cx = ICON_CX_LOGICAL * scale
    bar_h = max(2.0, BAR_H_LOGICAL * scale)
    bar_x0 = BAR_X_LOGICAL * scale
    bar_x1 = w - BAR_RIGHT_LOGICAL * scale
    r = RADIUS_LOGICAL * scale               # 圆角矩形，不是胶囊

    ss = 4                                   # 超采样倍数
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss,
                        fill=th["bg"])

    drawer = _ICONS.get(kind)
    if drawer is not None:
        icon_cy = h / 2.0 + CENTER_DY_LOGICAL * scale
        drawer(d, (icon_cx - icon_sz / 2.0) * ss, (icon_cy - icon_sz / 2.0) * ss,
               icon_sz * ss, th["fg"])

    if value is not None and value >= 0:
        cy = h / 2.0 + CENTER_DY_LOGICAL * scale
        by = (cy - bar_h / 2.0) * ss           # 与图标同轴（不是几何正中）
        bh = bar_h * ss
        rr = bh / 2.0
        bx0 = bar_x0 * ss                     # 超采样坐标系（下面一律用它）
        bx1 = bar_x1 * ss
        d.rounded_rectangle([bx0, by, bx1, by + bh],
                            radius=rr, fill=th["track"])
        span = 0.0
        if total:
            span = max(0.0, min(1.0, float(value) / float(total)))
        if span > 0:
            # 原生条满档是圆头，左端也圆 —— 至少留一个圆点宽度。
            # 注意全长和下限都必须在**超采样坐标**里算（踩过：忘了乘 ss，
            # 填充只有应有的 1/4，看起来永远像刚开一点点）。
            fill_w = max(bh, (bx1 - bx0) * span)
            d.rounded_rectangle([bx0, by, bx0 + fill_w, by + bh],
                                radius=rr, fill=th["fill"])

    return big.resize((w, h), Image.LANCZOS)


def _premultiply_bgra(img: Image.Image) -> bytes:
    """PIL 的 RGBA 是非预乘的；`UpdateLayeredWindow` 要预乘 + BGRA。"""
    try:
        import numpy as np
        arr = np.asarray(img, dtype=np.uint16)
        a = arr[:, :, 3:4]
        out = np.empty((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
        out[:, :, 0] = ((arr[:, :, 2] * a[:, :, 0]) // 255).astype(np.uint8)  # B
        out[:, :, 1] = ((arr[:, :, 1] * a[:, :, 0]) // 255).astype(np.uint8)  # G
        out[:, :, 2] = ((arr[:, :, 0] * a[:, :, 0]) // 255).astype(np.uint8)  # R
        out[:, :, 3] = a[:, :, 0].astype(np.uint8)                            # A
        return out.tobytes()
    except Exception:                            # noqa: BLE001
        pass
    # numpy 不在时的纯 Python 退路（慢一点，但 OSD 尺寸很小）
    px = img.load()
    w, h = img.size
    buf = bytearray(w * h * 4)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            buf[i] = (b * a) // 255
            buf[i + 1] = (g * a) // 255
            buf[i + 2] = (r * a) // 255
            buf[i + 3] = a
            i += 4
    return bytes(buf)


class _LayeredWindow:
    """一个常驻的 layered window，反复 present 不同位图。"""

    def __init__(self):
        self.hwnd = None
        self.hdc = None
        self.hbmp = None
        self.old = None
        self.bits = None
        self.size = (0, 0)
        self._cls = None

    def _register(self):
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                                     wt.WPARAM, wt.LPARAM)
        self._wndproc = WNDPROC(lambda h, m, w, l: _user32.DefWindowProcW(h, m, w, l))
        self._user32 = _user32

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = "AulaHubOsd"
        self._cls = wc
        _user32.RegisterClassExW(ctypes.byref(wc))

    def create(self, w: int, h: int):
        if self.hwnd:
            return
        self._register()
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
              | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        self.hwnd = _user32.CreateWindowExW(
            ex, "AulaHubOsd", "", WS_POPUP, 0, 0, w, h,
            None, None, None, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")

        self.hdc = _gdi32.CreateCompatibleDC(None)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h            # 负数 = 自顶向下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0        # BI_RGB
        bits = ctypes.c_void_p()
        self.hbmp = _gdi32.CreateDIBSection(
            self.hdc, ctypes.byref(bi), DIB_RGB_COLORS, ctypes.byref(bits),
            None, 0)
        if not self.hbmp:
            raise OSError(f"CreateDIBSection 失败 err={ctypes.get_last_error()}")
        self.old = _gdi32.SelectObject(self.hdc, self.hbmp)
        self.bits = bits
        self.size = (w, h)

    def present(self, img: Image.Image, x: int, y: int):
        w, h = img.size
        if (w, h) != self.size:
            self.destroy()
            self.create(w, h)
        data = _premultiply_bgra(img)
        ctypes.memmove(self.bits, data, min(len(data), w * h * 4))

        pt_dst = wt.POINT(x, y)
        sz = wt.SIZE(w, h)
        pt_src = wt.POINT(0, 0)
        bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = _user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(pt_dst), ctypes.byref(sz),
            self.hdc, ctypes.byref(pt_src), 0, ctypes.byref(bf), ULW_ALPHA)
        if not ok:
            raise OSError(f"UpdateLayeredWindow 失败 err={ctypes.get_last_error()}")
        _user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def hide(self):
        if self.hwnd:
            _user32.ShowWindow(self.hwnd, SW_HIDE)

    def destroy(self):
        if self.hbmp:
            _gdi32.SelectObject(self.hdc, self.old)
            _gdi32.DeleteObject(self.hbmp)
            self.hbmp = None
        if self.hdc:
            _gdi32.DeleteDC(self.hdc)
            self.hdc = None
        if self.hwnd:
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.size = (0, 0)


class Osd:
    """一条提示条。全局一个实例，`instance().show(...)` 直接调。"""

    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=32)
        self._thread = None
        self._ready = threading.Event()
        self.enabled = True
        self.dark = True
        self.scale = 1.0
        self.last_error = ""
        self._shown = 0
        self._win = None

    # ---------- 对外 ----------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="osd")
        self._thread.start()
        self._ready.wait(timeout=6)

    def show(self, kind: str, value: float = -1, total: float = 100,
             label: str = "", text: str = ""):
        """kind 决定图标：volume / mute / light / screen / effect / speed。"""
        if not self.enabled:
            return
        try:
            self._q.put_nowait((kind, value, total))
        except queue.Full:
            pass

    def stop(self):
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    # ---------- 线程里 ----------
    def _run(self):
        try:
            self.scale = dpi_scale()
            self.dark = detect_dark()
            self._win = _LayeredWindow()
            w = int(round(W_LOGICAL * self.scale))
            h = int(round(H_LOGICAL * self.scale))
            self._win.create(w, h)
        except Exception as exc:                 # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return

        self._ready.set()

        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        hide_at = [0.0]
        visible = [False]

        while True:
            now = time.time()
            try:
                while True:
                    item = self._q.get_nowait()
                    if item is None:
                        self._win.destroy()
                        return
                    kind, value, total = item
                    try:
                        img = render(kind, value, total, self.scale, self.dark)
                        x = (sw - img.width) // 2
                        y = sh - img.height - int(
                            round(MARGIN_BOTTOM_LOGICAL * self.scale))
                        self._win.present(img, x, y)
                        visible[0] = True
                        hide_at[0] = now + FADE_MS / 1000.0
                        self._shown += 1
                    except Exception as exc:     # noqa: BLE001
                        self.last_error = f"{type(exc).__name__}: {exc}"
            except queue.Empty:
                pass

            if visible[0] and now >= hide_at[0]:
                try:
                    self._win.hide()
                except Exception:                # noqa: BLE001
                    pass
                visible[0] = False

            time.sleep(TICK_MS / 1000.0)


_inst = None


def instance() -> Osd:
    global _inst
    if _inst is None:
        _inst = Osd()
    return _inst


def show(kind: str, value: float = -1, total: float = 100,
         label: str = "", text: str = ""):
    instance().show(kind, value, total, label, text)
