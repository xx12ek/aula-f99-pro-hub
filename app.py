"""AULA Hub — F99 Pro 现代化控制中心。

运行：python app.py
"""
import os
import ctypes
import sys
import threading
import time

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview  # noqa: E402
from core import chrome, settings  # noqa: E402
from core.aula import BACKUP_DIR, Hub  # noqa: E402
from core.music import MusicEngine  # noqa: E402
from core.knob_router import KnobRouter
from core.knob_mode import KnobModeWatcher  # noqa: E402
from core.mode_guard import ModeGuard  # noqa: E402
from core.tray import Tray  # noqa: E402

ICON = os.path.join(BASE, "assets", "icon.ico")
hub = Hub()
music = MusicEngine(hub)
router = KnobRouter(hub)
# 键盘固件有自己的模式（配置页 0x1A=1）：进去之后旋钮由固件直接调背光，
# 一个按键都不发给电脑，软件接管彻底失效。这个守护线程盯着那个字节。
mode_guard = ModeGuard(hub)
# 长按旋钮 = 切到下一个槽。判据是配置页 0x1A（固件专属模式标记），
# 详见 core/knob_mode.py 的头注释。
slot_watch = KnobModeWatcher(hub, on_toggle=router.cycle_mode)


def _toggle_music():
    """给旋钮 / 软件动作键用的「音乐律动 开 / 关」。

    这条会在 router 的工作线程里被调用，所以不能抛异常穿出去——
    那个线程死了整个接管就没反应了。律动本身要采音频、写 RGB，
    失败最多是没开起来，记在 last_error 里就行。
    """
    try:
        if music.running:
            music.stop(restore=True)
        else:
            music.start()
    except Exception as exc:                     # noqa: BLE001
        router.last_error = f"{type(exc).__name__}: {exc}"


router.music_toggle = _toggle_music
tray = Tray(on_show=lambda: _show_window(), on_quit=lambda: _quit())
_window = None
_really_quit = False


