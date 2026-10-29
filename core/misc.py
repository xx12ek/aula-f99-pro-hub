# -*- coding: utf-8 -*-
"""core/misc.py：原版有、但 AULA Hub 之前没做的几项功能。

都会先探测设备支持度；不支持就如实报错，不假装成功。
"""
import io
import json
import os
import time

# 配置页里 RTC 时间戳的偏移，用「按键触发一次写入」法探测出来之前先不写死，
# 见 probe_time_offset()。
PAGE_TIME_CANDIDATES = (0x10, 0x12, 0x14, 0x1C, 0x20)

# 原厂 tc_kb_led20「关闭」——LedOpt20=0,0,0,0,0,0,0，厂商把「关闭」映射到
# 硬件灯效 0。EFFECTS 里 (19, 0) 是自定义槽，所以「关闭」只能单独走一条。
EFFECT_OFF = 0

# 原厂 tc_msg54「按键去抖」+ KB.ini ShowDebounce=0x04010101。
# 配置页目前只确认了 0x18（休眠）与 0x1A（固件模式），去抖的偏移还没解出来。
DEBOUNCE_DEFAULT = 0x04010101


def _page_offsets(page):
    """把配置页里非零偏移列出来，供 UI 诊断用。"""
    return [(i, page[i]) for i in range(len(page)) if page[i]]


def probe_time_offset(hub):
    """探测配置页里哪一个字节是「时间/秒表」。

    做法：连读两次配置页，间隔 1.2 秒，看哪些偏移自己变了。设备在走秒的话
    会有一个或几个字节单调递增。找不到就返回空列表 —— 说明这个固件不存时间。
    """
    with hub.lock:
        hub._connect()
        a = bytes(hub.link.read_page())
        time.sleep(1.2)
        b = bytes(hub.link.read_page())
        c = bytes(hub.link.read_page())
    moving = [i for i in range(len(a)) if a[i] != b[i] or b[i] != c[i]]
    return moving


def export_config(hub, path):
    """把四层矩阵 + 宏库 + 配置页 + RGB 表导成一个 JSON。"""
    data = {"version": 1, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": "AULA F99 Pro"}
    layers = {}
    wireless = (getattr(hub.link, "kind", "") != "wired")
    for layer in (range(1) if wireless else range(4)):
        layers[str(layer)] = hub.read_matrix(layer, save=False).hex()
    data["matrix"] = layers
    try:
        data["page"] = bytes(hub.link.read_page()).hex()
    except Exception as exc:                       # noqa: BLE001
        data["page_error"] = f"{type(exc).__name__}: {exc}"
    try:
        data["rgb"] = [list(c) for c in hub.read_rgb(save=False)]
    except Exception as exc:                       # noqa: BLE001
        data["rgb_error"] = f"{type(exc).__name__}: {exc}"
    try:
        data["macros"] = hub.read_macros().hex()
    except Exception as exc:                       # noqa: BLE001
        data["macros_error"] = f"{type(exc).__name__}: {exc}"
    io.open(path, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=1))
    return {"ok": True, "path": path,
            "bytes": os.path.getsize(path),
            "layers": len(layers),
            "hasRgb": "rgb" in data, "hasMacros": "macros" in data}


def import_config(hub, path):
    """把 export_config 出来的 JSON 写回设备。

    只写这次真的读到了的段；写不进去的段（2.4G 下的矩阵/RGB/宏）会逐条
    记在 skipped 里，不会静默丢。
    """
    data = json.loads(io.open(path, encoding="utf-8").read())
    done, skipped = [], []
    wireless = (getattr(hub.link, "kind", "") != "wired")

    for key, raw in sorted((data.get("matrix") or {}).items()):
        layer = int(key)
        if wireless:
            skipped.append(f"矩阵第 {layer} 层（2.4G 写不了）")
            continue
        try:
            hub.write_matrix(layer, bytes.fromhex(raw))
            done.append(f"矩阵第 {layer} 层")
        except Exception as exc:                   # noqa: BLE001
            skipped.append(f"矩阵第 {layer} 层（{exc}）")

    if data.get("page"):
        try:
            hub.link.write_page(bytes.fromhex(data["page"]))
            done.append("配置页")
        except Exception as exc:                   # noqa: BLE001
            skipped.append(f"配置页（{exc}）")

    if data.get("rgb"):
        if wireless:
            skipped.append("逐键颜色（2.4G 写不了）")
        else:
            try:
                hub.write_rgb([tuple(c) for c in data["rgb"]], verify=False)
                done.append("逐键颜色")
            except Exception as exc:               # noqa: BLE001
                skipped.append(f"逐键颜色（{exc}）")

    if data.get("macros"):
        if wireless:
            skipped.append("宏库（2.4G 写不了）")
        else:
            try:
                hub.write_macros(bytes.fromhex(data["macros"]))
                done.append("宏库")
            except Exception as exc:               # noqa: BLE001
                skipped.append(f"宏库（{exc}）")

    hub.snap.last_ok = 0.0
    hub.last_full = 0.0
    return {"ok": not skipped, "done": done, "skipped": skipped,
            "at": data.get("at", "")}


def reset_all(hub):
    """恢复出厂（原版 tc_msg21）。

    走固件的 RESET 命令 0x11；这条命令会连灯效、改键、宏一起清掉，所以
    UI 上必须先确认。2.4G 没有 0x11，直接报错。
    """
    link = hub._wired()
    link.write(0x11, 0, b"")
    time.sleep(1.2)
    hub.snap.last_ok = 0.0
    hub.last_full = 0.0
    return {"ok": True}


def set_effect_off(hub):
    """灯效「关闭」（原版 tc_kb_led20）。

    写 0x0A=0 且清 0x09，设备会进「灯全灭」状态。这是靠配置页做到的，
    不需要额外命令，2.4G 也能用。
    """
    def fn(p):
        p[0x09] = 0
        p[0x0A] = EFFECT_OFF
        hub.snap.effect = EFFECT_OFF
        return True
    hub._patch(fn)
    return {"ok": True, "effect": EFFECT_OFF}


def read_debounce(hub):
    """读按键去抖。偏移还没解出来，先返回配置页里所有可疑字节供诊断。"""
    page = bytes(hub.link.read_page())
    return {"ok": True, "known": False,
            "page_nonzero": _page_offsets(page),
            "hint": "配置页里去抖的偏移还没解出来；KB.ini 里 ShowDebounce="
                    "0x04010101 说明这个型号确实有这项。"}


def write_raw_page(hub, hexstr):
    """整页写原始配置页 —— 给高级用户 / 排障用。"""
    data = bytes.fromhex(hexstr.replace(" ", "").replace("\n", ""))
    if len(data) != 128:
        raise ValueError(f"配置页必须 128 字节，收到 {len(data)}")
    if data[0x7E] != 0x5A or data[0x7F] != 0xA5:
        raise ValueError("配置页尾必须是 5A A5")
    hub.link.write_page(data)
    time.sleep(0.4)
    back = bytes(hub.link.read_page())
    hub.snap.last_ok = 0.0
    hub.last_full = 0.0
    return {"ok": back == data, "match": back == data,
            "read_back": back.hex()}
