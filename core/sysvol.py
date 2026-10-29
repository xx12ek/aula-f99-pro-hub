"""系统音量 / 静音 / 屏幕亮度 —— 走真正改得动声音的接口。

为什么弃用 waveOutSetVolume
---------------------------
真机实测（2026-10-22，`tmp/vol_probe.py`）：

    waveOutGetVolume(None) -> 0（成功），报 100%
    WASAPI master scalar   -> 0.3400（34%）

winmm 那套老混音器在这台机器上映射的是一个没人用的影子通道。写它既改不动
声音，Windows 也不会弹音量条 —— OSD 是系统音频服务在**真实**音量变化时画的。

用户 m07194 报的「接管旋钮后音量条不出来、系统 UI 不变、实际声音不变」就是
这个原因，不是钩子写错了。所以音量改走 WASAPI 的 IAudioEndpointVolume。

另一条实测结论（`tmp/osd_check.py` + 截图）：**程序直接改 WASAPI 音量，
系统自己的 OSD 不会弹**。所以 OSD 得自己画（见 `core/osd.py`）。

屏幕亮度
--------
WMI 的 `WmiMonitorBrightnessMethods.WmiSetBrightness`，实测写入 2~4 ms，
完全够挂在旋钮上（不用起 PowerShell 子进程）。只对内置屏有效；
外接屏要 DDC/CI，本机 `GetMonitorCapabilities` 返回 0，不支持。

踩过的坑
--------
- `comtypes.client.GetObject` 根本不存在，正确名字是 `CoGetObject`。
- lazybind（`dynamic.Dispatch`）拿不到 WMI 动态 provider 类的方法名，
  `WmiSetBrightness` 会 `NameError`。必须走 `SWbemLocator` + `ExecMethod`。
- `SWbemServices.ExecMethod` 的第一个参数要**字符串路径**（`obj.Path_.Path`），
  传 `ISWbemObjectPath` 指针会 `ctypes.ArgumentError: unicode string expected`。
- comtypes 给 `ExecMethod_` 加了补丁，实际方法名叫 `ExecMethod`（无下划线）。
- 每个线程用 COM 前都得自己 `CoInitialize()`（旋钮工作线程是独立线程）。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import (POINTER, HRESULT, byref, c_float, c_void_p)
from ctypes import wintypes as wt

import comtypes
import comtypes.client          # noqa: F401  —— CreateObject 在子模块里
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown, CoCreateInstance

# ── WASAPI：默认播放设备的音量 / 静音 ────────────────────────────────

_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioEndpointVolume = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

_E_RENDER, _E_CONSOLE = 0, 0


class _IMMDevice(IUnknown):
    """只用到 Activate（vtable 第 0 个），其余不声明也不影响调用。"""

    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], wt.DWORD, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
    ]


class _IMMDeviceEnumerator(IUnknown):
    _iid_ = _IID_IMMDeviceEnumerator
    _methods_ = [
        # 第 0 个：EnumAudioEndpoints —— 不用，但要占住 vtable 位置
        COMMETHOD([], HRESULT, "_M0",
                  (["in"], ctypes.c_int, "dataFlow"),
                  (["in"], wt.DWORD, "dwStateMask"),
                  (["out"], POINTER(c_void_p), "ppDevices")),
        # 第 1 个：GetDefaultAudioEndpoint —— 本模块唯一要用的
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], ctypes.c_int, "dataFlow"),
                  (["in"], ctypes.c_int, "role"),
                  (["out"], POINTER(POINTER(_IMMDevice)), "ppEndpoint")),
    ]


class _IAudioEndpointVolume(IUnknown):
    """Vtable 顺序必须和 Windows SDK 一致，所以前面的方法只占位不实现。

    用到的四个：第 5 个 SetMasterVolumeLevelScalar、第 7 个
    GetMasterVolumeLevelScalar、第 12 个 SetMute、第 13 个 GetMute。
    """

    _iid_ = _IID_IAudioEndpointVolume
    _methods_ = [
        COMMETHOD([], HRESULT, "_M0"),          # RegisterControlChangeNotify
        COMMETHOD([], HRESULT, "_M1"),          # UnregisterControlChangeNotify
        COMMETHOD([], HRESULT, "_M2", (["out"], POINTER(wt.UINT), "n")),   # GetChannelCount
        COMMETHOD([], HRESULT, "_M3"),          # SetMasterVolumeLevel(level, guid)
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar",               # 第 5 个
                  (["in"], c_float, "level"),
                  (["in"], c_void_p, "pguidEventContext")),
        COMMETHOD([], HRESULT, "_M5", (["out"], POINTER(c_float), "l")),   # GetMasterVolumeLevel
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar",               # 第 7 个
                  (["out"], POINTER(c_float), "level")),
        COMMETHOD([], HRESULT, "_M7"),          # SetChannelVolumeLevel
        COMMETHOD([], HRESULT, "_M8"),          # SetChannelVolumeLevelScalar
        COMMETHOD([], HRESULT, "_M9"),          # GetChannelVolumeLevel
        COMMETHOD([], HRESULT, "_M10"),         # GetChannelVolumeLevelScalar
        COMMETHOD([], HRESULT, "SetMute",                                  # 第 12 个
                  (["in"], wt.BOOL, "mute"),
                  (["in"], c_void_p, "pguidEventContext")),
        COMMETHOD([], HRESULT, "GetMute",                                  # 第 13 个
                  (["out"], POINTER(wt.BOOL), "mute")),
    ]


_com_init = threading.local()
# COM 接口指针有线程亲和性：哪个线程 CreateObject 的，就只能在那
# 个线程里调。界面线程和旋钮工作线程都要读音量/屏幕亮度，所以
# 指针必须每线程一份（放全局会让后用的那个线程全线失败，实测 -1）。
_tl = threading.local()


def _ensure_com():
    """每个线程用 COM 前必须 CoInitialize —— 旋钮工作线程是独立的。"""
    if getattr(_com_init, "done", False):
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        pass          # 已经初始化过（S_FALSE / RPC_E_CHANGED_MODE）都无所谓
    _com_init.done = True


def endpoint():
    """拿默认播放设备的 IAudioEndpointVolume。失败抛 RuntimeError。

    只在当前线程内复用；换线程会重新 CreateObject 一次（约 3ms）。
    """
    epv = getattr(_tl, "epv", None)
    if epv is not None:
        return epv
    _ensure_com()
    try:
        enum = CoCreateInstance(_CLSID_MMDeviceEnumerator, _IMMDeviceEnumerator,
                                CLSCTX_ALL)
        dev = enum.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE)
        raw = dev.Activate(_IID_IAudioEndpointVolume, CLSCTX_ALL, None)
        epv = raw.QueryInterface(_IAudioEndpointVolume)
    except Exception as exc:                     # noqa: BLE001
        raise RuntimeError(f"没有可用的播放设备：{type(exc).__name__}: {exc}")
    _tl.epv = epv
    return epv


def reset():
    """设备切换后（插拔耳机）丢掉旧指针，下次重新取。只清当前线程。"""
    _tl.epv = None


# ── 音量 ──────────────────────────────────────────────────────────────

VOL_STEP = 2          # 每格 2%，跟 Windows 自己的媒体键一致


def volume_get() -> int:
    """0..100；拿不到返回 -1。"""
    try:
        v = endpoint().GetMasterVolumeLevelScalar()
        return max(0, min(100, int(round(v * 100))))
    except Exception:                            # noqa: BLE001
        return -1


def volume_set(pct: int) -> int:
    """设成 pct（0..100），返回设备读回的值；失败返回 -1。"""
    pct = max(0, min(100, int(pct)))
    for attempt in (0, 1):
        try:
            endpoint().SetMasterVolumeLevelScalar(pct / 100.0, None)
            return volume_get()
        except Exception:                        # noqa: BLE001
            # 第一次失败多半是默认设备换了（插拔耳机），丢掉指针重取一次
            reset()
            if attempt:
                return -1
    return -1


def volume_step(delta: int) -> int:
    cur = volume_get()
    if cur < 0:
        return -1
    return volume_set(cur + int(delta))


def mute_get() -> bool:
    try:
        return bool(endpoint().GetMute())
    except Exception:                            # noqa: BLE001
        return False


def mute_set(on: bool) -> bool:
    for attempt in (0, 1):
        try:
            endpoint().SetMute(1 if on else 0, None)
            return mute_get()
        except Exception:                        # noqa: BLE001
            reset()
            if attempt:
                return mute_get()
    return mute_get()


def mute_toggle() -> bool:
    return mute_set(not mute_get())


# ── 屏幕亮度（WMI，只对内置屏）──────────────────────────────────────

_DEBUG = os.environ.get("AULA_DEBUG") == "1"


def _wmi_open():
    """当前线程的 WMI 连接（每线程一份，理由同 endpoint()）。"""
    svc = getattr(_tl, "wmi", None)
    if svc is not None:
        return svc
    _ensure_com()
    loc = comtypes.client.CreateObject("WbemScripting.SWbemLocator")
    svc = loc.ConnectServer(".", "root/wmi")
    _tl.wmi = svc
    return svc


def _screen_read() -> int:
    """真去读一次内置屏亮度；不支持或失败返回 -1。不做任何缓存。"""
    try:
        svc = _wmi_open()
        for o in svc.ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness"):
            return int(o.Properties_.Item("CurrentBrightness").Value)
    except Exception as exc:                     # noqa: BLE001
        # 以前这里静默 pass，导致 -1 分不清「不支持」还是「线程亲和性炸了」。
        if _DEBUG:
            print(f"[sysvol] 读屏幕亮度失败: {type(exc).__name__}: {exc}")
    return -1


def screen_available() -> bool:
    """内置屏才支持。

    判定方式就是「真能不能读到亮度」——以前用另一个查询(亮度方法类)去探，
    探出来的 True/False 会和 screen_get() 的结论打架，界面上就会出现
    「说支持、实际调不动」。结论按线程各记一份。
    """
    ok = getattr(_tl, "ok", None)
    if ok is not None:
        return ok
    ok = _screen_read() >= 0
    _tl.ok = ok
    return ok


def screen_get() -> int:
    """0..100；不支持或读失败返回 -1。"""
    v = _screen_read()
    if v >= 0:
        _tl.ok = True
    return v


def screen_set(pct: int) -> int:
    """写屏幕亮度，返回读回值；失败 -1。"""
    pct = max(0, min(100, int(pct)))
    try:
        svc = _wmi_open()
        mobj = None
        for o in svc.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods"):
            mobj = o
        if mobj is None:
            _tl.ok = False
            return -1
        # 第一个参数必须是字符串路径，传 ISWbemObjectPath 指针会 TypeError
        path = mobj.Path_.Path
        inp = mobj.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
        inp.Properties_.Item("Timeout").Value = 1
        inp.Properties_.Item("Brightness").Value = pct
        svc.ExecMethod(path, "WmiSetBrightness", inp)
    except Exception as exc:                     # noqa: BLE001
        if _DEBUG:
            print(f"[sysvol] 写屏幕亮度失败: {type(exc).__name__}: {exc}")
        return -1
    _tl.ok = True
    # 固件改完要 10~25ms 才生效（实测 scr_lat.txt）：立刻读回会拿到上一格，
    # 连转旋钮时值就会串。所以轮询到真读出新值再返回。
    deadline = time.perf_counter() + 0.4
    while time.perf_counter() < deadline:
        v = screen_get()
        if v == pct:
            return v
        time.sleep(0.01)
    return screen_get()


def screen_step(delta: int) -> int:
    cur = screen_get()
    if cur < 0:
        return -1
    return screen_set(cur + int(delta))


def probe() -> dict:
    """给界面/自检用的现状快照。"""
    return {
        "volume": volume_get(),
        "muted": mute_get(),
        "screen": screen_get(),
        "screenOk": screen_available(),
    }
