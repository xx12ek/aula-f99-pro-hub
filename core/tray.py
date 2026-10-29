"""系统托盘：常驻显示电量。"""
import threading
import time

from PIL import Image, ImageDraw, ImageFont

import pystray


def _icon_image(pct: int, charging: bool, ok: bool) -> Image.Image:
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, S - 3, S - 3], radius=16, fill=(14, 16, 22, 255))
    ring = (34, 211, 238, 255) if ok else (110, 116, 130, 255)
    # 电量弧
    d.arc([9, 9, S - 9, S - 9], start=-90, end=-90 + int(3.6 * max(0, min(100, pct))),
          fill=ring, width=5)
    if charging:
        d.rectangle([36, 20, 44, 34], fill=(251, 191, 36, 255))
        d.rectangle([40, 30, 46, 44], fill=(251, 191, 36, 255))
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/seguisb.ttf", 26)
    except Exception:
        font = ImageFont.load_default()
    txt = "--" if pct < 0 else str(pct)
    box = d.textbbox((0, 0), txt, font=font)
    d.text(((S - (box[2] - box[0])) / 2 - box[0], (S - (box[3] - box[1])) / 2 - box[1] - 1),
           txt, font=font, fill=(238, 241, 248, 255))
    return img


class Tray:
    def __init__(self, on_show, on_quit):
        self.on_show = on_show
        self.on_quit = on_quit
        self.icon = None
        self.lock = threading.Lock()

    def start(self):
        menu = pystray.Menu(
            pystray.MenuItem("打开 AULA Hub", lambda: self.on_show(), default=True),
            pystray.MenuItem("退出", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon(
            "aula-hub",
            _icon_image(-1, False, False),
            "AULA Hub · 正在连接",
            menu,
        )
        threading.Thread(target=self.icon.run, daemon=True).start()

    def update(self, pct: int, charging: bool, ok: bool):
        if not self.icon:
            return
        try:
            self.icon.icon = _icon_image(pct, charging, ok)
            self.icon.title = ("AULA Hub · 未连接" if not ok
                               else f"AULA Hub · {pct}%" + (" 充电中" if charging else ""))
        except Exception:
            pass

    def notify(self, msg: str, title: str = "AULA Hub"):
        """托盘气泡，第一次缩到托盘时提醒用户程序还在。"""
        if not self.icon:
            return
        try:
            self.icon.notify(msg, title)
        except Exception:
            pass

    def stop(self):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
