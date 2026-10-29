"""把键盘从「固件模式」里拉回来。

为什么需要这个
--------------
键盘固件有一个自己的模式（配置页 `0x1A` = 1）。一旦进去：

    旋钮  -> 固件直接调键盘背光，**不给电脑发任何按键**
    软件  -> 钩子一条事件都收不到，绑什么动作都没用

用户长按旋钮就会进去（固件行为，改不掉）。表现就是「转旋钮调的是键盘
亮度，不是我绑的功能」，而且软件侧完全无辜 —— 实测 `hits=0`。

`0x1A` 可写（写 0 立刻生效，读回一致），所以对策很简单：**盯着它，一
发现是 1 就写回 0**。这样用户长按多少次都不会把软件接管弄死。

开销
----
读一次配置页 63ms（实测），每 2 秒一次 ≈ 3% 的 HID 占用。够便宜。
只在有线通道下有意义 —— 2.4G 没有读配置页的命令。
"""
from __future__ import annotations

import threading
import time

PAGE_MODE = 0x1A        # 固件模式标记：0 = 软件模式，1 = 固件模式
POLL = 2.0              # 轮询间隔（秒）
DRAIN_MAX = 8


class ModeGuard:
    """盯着配置页 0x1A，发现键盘进了固件模式就把它拉回软件模式。"""

    def __init__(self, hub):
        self.hub = hub
        self.enabled = True
        self.running = False
        self.rescued = 0          # 拉回次数
        self.samples = 0
        self.last_at = 0.0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread = None

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self) -> dict:
        if self.running:
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mode-guard",
                                        daemon=True)
        self._thread.start()
        return self.status()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def status(self) -> dict:
        return {"running": self.running, "enabled": self.enabled,
                "rescued": self.rescued, "samples": self.samples,
                "lastAt": round(self.last_at, 2), "error": self.last_error}

    # ── 内部 ────────────────────────────────────────────────────
    def _page_link(self):
        """能读写配置页的链路。有线与 2.4G 都行（2.4G 走 0x44 / 0x04，实测可用）。"""
        hub = self.hub
        with hub.lock:
            hub._connect()
            link = hub.link
            if getattr(link, "kind", "") not in ("wired", "wireless"):
                return None
            return link

    def _run(self):
        self.running = True
        try:
            while not self._stop.is_set():
                if self.enabled:
                    try:
                        self._tick()
                    except Exception as exc:                  # noqa: BLE001
                        self.last_error = f"{type(exc).__name__}: {exc}"
                self._stop.wait(POLL)
        finally:
            self.running = False

    def _tick(self):
        hub = self.hub
        link = self._page_link()
        if link is None:
            self.last_error = "没有可用的设备链路"
            return

        with hub.lock:
            page = link.read_page()
            self.samples += 1
            if len(page) < 128 or page[-2] != 0x5A or page[-1] != 0xA5:
                self.last_error = "配置页读回不完整"
                return
            flag = page[PAGE_MODE]
            if flag != 1:
                self.last_error = ""
                return

            # 进了固件模式：把标记清掉，其余字节一个不动
            fixed = bytearray(page)
            fixed[PAGE_MODE] = 0
            link.write_page(bytes(fixed))
            time.sleep(0.10)
            back = link.read_page()

        if back[PAGE_MODE] == 0:
            self.rescued += 1
            self.last_at = time.time()
            self.last_error = ""
            # 让上层下一轮重新读：把缓存时间戳清掉，refresh() 就会走完整读页。
            hub.snap.last_ok = 0.0
            hub.last_full = 0.0
        else:
            self.last_error = "写回 0x1A 没被接受"


def instance(hub=None):
    """模块级单例，方便 Api 取状态。"""
    global _single
    if _single is None and hub is not None:
        _single = ModeGuard(hub)
    return _single


_single = None