class Api:
    """暴露给前端的接口。"""

    def state(self, force=False):
        return hub.state(bool(force))

    def set_effect(self, ui):
        hub.set_effect(int(ui))
        return hub.state()

    def set_brightness(self, level):
        hub.set_brightness(int(level))
        return hub.state()

    # 拖动预览：不走「读页 + 写页 + 回读校验」（_patch 一次实测 253ms，
    # 拖动时每帧都走会把界面拖成幻灯片），只裸写一帧 78ms。
    # 返回值刻意不调 hub.state()，省掉一次读页。
    def preview_brightness(self, level):
        try:
            hub.preview_brightness(int(round(float(level))))
            return {"ok": True, "brightness": int(round(float(level)))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def preview_speed(self, level):
        try:
            hub.preview_speed(int(round(float(level))))
            return {"ok": True, "speed": int(round(float(level)))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_speed(self, level):
        hub.set_speed(int(level))
        return hub.state()

    def set_color(self, idx):
        hub.set_color(int(idx))
        return hub.state()

    def set_sleep(self, sec):
        hub.set_sleep(int(sec))
        return hub.state()

    # ---------- 改键 / 宏 ----------
    def get_bindings(self):
        return hub.get_bindings()

    def set_key(self, layer, key_index, action_json):
        import json as _json
        action = _json.loads(action_json) if isinstance(action_json, str) else action_json
        hub.set_key(int(layer), int(key_index), action)
        return {"ok": True}

    def get_macros(self):
        return hub.get_macros()

    def save_macros(self, macros_json):
        import json as _json
        macros = _json.loads(macros_json) if isinstance(macros_json, str) else macros_json
        hub.save_macros(macros)
        return {"ok": True}

    def restore_all(self):
        """整组还原。返回真的写回去了哪些文件，别只回一个 ok。"""
        res = hub.restore_all()
        if isinstance(res, dict):
            return {"ok": True, **res}
        return {"ok": True}

    # ---------- 单键 RGB（仅有线通道支持） ----------
    def get_rgb(self):
        """读整张 126 槽颜色表。无线模式会抛 AulaError。"""
        table = hub.read_rgb()
        return {"ok": True, "slots": [list(c) for c in table]}

    def get_keymap(self):
        return hub.get_keymap()

    def use_custom_rgb(self):
        """切到自定义灯效 + 打开 custom 标志，单键颜色才会显示。"""
        hub.use_custom_rgb()
        return {"ok": True}

    # ---------- 旋钮 / 模式键（矩阵第 0 层 idx108）----------
    def get_knob(self):
        """返回旋钮当前绑定的功能；只有有线通道能读写矩阵。"""
        return hub.get_knob()

    def set_knob(self, action_json):
        import json as _json
        action = _json.loads(action_json) if isinstance(action_json, str) else action_json
        hub.set_knob(action)
        return {"ok": True}

    def reset_knob(self):
        hub.reset_knob()
        return {"ok": True}

    # ---------- 旋钮双向接管（顺时针 / 逆时针 / 按下 各绑一个动作）----------
    def knob_bind(self):
        """读双向绑定配置 + 运行状态。

        出厂状态固件已把方向翻成 Consumer Volume Up/Down/Mute（真机实测），
        矩阵 idx108 只能描述「旋钮整体」，所以方向分绑只能靠软件层拦这三个键。
        """
        st = router.status()
        try:
            st["modeGuard"] = mode_guard.status()
        except Exception:
            pass
        try:
            st["slotWatch"] = slot_watch.status()
        except Exception:
            pass
        return st

    def knob_set_bind(self, patch_json):
        """改绑定。运行中改即时生效，不用重启钩子。"""
        import json as _json
        patch = _json.loads(patch_json) if isinstance(patch_json, str) else patch_json
        return router.reload(patch or {})

    def mode_guard_rescue(self):
        """手动把键盘从固件模式拉回来（用户点了按钮时走这条）。"""
        try:
            with hub.lock:
                hub._connect()
                link = hub.link
                if getattr(link, "kind", "") not in ("wired", "wireless"):
                    return {"ok": False, "msg": "没连上设备"}
                page = link.read_page()
                was = page[0x1A]
                if was != 1:
                    return {"ok": True, "msg": "当前就是软件模式", "was": was}
                fixed = bytearray(page)
                fixed[0x1A] = 0
                link.write_page(bytes(fixed))
                time.sleep(0.1)
                back = link.read_page()
                hub.snap.last_ok = 0.0
                hub.last_full = 0.0
                ok = back[0x1A] == 0
                return {"ok": ok, "was": was, "now": back[0x1A],
                        "msg": "已切回软件模式" if ok else "写入没被接受"}
        except Exception as exc:
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}

    def knob_set_slot(self, which):
        """切到指定槽（序号或槽 id）。长按旋钮走的是 robin 轮回。"""
        try:
            return router.set_slot(which)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def knob_cycle_slot(self):
        """跳到下一个槽（界面上那个「切换」按钮走这条）。"""
        try:
            return router.cycle_mode()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def knob_save_slots(self, raw):
        """整表保存槽位（增 / 删 / 改名 / 改绑定都用它）。

        raw 是 JSON 字符串：{"active": 0, "slots": [{id,label,cw,ccw,press}, ...]}
        """
        try:
            import json as _json
            data = _json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
                return {"error": "槽位数据格式不对"}
            router.reload(data)
            return router.status()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def knob_reset_slots(self):
        """恢复出厂四个槽。"""
        try:
            from core import knob_router as _kr
            router.reload({"slots": [dict(x) for x in _kr.DEFAULT_SLOTS],
                           "active": 0})
            return router.status()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def knob_takeover(self, enabled):
        """开/关接管。关掉后旋钮恢复出厂行为（调音量 / 静音）。"""
        return router.reload({"enabled": bool(enabled)})

    def set_key_color(self, slot, r, g, b):
        hub.set_key_color(int(slot), (int(r), int(g), int(b)))
        return {"ok": True}

    def set_key_colors(self, mapping_json):
        """批量上色：{"12": [255,0,0], "13": [0,255,0]}"""
        import json as _json
        raw = _json.loads(mapping_json) if isinstance(mapping_json, str) else mapping_json
        mapping = {int(k): tuple(v) for k, v in raw.items()}
        hub.set_key_colors(mapping)
        return {"ok": True}

    # ---------- 音乐律动 ----------
    def music_status(self):
        return music.status()

    def music_start(self):
        return music.start()

    def music_stop(self):
        return music.stop(restore=True)

    def music_mood(self, mood):
        return music.set_mood(str(mood))

    def music_tune(self, fps=None, gain=None):
        st = music.status()
        if fps is not None:
            music.fps = max(8, min(40, int(fps)))
        if gain is not None:
            music.gain = max(0.2, min(2.5, float(gain)))
        return music.status()

    # ---------- 设置 / 备份 ----------
    # ---------- 杂项 ----------
    def misc_info(self):
        """杂项页要的静态信息（有哪些项、各自动不动得了）。"""
        try:
            from core import misc as _misc
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        st = hub.snap
        wireless = (st.mode != "wired")
        return {
            "ok": True,
            "link": st.mode,
            "effect": st.effect,
            "effectOff": st.effect in (0, None),
            "canWriteMatrix": not wireless,
            "canWriteRgb": not wireless,
            "canWriteMacro": not wireless,
            "canReset": not wireless,
            "debounceDefault": _misc.DEBOUNCE_DEFAULT,
            "backupDir": str(getattr(hub, "backup_dir", "") or ""),
            "notes": [
                "灯效「关闭」原版叫 tc_kb_led20，厂商把硬件 0 当关闭，我们原来漏了。",
                "按键去抖 KB.ini 里有 ShowDebounce=0x04010101，但配置页偏移还没解出来。",
                "恢复出厂走固件 0x11，会把灯效 / 改键 / 宏一起清掉，只有有线能用。",
                "配置导入导出包含四层矩阵、配置页、逐键颜色、宏库。",
            ],
        }

    def misc_effect_off(self):
        """灯效关闭（原版 tc_kb_led20）。"""
        try:
            from core import misc as _misc
            return _misc.set_effect_off(hub)
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_probe_time(self):
        """探测配置页里有没有在走的字节（固件时间戳）。"""
        try:
            from core import misc as _misc
            return {"ok": True, "moving": _misc.probe_time_offset(hub)}
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_read_debounce(self):
        try:
            from core import misc as _misc
            return _misc.read_debounce(hub)
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_export(self):
        """导出配置到默认目录，返回路径。"""
        try:
            from core import misc as _misc
            d = self.open_backup_dir()
            folder = (d or {}).get("path") or os.environ.get("TEMP", ".")
            name = "aula-config-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
            return _misc.export_config(hub, os.path.join(folder, name))
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_import(self, path=""):
        """从指定 JSON 导入配置。"""
        try:
            from core import misc as _misc
            if not path:
                return {"error": "没给文件路径"}
            return _misc.import_config(hub, str(path))
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_pick_import(self):
        """弹系统选文件框，选完直接导入。"""
        try:
            import tkinter as _tk
            from tkinter import filedialog as _fd
            from core import misc as _misc
            root = _tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = _fd.askopenfilename(
                title="选一份 AULA Hub 配置",
                filetypes=[("AULA Hub 配置", "*.json"), ("所有文件", "*.*")])
            root.destroy()
            if not path:
                return {"error": "取消了"}
            return _misc.import_config(hub, path)
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_reset_all(self):
        """恢复出厂（固件 0x11）。"""
        try:
            from core import misc as _misc
            return _misc.reset_all(hub)
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_write_page(self, hexstr):
        """整页写原始配置页（排障用）。"""
        try:
            from core import misc as _misc
            return _misc.write_raw_page(hub, str(hexstr))
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def misc_read_page(self):
        """读原始配置页（排障用）。"""
        try:
            with hub.lock:
                hub._connect()
                page = bytes(hub.link.read_page())
            return {"ok": True, "hex": page.hex(),
                    "nonzero": [[i, b] for i, b in enumerate(page) if b]}
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def get_settings(self):
        return settings.read()

    def set_settings(self, patch_json):
        import json as _json
        patch = _json.loads(patch_json) if isinstance(patch_json, str) else patch_json
        return settings.write(patch)

    def list_backups(self):
        """列出备份文件。

        名字里带的信息直接解析出来给 UI，不然一堆
        `matrix-L2-before-20261022-154056.bin` 看不出哪份是「改动前」、
        哪几份属于同一次备份。before=True 的那些是写坏之前的现场快照，
        只在你想手工回滚某一次误操作时才用，正常还原会跳过它们。
        """
        import glob as _glob
        import re as _re
        pat = _re.compile(
            r"^(?P<kind>[a-z]+(?:-L\d|-[a-z]+)?)"
            r"(?P<before>-before)?"
            r"-(?P<stamp>\d{8}-\d{6})"
            r"(?:-(?P<group>[0-9a-f]{6}))?"
            r"(?:-(?P<dup>\d+))?\.bin$"
        )
        items = []
        for path in _glob.glob(os.path.join(BACKUP_DIR, "*.bin")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            name = os.path.basename(path)
            m = pat.match(name)
            kind = m.group("kind") if m else name.split("-")[0]
            before = bool(m and m.group("before"))
            group = (m.group("group") if m else None) or ""
            items.append({
                "name": name,
                "tag": kind,
                "kind": kind,
                "before": before,
                "group": group,
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def latest_backup(self):
        """「还原到最近一次备份」会还原什么，先给 UI 看一眼。"""
        try:
            return {"ok": True, **hub.latest_backup_info()}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def backup_now(self):
        count = hub.backup_now()
        return {"ok": True, "files": count}

    def open_backup_dir(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.startfile(BACKUP_DIR)
        return {"ok": True}


def _tray_loop():
    while True:
        try:
            s = hub.state()
            tray.update(s["battery"], s["charging"], s["connected"])
        except Exception:
            pass
        time.sleep(8)


def _after_start():
    _fit_to_screen()
    chrome.watch("AULA Hub", ICON)
    # 踩过：这里原来写的是 lambda: (_window.show(), _window.restore())，
    # 但这两个都不动窗口坐标 —— 窗口还停在 park 的 -32000，
    # 用户点托盘图标看着「没反应」。必须走 _show_window() 才挪回来。
    tray.on_show = _show_window
    tray.on_quit = _quit
    tray.start()
    threading.Thread(target=_tray_loop, daemon=True).start()
    # 自绘提示条的后台线程先起来：它要建常驻 layered window，
    # 第一次 show 时才建会有明显延迟（第一格转过去条是迟到的）。
    try:
        from core import osd as _osd
        _osd.instance().start()
    except Exception:
        pass
    # 旋钮双向接管：用户开过「接管」才真正挂钩子（enabled 存在配置里）
    try:
        router.start()
    except Exception:
        pass
    # 固件模式守护：键盘长按会把自己切进「固件模式」（配置页 0x1A=1），
    # 那时旋钮由固件直接调背光、一个按键都不发给电脑，接管彻底失效。
    # 这个线程每 2 秒看一眼，发现是 1 就写回 0。
    try:
        mode_guard.enabled = True
        mode_guard.start()
        try:
            slot_watch.enabled = True
            slot_watch.start()
        except Exception as exc:                     # noqa: BLE001
            _dbg(f"长按切槽监听没起来：{type(exc).__name__}: {exc}")
    except Exception:
        pass
    tab = os.environ.get("AULA_TAB")          # 调试用：启动后直接切到某一页
    js = os.environ.get("AULA_JS")            # 调试用：启动后执行一段前端 JS
    probe = os.environ.get("AULA_PROBE")      # 调试用：执行 JS 并把结果写进文件
    osd_test = os.environ.get("AULA_OSD_TEST")  # 调试用：依次弹一串 OSD 给截图看

    def _osd_demo():
        """调试用：在**本进程**里依次弹各种 OSD。

        为什么必须在本进程：`core.osd` 的 Win32 窗口属于创建它的进程，
        外部脚本新起的 python 会另建一份实例（线程还没 start），
        截到的只能是空白桌面。
        """
        def _dbg(msg):
            try:
                with open(os.path.join(os.environ.get("TEMP", "."), "aula-osd-demo.txt"),
                          "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:
                pass

        try:
            from core import osd as _osd2
            inst = _osd2.instance()
            _dbg(f"inst thread={inst._thread} alive={inst._thread.is_alive() if inst._thread else None} "
                 f"err={inst.last_error!r} dark={inst.dark} scale={inst.scale}")
            for kind, val, tot in (("volume", 62, 100), ("mute", 0, 100),
                                   ("light", 6, 9), ("screen", 74, 100),
                                   ("effect", 7, 19), ("speed", 3, 4)):
                inst.show(kind, val, tot)
                time.sleep(3.0)
                _dbg(f"after show {kind}: shown={inst._shown} err={inst.last_error!r}")
        except Exception as exc:
            _dbg(f"EXC {type(exc).__name__}: {exc}")

    if osd_test:
        threading.Thread(target=_osd_demo, daemon=True).start()

    if tab or js or probe:
        time.sleep(1.4)
        try:
            if tab:
                _window.evaluate_js(f"switchTab({tab!r})")
            if js:
                time.sleep(0.6)
                _window.evaluate_js(js)
            if probe:
                # AULA_PROBE_WAIT 让外部脚本有机会在「读结果」之前操作界面
                # （例如用 SendInput 发真实鼠标事件，验证拖拽是否真的生效）
                time.sleep(float(os.environ.get("AULA_PROBE_WAIT") or 3.0))
                val = _window.evaluate_js(probe)
                with open(os.path.join(os.environ.get("TEMP", "."), "aula-probe.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(repr(val))
        except Exception as exc:
            try:
                with open(os.path.join(os.environ.get("TEMP", "."), "aula-probe.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write("ERR " + repr(exc))
            except Exception:
                pass


def _quit():
    global _really_quit
    _really_quit = True
    try:
        slot_watch.stop()
    except Exception:
        pass
    try:
        mode_guard.stop()
    except Exception:
        pass
    try:
        router.stop()
    except Exception:
        pass
    try:
        from core import osd as _osd
        _osd.instance().stop()
    except Exception:
        pass
    tray.stop()
    try:
        if _window:
            _window.destroy()
    except Exception:
        pass


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

_PARK_X, _PARK_Y = -32000, -32000
_park_state = {"parked": False, "ex": None, "x": 0, "y": 0}


def _hwnd():
    """找到主窗 HWND。pywebview 没给句柄，只能按标题枚举。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _lp):
            if user32.IsWindowVisible(h):
                n = user32.GetWindowTextLengthW(h)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 2)
                    user32.GetWindowTextW(h, buf, n + 2)
                    if buf.value.startswith("AULA Hub"):
                        found.append(h)
            return True

        user32.EnumWindows(cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def _park_window(park: bool):
    """把窗口挪出屏幕（park）或挪回来，而不是 hide。

    踩过的坑：`_window.hide()` 会让 pywebview 主循环结束、进程直接退出，
    于是旋钮接管和软件动作键全部失效 —— 用户以为只是「关了个窗口」，
    实际上整套后台服务都停了。

    实测（aula-hub/tmp/park_test.py）：HWND 保留、只是挪到 -32000 并加
    WS_EX_TOOLWINDOW 从任务栏隐去时，低级键盘钩子照常工作。所以托盘
    方案改成 park / unpark。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = _hwnd()
        if not hwnd:
            raise RuntimeError("找不到主窗")

        if park:
            if _park_state["parked"]:
                return
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            _park_state["x"] = rect.left
            _park_state["y"] = rect.top
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _park_state["ex"] = ex
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                 (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            user32.SetWindowPos(hwnd, 0, _PARK_X, _PARK_Y, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            _park_state["parked"] = True
        else:
            if not _park_state["parked"]:
                return
            if _park_state["ex"] is not None:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, _park_state["ex"])
            user32.SetWindowPos(hwnd, 0, _park_state["x"], _park_state["y"], 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            _park_state["parked"] = False
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"park({park}) failed: {exc!r}")
        raise


def _show_window():
    """托盘菜单「打开 AULA Hub」。"""
    try:
        _park_window(False)
        if _window:
            _window.restore() if hasattr(_window, "restore") else None
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"show failed: {exc!r}")


def _window_rect():
    """主窗在屏幕上的矩形，用来确认 park / unpark 真的动了窗口。"""
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _hwnd()
        if not hwnd:
            return None
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


_MUTEX = None            # 必须留引用，句柄一松互斥体就没了
_MUTEX_NAME = "Local\\AULA-Hub-Single-Instance"
_WAKE_MSG = None         # RegisterWindowMessageW 的结果，两边算出同一个值


def _wake_msg():
    """`AULA_HUB_SHOW` 的自定义窗口消息号（进程间一致）。"""
    global _WAKE_MSG
    if _WAKE_MSG is None:
        try:
            _WAKE_MSG = ctypes.windll.user32.RegisterWindowMessageW("AULA_HUB_SHOW")
        except Exception:
            _WAKE_MSG = 0
    return _WAKE_MSG


def _acquire_single_instance():
    """拿到互斥体返回 True；已有实例在跑返回 False。"""
    global _MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        _MUTEX = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        already = kernel32.GetLastError() == 183          # ERROR_ALREADY_EXISTS
        _dbg(f"single-instance: mutex={_MUTEX} already={already}")
        return not already
    except Exception as exc:                              # noqa: BLE001
        _dbg(f"single-instance failed {exc!r}")
        return True                                       # 出错就不拦


def _wake_existing():
    """叫已有实例把窗口收回来。找不到窗口就放弃（对方可能还在启动）。"""
    for _ in range(12):
        hwnd = _hwnd()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.PostMessageW(ctypes.c_void_p(hwnd), _wake_msg(), 0, 0)
            _dbg(f"single-instance: 已唤醒 hwnd={hwnd}")
            return True
        time.sleep(0.25)
    _dbg("single-instance: 没找到已有窗口")
    return False


WM_CLOSE = 0x0010
GWL_WNDPROC = -4
_WNDPROC = None          # 必须留引用，否则回调被 GC 后窗口消息直接崩
_OLD_WNDPROC = None


def _install_close_hook():
    """子类化主窗 wndproc，自己处理 WM_CLOSE。

    为什么不监听 pywebview 的 closing 事件：真机上它压根没触发
    （启动日志确认 handlers=1，但发 WM_CLOSE 后一条日志都没有，
    窗口直接 IsWindow=False、进程退出）。自己挂 wndproc 是唯一
    可靠的办法。
    """
    global _WNDPROC, _OLD_WNDPROC
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = _hwnd()
        if not hwnd:
            # _after_start() 触发时窗口常常还没显示出来（IsWindowVisible 为假），
            # 这时 _hwnd() 找不到主窗。等一会儿再挂。
            _dbg("close-hook: 主窗未就绪，等待…")

            def _retry():
                for _ in range(40):
                    time.sleep(0.25)
                    if _hwnd():
                        _install_close_hook()
                        return
                _dbg("close-hook: 等待超时，放弃")

            threading.Thread(target=_retry, daemon=True).start()
            return False

        if _OLD_WNDPROC:
            return True          # 已经挂过了

        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                            ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        LRESULT = ctypes.c_void_p
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)

        def _proc(h, msg, wparam, lparam):
            if msg and msg == _wake_msg():
                try:
                    _show_window()
                    _dbg("close-hook: 收到唤醒消息，窗口已还原")
                except Exception as exc:         # noqa: BLE001
                    _dbg(f"close-hook wake err {exc!r}")
                return 0
            if msg == WM_CLOSE:
                try:
                    if _really_quit or not settings.read().get("close_to_tray", True):
                        _dbg("close-hook: 放行退出")
                    else:
                        _park_window(True)
                        _dbg("close-hook: 已 park，窗口保留")
                        if os.environ.get("AULA_PARK_TEST"):
                            def _unpark_later():
                                time.sleep(2.0)
                                _show_window()
                                time.sleep(0.6)
                                _dbg(f"park-test: 已还原 rect={_window_rect()}")
                            threading.Thread(target=_unpark_later, daemon=True).start()
                        if os.environ.get("AULA_TRAY_TEST"):
                            # 走 pystray 菜单项 default=True 的同一条回调
                            # （菜单项是 lambda: self.on_show()）
                            def _tray_later():
                                time.sleep(2.5)
                                try:
                                    tray.on_show()
                                    time.sleep(0.5)
                                    _dbg(f"tray-test: rect={_window_rect()}")
                                except Exception as exc:     # noqa: BLE001
                                    _dbg(f"tray-test err {exc!r}")
                            threading.Thread(target=_tray_later, daemon=True).start()
                        try:
                            tray.notify("AULA Hub 还在后台跑，点托盘图标可以打开")
                        except Exception:       # noqa: BLE001
                            pass
                        return 0
                except Exception as exc:         # noqa: BLE001
                    _dbg(f"close-hook err {exc!r}")
            return user32.CallWindowProcW(_OLD_WNDPROC, h, msg, wparam, lparam)

        _WNDPROC = WNDPROC(_proc)
        _OLD_WNDPROC = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC,
                                               ctypes.cast(_WNDPROC, ctypes.c_void_p))
        _dbg(f"close-hook installed hwnd={hwnd} old={_OLD_WNDPROC}")
        return bool(_OLD_WNDPROC)
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"close-hook failed {exc!r}")
        return False


def _dbg(msg):
    """诊断日志：只在 AULA_DEBUG_LOG 指向一个文件时才写。"""
    path = os.environ.get("AULA_DEBUG_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _on_closing():
    """点 ✕ 时按设置决定：缩到托盘 or 退出。返回 False 阻止关闭。

    踩过的坑：早先一个 try 包住 hide 与 notify，notify 抛异常就会让
    整个函数返回 True → 进程直接退出，用户以为「关了就是关了」，
    实际上旋钮接管和软件动作键也一起没了。所以两步分开兜，hide 成功
    就算缩托盘成功（气泡没弹出来无所谓）。
    """
    try:
        want_tray = settings.read().get("close_to_tray", True)
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"closing: read settings failed {exc!r}")
        want_tray = True

    _dbg(f"closing: really_quit={_really_quit} want_tray={want_tray}")
    if _really_quit or not want_tray:
        _really_quit = True
        return True

    try:
        _park_window(True)
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"closing: park failed {exc!r}")
        return True
    _dbg("closing: parked ok")
    try:
        tray.notify("AULA Hub 还在后台跑，点托盘图标可以打开")
    except Exception as exc:                    # noqa: BLE001
        _dbg(f"closing: notify failed {exc!r}")
    return False


def _fit_to_screen():
    """把窗口收进屏幕可用区域，并居中。

    踩过的坑：写死 1240x830，在 15 寸 150% 缩放的机器上（物理 1707x1067），
    窗口右边缘跑到屏幕外——滑轨最右端鼠标物理上够不到，用户看到的就是
    「拉满还是不到头」；同时窗口溢出去的部分被别的程序压着，鼠标点不到。
    pywebview 在非 DPI-aware 进程里按「逻辑像素」建窗，系统再乘缩放，
    所以要拿 GetDpiForWindow 的真实 DPI 反算逻辑上限，再 resize。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = None

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(h, _lp):
            nonlocal hwnd
            n = user32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                if buf.value == "AULA Hub" and user32.IsWindowVisible(h):
                    hwnd = h
            return True

        user32.EnumWindows(_cb, 0)
        if not hwnd:
            return
        try:
            dpi = user32.GetDpiForWindow(hwnd) or 96
        except Exception:
            dpi = 96
        scale = dpi / 96.0 or 1.0
        screen_w = user32.GetSystemMetrics(0) / scale
        screen_h = user32.GetSystemMetrics(1) / scale
        # 任务栏高度：物理像素 → 逻辑像素
        work_h = user32.GetSystemMetrics(1) / scale
        try:
            rect = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                work_h = (rect.bottom - rect.top) / scale
        except Exception:
            pass

        want_w = min(1240.0, screen_w - 48)
        want_h = min(830.0, work_h - 48)
        if want_w < 880:
            want_w = max(720.0, screen_w - 24)
        if want_h < 620:
            want_h = max(520.0, work_h - 24)
        cur_w = _window.width if hasattr(_window, "width") else 0
        if abs(cur_w - want_w) > 4 or abs((_window.height or 0) - want_h) > 4:
            _window.resize(int(want_w), int(want_h))
        # 居中（同尺寸时避免每次启动都动一下）
        try:
            _window.move(int((screen_w - want_w) / 2), int(max(0, (work_h - want_h) / 2)))
        except Exception:
            pass
    except Exception:
        pass


def main():
    global _window, _really_quit
    _really_quit = False

    # 关窗后程序常驻后台，用户很可能再双击一次快捷方式。
    # 不做保护就会出现两个托盘图标 + 两套键盘钩子互相抢事件。
    if not _acquire_single_instance():
        _wake_existing()
        _dbg("single-instance: 已有实例在跑，本进程退出")
        return

    _window = webview.create_window(
        "AULA Hub",
        os.path.join(BASE, "ui", "index.html"),
        js_api=Api(),
        width=1240,
        height=830,
        min_size=(900, 600),
        background_color="#08090d",
        easy_drag=False,
    )
    _window.events.closing += _on_closing
    _dbg(f"startup: closing handlers={len(_window.events.closing)} "
         f"log={os.environ.get('AULA_DEBUG_LOG')!r}")
    _install_close_hook()
    try:
        webview.start(func=_after_start, debug=bool(os.environ.get("AULA_DEBUG")))
    finally:
        tray.stop()


if __name__ == "__main__":
    main()
