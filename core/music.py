# -*- coding: utf-8 -*-
"""软件侧音乐律动（music mode）。

原理：原版是键盘固件自己做的（驱动里 CMusicDlg + FFT::Compute，走 LightBarMusicCmd /
SendMusicData 一类私有命令）。那套命令没有公开协议，2.4G 更是没有映射。这里换一条
已验证的路：用 WASAPI loopback 采系统声音 → FFT → 按频谱写 0x06 RGB 表。

实测吞吐：`_set_feature` 之间各留 2ms/4ms 时 146 fps，留 5ms/10ms 时 63 fps。
所以 20~30 fps 的音乐律动完全跑得动，每帧一次全表写入。

灯效形态（对应原版 tc_music_mode1..10 的十种气质）：
  soft   音频轻舞-轻柔  低频少、变化慢
  rock   光彩夺目-摇滚  高频强、跳变快
  normal 云起雪飞-常规  均衡
  vocal  光场变化-人声  中频突出
  ...
本实现给 5 种可感知的形态（柔和/摇滚/均衡/人声/激情），足以覆盖听感差异。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .aula import RGB_SLOTS, AulaError, Hub

# 音乐律动写入时用的宽松间隔（实测 63~146 fps）
FAST_PRE = 0.002
FAST_POST = 0.004

# 音量→亮度 的定标。真机实测：loopback 采到的 rms 在安静时 ~0，正常音乐
# 0.02~0.06，很响 0.12~0.15。所以 0.003 以下当静音，0.09 跨到满亮。
RMS_FLOOR = 0.003
RMS_SPAN = 0.090

# 十种风格，名字照抄原版 tc_music_mode1..10（"音频轻舞-轻柔" 这种）。
# 每个都是真正不同的一组参数，不是换个名字：
#   权重  = 低/中/高频对亮度的贡献比例（决定哪种声音点得亮）
#   平滑  = 时间平滑系数，越小越闪、越大越稳
#   gain  = 亮度增益
#   fps   = 该风格默认刷新率（越快越跟手，也越吃 USB 带宽）
PRESETS = [
    # key              label                    低    中    高   平滑   gain  fps
    ("light_dance",   "音频轻舞 · 轻柔",        (1.20, 0.85, 0.40, 0.78), 0.78, 16),
    ("dazzle",        "光彩夺目 · 摇滚",        (0.90, 1.00, 1.50, 0.18), 1.05, 32),
    ("cloud_snow",    "云起雪飞 · 常规",        (1.00, 1.00, 1.00, 0.45), 0.92, 22),
    ("light_field",   "光场变化 · 人声",        (0.50, 1.60, 0.55, 0.35), 0.95, 24),
    ("stream",        "溪水潺潺 · 常规",        (1.10, 0.90, 0.60, 0.66), 0.82, 14),
    ("bloom",         "百花争艳 · 激情",        (1.10, 1.10, 1.20, 0.22), 1.10, 30),
    ("pearls",        "珠落玉盘 · 摇滚",        (0.70, 1.10, 1.60, 0.10), 1.00, 36),
    ("moon",          "彩云逐月 · 激情",        (1.35, 1.00, 0.80, 0.16), 1.05, 28),
    ("mountain",      "高山流水 · 激情",        (1.25, 1.05, 0.95, 0.30), 1.00, 26),
    ("rain_silk",     "飘雨如丝 · 常规",        (0.85, 1.15, 0.75, 0.55), 0.88, 20),
]

PRESET_BY_KEY = {p[0]: p for p in PRESETS}
DEFAULT_PRESET = "cloud_snow"

# 兼容旧名字：之前只有 5 个 mood key
MOOD_ALIAS = {
    "soft": "light_dance",
    "rock": "dazzle",
    "normal": "cloud_snow",
    "vocal": "light_field",
    "drive": "moon",
}


def preset_label(key: str) -> str:
    row = PRESET_BY_KEY.get(key)
    return row[1] if row else key


def _fast_write(link, code: int, param: int, data: bytes, pre=FAST_PRE, post=FAST_POST):
    """与 WiredLink.write 同格式，但把固定 65ms 的 sleep 收到几毫秒。"""
    from .aula import HEADER, REPORT_LEN

    total = max(1, (len(data) + 511) // 512)
    for i in range(total):
        chunk = data[i * 512:(i + 1) * 512]
        buf = bytearray(REPORT_LEN)
        buf[0] = link.report_id
        buf[1] = code
        buf[2] = param
        buf[4] = total
        buf[5] = i
        buf[6] = len(chunk) & 0xFF
        buf[7] = (len(chunk) >> 8) & 0xFF
        buf[HEADER:HEADER + len(chunk)] = chunk
        if pre:
            time.sleep(pre)
        link._set_feature(buf)
        if post:
            time.sleep(post)


class MusicEngine:
    """系统声音 → 键盘逐键颜色。

    仅在「有线 + 自定义灯效」下有意义：单键色只在自定义模式显示。
    """

    def __init__(self, hub: Hub):
        self.hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.mood = DEFAULT_PRESET
        self.fps = 22
        self.gain = 1.0
        self._err: str | None = None
        self._frames = 0
        self._peak = 0.0
        self._level_last = 0.0
        # 每帧要写回的颜色缓存，stop 时用来还原
        self._orig: list[tuple[int, int, int]] | None = None
        # 开始前的灯效，stop 时还原（用户可能原本停在「梦幻彩虹」）
        self._orig_effect: int | None = None

    # ---------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.running,
            "mood": self.mood,
            "moodLabel": preset_label(self.mood),
            "fps": self.fps,
            "gain": round(self.gain, 2),
            "frames": self._frames,
            "peak": round(self._peak, 3),
            "error": self._err,
            "moods": [{"key": p[0], "label": p[1]} for p in PRESETS],
        }

    # ---------------------------------------------------------------- 控制
    def start(self) -> dict:
        if self.running:
            return self.status()
        snap = self.hub.refresh(force=True)
        if snap.mode != "wired":
            raise AulaError("音乐律动要插数据线：2.4G 通道没有单键颜色命令")
        self._err = None
        self._frames = 0
        self._stop.clear()
        self._orig = self.hub.read_rgb(save=True)
        # 单键颜色只在「自定义」灯效下显示。用户可能正停在「梦幻彩虹」这类
        # 内置灯效上，此时写 RGB 表设备照收但屏幕毫无变化 —— 必须先切过去，
        # 并把原灯效记下来，停止时还原，否则等于偷偷改了用户的灯效设置。
        self._orig_effect = snap.effect
        if snap.effect != 19:
            try:
                self.hub.use_custom_rgb()
            except Exception as e:
                self._err = "切到自定义灯效失败：%s" % (e,)
        self._thread = threading.Thread(target=self._run, name="aula-music", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self, restore: bool = True) -> dict:
        was = self.running
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.5)
        self._thread = None
        if was and restore and self._orig is not None:
            try:
                with self.hub.lock:
                    link = self.hub._wired()
                    from .aula import rgb_to_planes
                    _fast_write(link, 0x06, 0, rgb_to_planes(self._orig), 0.004, 0.010)
            except Exception as e:            # 还原失败不要盖掉停止动作本身
                self._err = "还原原色失败：%s" % (e,)
        # 灯效也要还原：用户原本在「梦幻彩虹」，律动结束后不该被留在自定义
        if was and restore and self._orig_effect is not None and self._orig_effect != 19:
            try:
                self.hub.set_effect(self._orig_effect)
            except Exception as e:
                self._err = "还原灯效失败：%s" % (e,)
        return self.status()

    def set_mood(self, mood: str) -> dict:
        key = MOOD_ALIAS.get(mood, mood)
        row = PRESET_BY_KEY.get(key)
        if not row:
            raise AulaError("未知的律动风格：%s" % mood)
        self.mood = key
        # 换风格时把该风格的推荐增益/刷新率一起带过去（摇滚要快，溪水要慢）
        self.gain = row[3]
        self.fps = row[4]
        return self.status()

    # ---------------------------------------------------------------- 主循环
    def _run(self):
        try:
            self._loop()
        except Exception as e:               # 线程内异常要收进 status，不能静默死掉
            self._err = "%s: %s" % (type(e).__name__, e)
        finally:
            self._thread = None

    def _loop(self):
        import soundcard as sc

        order = self._slot_order()
        cnt = len(order)
        frame_dt = 1.0 / max(8, min(40, self.fps))
        n = 2048                                 # FFT 窗口（48k 下 ~43ms）

        spk = sc.default_speaker()
        mic = sc.get_microphone(spk.id, include_loopback=True)

        # 上一次的颜色用于时间平滑，避免频闪
        prev = np.zeros((cnt, 3), dtype=np.float32)
        # 每键在波形上的相位：从中间向两侧荡开
        phase = (np.arange(cnt) / max(1, cnt - 1)) * 2 * np.pi
        from .aula import rgb_to_planes

        with mic.recorder(samplerate=48000, channels=1, blocksize=1024) as rec:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                data = rec.record(numframes=n)
                mono = np.asarray(data, dtype=np.float32).reshape(-1)

                # ---- 音量（RMS）驱动整体亮度 ----
                rms = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
                level = (rms - RMS_FLOOR) / RMS_SPAN
                level = float(np.clip(level, 0.0, 1.0))

                # ---- 频谱形态驱动颜色与起伏 ----
                spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
                freqs = np.fft.rfftfreq(len(mono), 1 / 48000)
                low = float(spec[(freqs >= 30) & (freqs < 250)].mean() or 0.0)
                mid = float(spec[(freqs >= 250) & (freqs < 2000)].mean() or 0.0)
                high = float(spec[(freqs >= 2000) & (freqs < 12000)].mean() or 0.0)
                row = PRESET_BY_KEY.get(self.mood) or PRESET_BY_KEY[DEFAULT_PRESET]
                wl, wm, wh, smooth = row[2]
                g = self.gain

                self._peak = max(self._peak * 0.90, level)
                self._level_last = level

                # 光谱重心 → 色相偏移；高频越多越偏青紫
                tot = low * wl + mid * wm + high * wh
                if tot > 1e-6:
                    tilt = (high * wh - low * wl) / tot      # -1..1
                else:
                    tilt = 0.0
                base_hue = (265.0 + tilt * 95.0 + (time.time() * 26.0)) % 360.0
                hue_arr = (base_hue + phase * 22.0) % 360.0
                sat = float(np.clip(0.55 + abs(tilt) * 0.35, 0.3, 1.0))

                # 每键的起伏：低频强时整体一起亮，高频强时快速抖动
                wob_freq = 3.0 + high / max(1.0, low + mid + high) * 9.0
                wob = 0.62 + 0.38 * np.sin(phase * 1.2 - time.time() * wob_freq)
                val = np.clip(level * wob * g, 0.0, 1.0)

                rgb = _hsv_to_rgb_arr(hue_arr, sat, val)  # (cnt,3) 0..1

                alpha = 1.0 - smooth
                prev = prev * smooth + rgb * alpha
                frame = np.clip(prev * 255.0, 0, 255).astype(np.uint8)

                slots = [(0, 0, 0)] * RGB_SLOTS
                for i, slot in enumerate(order):
                    if 0 <= slot < RGB_SLOTS:
                        slots[slot] = (int(frame[i][0]), int(frame[i][1]), int(frame[i][2]))

                # 其它未映射的槽保持全黑，避免残留旧色
                with self.hub.lock:
                    link = self.hub._wired()
                    _fast_write(link, 0x06, 0, rgb_to_planes(slots))
                self._frames += 1

                # 补齐到目标帧间隔
                spend = time.perf_counter() - t0
                if spend < frame_dt:
                    self._stop.wait(frame_dt - spend)

    def _slot_order(self) -> list[int]:
        """返回要参与律动的槽号，按物理排列顺序（左到右，从底行往上）。

        只取真的有按键的槽（keymap.json 里的 99 个），避免浪费帧预算。
        """
        km = self.hub.get_keymap()["keys"]
        rows: dict[int, list[tuple[int, int]]] = {}
        for idx, g in km.items():
            rows.setdefault(int(g["y"]), []).append((int(g["x"]), int(idx)))
        order: list[int] = []
        for y in sorted(rows):
            for _, idx in sorted(rows[y]):
                order.append(idx)
        return order


def _hsv_to_rgb_arr(h, s, v):
    """向量化 HSV→RGB。

    h / s / v 都可以是标量或等长数组（这里 v 常是逐键起伏的数组，写成只支持标量
    会在 `rgb[msk] = c` 处报 shape mismatch）。输出 (N,3) float 0..1。
    """
    h = np.atleast_1d(np.asarray(h, dtype=np.float32)) % 360.0
    s = np.broadcast_to(np.asarray(s, dtype=np.float32), h.shape)
    v = np.broadcast_to(np.asarray(v, dtype=np.float32), h.shape)

    c = v * s
    hp = h / 60.0
    x = c * (1.0 - np.abs((hp % 2.0) - 1.0))
    z = np.zeros_like(c)
    m = v - c

    # HSV 六段：按顺序覆盖，用 prev 避免后段盖掉前段
    segs = (
        (hp < 1, c, x, z),
        (hp < 2, x, c, z),
        (hp < 3, z, c, x),
        (hp < 4, z, x, c),
        (hp < 5, x, z, c),
    )
    r = np.zeros_like(c)
    g = np.zeros_like(c)
    b = np.zeros_like(c)
    done = np.zeros(hp.shape, dtype=bool)
    for cond, rr, gg, bb in segs:
        msk = cond & ~done
        r = np.where(msk, rr, r)
        g = np.where(msk, gg, g)
        b = np.where(msk, bb, b)
        done |= cond
    # 剩下的是 5..6（洋红→红）
    msk = ~done
    r = np.where(msk, c, r)
    b = np.where(msk, x, b)

    return np.stack([r + m, g + m, b + m], axis=1)
