"""AULA F99 Pro 控制核心：设备发现、电量、灯效配置。

协议来源（已在本机实测）：
  有线  VID 258a:010c，report id 6，520 字节特性报告（8 字节头 + 512 数据）
  2.4G  VID 3554:fa09，report id 0x13，20 字节中断报告
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass

import hid

from core.macro import build_bank, parse_bank

VID_WIRED, PID_WIRED = 0x258A, 0x010C
VID_WIRELESS, PID_WIRELESS = 0x3554, 0xFA09

PAGE_LEN = 128
HEADER = 8
REPORT_LEN = 520
WIRELESS_LEN = 20
MATRIX_LEN = 512      # 4 字节 × MatrixLen(128)
MACRO_MAX = 0x2800    # 10240

# 单键 RGB：固定 126 槽，每槽 3 字节。设备传输时按「平面」排列
# （R 平面 126B + G 平面 126B + B 平面 126B），不是按槽交错的 RGBRGB…
# 这点搞错会让整块颜色乱套，所以读写都统一走 rgb_to_planes / rgb_from_planes。
RGB_SLOTS = 126


def rgb_to_planes(slots: list[tuple[int, int, int]]) -> bytes:
    """[ (r,g,b) × 126 ] → 平面字节（R×126 + G×126 + B×126）。"""
    if len(slots) != RGB_SLOTS:
        raise AulaError(f"RGB 表必须是 {RGB_SLOTS} 槽")
    out = bytearray(RGB_SLOTS * 3)
    for i, (r, g, b) in enumerate(slots):
        out[i] = max(0, min(255, int(r)))
        out[RGB_SLOTS + i] = max(0, min(255, int(g)))
        out[RGB_SLOTS * 2 + i] = max(0, min(255, int(b)))
    return bytes(out)


def rgb_from_planes(data: bytes) -> list[tuple[int, int, int]]:
    """平面字节 → [ (r,g,b) × 126 ]。"""
    if len(data) < RGB_SLOTS * 3:
        raise AulaError(f"RGB 平面长度不足（{len(data)}）")
    return [
        (data[i], data[RGB_SLOTS + i], data[RGB_SLOTS * 2 + i])
        for i in range(RGB_SLOTS)
    ]


# ── 旋钮（模式键）───────────────────────────────────────────────────────
# 矩阵第 0 层 idx108 就是右侧那颗旋钮，工厂值 `07 00 00 1d`（b0=7 内建组 / b3=0x1D）。
# 2026-10-22 实测：这条记录**完全可写**，写 00/普通键/Consumer 都能原样读回，
# 所以旋钮的动作可以由本软件指定，不必受固件默认行为约束。
# 注意 idx108 在 keymap.json 里没有几何坐标（KB.ini 里 K100 的四个 0），
# 改键页画不出它，只能用「旋钮」独立卡片来编辑。
KNOB_IDX = 108
KNOB_LAYER = 0
KNOB_FACTORY = bytes.fromhex("0700001d")

# 上游 Linux 驱动（AulaSofwareLinux/src/worker.rs）只允许这 8 种旋钮动作，
# 说明它们是实测能用的；其余写法设备会收下（我们验过读回一致），但旋钮
# 转动时到底报不报得出来没人验证过 —— UI 要把这个区别说清楚。
KNOB_VERIFIED = [
    ("播放 / 暂停", {"type": "media", "name": "播放/暂停"}),
    ("静音", {"type": "media", "name": "静音"}),
    ("下一曲", {"type": "media", "name": "下一曲"}),
    ("上一曲", {"type": "media", "name": "上一曲"}),
    ("停止", {"type": "media", "name": "停止"}),
    ("音量 +", {"type": "media", "name": "音量+"}),
    ("音量 −", {"type": "media", "name": "音量-"}),
]
KNOB_VERIFIED_RECORDS = [
    bytes.fromhex("0700001d"),
    bytes.fromhex("020000cd"),
    bytes.fromhex("020000e2"),
    bytes.fromhex("020000b5"),
    bytes.fromhex("020000b6"),
    bytes.fromhex("020000b7"),
    bytes.fromhex("020000e9"),
    bytes.fromhex("020000ea"),
]


def is_knob_record(rec: bytes) -> bool:
    """判断某条 4 字节记录是不是「旋钮标记」（b0=7 / b3=0x1D）。"""
    return len(rec) >= 4 and rec[0] == 7 and rec[3] == 0x1D

# 灯效槽位：N = 配置页 0x0A 里存的编号，也是 KB.ini 的 LedOptN 序号；
# 第二个值是硬件效果号（LedOptN 的第 2 个数字）。
EFFECTS = [
    (1, 1), (2, 3), (3, 2), (4, 19), (5, 15), (6, 13), (7, 20), (8, 16),
    (9, 18), (10, 5), (11, 7), (12, 17), (13, 12), (14, 8), (15, 28),
    (16, 30), (17, 14), (18, 29), (19, 0),
]

# 灯效名直接照抄原版 text.xml 的 tc_kb_led1..20（按 LedOptN 序号）
EFFECT_NAMES = {
    # 原版灯效列表最后一项 tc_kb_led20「关闭」，厂商把硬件灯效号 0 当关闭。
    # LedOpt20=0,0,0,0,0,0,0 —— 全零，就是这个意思。
    0: "关闭",
    1: "常亮", 2: "呼吸", 3: "梦幻彩虹", 4: "一触即发", 5: "雨中漫步",
    6: "彩虹轮盘", 7: "涟漪扩散", 8: "繁星点点", 9: "踏雪无痕", 10: "川流不息",
    11: "随波逐流", 12: "如影随形", 13: "正弦光波", 14: "左右扫描", 15: "旋转风车",
    16: "七彩瀑布", 17: "花开富贵", 18: "旋转风暴", 19: "自定义",
}
KNOWN_NAMES = EFFECT_NAMES

COLORS = ["红", "绿", "蓝", "黄", "品红", "青", "白", "随机"]
COLOR_HEX = ["#ff3b30", "#34c759", "#0a84ff", "#ffd60a", "#ff2d92", "#32d5f5", "#f2f5fa"]

SLEEP_UI = [60, 90, 120, 180, 240, 300, 600, 900, 1200]
SLEEP_HW = [2, 3, 4, 6, 8, 10, 20, 30, 40]

# 配置页偏移
OFF_EFFECT, OFF_CUSTOM, OFF_SLEEP, OFF_TABLE = 0x0A, 0x09, 0x18, 0x3A
OFF_CUSTOM_BRIGHT = 0x75

# 亮度是配置页里独立的整字节，速度是同一槽下一字节的高半字节。
# 实测（tmp/gran_probe.py，真机有线）：固件对 0..255 的亮度、0..15 的速度
# 全部原样接受，读回一字不差，没有任何回夹。
# 真实刻度看固件自己的用法：出场配置页 19 个槽的亮度只取 [4, 5, 7, 9]，
# 自定义亮度 0x75 也是 9 —— 固件拿 0..9 当十档用。
# 官方 UI 把它压成 5 档（KB.ini Light=0,1,2,3,4）才是信息丢失的那一步，
# 老代码照抄 5 档又用 min(4, …) 读回，于是把出场的 9 显示成「满档 4」。
BRIGHT_MAX = 9
SPEED_MAX = 4      # 速度只有 0..4 是能看出区别的，0..15 是寄存器容量，别乱给

# 读回备份目录（改键/宏写入前自动保存）
# 打包后放 %LOCALAPPDATA%，避免写进 Program Files 旁的 _internal
def _backup_dir() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AULA-Hub")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "backup")


BACKUP_DIR = _backup_dir()

# HID usage → 中文键名（宏步骤 / 矩阵记录显示用）
_HID_CN = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F", 0x0A: "G",
    0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L", 0x10: "M", 0x11: "N",
    0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R", 0x16: "S", 0x17: "T", 0x18: "U",
    0x19: "V", 0x1A: "W", 0x1B: "X", 0x1C: "Y", 0x1D: "Z",
    0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4", 0x22: "5", 0x23: "6",
    0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space",
    0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";", 0x34: "'",
    0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/", 0x39: "Caps",
    0x3A: "F1", 0x3B: "F2", 0x3C: "F3", 0x3D: "F4", 0x3E: "F5", 0x3F: "F6",
    0x40: "F7", 0x41: "F8", 0x42: "F9", 0x43: "F10", 0x44: "F11", 0x45: "F12",
    0x46: "PrtSc", 0x47: "ScrLk", 0x48: "Pause", 0x49: "Insert", 0x4A: "Home",
    0x68: "F13", 0x69: "F14", 0x6A: "F15", 0x6B: "F16", 0x6C: "F17",
    0x6D: "F18", 0x6E: "F19", 0x6F: "F20", 0x70: "F21", 0x71: "F22",
    0x72: "F23", 0x73: "F24",
    0x4B: "PgUp", 0x4C: "Del", 0x4D: "End", 0x4E: "PgDn",
    0x4F: "→", 0x50: "←", 0x51: "↓", 0x52: "↑",
    0x53: "NumLk", 0x54: "KP/", 0x55: "KP*", 0x56: "KP-", 0x57: "KP+",
    0x58: "KPEnter", 0x59: "KP1", 0x5A: "KP2", 0x5B: "KP3", 0x5C: "KP4",
    0x5D: "KP5", 0x5E: "KP6", 0x5F: "KP7", 0x60: "KP8", 0x61: "KP9",
    0x62: "KP0", 0x63: "KP.", 0x64: "\\", 0x65: "Menu", 0x67: "KP=",
    0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin",
    0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt", 0xE7: "RWin",
}


def hid_label(usage: int) -> str:
    return _HID_CN.get(int(usage), f"0x{int(usage):02X}")

# ── 软件动作键 ──────────────────────────────────────────────────────
# 键盘固件只会「发一个按键给电脑」，切灯效 / 开关音乐律动这类事它自己做不到。
# 做法：把物理键在矩阵里改成 F13..F21（真键盘上不存在的虚拟键），
# 由 knob_router 的低级键盘钩子接住这几个 VK，执行真正的软件动作。
#
# 只能用 F13..F21：键盘 HID 描述符是 USAGE_MIN=0x04 / USAGE_MAX=0x70
# （实测 `05 01 09 06 a1 01 85 04 05 07 19 04 29 70 ...`），
# 0x70 就是 F21，再往上 F22..F24 固件发不出去。
SOFT_KEYS = [
    (0x68, 0x7C, "bright_up",    "键盘亮度 +"),
    (0x69, 0x7D, "bright_down",  "键盘亮度 −"),
    (0x6A, 0x7E, "speed_up",     "灯效速度 +"),
    (0x6B, 0x7F, "speed_down",   "灯效速度 −"),
    (0x6C, 0x80, "effect_next",  "下一个灯效"),
    (0x6D, 0x81, "effect_prev",  "上一个灯效"),
    (0x6E, 0x82, "custom_rgb",   "切到自定义灯效"),
    (0x6F, 0x83, "music_toggle", "音乐律动 开 / 关"),
]
SOFT_BY_USAGE = {u: (vk, act, lbl) for u, vk, act, lbl in SOFT_KEYS}
SOFT_BY_VK = {vk: (u, act, lbl) for u, vk, act, lbl in SOFT_KEYS}



# HID Consumer Page usage（矩阵 b0=2 记录，媒体/系统快捷）
_CONSUMER = {
    0x006F: "亮度+", 0x0070: "亮度-", 0x0071: "亮度自动",
    0x00E9: "音量+", 0x00EA: "音量-", 0x00E2: "静音",
    0x00B5: "下一曲", 0x00B6: "上一曲", 0x00CD: "播放/暂停", 0x00B7: "停止",
    0x00B3: "快进", 0x00B4: "快退",
    0x018A: "邮件", 0x0192: "计算器", 0x0194: "我的电脑",
    0x0196: "媒体播放器", 0x0197: "浏览器", 0x019C: "收藏夹",
    0x0221: "搜索", 0x0223: "主页", 0x0224: "后退", 0x0225: "前进",
    0x0226: "停止浏览", 0x0227: "刷新",
    0x0183: "配置工具", 0x0181: "新建", 0x0182: "打开",
    0x0184: "退出", 0x0185: "帮助", 0x0186: "文件",
    0x0187: "编辑", 0x0188: "查看", 0x0189: "打印",
    0x018B: "撤销", 0x018C: "剪切", 0x018D: "复制", 0x018E: "粘贴",
    0x018F: "全部选中", 0x0190: "查找", 0x0191: "另存为",
    0x0067: "寻找", 0x0068: "滚动上", 0x0069: "滚动下",
    0x00E0: "音量", 0x00E5: "音量恢复", 0x00E7: "静音(替代)",
    0x00B0: "播放", 0x00B1: "暂停", 0x00CF: "媒体选择",
}


def consumer_label(cu: int) -> str:
    return _CONSUMER.get(int(cu), f"媒体 0x{int(cu):04X}")


# 组合键：b1 = 修饰掩码，b3 = 主键 HID usage。
# 低 4 位是左侧修饰键，高 3 位是右侧修饰键——工厂矩阵实证：
# L0 idx4(LShift)=00 02..、idx5(LCtrl)=00 01..、idx11(LWin)=00 08..、idx17(LAlt)=00 04..、
# idx77(RCtrl)=00 10..、idx82(RShift)=00 20..、idx53(RAlt)=00 40..
_MOD_BITS = [
    (0x01, "Ctrl"), (0x02, "Shift"), (0x04, "Alt"), (0x08, "Win"),
    (0x10, "RCtrl"), (0x20, "RShift"), (0x40, "RAlt"),
]


def mods_label(mods: int) -> str:
    """修饰掩码 → 「Ctrl」「Ctrl+Shift」这类纯修饰键名（不带主键）。"""
    parts = [name for bit, name in _MOD_BITS if mods & bit]
    if not parts:
        if mods:
            return f"修饰 {mods:#04x}"
        return "?"
    return "+".join(parts)


def combo_label(mods: int, usage: int) -> str:
    parts = [name for bit, name in _MOD_BITS if mods & bit]
    if not parts and mods:
        parts.append(f"修饰 {mods:#04x}")
    parts.append(hid_label(usage) if usage else "?")
    return "+".join(parts)


# 常用命令：矩阵 b0=1 记录，b1 = 命令号（原版 text.xml 的 tc_cmd1..13）
# 注意 cmd10 在原版文案表里缺失，命令号保持原编号不动。
CMD_NAMES = {
    1: "关闭窗口", 2: "锁定计算机", 3: "运行命令", 4: "显示桌面",
    5: "放大", 6: "缩小", 7: "任务管理器", 8: "撤销", 9: "保存",
    11: "锁 Win 键", 12: "锁键盘", 13: "复位键盘",
}


def cmd_label(n: int) -> str:
    return CMD_NAMES.get(int(n), f"常用命令 #{int(n)}")


# 鼠标组：矩阵 b0=5 记录。b1 是按键掩码位。
# 依据：OemDrv.exe 记录编码器（VA 0x48d170 → 类别 8 分发表 0x48d79c）里
#   条目 tc_click(0x4c) → mov eax,0x105 → 记录字节 [05 01 00 00]
#   条目 tc_menu (0x4e) → mov eax,0x205 → 记录字节 [05 02 00 00]
#   条目 tc_middle(0x50)→ mov eax,0x405 → 记录字节 [05 04 00 00]
# 另 0xa6 条目走 `or eax,0x505` 且 (v-1)<<24 → [05 05 00 n]，
# 是「扩展鼠标功能」b1=5、b3=子功能号-1（前进/后退/双击/滚轮/倾向），子号次序未实测，暂不放出。
MOUSE_NAMES = {0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键"}
# 面板名的前两个字必须与解码名一致，这样键帽小字和弹窗里写的是同一句话
CAT_MOUSE = [(mask, name) for mask, name in sorted(MOUSE_NAMES.items())]


def mouse_label(mask: int) -> str:
    if mask in MOUSE_NAMES:
        return MOUSE_NAMES[mask]
    if mask == 0x05:
        return "鼠标扩展功能"
    return f"鼠标 {mask:#04x}"


# 键盘内建功能组 A：矩阵 b0=7 记录，b3 = 功能码。
# 这组由键盘固件自己执行（切通道 / 切系统模式 / 锁键 / 显示电量），
# 不会作为按键事件发给电脑，所以原版界面里也不给中文名。
_OEM7 = {
    0x01: "锁 Win 键",
    0x04: "恢复出厂设置",
    0x05: "蓝牙通道 1",
    0x06: "蓝牙通道 2",
    0x07: "蓝牙通道 3",
    0x08: "2.4G 通道",
    0x0A: "游戏模式",
    0x0C: "显示电量",
    0x18: "Windows 系统模式",
    0x19: "Android 系统模式",
    0x1A: "macOS 系统模式",
    0x1B: "Linux 系统模式",
    # 0x1D = 右侧旋钮 / 模式键。实证：L0 矩阵 idx108 = 07 00 00 1d，
    # KB.ini 里对应 `;Wheel` + `K100=0,0,0,0, 0x09,0x01,0x0700001D,108`（无几何坐标，
    # 所以改键页画不出它）。官方手册：长按切办公/游戏模式，短按「游戏=切灯效 / 办公=静音」，
    # 旋转「游戏=亮度 / 办公=音量」——这套行为写在固件里，矩阵记录只标记它是旋钮。
    0x1D: "旋钮 / 模式键",
}

# 键盘内建功能组 B：矩阵 b0=8 记录，格式 [8][类别][参数][参数]
_OEM8 = {
    (2, 0, 0): "切换灯效",
    (2, 0, 2): "切换灯效方向",
    (3, 1, 0): "键盘亮度 +",
    (3, 2, 0): "键盘亮度 -",
    (3, 0, 2): "灯效方向切换",
    (4, 1, 0): "灯效速度 +",
    (4, 2, 0): "灯效速度 -",
    (4, 0, 2): "灯效速度复位",
    (2, 0, 0): "切换灯效",
}


def oem_label(b0: int, b1: int, b2: int, b3: int) -> str:
    if b0 == 7:
        return _OEM7.get(b3, f"内建功能 7-{b3:02X}")
    return _OEM8.get((b1, b2, b3), f"内建功能 8-{b1}-{b2}-{b3}")


# 供 UI 选择器使用：媒体功能表（名 → Consumer usage）
MEDIA = {label: cu for cu, label in _CONSUMER.items()}
MEDIA.update({
    "播放": 0x00B0, "暂停": 0x00B1,
    "亮度+": 0x006F, "亮度-": 0x0070,
    "音量+": 0x00E9, "音量-": 0x00EA, "静音": 0x00E2,
})
BUILTINS = {label: code for code, label in _OEM7.items()}

# ---------- UI 快捷面板目录（原版「键盘/鼠标/多媒体/宏/常用命令/组合键」翻新版） ----------
# 媒体项直接从 _CONSUMER 取名字，保证「面板上写的」和「键帽小字解码出来的」完全一致
_MEDIA_ORDER = [
    0x00CD, 0x00B7, 0x00B6, 0x00B5, 0x00E9, 0x00EA, 0x00E2,
    0x006F, 0x0070, 0x018A, 0x0192, 0x0194,
    0x0197, 0x019C, 0x0196, 0x0223, 0x0221, 0x0227, 0x0224, 0x0225,
]
CAT_MEDIA = [(consumer_label(cu), cu) for cu in _MEDIA_ORDER]

CAT_COMBO = [
    (0x01, 0x06, "复制"), (0x01, 0x19, "粘贴"), (0x01, 0x1B, "剪切"),
    (0x01, 0x1D, "撤销"), (0x01, 0x04, "全选"), (0x01, 0x16, "保存"),
    (0x01, 0x09, "查找"), (0x01, 0x13, "打印"), (0x01, 0x17, "新标签"),
    (0x01, 0x1A, "关标签"), (0x04, 0x2B, "切窗口"), (0x04, 0x3D, "关闭窗口"),
    (0x08, 0x08, "资源管理器"), (0x08, 0x07, "显示桌面"), (0x08, 0x0F, "锁屏"),
    (0x08, 0x15, "运行"), (0x03, 0x29, "任务管理器"), (0x03, 0x11, "新建"),
    (0x01, 0x0B, "浏览器历史"),
]

# 分组 → 前端展示用：每项 {"label","action"}，action 直接喂给 set_key
def catalog() -> dict:
    """原版六个分类的翻新版目录：键盘 / 鼠标 / 多媒体 / 宏 / 常用命令 / 组合键。"""
    from core.macro import KEYS
    skip = {"lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt", "lwin", "rwin"}
    # 键帽印字（不是 VB 键名），顺序按键盘从左上到右下，便于找
    PRINT_NAME = {
        "minus": "-", "equal": "=", "lbracket": "[", "rbracket": "]",
        "backslash": "\\", "semicolon": ";", "quote": "'", "grave": "`",
        "comma": ",", "period": ".", "slash": "/", "capslock": "Caps",
        "printscreen": "PrtSc", "scrolllock": "ScrLk", "pageup": "PgUp",
        "pagedown": "PgDn", "delete": "Del", "insert": "Ins", "numlock": "NumLk",
        "enter": "Enter", "backspace": "Bksp", "space": "Space", "esc": "Esc",
        "tab": "Tab", "home": "Home", "end": "End", "up": "↑", "down": "↓",
        "left": "←", "right": "→", "pause": "Pause",
    }
    ORDER = (list("abcdefghijklmnopqrstuvwxyz") + list("0123456789")
             + ["esc", "tab", "capslock", "enter", "space", "backspace",
                "minus", "equal", "lbracket", "rbracket", "backslash",
                "semicolon", "quote", "grave", "comma", "period", "slash",
                "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
                "f11", "f12", "printscreen", "scrolllock", "pause", "insert",
                "home", "pageup", "delete", "end", "pagedown",
                "up", "down", "left", "right", "numlock"]
             + [f"kp{d}" for d in "0123456789"])
    seen = set()
    keys = []
    for n in ORDER:
        if n in seen or n not in KEYS or n in skip:
            continue
        seen.add(n)
        label = PRINT_NAME.get(n, n.upper() if len(n) == 1 else n.capitalize())
        keys.append({"label": label, "action": {"type": "key", "name": n}})
    media = [{"label": lbl, "action": {"type": "media", "usage": cu}}
             for lbl, cu in CAT_MEDIA]
    # 软件功能：键盘自己做不到、要由本软件执行的（切灯效、开关律动…）。
    # 写进矩阵的是一条普通按键记录（F13..F21），钩子接住后转成真正的动作。
    soft = [{"label": lbl, "action": {"type": "key", "usage": u}}
            for u, _vk, _act, lbl in SOFT_KEYS]
    combo = [{"label": f"{combo_label(m, u)} {desc}",
              "action": {"type": "combo", "mods": mods_of(m), "usage": u}}
             for m, u, desc in CAT_COMBO]
    cmds = [{"label": name, "action": {"type": "cmd", "cmd": n}}
            for n, name in sorted(CMD_NAMES.items())]
    oems = [{"label": name, "action": {"type": "oem", "grp": 7, "code": c}}
            for c, name in sorted(_OEM7.items())]
    oem8 = [{"label": name, "action": {"type": "oem", "grp": 8,
                                       "b1": k[0], "b2": k[1], "b3": k[2]}}
            for k, name in sorted(_OEM8.items())]
    mouse = [{"label": lbl, "action": {"type": "mouse", "mask": mask}}
             for mask, lbl in CAT_MOUSE]
    return {
        "key": keys,
        "mouse": mouse,
        "media": media,
        "combo": combo,
        "cmd": cmds,
        "oem": oems + oem8,
        "soft": soft,
        "mods": [{"label": lbl, "action": {"type": "mod", "mods": [k]}}
                 for k, lbl in (("ctrl", "Ctrl"), ("shift", "Shift"),
                                ("alt", "Alt"), ("win", "Win"))],
    }


def mods_of(mask: int) -> list[str]:
    out = []
    for bit, name in ((0x01, "ctrl"), (0x02, "shift"), (0x04, "alt"), (0x08, "win")):
        if mask & bit:
            out.append(name)
    return out

# 组合键修饰位（低 4 位左侧键，高 3 位右侧键）
_MOD_LOOKUP = {
    "ctrl": 0x01, "shift": 0x02, "alt": 0x04, "win": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40,
}


class AulaError(Exception):
    pass


def _resolve_keymap() -> str:
    """找 core/keymap.json 的真实位置。

    keymap.json 是**数据文件**，PyInstaller 不会把它打进 PYZ 归档，必须用
    --add-data 显式带上；漏了就会出现「开发态好、安装版改键页整页空白」。
    四个位置都试，命中即返回：
      1) 本文件同目录（开发态；打包后若 --add-data "core\\keymap.json;core" 也在这）
      2) _MEIPASS/core/keymap.json（PyInstaller 的 --add-data 落点）
      3) _MEIPASS/keymap.json（有人写成 --add-data "core\\keymap.json;."）
      4) 上一层的 core/keymap.json（数据被拷到 _internal 根时的兜底）
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "keymap.json")]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(os.path.join(meipass, "core", "keymap.json"))
        cands.append(os.path.join(meipass, "keymap.json"))
    cands.append(os.path.join(os.path.dirname(here), "core", "keymap.json"))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]                    # 都不在就报原位，报错信息里能看到找过哪


