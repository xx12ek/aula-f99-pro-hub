"""长按旋钮 = 切换旋钮功能模式（声音 / 亮度）——全软件实现，不碰固件。

## 为什么不用碰固件

长按旋钮是固件内部状态机，原厂驱动也控不了它：
```
grep -aiE "wheelmode|showmodesw|showsideled|defsideled" Dev/kb/F99Pro/KB.ini   -> 全空
```
F99 Pro 连这个配置项都没有。但长按**会留下痕迹**，抓痕迹就够了。

## 判据的演进（两次推翻自己，别再走回头路）

### 第一版：抓 `06 0a 07 ...` 通知包 —— 否掉
`tmp/mode_notify2.txt`：我自己调 `set_brightness()` 之后设备也发
`06 0a 07 00 10 ...`，纯轮询也能收到 `06 0a 07 00 00 ...`。它其实是
「配置页被写过」的通告，任何写入都会触发，不能当长按指纹。

### 第二版：抓「灯效 + 亮度/速度一起变」—— 有致命 bug
记录下来的长按样本是 19↔2 切换，于是判据写成「effect 变了 且 亮度或速度
也变了 且 跨 19 边界」。

`tmp/knob_raw_watch.py` 的真机原始取证把这条打穿了：**升级后的固件长按是
把灯效写成 0，不是 2**。
```
[ 34.700s] 配置页变化 #2: 0x09:01->00, 0x0A:13->00
```
而旧 `_fingerprint()` 里有 `if eff < 1: return None` —— 灯效 0 被当成
「读不懂」，整条指纹被丢弃，长按永远检测不到。150 秒 370 次采样、0 命中，
就是这个原因，不是用户没按。

### 第三版（现在这版）：抓 `0x1A` —— 固件专属模式标记
同一次取证里还有一行关键证据：
```
[ 29.375s] 配置页变化 #1: 0x1A:00->01
```
`0x1A` 只在固件自己动作时才变。`tmp/byte1a_test.py` 做了对照：软件连改
`set_effect(3) / (7) / (12) / (1) / (19)`，`0x1A` 全程稳定 `00`，
整页还原后仍是 `00`。

所以判据变成两级：

1. **主判据**：`0x1A` 变了 → 一定是固件内部状态机动了（长按切模式，或
   短按切灯效/静音）。这是唯一不会被我们自己的写入干扰的信号。
2. **备判据**：灯效变了 **且** 亮度/速度也变 → 固件整页重载。
   `effect=0` 现在算合法值（不再丢弃），`19` 和 `0` 都视作「模式边界」。

两条命中任一条即算一次长按，但受下面两道过滤。

## 两道过滤

1. **自写排除**：`Hub.set_*()` 都会经过 `_patch` / `_apply_fast`，写成功后
   打 `hub._last_write_at`。`SELF_WRITE_GUARD` 秒内看到的变化一律判为自己
   造成的，直接跳过（`0x1A` 那条不受影响，因为软件改不动它，但留着更安全）。
2. **去抖**：`DEBOUNCE` 秒内只算一次。固件整页重载会连发好几条变化
   （取证里 34.7s 那一下就同时改了 `0x09` 和 `0x0A`），必须合并成一次。

## 附带发现的未启用线索

`0x38` 是**旋钮旋转计数器**，跟着转动单调走：
```
[ 38.185s] 0x38:80->81
[ 39.473s] 0x38:81->82
```
如果哪天发现无线模式下旋钮不发音量键（软件钩子收不到），可以直接读
`0x38` 的增量来判断「转了几格、哪个方向」，不必依赖键盘事件。暂不启用。

## 通知从哪来（实测 tmp/mode_dual.txt）

设备不主动推 —— 只在**被请求之后**把排队的通知塞进输入队列，而且会同时
塞给所有已打开的句柄。所以维持一个轻量轮询：读一次配置页（63 ms）拿到
指纹，顺手把输入队列读空（0x07 包只用来记日志，不参与判定）。
"""
from __future__ import annotations

import threading
import time

