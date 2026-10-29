"""应用设置（只影响这台电脑上的 AULA Hub，不写键盘）。

原版 `tc_msg1 全局设置` 里的项目在这里落地：
- 关闭窗口时：直接退出 / 缩小到托盘（tc_msg6/7/8）
- 开机自动运行（tc_msg11）
- 自动检查更新（原版有，我们不做联网，直接省掉）

键盘里那些设置（灯效、休眠、按键去抖）属于设备，存在键盘自己的配置页里，
不放这里。
"""
from __future__ import annotations

import json
import os
import sys
import winreg

APP_NAME = "AULA-Hub"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULTS = {
    "close_to_tray": True,      # 点 ✕ 缩到托盘，而不是退出
    "autostart": False,         # 开机自动运行
    "poll_seconds": 5,          # 界面刷新间隔
}

_APP_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME
)
_PATH = os.path.join(_APP_DIR, "settings.json")


def _load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return data


def _store(data: dict):
    os.makedirs(_APP_DIR, exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


# ---------- 开机自启（HKCU\...\Run） ----------

INSTALLED_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "Programs", "AULA-Hub", "AULA-Hub.exe")


def _launch_command() -> str:
    """打包后指向 exe；开发态也优先指向已安装的 exe。

    为什么开发态要优先装好的 exe：正在跑的可能是一次临时调试（pythonw + app.py
    指向工作区），写成自启项后机器一重启就会去加载源码目录，装好的那份反而没人管。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    if os.path.exists(INSTALLED_EXE):
        return f'"{INSTALLED_EXE}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    return f'"{exe}" "{app}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ---------- 对外 ----------

def read() -> dict:
    data = _load()
    # 注册表才是真相：用户可能在任务管理器里禁用了启动项
    data["autostart"] = autostart_enabled() if data.get("autostart") else False
    data["path"] = _PATH
    return data


def write(patch: dict) -> dict:
    data = _load()
    if "autostart" in patch:
        set_autostart(bool(patch["autostart"]))
        data["autostart"] = bool(patch["autostart"])
    for key in ("close_to_tray", "poll_seconds"):
        if key in patch:
            data[key] = patch[key]
    if "poll_seconds" in data:
        try:
            data["poll_seconds"] = max(2, min(60, int(data["poll_seconds"])))
        except (TypeError, ValueError):
            data["poll_seconds"] = DEFAULTS["poll_seconds"]
    _store(data)
    return read()