def _chk(page: bytes) -> bool:
    return len(page) >= PAGE_LEN and page[-2] == 0x5A and page[-1] == 0xA5


class WiredLink:
    """有线：520 字节特性报告。"""

    kind = "wired"
    report_id = 6

    def __init__(self, path):
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(1)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    def _set_feature(self, buf: bytearray):
        for _ in range(3):
            try:
                self.dev.send_feature_report(bytes(buf))
                return
            except Exception:
                time.sleep(0.07)
        raise AulaError("发送失败")

    def _get_feature(self, length: int):
        for _ in range(3):
            try:
                rep = self.dev.get_feature_report(self.report_id, REPORT_LEN)
                if rep:
                    return list(rep)
            except Exception:
                pass
            time.sleep(0.07)
        raise AulaError("读取失败")

    def read(self, code: int, param: int, length: int) -> bytes:
        """按 512 字节分片读（宏库最多 20 包）。"""
        total = max(1, (length + 511) // 512)
        out = bytearray()
        for i in range(total):
            want = min(512, length - i * 512)
            buf = bytearray(REPORT_LEN)
            buf[0] = self.report_id
            buf[1] = code
            buf[2] = param
            buf[4] = total
            buf[5] = i
            buf[6] = want & 0xFF
            buf[7] = (want >> 8) & 0xFF
            time.sleep(0.015)
            self._set_feature(buf)
            time.sleep(0.03)
            rep = self._get_feature(REPORT_LEN)
            if len(rep) < HEADER or rep[1] != code:
                raise AulaError("应答不匹配")
            got = rep[6] | (rep[7] << 8)
            if got == 0 or got > want or got > REPORT_LEN - HEADER:
                got = want
            out += bytes(rep[HEADER:HEADER + got])
            if len(out) >= length:
                break
        return bytes(out[:length])

    def write(self, code: int, param: int, data: bytes):
        """按 512 字节分片写；byte4=总包数、byte5=包序号。"""
        total = max(1, (len(data) + 511) // 512)
        for i in range(total):
            chunk = data[i * 512:(i + 1) * 512]
            buf = bytearray(REPORT_LEN)
            buf[0] = self.report_id
            buf[1] = code
            buf[2] = param
            buf[4] = total
            buf[5] = i
            buf[6] = len(chunk) & 0xFF
            buf[7] = (len(chunk) >> 8) & 0xFF
            buf[HEADER:HEADER + len(chunk)] = chunk
            time.sleep(0.015)
            self._set_feature(buf)
            time.sleep(0.05)

    # --- 业务 ---
    def password(self) -> bytes:
        return self.read(0x82, 0x01, 6)

    def power(self):
        d = self.read(0x87, 0x02, 2)
        return d[0], d[1]

    def read_page(self) -> bytes:
        page = self.read(0x84, 0, PAGE_LEN)
        if not _chk(page):
            raise AulaError("配置页校验失败")
        return page

    def write_page(self, page: bytes):
        if not _chk(page):
            raise AulaError("配置页缺少 5AA5 标记")
        self.write(0x04, 0, page)

    # --- 矩阵 / 宏（仅有线支持） ---
    def read_matrix(self, layer: int = 0) -> bytes:
        return self.read(0x83, layer & 3, MATRIX_LEN)

    def write_matrix(self, layer: int, data: bytes):
        if len(data) != MATRIX_LEN:
            raise AulaError(f"矩阵页必须是 {MATRIX_LEN} 字节")
        self.write(0x03, layer & 3, data)

    def read_macros(self, length: int = MACRO_MAX) -> bytes:
        return self.read(0x85, 0, length)

    def write_macros(self, blob: bytes):
        if len(blob) > MACRO_MAX:
            raise AulaError("宏库超长")
        self.write(0x05, 0, blob)

    # --- 单键 RGB（仅有线支持） ---
    def read_rgb(self) -> list[tuple[int, int, int]]:
        """读 126 槽 RGB 表。设备按「平面」传：R 126B + G 126B + B 126B。"""
        planes = self.read(0x86, 0, RGB_SLOTS * 3)
        if len(planes) < RGB_SLOTS * 3:
            raise AulaError(f"RGB 表长度不足（{len(planes)}）")
        return rgb_from_planes(planes)

    def write_rgb(self, slots: list[tuple[int, int, int]]):
        if len(slots) != RGB_SLOTS:
            raise AulaError(f"RGB 表必须是 {RGB_SLOTS} 槽")
        self.write(0x06, 0, rgb_to_planes(slots))


class WirelessLink:
    """2.4G 接收器：20 字节中断报告。"""

    kind = "wireless"

    def __init__(self, path):
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(1)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    @staticmethod
    def _pkt(cmd, op, data, total, index):
        p = bytearray(WIRELESS_LEN)
        p[0] = 0x13
        p[1] = cmd
        p[2] = total
        p[3] = index
        p[4] = (op << 4) | len(data)
        p[5:5 + len(data)] = data
        p[19] = sum(p[:19]) & 0xFF
        return bytes(p)

    def _drain(self):
        while True:
            if not self.dev.read(WIRELESS_LEN, timeout_ms=30):
                return

    def read(self, cmd: int, timeout: float = 1.8) -> bytes:
        self._drain()
        self.dev.write(self._pkt(cmd, 0, b"", 1, 0))
        deadline = time.time() + timeout
        chunks = {}
        total = 0
        while time.time() < deadline:
            r = self.dev.read(WIRELESS_LEN, timeout_ms=300)
            if not r or len(r) < WIRELESS_LEN or r[0] != 0x13:
                continue
            if (r[1] & 0x7F) != (cmd & 0x7F) or r[19] != (sum(r[:19]) & 0xFF):
                continue
            if r[1] & 0x80:
                raise AulaError("设备拒绝该读取")
            total = r[2] or total
            idx = r[3]
            ln = r[4] & 0x0F
            chunks[idx] = bytes(r[5:5 + ln])
            if total and len(chunks) == total:
                return b"".join(chunks[i] for i in range(total))
        if chunks:
            return b"".join(chunks[i] for i in sorted(chunks))
        raise AulaError("无应答（键盘可能休眠）")

    def write(self, cmd: int, data: bytes):
        self._drain()
        total = max(1, (len(data) + 13) // 14)
        for i in range(total):
            chunk = data[i * 14:(i + 1) * 14]
            self.dev.write(self._pkt(cmd, 1, chunk, total, i))
            time.sleep(0.02)
        time.sleep(0.05)

    # --- 业务 ---
    def password(self) -> bytes:
        return self.read(0x05)[:6]

    def power(self):
        d = self.read(0x4A)
        if len(d) < 2:
            raise AulaError("电量数据异常")
        return d[0], d[1]

    def read_page(self) -> bytes:
        page = self.read(0x44)[:PAGE_LEN]
        if not _chk(page):
            raise AulaError("配置页校验失败")
        return page

    def write_page(self, page: bytes):
        if not _chk(page):
            raise AulaError("配置页缺少 5AA5 标记")
        self.write(0x04, page)


def _find_wired():
    for d in hid.enumerate(VID_WIRED, PID_WIRED):
        if d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0001:
            try:
                link = WiredLink(d["path"])
                link.password()          # 只有能应答的通道才是数据通道
                return link
            except Exception:
                try:
                    link.close()
                except Exception:
                    pass
    return None


def _find_wireless():
    for d in hid.enumerate(VID_WIRELESS, PID_WIRELESS):
        if d.get("usage_page") == 0xFF02 and d.get("usage") == 0x0002:
            try:
                return WirelessLink(d["path"])
            except Exception:
                pass
    return None


def effect_name(ui: int, hw: int) -> str:
    """配置页 0x0A 存的编号就是 LedOptN 序号，名字照抄原版文案表。"""
    n = int(ui)
    if n == 0:
        return "关闭"
    return EFFECT_NAMES.get(n, f"灯效 {n:02d}")


@dataclass
class Snapshot:
    connected: bool = False
    mode: str = "none"
    model: str = "AULA F99 Pro"
    firmware: int = 24
    battery: int = -1
    charging: bool = False
    charge_flag: int = 0
    effect: int = 1
    brightness: int = 4
    speed: int = 4
    color: int = 7
    sleep: int = 10
    updated: float = 0.0
    last_ok: float = 0.0
    error: str = ""


class Hub:
    _group = None          # 最近一次 backup_now() 的组号，见 _save()
    _baseline_done = False  # 首次连上时补过基线备份没有
    """线程安全的设备会话。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.link = None
        self.snap = Snapshot()
        self.page: bytes | None = None
        self.fails = 0
        self.last_full = 0.0
        # 轻量轮询：电量每 10s 一次，完整配置每 60s 重读，连续 3 次失败才判定断开
        self.poll_interval = 10.0
        self.full_interval = 60.0
        # 最近一次真正把配置页写进设备的时刻（time.time()）。长按旋钮的检测器
        # 靠它排除「自己刚写完」造成的假长按，见 core/knob_mode.py。
        self._last_write_at = 0.0

    # ---------- 连接 ----------
    def _connect(self):
        if self.link:
            return
        link = _find_wired()
        if link is None:
            link = _find_wireless()
        if link is None:
            raise AulaError("未找到设备")
        self.link = link
        self.page = None

    def _drop(self):
        if self.link:
            self.link.close()
        self.link = None
        self.page = None

    def refresh(self, force: bool = False) -> Snapshot:
        with self.lock:
            s = self.snap
            now = time.time()
            # 命中缓存：不打扰键盘，避免无线模式下频繁唤醒/掉线
            if s.connected and not force and s.last_ok and (now - s.last_ok) < self.poll_interval:
                return s
            try:
                self._connect()
                pct, flag = self.link.power()
                need_full = force or not s.connected or (now - self.last_full) > self.full_interval
                if need_full:
                    info = self.link.password()
                    page = self.link.read_page()
                    self.page = page
                    self.last_full = now
                    s.model = "AULA F99 Pro" if info[5] == 0x58 else "AULA F99 系列"
                    # 0 是「关闭」，不是无效值 —— 之前写 `or 1` 把关闭吃掉了
                    s.effect = page[OFF_EFFECT]
                    s.sleep = page[OFF_SLEEP]
                    if s.effect == 0:
                        # 灯效关闭：槽位数据没有意义，亮度/速度保持原值
                        pass
                    elif page[OFF_CUSTOM] == 1:
                        # 亮度是整字节，别再 & 0x0F —— 出场值就是 9，掩码会把 >15 的值抹掉
                        s.brightness = min(BRIGHT_MAX, page[OFF_CUSTOM_BRIGHT])
                        s.speed, s.color = SPEED_MAX, 7
                    else:
                        slot = max(0, s.effect - 1) * 2 + OFF_TABLE
                        if slot + 1 < PAGE_LEN - 2:
                            s.brightness = min(BRIGHT_MAX, page[slot])
                            packed = page[slot + 1]
                            s.speed = min(SPEED_MAX, (packed >> 4) & 0x0F)
                            s.color = packed & 0x0F
                s.connected = True
                s.mode = self.link.kind
                s.battery = pct if 0 <= pct <= 100 else s.battery
                # 电量应答第二字节是状态位：实测有线供电时 0x10，2.4G 电池供电时 0x01。
                # 只有 bit4 视为“正在充电”，避免拔线时误报充电中。
                s.charging = bool(flag & 0x10)
                s.charge_flag = flag
                s.updated = now
                s.last_ok = now
                s.error = ""
                self.fails = 0
                self._maybe_baseline()
            except Exception as e:
                self.fails += 1
                s.error = str(e) or e.__class__.__name__
                # 单次抖动不改变连接状态，避免 UI 闪“等待连接”
                if self.fails >= 3:
                    s.connected = False
                    s.mode = "none"
                    self._drop()
            return s

    # ---------- 矩阵 / 宏 ----------
    def _wired(self) -> "WiredLink":
        """真正需要「写」改键/宏时才用：2.4G 写不进去。"""
        self._connect()
        if not isinstance(self.link, WiredLink):
            raise AulaError(
                "2.4G 只能读不能写：改键 / 宏 / 逐键颜色要插数据线")
        return self.link

    def _save(self, tag: str, data: bytes, group: str = "") -> str:
        """写一份备份，返回路径。

        文件名带秒级时间戳，同一秒里连存两次会**互相覆盖**（实测 backup_now()
        连跑两次只多出 1 个文件）。所以 restore 一律按 mtime 排序，别信文件名。

        group 是给「整组备份」用的：同一次 backup_now() 写出的 5 个文件共用
        一个 group 串，还原时才能保证四层矩阵 + 宏库来自同一时刻。
        """
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{tag}-{stamp}.bin"
        if group:
            name = f"{tag}-{stamp}-{group}.bin"
        path = os.path.join(BACKUP_DIR, name)
        # 同秒重名时加 _2 _3，宁可多一个文件也别把上一份悄悄盖掉
        n = 2
        while os.path.exists(path):
            name = f"{tag}-{stamp}-{n}.bin" if not group else f"{tag}-{stamp}-{group}-{n}.bin"
            path = os.path.join(BACKUP_DIR, name)
            n += 1
        with open(path, "wb") as f:
            f.write(data)
        return path

    # ---------- 备份检索 ----------
    @staticmethod
    def _pick_backup(pattern: str, before: bool = False):
        """按 mtime 取最新一个匹配的文件，找不到返回 None。

        before=True 取「改动前快照」（文件名含 -before-），False 取正常读回快照。
        这条区分是关键：以前用 sorted()[-1] 会把 -before- 那份当成最新的，
        还原按钮实际回退了两次改动。
        """
        import glob as _g
        cands = []
        for p in _g.glob(os.path.join(BACKUP_DIR, pattern)):
            is_before = "-before-" in os.path.basename(p)
            if is_before != before:
                continue
            try:
                cands.append((os.path.getmtime(p), p))
            except OSError:
                continue
        if not cands:
            return None
        cands.sort()
        return cands[-1][1]

    @staticmethod
    def _latest_group():
        """找出最近一次 backup_now() 写的 group 串，找不到返回 None。

        老版本（2026-10-22 之前）的 snapshot 是 `snapshot-L0-<stamp>.bin` 这种
        不带 group 的命名，且四层与宏库分属两个不同的秒 —— 那种只能尽力凑。
        """
        import glob as _g
        best = None
        for p in _g.glob(os.path.join(BACKUP_DIR, "snapshot-L0-*.bin")):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if best is None or mt > best[0]:
                m = re.search(r"-\d{8}-\d{6}-([0-9a-f]{6})\.bin$", os.path.basename(p))
                best = (mt, m.group(1) if m else None)
        return best[1] if best else None

    def read_matrix(self, layer: int = 0, save: bool = True) -> bytes:
        """读一层矩阵。

        有线返回整页 512B。2.4G 只有 `0x41` 一条读命令，返回前 504B
        （126 键，缺最后 8 字节），而且选不了层 —— 拿到的永远是第 0 层。
        """
        with self.lock:
            self._connect()
            link = self.link
            if isinstance(link, WiredLink):
                data = link.read_matrix(layer)
            else:
                if layer not in (0,):
                    raise AulaError("2.4G 只能读第 0 层矩阵，其他层要插数据线")
                data = link.read(0x41)
            if save:
                self._save(f"matrix-L{layer}", data)
            return data

    def write_matrix(self, layer: int, data: bytes):
        """整页替换：先备份，写后读回比对，不一致就恢复。

        2.4G 写不进去（`0x03` 在这条通道上无效，实测所有 cmd×op 组合都不生效），
        所以这里先要有线链路，报错才说得清原因。
        """
        with self.lock:
            link = self._wired()
            old = link.read_matrix(layer)
            self._save(f"matrix-L{layer}-before", old)
            link.write_matrix(layer, data)
            time.sleep(0.1)
            back = link.read_matrix(layer)
            if back != data:
                try:
                    link.write_matrix(layer, old)
                except Exception:
                    pass
                raise AulaError("矩阵写入未被接受，已恢复原值")
            return True

    def read_macros(self, save: bool = True) -> bytes:
        with self.lock:
            data = self._wired().read_macros()
            if save:
                self._save("macro", data)
            return data

    def write_macros(self, blob: bytes):
        with self.lock:
            link = self._wired()
            old = link.read_macros()
            self._save("macro-before", old)
            link.write_macros(blob)
            time.sleep(0.1)
            back = link.read_macros(len(blob))
            if back[:len(blob)] != blob:
                try:
                    link.write_macros(old)
                except Exception:
                    pass
                raise AulaError("宏库写入未被接受，已恢复原值")
            return True

    def bind_macro(self, layer: int, key_index: int, slot: int):
        """把某层的第 key_index 个键绑到宏槽 slot（1 起）。"""
        page = bytearray(self.read_matrix(layer))
        at = key_index * 4
        page[at] = 0x03
        page[at + 1] = 0x01
        page[at + 2] = 0x01
        page[at + 3] = (slot - 1) & 0xFF
        self.write_matrix(layer, bytes(page))

    # ---------- 改键 / 宏（UI 用） ----------
    KEYMAP_PATH = ""               # 类加载后由 _resolve_keymap() 覆盖

    def _keymap(self) -> dict:
        if not hasattr(self, "_km"):
            path = _resolve_keymap()
            try:
                with open(path, encoding="utf-8") as f:
                    self._km = json.load(f)
            except OSError as exc:
                # 缺这个文件不该让「改键页」整页空白并弹红条：
                # 报一句清楚的错，其他功能照常。
                raise AulaError(
                    f"读不到键盘布局数据（{os.path.basename(path)}）：{exc}") from exc
        return self._km

    @staticmethod
    def _decode_rec(b4: bytes) -> dict:
        """矩阵 4 字节记录 → 人类可读。

        统一格式 [b0][b1][b2][b3]，b0 决定记录类型：
          b0=0  b1=修饰掩码, b3=HID usage → 普通键(b1=0) / 组合键(b1≠0) / 纯修饰键(b3=0)
          b0=1  b1=常用命令号（见 CMD_NAMES）
          b0=2  Consumer usage = (b2<<8)|b3 → 多媒体 / 系统快捷
          b0=3  b1=触发模式, b3=宏槽位-1
          b0=5  b1=鼠标按键掩码（1 左 / 2 右 / 4 中）
          b0=7  b3=键盘内建功能码（切通道、切系统、锁 Win…），不进系统
          b0=8  [8][类别][参数][参数] 键盘内建功能（切灯效、亮度±、速度±）
        """
        b0, b1, b2, b3 = b4
        if b4 == b"\x00\x00\x5a\xa5":
            return {"kind": "sentinel"}
        if b0 == 0:
            # [0][修饰掩码][0][HID usage]：b3=0 表示纯修饰键，b1=0 表示普通键
            if b3:
                if b1:
                    return {"kind": "combo", "mods": b1, "usage": b3,
                            "name": combo_label(b1, b3)}
                soft = SOFT_BY_USAGE.get(b3)
                if soft:
                    # 这条记录是「软件动作键」：键帽上要写它真正干的事，
                    # 不能只写 F13——用户按下去看不到 F13 这个名字。
                    return {"kind": "soft", "usage": b3, "action": soft[1],
                            "name": soft[2]}
                return {"kind": "key", "usage": b3, "name": hid_label(b3)}
            if b1:
                return {"kind": "mod", "mods": b1, "name": mods_label(b1)}
            return {"kind": "empty"}
        if b0 == 1:
            return {"kind": "cmd", "cmd": b1, "p2": b2, "p3": b3,
                    "name": cmd_label(b1)}
        if b0 == 2:
            cu = (b2 << 8) | b3
            return {"kind": "media", "usage": cu, "name": consumer_label(cu)}
        if b0 == 3:
            return {"kind": "macro", "slot": b3 + 1, "mode": b1, "param": b2,
                    "name": f"宏 {b3 + 1}"}
        if b0 == 5:
            return {"kind": "mouse", "mask": b1, "p2": b2, "p3": b3,
                    "name": mouse_label(b1)}
        if b0 == 0x0D:
            # Fn 键本体：工厂矩阵 L0/L1 idx59 都是 `0d 00 00 00`。
            # 这不是可改键记录，是键盘固件用来标记 Fn 位置的占位符。
            return {"kind": "fn", "name": "Fn 键"}
        if b0 in (7, 8):
            return {"kind": "oem", "grp": b0, "code": b3,
                    "name": oem_label(b0, b1, b2, b3)}
        return {"kind": "special", "raw": b4.hex(" ")}

    def get_keymap(self) -> dict:
        """只返回键位几何（idx → {x,y,w,h,label}），不碰设备，用于单键调色页。"""
        out = {}
        for idx_str, geo in self._keymap().items():
            out[idx_str] = dict(geo)
        return {"keys": out, "slots": RGB_SLOTS}

    def get_bindings(self) -> dict:
        """读 4 层矩阵 + 解码 + 键位几何。"""
        with self.lock:
            self._connect()
            layers = {}
            wireless = not isinstance(self.link, WiredLink)
            # 2.4G 只有一条 0x41，选不了层，拿到的永远是第 0 层
            for layer in (range(1) if wireless else range(4)):
                page = self.read_matrix(layer, save=False)
                self._save(f"matrix-L{layer}", page)
                keys = {}
                for idx_str, geo in self._keymap().items():
                    idx = int(idx_str)
                    keys[idx_str] = {**geo, "rec": self._decode_rec(page[idx * 4:idx * 4 + 4])}
                layers[layer] = keys
            return {"layers": layers}

    def set_key(self, layer: int, key_index: int, action: dict):
        """action 的 type 取值：
        clear          清空（恢复该键为此层默认无功能）
        key            单键，usage=HID usage 或 name
        combo          组合键，mods=["ctrl","shift",...]，usage=HID usage 或 name
        mod            纯修饰键，mods=[...]
        media          多媒体/系统，usage=Consumer usage 或 name
        cmd            常用命令，cmd=1..13
        macro          绑定宏，slot=1..34
        mouse          鼠标按键，mask=1/2/4（左/右/中）
        oem            键盘内建功能，grp=7/8，code / b1,b2,b3
        """
        t = action.get("type")
        if t == "key":
            usage = self._usage_of(action) & 0xFF
            rec = bytes([0, 0, 0, usage])
        elif t == "combo":
            usage = self._usage_of(action) & 0xFF
            mods = self._mods_mask(action.get("mods"))
            rec = bytes([0, mods, 0, usage])
        elif t == "mod":
            mods = self._mods_mask(action.get("mods"))
            rec = bytes([0, mods, 0, 0])
        elif t == "macro":
            rec = bytes([3, 1, 1, (int(action["slot"]) - 1) & 0xFF])
        elif t == "clear":
            rec = bytes(4)
        elif t == "media":
            cu = self._usage_of(action, table=MEDIA)
            rec = bytes([2, 0, (cu >> 8) & 0xFF, cu & 0xFF])
        elif t == "cmd":
            n = int(action.get("cmd", action.get("code", 0)))
            if n not in CMD_NAMES:
                raise AulaError(f"常用命令号无效: {n}")
            rec = bytes([1, n & 0xFF, 0, 0])
        elif t == "mouse":
            mask = int(action.get("mask", 1)) & 0xFF
            rec = bytes([5, mask, 0, 0])
        elif t == "oem":
            grp = int(action.get("grp", 7))
            if grp == 7:
                rec = bytes([7, 0, 0, int(action.get("code", 0)) & 0xFF])
            else:
                rec = bytes([8, int(action.get("b1", 0)) & 0xFF,
                             int(action.get("b2", 0)) & 0xFF,
                             int(action.get("b3", 0)) & 0xFF])
        else:
            raise AulaError(f"未知动作: {action}")
        with self.lock:
            link = self._wired()
            page = bytearray(link.read_matrix(layer))
            page[key_index * 4:key_index * 4 + 4] = rec
            self.write_matrix(layer, bytes(page))
        return True

    @staticmethod
    def _mods_mask(mods) -> int:
        """把修饰键描述折成掩码。

        面板给的是 `["ctrl","shift"]`，但内部（如旋钮快捷项）也会直接传
        已算好的整数掩码；两种都要认，否则 `for m in 0x03` 会 TypeError。
        """
        if mods is None:
            return 0
        if isinstance(mods, int):
            return mods & 0xFF
        mask = 0
        for m in mods:
            mask |= _MOD_LOOKUP.get(str(m).lower(), 0)
        return mask & 0xFF

    @staticmethod
    def _usage_of(action: dict, table: dict = None) -> int:
        if "usage" in action:
            return int(action["usage"])
        from core.macro import KEYS
        n = str(action["name"]).lower().strip()
        src = table or KEYS
        if n in src:
            return src[n]
        raise AulaError(f"不认识的功能: {action['name']}")

    def get_macros(self) -> list[dict]:
        with self.lock:
            bank = self._wired().read_macros()
        out = []
        for i, m in enumerate(parse_bank(bank)):
            out.append({
                "slot": i + 1,
                "name": m["name"],
                "steps": [{"kind": s["kind"], "delay": s["delay"], "press": s["press"],
                           "usage": s["usage"], "name": hid_label(s["usage"])} for s in m["steps"]],
            })
        return out

    def save_macros(self, macros: list[dict]):
        """整库替换。macros: [{name, steps:[{name,delay,press?}]}]，press 缺省为 tap。"""
        from core.macro import key_press, key_release, MacroError
        items = []
        for m in macros:
            steps = b""
            for s in m["steps"]:
                d = int(s.get("delay", 0))
                if s.get("press", True):
                    steps += key_press(s["name"], d)
                else:
                    steps += key_release(s["name"], d)
            items.append((m["name"], steps))
        blob = build_bank(items)
        with self.lock:
            link = self._wired()
            old = link.read_macros()
            self._save("macro-before", old)
            link.write_macros(blob)
            time.sleep(0.1)
            back = link.read_macros()
            if back[:len(blob)] != blob:
                try:
                    link.write_macros(old)
                except Exception:
                    pass
                raise AulaError("宏库写入未被接受，已恢复原值")
        return True

    def backup_now(self) -> int:
        """把当前设备状态整组存一份，返回写出的文件数。

        一组 = 四层矩阵 + 宏库 + 配置页 + RGB 表，全部打同一个 group 串，
        还原时才能保证「退回到同一时刻」，而不是四层来自不同时间拼出来的怪物。
        """
        with self.lock:
            link = self._wired()
            group = "%06x" % (int(time.time() * 1000) & 0xFFFFFF)
            n = 0
            for layer in range(4):
                self._save(f"snapshot-L{layer}", link.read_matrix(layer), group)
                n += 1
            self._save("snapshot-macro", link.read_macros(), group)
            n += 1
            try:
                self._save("snapshot-page", link.read_page(), group)
                n += 1
            except Exception:
                pass
            try:
                self._save("snapshot-rgb", link.read(0x86, 0, RGB_SLOTS * 3), group)
                n += 1
            except Exception:
                pass
            self._group = group
            return n

    def latest_backup_info(self) -> dict:
        """看一眼「还原到最近一次备份」到底会拿回什么，给 UI 显示用。"""
        group = self._latest_group()
        out = {"group": group, "parts": [], "missing": []}
        for layer in range(4):
            pat = f"snapshot-L{layer}-*-{group}.bin" if group else f"snapshot-L{layer}-*.bin"
            p = self._pick_backup(pat)
            if p is None:
                # 老版本命名没有 group 段
                p = self._pick_backup(f"snapshot-L{layer}-*.bin")
            if p is None:
                # 再退一步：日常改键/还原留下的 matrix-L* 备份（不带 group）
                p = self._pick_backup(f"matrix-L{layer}-[0-9]*-*.bin")
            if p:
                out["parts"].append({"kind": f"L{layer}", "file": os.path.basename(p),
                                     "time": time.strftime("%m-%d %H:%M:%S",
                                                          time.localtime(os.path.getmtime(p)))})
            else:
                out["missing"].append(f"L{layer}")
        pat = f"snapshot-macro-*-{group}.bin" if group else "snapshot-macro-*.bin"
        p = self._pick_backup(pat) or self._pick_backup("snapshot-macro-*.bin")
        if p:
            out["parts"].append({"kind": "macro", "file": os.path.basename(p),
                                 "time": time.strftime("%m-%d %H:%M:%S",
                                                       time.localtime(os.path.getmtime(p)))})
        else:
            out["missing"].append("macro")
        # 配置页（灯效/亮度/速度/休眠）与 RGB 表：老备份里没有，不算缺失
        for kind, base in (("page", "snapshot-page"), ("rgb", "snapshot-rgb")):
            cand = ([f"{base}-*-{group}.bin"] if group else []) + [f"{base}-*.bin"]
            picked = None
            for pat2 in cand:
                picked = self._pick_backup(pat2)
                if picked:
                    break
            if picked:
                out["parts"].append({"kind": kind, "file": os.path.basename(picked),
                                     "time": time.strftime("%m-%d %H:%M:%S",
                                                           time.localtime(
                                                               os.path.getmtime(picked)))})
        return out

    def restore_all(self):
        """从 backup/ 里最新一组快照整组还原（应急按钮）。

        关键：按 mtime 取，且**排除 -before-** 文件。以前用 sorted()[-1]，
        文件名里 `-before-` 字典序比纯时间戳靠后，于是它总是被选中 ——
        点还原实际退回到了「上一次改键之前」，越还原越乱。
        """
        with self.lock:
            link = self._wired()
            group = self._latest_group()
            done = []

            def _one(kind: str, pats, apply):
                for pat in pats:
                    p = self._pick_backup(pat)
                    if p:
                        apply(open(p, "rb").read())
                        done.append(f"{kind}:{os.path.basename(p)}")
                        return True
                return False

            ok_macro = _one(
                "macro",
                [f"snapshot-macro-*-{group}.bin", "snapshot-macro-*.bin",
                 "macro-[0-9]*-*.bin"],
                link.write_macros,
            ) if group else _one(
                "macro",
                ["snapshot-macro-*.bin", "macro-[0-9]*-*.bin"],
                link.write_macros,
            )

            for layer in range(4):
                pats = ([f"snapshot-L{layer}-*-{group}.bin"] if group else [])
                pats += [f"snapshot-L{layer}-*.bin", f"matrix-L{layer}-[0-9]*-*.bin"]
                _one(f"L{layer}", pats,
                     lambda data, L=layer: link.write_matrix(L, data))

            page_pats = ([f"snapshot-page-*-{group}.bin"] if group else [])
            page_pats += ["snapshot-page-*.bin"]
            _one("page", page_pats, link.write_page)

            rgb_pats = ([f"snapshot-rgb-*-{group}.bin"] if group else [])
            rgb_pats += ["snapshot-rgb-*.bin"]
            _one("rgb", rgb_pats, lambda data: link.write(0x06, 0, data))

        self.snap.last_full = 0.0        # 失效缓存，下次 refresh 真读一遍
        return {"ok": True, "restored": done, "macro": ok_macro}

    def _maybe_baseline(self) -> None:
        """首次连上时补一份完整基线备份，只做一次。

        必须在**另一个线程**里跑：写 7 个文件要 1 秒多，refresh() 是每 5 秒
        被 UI 同步调的，卡在里面会让界面一直转圈。而且这一步失败不影响任何
        功能，纯属给用户留退路，所以异常一律吞掉。
        """
        if self._baseline_done:
            return
        if not isinstance(self.link, WiredLink):
            return                      # 2.4G 读不到宏库与矩阵，等插线再说
        self._baseline_done = True
        import glob as _g
        import threading as _th
        group = self._latest_group()
        if group:
            return                      # 已经有整组备份，不必再补

        def _run():
            try:
                self.backup_now()
            except Exception:
                pass

        _th.Thread(target=_run, name="aula-baseline", daemon=True).start()

    # ---------- 单键 RGB（仅有线） ----------
    def read_rgb(self, save: bool = True) -> list[tuple[int, int, int]]:
        """读 126 槽单键颜色表（设备按平面传：R126 + G126 + B126）。

        有线走 `0x86`，2.4G 走 `0x42`，两边都能读；写只有有线能做。
        """
        with self.lock:
            self._connect()
            link = self.link
            if isinstance(link, WiredLink):
                planes = link.read(0x86, 0, RGB_SLOTS * 3)
            else:
                planes = link.read(0x42)
            if len(planes) < RGB_SLOTS * 3:
                raise AulaError(f"RGB 表长度不足（{len(planes)}）")
            if save:
                self._save("rgb-table", planes)
            return rgb_from_planes(planes)

    def write_rgb(self, slots: list[tuple[int, int, int]], verify: bool = True):
        """整表写入。默认读回校验，不一致抛错（不自动回滚，由调用方决定）。

        2.4G 写不进去（`0x06` 在这条通道上无效），先要有线链路。
        """
        with self.lock:
            link = self._wired()
            old = link.read(0x86, 0, RGB_SLOTS * 3)
            self._save("rgb-before", old)
            link.write(0x06, 0, rgb_to_planes(slots))
            if not verify:
                return True
            time.sleep(0.08)
            back = link.read(0x86, 0, RGB_SLOTS * 3)
            if back != rgb_to_planes(slots):
                try:
                    link.write(0x06, 0, old)
                except Exception:
                    pass
                raise AulaError("RGB 写入未被接受，已恢复原值")
            return True

    def set_key_color(self, slot: int, rgb: tuple[int, int, int]):
        """只改一个槽：读全表 → 改一槽 → 写回 → 校验。"""
        if not 0 <= int(slot) < RGB_SLOTS:
            raise AulaError(f"RGB 槽号超出范围 0..{RGB_SLOTS - 1}")
        with self.lock:
            table = self.read_rgb(save=False)
            r, g, b = (max(0, min(255, int(v))) for v in rgb)
            table[int(slot)] = (r, g, b)
            return self.write_rgb(table)

    def set_key_colors(self, mapping: dict[int, tuple[int, int, int]]):
        """一次改多个槽（批量上色 / 整行填充）。"""
        with self.lock:
            table = self.read_rgb(save=False)
            for slot, rgb in mapping.items():
                if 0 <= int(slot) < RGB_SLOTS:
                    r, g, b = (max(0, min(255, int(v))) for v in rgb)
                    table[int(slot)] = (r, g, b)
            return self.write_rgb(table)

    # ---------- 灯效 ----------
    def _read_page_retry(self, tries: int = 4, gap: float = 0.12) -> bytes:
        """读配置页，偶发「应答不匹配」时重试。

        连写十几次之后链路会短暂拥堵，一条 0x84 读不回来就抛异常会把
        整串操作打断（踩过：还原亮度那步直接崩，设备停在亮度 0）。
        读是幂等的，重试安全。
        """
        last = None
        for i in range(tries):
            try:
                return self.link.read_page()
            except AulaError as exc:
                last = exc
                if i < tries - 1:
                    time.sleep(gap)
        raise last

    def _patch(self, fn):
        with self.lock:
            self._connect()
            page = bytearray(self._read_page_retry())
            changed = fn(page)
            if changed:
                self.link.write_page(bytes(page))
                self._last_write_at = time.time()
                time.sleep(0.05)
                back = self._read_page_retry()
                if bytes(back) != bytes(page):
                    raise AulaError("写入未被设备接受")
                self.page = back
            self.snap.updated = time.time()

    def set_effect(self, ui: int):
        def fn(p):
            p[OFF_CUSTOM] = 0
            p[OFF_EFFECT] = max(1, min(19, int(ui)))
            self.snap.effect = p[OFF_EFFECT]
            return True
        self._patch(fn)

    def _slot(self) -> int:
        # 灯效 0（关闭）没有自己的槽，退回槽 0，避免把亮度写进看不见的地方
        eff = self.snap.effect or 1
        return max(0, min(18, eff - 1)) * 2 + OFF_TABLE

    def set_brightness(self, level: int):
        level = max(0, min(BRIGHT_MAX, int(round(level))))

        def fn(p):
            if p[OFF_CUSTOM] == 1:
                p[OFF_CUSTOM_BRIGHT] = level
            else:
                p[self._slot()] = level
            self.snap.brightness = level
            return True
        self._patch(fn)

    def _apply_fast(self, fn):
        """拖动预览用：不读页、不回读，只改缓存页再裸写一次。

        实测 _patch 一次 253 ms（读页 + 写页 + sleep 0.05 + 回读校验），
        而裸 write_page 只要 78 ms。拖动时每一帧都走 _patch 会把界面拖成幻灯片，
        所以拖动过程中的实时预览走这条快路，松手后再用 set_brightness() /
        set_speed() 做一次带校验的正式写入，把值钉死。
        """
        with self.lock:
            self._connect()
            page = bytearray(self.page if self.page is not None else self.link.read_page())
            if not fn(page):
                return
            self.link.write_page(bytes(page))
            self._last_write_at = time.time()
            self.page = bytes(page)
            self.snap.updated = time.time()

    def preview_brightness(self, level: int):
        level = max(0, min(BRIGHT_MAX, int(round(level))))

        def fn(p):
            if p[OFF_CUSTOM] == 1:
                p[OFF_CUSTOM_BRIGHT] = level
            else:
                p[self._slot()] = level
            self.snap.brightness = level
            return True
        self._apply_fast(fn)

    def preview_speed(self, level: int):
        level = max(0, min(SPEED_MAX, int(round(level))))

        def fn(p):
            at = self._slot() + 1
            p[at] = (level << 4) | (p[at] & 0x0F)
            self.snap.speed = level
            return True
        self._apply_fast(fn)

    def set_speed(self, level: int):
        level = max(0, min(SPEED_MAX, int(round(level))))

        def fn(p):
            at = self._slot() + 1
            p[at] = (level << 4) | (p[at] & 0x0F)
            self.snap.speed = level
            return True
        self._patch(fn)

    def set_color(self, idx: int):
        idx = max(0, min(7, int(idx)))

        def fn(p):
            at = self._slot() + 1
            p[at] = (p[at] & 0xF0) | idx
            self.snap.color = idx
            return True
        self._patch(fn)

    def set_sleep(self, ui_seconds: int):
        hw = SLEEP_HW[SLEEP_UI.index(int(ui_seconds))]

        def fn(p):
            p[OFF_SLEEP] = hw
            self.snap.sleep = hw
            return True
        self._patch(fn)

    def use_custom_rgb(self):
        """切到「自定义」灯效，并打开 custom 标志。

        单键颜色只在自定义模式生效，而且必须把配置页 0x09 置 1 ——
        只把 0x0A 改成 19 是不够的（实测那样子键盘仍按内置灯效跑）。
        """
        def fn(p):
            p[OFF_CUSTOM] = 1
            p[OFF_EFFECT] = 19
            self.snap.effect = 19
            return True
        self._patch(fn)

    # ── 旋钮（模式键 / idx108）────────────────────────────────────────
    def get_knob(self) -> dict:
        """读旋钮当前绑定的功能，附带它是不是出厂值。"""
        with self.lock:
            page = self.read_matrix(KNOB_LAYER, save=False)
        rec = page[KNOB_IDX * 4: KNOB_IDX * 4 + 4]
        return {
            "idx": KNOB_IDX,
            "record": rec.hex(" "),
            "action": self._decode_rec(rec),
            "factory": rec == KNOB_FACTORY,
            "factoryRecord": KNOB_FACTORY.hex(" "),
            "verified": bytes(rec) in KNOB_VERIFIED_RECORDS,
            "quick": [{"label": lbl, "action": act} for lbl, act in KNOB_VERIFIED],
        }

    def set_knob(self, action: dict):
        """把旋钮换成指定功能。

        写入路径与改键完全一致（同一条矩阵记录），所以直接用 set_key；
        但 idx108 没有几何坐标，改键页的键帽点不到它，必须走这条独立入口。

        注意：旋钮的「长按切办公/游戏模式」是固件行为，改这条记录只能改
        「按下 / 旋转时上报什么」，**长按切模式不会因此消失**（实证见 NOTES H 节）。
        """
        return self.set_key(KNOB_LAYER, KNOB_IDX, action)

    def reset_knob(self):
        """把旋钮恢复成出厂标记（07 00 00 1d）。"""
        with self.lock:
            link = self._wired()
            page = bytearray(link.read_matrix(KNOB_LAYER))
            page[KNOB_IDX * 4: KNOB_IDX * 4 + 4] = KNOB_FACTORY
            self.write_matrix(KNOB_LAYER, bytes(page))
        return True

    def state(self, force: bool = False) -> dict:
        s = self.refresh(force)
        return {
            "connected": s.connected,
            "mode": s.mode,
            "modeLabel": {"wired": "有线连接", "wireless": "2.4G 无线", "none": "未连接"}[s.mode],
            "model": s.model,
            "firmware": s.firmware,
            "battery": s.battery,
            "charging": s.charging,
            "chargeFlag": s.charge_flag,
            "effect": s.effect,
            "effectLabel": effect_name(s.effect, 0),
            "brightness": s.brightness,
            "speed": s.speed,
            "brightMax": BRIGHT_MAX,
            "speedMax": SPEED_MAX,
            "color": s.color,
            "sleep": s.sleep,
            "updated": s.updated,
            "age": (round(time.time() - s.last_ok, 1) if s.last_ok else -1),
            "fails": self.fails,
            "error": s.error,
            "effects": (
                [{"ui": ui, "hw": hw, "name": effect_name(ui, hw)}
                 for ui, hw in EFFECTS]
                + [{"ui": 0, "hw": 0, "name": "关闭"}]      # 原版 tc_kb_led20
            ),
            "colors": COLOR_HEX,
            "colorNames": COLORS,
            "sleepOptions": SLEEP_UI,
            "catalog": catalog(),
        }