PAGE_CUSTOM = 0x09          # 自定义灯效标志（0/1）
PAGE_EFFECT = 0x0A          # 当前灯效编号（0 = 灯效关，19 = 自定义）
PAGE_MODE = 0x1A            # 固件专属模式标记：只有固件自己会改
PAGE_SPIN = 0x38            # 旋钮旋转计数器（未启用，留给无线兜底）
PAGE_TABLE = 0x3A           # 灯效槽表起点
EFFECT_STRIDE = 2
EFFECT_CUSTOM = 19          # 自定义灯效（单键颜色只在它下面显示）

POLL_SECONDS = 0.40         # 轮询间隔，也是长按检测的最坏延迟
SELF_WRITE_GUARD = 1.5      # 自己写完多久之内看到的变化不算长按
DEBOUNCE = 2.0              # 一次长按只算一次
DRAIN_MAX = 16

# 固件整页重载时会把灯效换成这两个之一，旧的样本是 19 <-> 2，
# 升级后的固件是 19 <-> 0，两个都认。
MODE_EDGES = (0, EFFECT_CUSTOM)


class KnobModeWatcher:
    """盯着配置页，抓到「固件内部动作」就调 `on_toggle()`。

    用法：
        w = KnobModeWatcher(hub, router.cycle_mode)
        w.start()          # 内部自己起线程
        ...
        w.stop()
    """

    def __init__(self, hub, on_toggle=None):
        self.hub = hub
        self.on_toggle = on_toggle
        self._stop = threading.Event()
        self._thread = None
        self.enabled = True          # 「长按切模式」总开关
        self.hits = 0                # 抓到几次长按
        self.last_at = 0.0
        self.last_error = ""
        self.samples = 0             # 轮询轮数（诊断）
        self.packets = 0             # 收到多少条 0x0A 通知（诊断）
        self.last_fp = None          # 上次指纹
        self.last_reason = ""        # 上次判定的理由（诊断）
        self.mode_raw = 0            # 上次读到的 0x1A 值（诊断）
        self.spin_raw = 0            # 上次读到的 0x38 值（诊断）

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> dict:
        if self.running:
            return self.status()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="knob-mode-watch", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    def status(self) -> dict:
        return {
            "running": self.running,
            "enabled": bool(self.enabled),
            "hits": self.hits,
            "samples": self.samples,
            "packets": self.packets,
            "lastAt": self.last_at,
            "reason": self.last_reason,
            "error": self.last_error,
            "modeRaw": self.mode_raw,
            "spinRaw": self.spin_raw,
        }

    # ---------- 内部 ----------
    def _dev(self):
        """当前链路，能做「读配置页」就返回它，否则 None。

        有线（wired）与 2.4G（wireless）都能读页 —— 2.4G 走 0x44，实测可用。
        只是无线没有 0xFF00 那个可读的通知通道，`_drain()` 拿不到包而已。
        """
        link = getattr(self.hub, "link", None)
        if link is None or getattr(link, "kind", "") not in ("wired", "wireless"):
            return None
        return link

    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as exc:                 # noqa: BLE001
                # 单次失败不能让线程死掉——静默退出是最难查的 bug
                self.last_error = f"{type(exc).__name__}: {exc}"
            rest = POLL_SECONDS - (time.time() - t0)
            if rest > 0:
                self._stop.wait(rest)

    def _tick(self):
        dev = self._dev()
        if dev is None:
            return
        link = self.hub.link
        lock = getattr(self.hub, "lock", None)

        def work():
            page = link.read_page()
            packets = self._drain(dev)
            return page, packets

        if lock is None:
            page, packets = work()
        else:
            with lock:
                page, packets = work()

        self.samples += 1
        self.packets += len(packets)

        fp = self._fingerprint(page)
        if fp is None:
            return
        self.mode_raw = fp[3]
        self.spin_raw = fp[4]

        prev, self.last_fp = self.last_fp, fp
        if prev is None or prev == fp:
            return

        hit, why = self._looks_like_flip(prev, fp)
        if not hit:
            self.last_reason = why
            return

        # 自写排除**只对备判据生效**。
        # 主判据是 0x1A（固件专属模式标记），软件正常工作流从不写它，
        # 所以只要它变了就一定是固件干的 —— 此时不能被「刚写过配置页」
        # 挡掉，否则「转一下旋钮再长按」就会漏（端到端自测抓到的隐患）。
        # 备判据（整页重载）跟手动写灯效长得很像，才需要这道过滤。
        if not why.startswith("长按（模式标记"):
            own = getattr(self.hub, "_last_write_at", 0.0)
            if time.time() - own < SELF_WRITE_GUARD:
                self.last_reason = "自写"
                return

        now = time.time()
        if now - self.last_at < DEBOUNCE:
            return
        self.last_at = now
        self.hits += 1
        self.last_reason = why
        if not self.enabled:
            return
        cb = self.on_toggle
        if callable(cb):
            try:
                cb()
            except Exception as exc:                 # noqa: BLE001
                self.last_error = f"切模式失败：{type(exc).__name__}: {exc}"

    @staticmethod
    def _looks_like_flip(prev, cur):
        """指纹跳变 → (是不是长按, 理由)。

        指纹 = (effect, brightness, speed, mode_flag, spin)

        主判据：`mode_flag`（配置页 0x1A）变了。这个字节只有固件自己会改，
        软件连改五次灯效它都纹丝不动（`tmp/byte1a.txt` 实测），所以一旦它
        变化，必然是固件内部状态机动了 —— 也就是长按。

        备判据：灯效变了 且 亮度或速度也变了 且 新旧灯效里有一个落在
        模式边界（0 或 19）。这是旧版判据，修掉了 `effect=0` 被丢弃的 bug
        之后仍然有效，作为 0x1A 不变化的老固件兜底。
        """
        if prev is None or cur is None:
            return False, "指纹不可用"
        eff_a, br_a, sp_a, m_a, _ = prev
        eff_b, br_b, sp_b, m_b, _ = cur

        if m_a != m_b:
            return True, f"长按（模式标记 {m_a}->{m_b}）"

        if eff_a == eff_b:
            return False, "指纹变了但不涉及模式标记和灯效"

        if br_a == br_b and sp_a == sp_b:
            return False, "只动灯效（手动切灯效）"

        if eff_a not in MODE_EDGES and eff_b not in MODE_EDGES:
            return False, f"灯效 {eff_a}->{eff_b} 不在模式边界"

        return True, f"长按（整页重载 {eff_a}->{eff_b}）"

    def _drain(self, link) -> list[bytes]:
        """把链路上排队的输入报告读空。

        只有有线链路（258a:010c 的 0xFF00 集合）能读到通知包；2.4G 的接收器
        不推这类包，`read()` 还会等超时，所以直接跳过 —— 2.4G 的判据完全靠
        配置页指纹（`0x1A` 与灯效/亮度/速度），不依赖通知包。
        """
        if getattr(link, "kind", "") != "wired":
            return []
        dev = getattr(link, "dev", None)
        if dev is None:
            return []
        out = []
        for _ in range(DRAIN_MAX):
            try:
                data = dev.read(64)
            except Exception:
                break
            if not data:
                break
            out.append(bytes(data))
        return out

    @staticmethod
    def _fingerprint(page: bytes):
        """(effect, brightness, speed, mode_flag, spin)。读不懂就返回 None。

        注意：`effect == 0` 是**合法值**（灯效关），不能再当成读失败丢掉 ——
        这正是上一版漏掉长按的原因。
        """
        if not page or len(page) <= PAGE_MODE:
            return None
        eff = page[PAGE_EFFECT]
        mode_flag = page[PAGE_MODE]
        spin = page[PAGE_SPIN] if len(page) > PAGE_SPIN else 0
        if eff < 1:
            # 灯效关：亮度/速度读不到实际值，用 -1 占位，仍然保留指纹
            return (eff, -1, -1, mode_flag, spin)
        slot = (eff - 1) * EFFECT_STRIDE + PAGE_TABLE
        if slot + 1 >= len(page):
            return (eff, -1, -1, mode_flag, spin)
        return (eff, page[slot], (page[slot + 1] >> 4) & 0x0F, mode_flag, spin)
