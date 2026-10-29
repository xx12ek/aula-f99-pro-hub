"""宏库（0x05/0x85）与按键矩阵（0x03/0x83）编码。

协议来自对 OemDrv.exe 的静态逆向量化（见 docs/macro-protocol.md）：
  宏库 = [nMacro × {u16 offset, u16 len}] + 记录区
  记录 = [u8 nameLenBytes][UTF-16LE 名字][步骤 × 4 字节]
  步骤 = {b0=(time>>16&0xF)|type<<4|0x80(按下), b1=time>>8, b2=time, b3=动作}

  按键矩阵 = 4 字节 × 128（KB.ini MatrixLen=128），param = 层号 0..3
  键→宏：{0x03, 模式(1/2/4), 参数, 宏槽号-1}
"""
from __future__ import annotations

import struct

MAX_BANK = 0x2800  # 10240
NAME_MAX = 30      # 字符数上限（0x1E）

# 步骤类型
T_KEY, T_MOD, T_MOUSE_BTN, T_MOUSE_X, T_MOUSE_Y, T_WHEEL = 0, 1, 2, 3, 4, 5

MODS = {
    "lctrl": 0xE0, "lshift": 0xE1, "lalt": 0xE2, "lwin": 0xE3,
    "rctrl": 0xE4, "rshift": 0xE5, "ralt": 0xE6, "rwin": 0xE7,
}

# 常用键 → HID usage（标准 HID Keyboard/Keypad 页 0x07）
KEYS = {
    **{c: 0x04 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
    **{str(d): 0x1E + i for i, d in enumerate("1234567890")},
    "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
    "minus": 0x2D, "equal": 0x2E, "lbracket": 0x2F, "rbracket": 0x30,
    "backslash": 0x31, "semicolon": 0x33, "quote": 0x34, "grave": 0x35,
    "comma": 0x36, "period": 0x37, "slash": 0x38, "capslock": 0x39,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49,
    "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "numlock": 0x53, **{f"kp{d}": 0x59 + i for i, d in enumerate("1234567890")},
    **MODS,
}
USAGE_NAME = {v: k for k, v in KEYS.items()}


class MacroError(Exception):
    pass


def step(usage: int, delay_ms: int = 0, press: bool = True, kind: int = T_KEY,
         delta: int = 0) -> bytes:
    """一步 4 字节。delay 为 20 位（单位推测 ms）。"""
    d = max(0, min(0xFFFFF, int(delay_ms)))
    # b0: bit0-3 延时高 4 位，bit4-6 类型（3 位足够，0..6），bit7 按下标记
    b0 = ((d >> 16) & 0x0F) | ((kind & 0x07) << 4)
    if kind not in (T_MOUSE_X, T_MOUSE_Y, T_WHEEL) and press:
        b0 |= 0x80
    return bytes([b0, (d >> 8) & 0xFF, d & 0xFF, usage & 0xFF])


def key_press(name: str, delay_ms: int = 0) -> bytes:
    return step(_usage(name), delay_ms, True)


def key_release(name: str, delay_ms: int = 0) -> bytes:
    return step(_usage(name), delay_ms, False)


def tap(name: str, delay_ms: int = 0) -> bytes:
    """按下 + 0ms 释放两步。"""
    return key_press(name, delay_ms) + key_release(name)


def chord(keys: list[str], hold_ms: int = 0) -> bytes:
    """组合键：依次按下修饰键和主键，再逆序释放。"""
    out = b""
    for i, k in enumerate(keys):
        out += key_press(k, 0 if i else 0)
    out += key_release(keys[-1], hold_ms)
    for k in reversed(keys[:-1]):
        out += key_release(k, 0)
    return out


def _usage(name: str) -> int:
    n = name.lower().replace(" ", "")
    if n in KEYS:
        return KEYS[n]
    if len(n) == 1 and n.isalnum():
        if n in KEYS:
            return KEYS[n]
    raise MacroError(f"不认识的键: {name}")


def decode_step(b: bytes) -> dict:
    if len(b) < 4:
        raise MacroError("步骤长度不足")
    kind = (b[0] >> 4) & 0x07
    delay = ((b[0] & 0x0F) << 16) | (b[1] << 8) | b[2]
    return {"kind": kind, "delay": delay, "press": bool(b[0] & 0x80), "usage": b[3],
            "name": USAGE_NAME.get(b[3], f"0x{b[3]:02X}")}


def build_bank(macros: list[tuple[str, bytes]]) -> bytes:
    """macros: [(名字, 步骤字节串)] → 宏库字节串（含目录）。"""
    if not macros:
        return b""
    head = len(macros) * 4
    dirs, recs, off = [], bytearray(), head
    for name, steps in macros:
        if len(steps) % 4:
            raise MacroError("步骤必须是 4 的倍数")
        raw = name[:NAME_MAX].encode("utf-16le")
        rec = bytes([len(raw)]) + raw + steps
        dirs.append((off, len(rec)))
        recs += rec
        off += len(rec)
    blob = bytearray()
    for o, l in dirs:
        blob += struct.pack("<HH", o, l)
    blob += recs
    if len(blob) > MAX_BANK:
        raise MacroError(f"宏库超过上限 {MAX_BANK} 字节")
    return bytes(blob)


def parse_bank(blob: bytes) -> list[dict]:
    """解析宏库（读回校验 / 导入导出用）。"""
    out = []
    if len(blob) < 4:
        return out
    # 目录项连续，直到 offset/len 不合理为止
    i = 0
    while i + 4 <= len(blob):
        off, ln = struct.unpack("<HH", blob[i:i + 4])
        if off < 4 or ln == 0 or off + ln > len(blob) or ln > len(blob):
            break
        rec = blob[off:off + ln]
        nlen = rec[0]
        name = rec[1:1 + nlen].decode("utf-16le", "ignore")
        steps = [decode_step(rec[j:j + 4]) for j in range(1 + nlen, len(rec) - 3, 4)]
        out.append({"name": name, "bytes": ln, "steps": steps,
                    "raw": rec[1 + nlen:]})
        i += 4
        if i >= off:
            break
    return out
