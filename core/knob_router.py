"""旋钮双向接管：把「顺时针 / 逆时针 / 按下」分别绑成三个不同动作。

为什么需要这一层
----------------
出厂状态下键盘固件已经把旋钮方向翻译好了（真机实测，2026-10-22）：

    顺时针  -> Consumer Volume Up   (VK 0xAF)
    逆时针  -> Consumer Volume Down (VK 0xAE)
    按下    -> Consumer Mute        (VK 0xAD)

所以方向信息本来就存在，只是全部指向音量。矩阵里 idx108 那条记录
（`07 00 00 1d`）只描述「旋钮整体是什么」，写它只能让三个方向共用一个
功能 —— 这就是之前「往哪扭都一样」的根因。

要做「顺时针加亮度 / 逆时针减亮度」，只能在软件层把这三个键接过来：
低级键盘钩子（WH_KEYBOARD_LL）返回 1 就能吞掉，实测音量全程不动
（439 次采样 0 次变动，钩子命中 40 条）。

不能用的两条路（都已实测排除，别再试）
------------------------------------
- `RIDEV_NOLEGACY` 注册 Consumer 页直接返回 0，微软文档也写明它只对
  鼠标/键盘页有效。
- `keybd_event` 注入的按键改不了系统音量，没法拿来做基线对照。

设计要点
--------
1. 钩子回调里**只入队、不做 I/O**。写一次 HID 要 80ms，在钩子回调里做会
   拖慢整个系统的按键响应（超过 LowLevelHooksTimeout 钩子会被摘掉）。
2. 快速旋转时事件很密（实测 25 次/秒），工作线程按「合并增量」处理：
   攒够一段时间才真正写一次设备，中间只累加 +/-。
3. **音量优先放行给 Windows**。系统 OSD 只认真实硬件按键：程序化改音量
   （WASAPI / SendInput / keybd_event / 扫描码 / WM_APPCOMMAND）实测全都
   改得动音量但**一律不弹 OSD**（见 `tmp/osd_which.py` 穷举）。所以只要
   这一格绑的动作正好等于该键的原生含义（顺时针=音量+、逆时针=音量−、
   按下=静音），就直接 `CallNextHookEx` 放行，让 Windows 自己画它原生的
   那根条 —— 那才是最像系统、也是用户要的「以系统那个为优先」。
4. 只有「放行了方向就不对」或「系统压根没有这个功能」（键盘背光 / 屏幕
   亮度 / 灯效 / 速度）才吞下来自己处理：
   - 音量走 `core.sysvol`（WASAPI `IAudioEndpointVolume`）。**不能用
     `waveOutSetVolume`** —— 那是个死接口，实测调用后音量纹丝不动。
   - 自绘的条走 `core.osd`（Win11 胶囊形态复刻）。
5. 绑定项为 `system` 时不吞事件，原厂行为照旧（调音量 + 系统原生 OSD）。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import time
from ctypes import wintypes as wt

from .aula import (BRIGHT_MAX, EFFECTS, SOFT_BY_VK, SPEED_MAX,
                   AulaError)

try:                     # OSD 依赖 PIL，缺了也不该让旋钮瘫掉
    from . import osd as _osd
except Exception:        # noqa: BLE001
    _osd = None

try:
    from . import sysvol as _sysvol
except Exception:        # noqa: BLE001
    _sysvol = None

# ── 绑定动作 ──────────────────────────────────────────────────────────
# system      不拦截，交给 Windows（出厂行为：调音量 / 静音）—— **会弹系统原生 OSD**
# bright_up / bright_down   键盘背光亮度 ±1 档（自绘 OSD）
# speed_up  / speed_down    灯效速度 ±1 档（自绘 OSD）
# effect_next / effect_prev 换下一个 / 上一个灯效（自绘 OSD）
# vol_up    / vol_down      系统音量 ±2%（吞键时走 WASAPI，自绘 OSD）
# mute_toggle               系统静音开关
# screen_up / screen_down   屏幕亮度 ±5%（内置屏，自绘 OSD）
# custom_rgb                切到自定义灯效（单键颜色才显示）
# none                      吞掉，什么都不做
ACTIONS = [
    ("system", "系统默认（音量 / 静音，弹系统原生条）"),
    ("bright_up", "键盘亮度 +"),
    ("bright_down", "键盘亮度 −"),
    ("speed_up", "灯效速度 +"),
    ("speed_down", "灯效速度 −"),
    ("effect_next", "下一个灯效"),
    ("effect_prev", "上一个灯效"),
    ("vol_up", "系统音量 +"),
    ("vol_down", "系统音量 −"),
    ("mute_toggle", "系统静音开关"),
    ("screen_up", "屏幕亮度 +"),
    ("screen_down", "屏幕亮度 −"),
    ("custom_rgb", "切到自定义灯效"),
    ("music_toggle", "音乐律动 开 / 关"),
    ("none", "什么都不做"),
]
ACTION_MAP = dict(ACTIONS)

# ── 旋钮槽位 ──────────────────────────────────────────────────────────
# 长按旋钮在这几个槽之间轮回切换。每个槽自带一套 cw/ccw/press 绑定，
# 所以「顺时针调屏幕亮度、逆时针调键盘亮度」这种混搭也成立。
# **切换只改软件状态，不写设备一个字节**（键盘固件那套 V/L 闪灯方案
# 用户已经废掉了，见 m258477）。
DEFAULT_SLOTS = [
    {"id": "sound", "label": "声音",
     "cw": "system", "ccw": "system", "press": "system",
     "hint": "旋钮 = 系统音量，放行真键，弹 Windows 原生音量条"},
    {"id": "bright", "label": "屏幕亮度",
     "cw": "screen_up", "ccw": "screen_down", "press": "system",
     "hint": "旋钮 = 屏幕亮度（笔记本内置屏），弹本软件的亮度条"},
    {"id": "kblight", "label": "键盘亮度",
     "cw": "bright_up", "ccw": "bright_down", "press": "system",
     "hint": "旋钮 = 键盘背光亮度，弹本软件的亮度条"},
    {"id": "effect", "label": "灯效",
     "cw": "effect_next", "ccw": "effect_prev", "press": "system",
     "hint": "旋钮 = 换下一个 / 上一个灯效"},
]
SLOT_BIND_KEYS = ("cw", "ccw", "press")
SLOT_MAX = 12

# 全局热键：Ctrl+Alt+K 切到下一个槽
HOTKEY_ID = 0xA0B0
HOTKEY_MOD = 0x0003          # MOD_ALT | MOD_CONTROL
HOTKEY_VK = 0x4B             # 'K'
WM_HOTKEY = 0x0312

DEFAULTS = {
    "cw": "system",       # 顺时针
    "ccw": "system",      # 逆时针
    "press": "system",    # 按下
    "step": 1,            # 亮度/速度每次跳几档
    "enabled": False,     # 是否接管；关掉时完全不加钩子
}


VK_VOL_DOWN, VK_VOL_UP, VK_VOL_MUTE = 0xAE, 0xAF, 0xAD
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
TRACK_TTL = 3.0        # 追踪值的保鲜期（秒），超过就重新读设备
QUIET = 0.16           # 转动手停下来多久算「这一格转完了」
MAX_WAIT = 0.55        # 一直不停手也最多攒这么久
MIN_GAP = 0.05         # 两次写入之间的最小间隔

# 每个旋钮键「原生就是这么用」时对应的动作。
# 命中它就别吞键、直接放行 —— 只有让真实硬件事件穿过钩子到达 Windows，
# 系统才会画原生 OSD（程序化改音量一律不弹，已穷举实测）。
NATIVE_ACTION = {
    VK_VOL_UP: "vol_up",
    VK_VOL_DOWN: "vol_down",
    VK_VOL_MUTE: "mute_toggle",
}

_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AULA-Hub")
_PATH = os.path.join(_DIR, "knob-bind.json")


_SLOT_PATH = os.path.join(_DIR, "knob-slots.json")


def _norm_slot(raw, idx: int) -> dict:
    """把一条槽位数据洗成合法结构。"""
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    sid = str(raw.get("id") or f"slot{idx + 1}")[:32]
    out["id"] = sid
    label = str(raw.get("label") or f"槽位 {idx + 1}")[:24]
    out["label"] = label
    for k in SLOT_BIND_KEYS:
        v = raw.get(k, "system")
        out[k] = v if v in ACTION_MAP else "system"
    out["hint"] = str(raw.get("hint") or "")[:120]
    return out


def load_slots() -> dict:
    """读槽位表。文件不在 / 坏了就用出厂四槽。"""
    data = None
    try:
        with open(_SLOT_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    slots = []
    if isinstance(data, dict) and isinstance(data.get("slots"), list):
        for i, raw in enumerate(data["slots"][:SLOT_MAX]):
            slots.append(_norm_slot(raw, i))
    if not slots:
        slots = [_norm_slot(dict(x), i) for i, x in enumerate(DEFAULT_SLOTS)]
    active = 0
    if isinstance(data, dict):
        try:
            active = int(data.get("active", 0))
        except (TypeError, ValueError):
            active = 0
    active = max(0, min(len(slots) - 1, active))
    return {"active": active, "slots": slots}


def save_slots(data: dict) -> dict:
    os.makedirs(_DIR, exist_ok=True)
    payload = {
        "active": int(data.get("active", 0)),
        "slots": [
            {k: v for k, v in _norm_slot(raw, i).items() if k != "hint"}
            for i, raw in enumerate(data.get("slots", [])[:SLOT_MAX])
        ],
    }
    tmp = _SLOT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _SLOT_PATH)
    return load_slots()


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data.update(json.load(fh))
    except (OSError, ValueError):
        pass
    for k in ("cw", "ccw", "press"):
        if data.get(k) not in ACTION_MAP:
            data[k] = "system"
    try:
        data["step"] = max(1, min(4, int(data.get("step", 1))))
    except (TypeError, ValueError):
        data["step"] = 1
    data["enabled"] = bool(data.get("enabled"))
    sl = load_slots()
    data["slots"] = sl["slots"]
    data["active"] = sl["active"]
    cur = sl["slots"][sl["active"]] if sl["slots"] else None
    if cur:
        # 绑定以**当前槽**为准：界面上的下拉框改的就是它
        for k in SLOT_BIND_KEYS:
            data[k] = cur[k]
        data["slotLabel"] = cur["label"]
        data["slotHint"] = cur.get("hint", "")
    data["hotkey"] = "Ctrl+Alt+K"
    data["path"] = _PATH
    data["actions"] = [{"key": k, "label": lbl} for k, lbl in ACTIONS]
    return data


def save(patch: dict) -> dict:
    """保存。

    `cw/ccw/press` → 写进**当前槽**（下拉框改的就是当前槽的绑定）。
    `active` / `slot` → 切槽。
    `enabled` / `step` → knob-bind.json。
    `slots` → 整表替换（增删改槽位走这条）。
    """
    sl = load_slots()
    slots = sl["slots"]
    active = sl["active"]

    if isinstance(patch.get("slots"), list):
        slots = [_norm_slot(x, i) for i, x in enumerate(patch["slots"][:SLOT_MAX])]
        if not slots:
            slots = [_norm_slot(dict(x), i) for i, x in enumerate(DEFAULT_SLOTS)]
        active = min(active, len(slots) - 1)

    if "slot" in patch:
        want = patch["slot"]
        for i, one in enumerate(slots):
            if one["id"] == want:
                active = i
                break
    if "active" in patch:
        try:
            active = max(0, min(len(slots) - 1, int(patch["active"])))
        except (TypeError, ValueError):
            pass

    # 绑定改动落到当前槽
    if slots:
        cur = slots[active]
        for k in SLOT_BIND_KEYS:
            if k in patch and patch[k] in ACTION_MAP:
                cur[k] = patch[k]

    save_slots({"active": active, "slots": slots})

    # enabled / step 还在 knob-bind.json
    data = {"enabled": False, "step": 1}
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            old = json.load(fh)
        data["enabled"] = bool(old.get("enabled"))
        data["step"] = max(1, min(4, int(old.get("step", 1))))
    except (OSError, ValueError, TypeError):
        pass
    if "enabled" in patch:
        data["enabled"] = bool(patch["enabled"])
    if "step" in patch:
        try:
            data["step"] = max(1, min(4, int(patch["step"])))
        except (TypeError, ValueError):
            pass
    os.makedirs(_DIR, exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)
    return load()


# ── 系统音量 ─────────────────────────────────────────────────────────
# 走 `core.sysvol` 的 WASAPI 实现。**别用 waveOutGetVolume /
# waveOutSetVolume**：那是 winmm 的遗留接口，实测调用返回成功、音量却纹丝
# 不动（`tmp/vol_probe.py` 逐条验证过），当初就是它导致「旋钮转了没反应」。

def _vol_get() -> int:
    if _sysvol is None:
        return -1
    return _sysvol.volume_get()


def _vol_set(pct: int) -> int:
    if _sysvol is None:
        return -1
    return _sysvol.volume_set(max(0, min(100, int(pct))))


def _mute_get() -> bool:
    if _sysvol is None:
        return False
    return _sysvol.mute_get()


def _mute_set(on: bool) -> bool:
    if _sysvol is None:
        return False
    return _sysvol.mute_set(bool(on))


class KnobRouter:
    """装钩子、吞旋钮那三个键，按绑定拨到不同动作。

    用法：
        router = KnobRouter(hub)
        router.start()      # 读绑定，enabled 才真正挂钩子
        router.stop()
    """

    def __init__(self, hub):
        self.hub = hub
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._hook_thread = None
        self._work_thread = None
        self._hook = None
        self._hookproc = None
        self._bind = load()
        self._lock = threading.Lock()
        self.hits = 0            # 累计接管的次数（给 UI 看）
        self.last_action = ""    # 最近一次实际执行的动作
        self.last_error = ""
        self.installed = False
        self._bright = None          # 亮度追踪值（避免 refresh() 的 10s 缓存）
        self._speed = None
        self._tracked_at = None
        self._last_write = 0.0
        self._effect = None
        self._effect_at = None
        self.writes = 0              # 实际写了多少次设备（给 UI 看闪烁情况）
        self.soft_hits = 0           # 软件动作键按了多少次
        self.music_toggle = None     # 由 app.py 注入：开/关音乐律动
        self.osd_enabled = True      # 自绘提示条总开关（见 core/osd.py）
        self.raw_hits = 0            # 回调进来的原始键盘事件数（诊断用）
        self.hook_errors = 0         # 回调异常次数（诊断用）
        self.mode_hits = 0           # 热键切换了几次
        self._hotkey_ok = False      # 全局热键有没有注册上

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._hook_thread is not None and self._hook_thread.is_alive()

    def start(self) -> dict:
        if self.running:
            return self.status()
        self._bind = load()
        # 注意：这里**不能**因为 enabled=False 就提前返回。
        # 软件动作键（矩阵里的 F13..F20）同样要经过这个钩子，
        # 关掉「接管旋钮」只应该放开那三个音量键，不该让软件键一起哑掉。
        self._stop.clear()
        self._work_thread = threading.Thread(target=self._work, daemon=True,
                                             name="knob-work")
        self._work_thread.start()
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True,
                                             name="knob-hook")
        self._hook_thread.start()
        # 等钩子装好（最多 1 秒），好把失败原因回给界面
        for _ in range(100):
            if self.installed or not self._hook_thread.is_alive():
                break
            time.sleep(0.01)
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        self._q.put("__quit__")
        t = self._hook_thread
        if t is not None and t.is_alive():
            # 钩子必须在装它的那个线程里卸，靠 WM_QUIT 让消息循环退出
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    t.ident, 0x0012, 0, 0)   # WM_QUIT
            except Exception:
                pass
            t.join(timeout=1.5)
        self._hook_thread = None
        w = self._work_thread
        if w is not None and w.is_alive():
            w.join(timeout=1.5)
        self._work_thread = None
        self.installed = False
        return self.status()

    def reload(self, patch: dict = None) -> dict:
        """改绑定。运行中改即时生效，不用重启钩子。"""
        if patch:
            self._bind = save(patch)
        else:
            self._bind = load()
        if not self.running:
            self.start()
        return self.status()

    # ---------- 槽位 ----------
    @property
    def slots(self) -> list:
        return self._bind.get("slots") or []

    @property
    def active(self) -> int:
        try:
            return int(self._bind.get("active", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def slot_label(self) -> str:
        return self._bind.get("slotLabel") or ""

    def _ensure_takeover(self):
        """切到「不是纯音量」的槽时自动打开接管。

        钩子在 enabled=False 时一律放行，旋钮根本到不了软件层 ——
        不自动开的话，用户切完槽会发现「转了没反应」。
        """
        if self._bind.get("enabled"):
            return
        sl = self.slots
        cur = sl[self.active] if sl else {}
        if cur.get("cw") == "system" and cur.get("ccw") == "system":
            return                       # 纯声音槽，接不接管都一样
        self._bind = save({"enabled": True})

    def set_slot(self, which, persist: bool = True) -> dict:
        """切到指定槽。which 可以是序号，也可以是槽 id。"""
        if persist:
            self._bind = save({"slot": which} if isinstance(which, str)
                              else {"active": which})
        else:
            sl = list(self.slots)
            if isinstance(which, str):
                for i, one in enumerate(sl):
                    if one["id"] == which:
                        self._bind["active"] = i
                        break
            else:
                self._bind["active"] = max(0, min(len(sl) - 1, int(which)))
        cur = None
        sl = self.slots
        if sl:
            cur = sl[self.active]
            for k in SLOT_BIND_KEYS:
                self._bind[k] = cur[k]
            self._bind["slotLabel"] = cur["label"]
        self._ensure_takeover()
        if not self.running:
            self.start()
        # 切槽只改软件状态：不写设备、不动灯
        self.last_action = f"槽位 → {(cur or {}).get('label', '')}"
        self.last_error = ""
        try:
            if _osd is not None and self.osd_enabled:
                _osd.show("mode_" + ("sound" if (cur or {}).get("cw") == "system"
                                     else "bright"), -1, 0)
        except Exception:                                  # noqa: BLE001
            pass
        return self.status()

    def cycle_mode(self) -> dict:
        """长按旋钮 / 热键走这条：跳到下一个槽，末尾回到第一个。"""
        self.mode_hits += 1
        sl = self.slots
        if not sl:
            return self.status()
        return self.set_slot((self.active + 1) % len(sl))

    def status(self) -> dict:
        b = dict(self._bind)
        b.pop("actions", None)
        return {
            "running": self.running,
            "installed": self.installed,
            "hits": self.hits,
            "writes": self.writes,
            "softHits": self.soft_hits,
            "rawHits": self.raw_hits,
            "hookErrors": self.hook_errors,
            "lastAction": self.last_action,
            "error": self.last_error,
            "binding": b,
            "actions": [{"key": k, "label": lbl} for k, lbl in ACTIONS],
            "active": self.active,
            "slotLabel": self.slot_label,
            "slotHint": self._bind.get("slotHint", ""),
            "slots": self.slots,
            "modeHits": self.mode_hits,
            "hotkey": "Ctrl+Alt+K",
            "hotkeyOk": self._hotkey_ok,
            "bindKeys": list(SLOT_BIND_KEYS),
            "brightMax": BRIGHT_MAX,
            "speedMax": SPEED_MAX,
            # 屏幕亮度走 WMI，只对笔记本内置屏有效；外接屏的 DDC/CI 在这台机器
            # 上 GetMonitorCapabilities 返回 caps=0（实测），改不动。
            "screenAvailable": self.screen_available(),
        }

    @staticmethod
    def screen_available() -> bool:
        try:
            return bool(_sysvol.screen_available())
        except Exception:                     # noqa: BLE001
            return False

    # ---------- 钩子线程 ----------
    def _hook_loop(self):
        user32 = ctypes.windll.user32

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      wt.WPARAM, wt.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                        ("flags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        user32.SetWindowsHookExW.restype = wt.HANDLE
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             wt.HINSTANCE, wt.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]

        knob_vk = {VK_VOL_UP: "cw", VK_VOL_DOWN: "ccw", VK_VOL_MUTE: "press"}
        pressed = set()

        # 全局热键 Ctrl+Alt+K：不用开窗口就能切模式
        try:
            user32.RegisterHotKey.restype = wt.BOOL
            user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int,
                                              wt.UINT, wt.UINT]
            user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
            self._hotkey_ok = bool(user32.RegisterHotKey(
                None, HOTKEY_ID, HOTKEY_MOD, HOTKEY_VK))
        except Exception:                                  # noqa: BLE001
            self._hotkey_ok = False

        def proc(code, wparam, lparam):
            # 外层兜底：低级钩子回调里抛异常，ctypes 会静默返回 0，
            # 表现为「钩子装了但什么都不生效」，极难排查。这里把异常记下来。
            try:
                return _proc_inner(code, wparam, lparam)
            except BaseException as exc:          # noqa: BLE001
                self.hook_errors += 1
                if self.hook_errors <= 5:
                    self.last_error = f"钩子回调异常: {type(exc).__name__}: {exc}"
                return user32.CallNextHookEx(None, code, wparam, lparam)

        def _proc_inner(code, wparam, lparam):
            if code == 0:
                kb = KBDLLHOOKSTRUCT.from_buffer_copy(
                    ctypes.string_at(lparam, ctypes.sizeof(KBDLLHOOKSTRUCT)))
                self.raw_hits += 1
                down = int(wparam) in (WM_KEYDOWN, WM_SYSKEYDOWN)

                # ── 软件动作键：物理键被改成 F13..F20，只有本软件认得 ──
                soft = SOFT_BY_VK.get(kb.vkCode)
                if soft is not None:
                    if not down:
                        pressed.discard(kb.vkCode)
                        return 1                       # 抬起也吞，成对
                    if kb.vkCode in pressed:            # 按住不放的自动重复
                        return 1
                    pressed.add(kb.vkCode)
                    self.soft_hits += 1
                    self._q.put(soft[1])
                    return 1

                # ── 旋钮三键 ──
                slot = knob_vk.get(kb.vkCode)
                if slot is not None:
                    act = self._bind.get(slot, "system")
                    # 放行条件：没开接管 / 绑成 system / 绑的正好是这个键
                    # 的原生含义。最后一条是关键 —— 放行真键才能让 Windows
                    # 画原生 OSD，这是唯一能唤出系统 OSD 的路子。
                    if (not self._bind.get("enabled")
                            or act == "system"
                            or act == NATIVE_ACTION.get(kb.vkCode)):
                        if not down:
                            pressed.discard(kb.vkCode)
                        return user32.CallNextHookEx(None, code, wparam, lparam)
                    if down:
                        if kb.vkCode in pressed:
                            return 1
                        pressed.add(kb.vkCode)
                        self.hits += 1
                        self._q.put(act)
                        return 1                       # 吞掉
                    pressed.discard(kb.vkCode)
                    return 1                           # 抬起也吞，成对
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._hookproc = HOOKPROC(proc)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hookproc, None, 0)
        if not self._hook:
            self.last_error = f"钩子安装失败 (err={ctypes.get_last_error()})"
            self.installed = False
            return
        self.installed = True
        self.last_error = ""

        # 低级钩子要求所在线程有消息循环
        msg = wt.MSG()
        while not self._stop.is_set():
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if got:
                if msg.message == 0x0012:      # WM_QUIT
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self.cycle_mode()
                    except Exception as exc:               # noqa: BLE001
                        self.last_error = f"切模式失败: {type(exc).__name__}: {exc}"
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.004)

        try:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        self._hotkey_ok = False
        try:
            user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        self.installed = False

    # ---------- 工作线程：合并 + 执行 ----------
    def _work(self):
        """把旋钮事件攒起来，**停下之后只写一次**。

        为什么不是「每 120ms 写一次」：一次配置页写入要 79 ms，而且固件收到
        0x04 会整块重载灯效 —— 表现出来就是灯先黑一下再按新值亮起。原驱动
        也是这个毛病（用户实测确认：「先黑再应用」）。快速转旋钮时如果每次
        都写，就会变成每秒闪八九次。

        所以改成「尾部去抖」：
          - 有事件就一直攒着**不写**，队列空了再写一次；
          - 慢慢转（每次间隔 > QUIET）时，每格都有一次写入，看得见变化；
          - 快速转时中间值全部跳过，只落最后一格，不会闪。
        """
        pending = {}
        last_event = first_event = 0.0

        while not self._stop.is_set():
            try:
                act = self._q.get(timeout=0.05)
            except queue.Empty:
                act = None
            now = time.time()

            if act is not None:
                if act == "__quit__":
                    break
                if not pending:
                    first_event = now
                pending[act] = pending.get(act, 0) + 1
                last_event = now
                continue

            if not pending:
                continue

            quiet = now - last_event
            waited = now - first_event
            # 停手了，或者攒太久（一直转不停也不能永远不落笔）
            if quiet < QUIET and waited < MAX_WAIT:
                continue
            if now - self._last_write < MIN_GAP:
                continue

            for name, n in list(pending.items()):
                try:
                    self._exec(name, n)
                except AulaError as exc:
                    self.last_error = str(exc)
                except Exception as exc:                  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
            self._last_write = time.time()
            self.writes += 1
            pending.clear()

    # ---------- 具体动作 ----------
    def _exec(self, act: str, times: int = 1):
        hub = self.hub
        step = int(self._bind.get("step", 1)) * times
        self.last_action = act
        # 每次动作前清掉上次的错误：否则曾经失败过一次（哪怕早就修好了），
        # 界面上那句"接管没生效：…"会一直挂着，看着像坏了。
        # 这次要是又失败，下面的 except 会重新写进去。
        self.last_error = ""

        if act == "none":
            return

        if act == "bright_up":
            self._nudge_bright(+step)
        elif act == "bright_down":
            self._nudge_bright(-step)
        elif act == "speed_up":
            self._nudge_speed(+step)
        elif act == "speed_down":
            self._nudge_speed(-step)
        elif act == "effect_next":
            self._cycle_effect(+times)
        elif act == "effect_prev":
            self._cycle_effect(-times)
        elif act == "custom_rgb":
            hub.use_custom_rgb()
            self._osd_show("effect", 19, 19)
        elif act == "music_toggle":
            if callable(self.music_toggle):
                self.music_toggle()
        elif act == "vol_up":
            v = _vol_get()
            if v >= 0:
                nv = _vol_set(v + 2 * times)
                self._osd_show("volume", nv if nv >= 0 else v + 2 * times, 100)
        elif act == "vol_down":
            v = _vol_get()
            if v >= 0:
                nv = _vol_set(v - 2 * times)
                self._osd_show("volume", nv if nv >= 0 else v - 2 * times, 100)
        elif act == "mute_toggle":
            on = _mute_set(not _mute_get())
            v = _vol_get()
            self._osd_show("mute" if on else "volume",
                           v if v >= 0 else 0, 100)
        elif act == "screen_up":
            self._nudge_screen(+times)
        elif act == "screen_down":
            self._nudge_screen(-times)
        elif act == "key_light_up":
            self._nudge_backlight(+times)
        elif act == "key_light_down":
            self._nudge_backlight(-times)

    # ---------- OSD ----------
    def _osd_show(self, kind: str, value: float, total: float):
        """自绘提示条。系统没有对应 OSD 的动作才走这里。"""
        if _osd is None or not self.osd_enabled:
            return
        try:
            _osd.show(kind, value, total)
        except Exception as exc:                     # noqa: BLE001
            self.last_error = f"OSD: {type(exc).__name__}: {exc}"

    def _nudge_screen(self, delta: int):
        """屏幕亮度（内置屏 WMI）。系统 OSD 不弹 —— WMI 改亮度实测无 OSD。

        注意：本方法跑在 router 的工作线程里，而 WMI/COM 接口指针有线程亲和性，
        所以 sysvol 内部是按线程各存一份连接（它以前用全局变量，工作线程里
        每次读都失败返回 -1，界面就报「这台机器不支持调屏幕亮度」）。
        """
        if _sysvol is None:
            return
        cur = _sysvol.screen_get()
        if cur < 0:
            # 真读不到才说调不了。外接屏、远程桌面、WMI 服务异常都会走到这里，
            # 笼统地说"不支持"会把排查方向带偏。
            self.last_error = "读不到屏幕亮度（只有笔记本内置屏支持调亮度）"
            return
        # 每格 5%，跟 Windows 自己的亮度键一致
        nv = _sysvol.screen_set(max(0, min(100, cur + delta * 5)))
        if nv < 0:
            self.last_error = "写屏幕亮度失败"
        self._osd_show("screen", nv if nv >= 0 else cur + delta * 5, 100)

    def _nudge_backlight(self, delta: int):
        """屏幕背光 / 键盘灯亮度走的都是这个（灯效亮度）。"""
        self._nudge_bright(delta)

    def _nudge_bright(self, delta: int):
        v = self._track(self._bright, delta, BRIGHT_MAX,
                        lambda x: self.hub.preview_brightness(x),
                        lambda: self.hub.refresh().brightness)
        self._bright = v
        if v is not None:
            self._osd_show("light", v, BRIGHT_MAX)

    def _nudge_speed(self, delta: int):
        v = self._track(self._speed, delta, SPEED_MAX,
                        lambda x: self.hub.preview_speed(x),
                        lambda: self.hub.refresh().speed)
        self._speed = v
        if v is not None:
            self._osd_show("speed", v, SPEED_MAX)

    def _track(self, cur, delta: int, top: int, write, read_dev):
        """线性累加，不要每次回读设备。

        踩过的坑：一开始每次 `hub.refresh().brightness` 拿当前值再 ±1，
        但 refresh() 有 10 秒缓存 —— 快速旋转时读到的是旧值，写进去的
        亮度就会乱跳（实测序列 0,2,3,8,9,6,5,0，完全不单调）。
        所以这里自己记一份追踪值：第一次用时从设备读一次，之后纯累加。
        超过 TRACK_TTL 没动就丢弃，下次重新读设备 —— 这样用户在界面上
        拖滑轨改了亮度，旋钮也不会拿着过期值乱跳。
        """
        now = time.time()
        stale = (self._tracked_at is None or now - self._tracked_at > TRACK_TTL)
        if cur is None or stale:
            try:
                cur = read_dev()
            except Exception:
                cur = 0
            cur = 0 if cur is None or cur < 0 else cur
        v = max(0, min(top, int(cur) + int(delta)))
        try:
            write(v)
            self._tracked_at = now
            return v
        except Exception:
            return None        # 写失败就丢追踪值，下次重读

    def _cycle_effect(self, delta: int):
        hub = self.hub
        nums = [ui for ui, _ in EFFECTS]
        # refresh() 有 10 秒缓存：连按「下一个灯效」时每次都会读到同一个旧值，
        # 看起来像只跳了一格。所以跟亮度一样自己记一份追踪值。
        now = time.time()
        if self._effect_at is None or now - self._effect_at > TRACK_TTL:
            cur = hub.refresh().effect or 1
        else:
            cur = self._effect
        if cur not in nums:
            cur = nums[0]
        nxt = nums[(nums.index(cur) + delta) % len(nums)]
        hub.set_effect(nxt)
        self._effect = nxt
        self._effect_at = time.time()
        self._osd_show("effect", nxt, len(nums))
