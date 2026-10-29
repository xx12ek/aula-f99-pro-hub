# -*- coding: utf-8 -*-
"""发布前功能自检：把每一项能力真机跑一遍，输出 PASS/FAIL 清单。

这是「保证所有功能正常」的凭证，也进 docs/SELF-CHECK.md。

修正记录：`password()` / `write_page()` 是 `WiredLink` 的方法，不是 `Hub` 的；
`get_keymap()` 返回 `{"keys": {...}, "slots": {...}}` 不是扁平 dict。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.aula import (EFFECTS, RGB_SLOTS, SLEEP_HW, SLEEP_UI, Hub, catalog)   # noqa: E402

OK = []
BAD = []


def chk(name, fn):
    try:
        got = fn()
        OK.append((name, got))
        print(f"PASS  {name}  {got}")
    except Exception as exc:                                   # noqa: BLE001
        BAD.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL  {name}  {type(exc).__name__}: {exc}")


hub = Hub()
snap = hub.refresh(force=True)
print(f"链路={snap.mode} 电量={snap.battery}% 灯效={snap.effect} "
      f"亮度={snap.brightness} 速度={snap.speed} 休眠={snap.sleep}")

with hub.lock:
    hub._connect()
    link = hub.link
    page0 = bytes(link.read_page())
effect0 = page0[0x0A]
bright0 = snap.brightness
speed0 = snap.speed
sleep0 = snap.sleep

# ---------- 只读能力 ----------
chk("身份/型号", lambda: link.password().hex() if link.kind == "wired" else "-")
chk("矩阵读取(512B)", lambda: f"{len(hub.read_matrix(0, save=False))}B")
chk("改键解码层数", lambda: f"{len(hub.get_bindings().get('layers', []))} 层")


def _km():
    km = hub.get_keymap()
    keys = km.get("keys", {}) if isinstance(km, dict) else {}
    return f"{len(keys)} 键"


chk("键位表", _km)
chk("灯效种类", lambda: f"{len(EFFECTS)} 项 + 关闭")
chk("休眠档位", lambda: len(SLEEP_UI))
chk("功能目录", lambda: " + ".join(f"{k}{len(v)}" for k, v in catalog().items()))
chk("逐键颜色读取", lambda: f"{len(hub.read_rgb(save=True))} 槽")
chk("旋钮记录", lambda: hub.get_knob().get("record"))
chk("配置页校验", lambda: "5AA5" if page0[-2:] == b"\x5a\xa5" else "BAD")
if link.kind == "wired":
    chk("宏库读取", lambda: f"{len(hub.read_macros())}B")


# ---------- 写能力（写完还原） ----------
def _effect():
    nxt = 7 if effect0 != 7 else 11
    hub.set_effect(nxt)
    got = hub.refresh(force=True).effect
    hub.set_effect(effect0)
    assert got == nxt, f"写 {nxt} 读回 {got}"
    return f"{effect0}->{nxt}->{effect0}"


def _bright():
    v = 5 if bright0 != 5 else 6
    hub.set_brightness(v)
    got = hub.refresh(force=True).brightness
    hub.set_brightness(bright0)
    assert got == v, f"写 {v} 读回 {got}"
    return f"{bright0}->{v}->{bright0}"


def _speed():
    v = 2 if speed0 != 2 else 3
    hub.set_speed(v)
    got = hub.refresh(force=True).speed
    hub.set_speed(speed0)
    assert got == v, f"写 {v} 读回 {got}"
    return f"{speed0}->{v}->{speed0}"


def _sleep():
    ui = SLEEP_UI[SLEEP_HW.index(sleep0)]
    alt = 300 if ui != 300 else 60
    hub.set_sleep(alt)
    hw = hub.refresh(force=True).sleep
    hub.set_sleep(ui)
    assert hw == SLEEP_HW[SLEEP_UI.index(alt)], f"写 {alt} 读回 HW={hw}"
    return f"{ui}s->{alt}s->{ui}s"


chk("灯效切换", _effect)
chk("键盘背光", _bright)
chk("灯效速度", _speed)
chk("自动休眠", _sleep)

if link.kind == "wired":
    def _page():
        with hub.lock:
            hub._connect()
            hub.link.write_page(page0)
        with hub.lock:
            back = bytes(hub.link.read_page())
        assert back == page0, "回读不一致"
        return "整页回写一致"

    chk("配置页回写", _page)

# ---------- 系统侧 ----------
from core import osd as _osd                                        # noqa: E402
from core import sysvol as _sysvol                                  # noqa: E402
from core.knob_router import ACTIONS, DEFAULT_SLOTS, NATIVE_ACTION    # noqa: E402

chk("系统音量", lambda: f"{_sysvol.volume_get()}%")
chk("静音状态", lambda: _sysvol.mute_get())
chk("屏幕亮度", lambda: _sysvol.screen_get())
chk("OSD 尺寸", lambda: f"{_osd.W_LOGICAL}x{_osd.H_LOGICAL} 逻辑")
chk("OSD 主题", lambda: "深色" if _osd.detect_dark() else "浅色")
chk("OSD 强调色", lambda: _osd.accent())
chk("旋钮动作表", lambda: len(ACTIONS))
chk("旋钮出厂槽", lambda: [s["label"] for s in DEFAULT_SLOTS])
chk("原生放行表", lambda: NATIVE_ACTION)

print()
print(f"=== PASS {len(OK)} / FAIL {len(BAD)} ===")
for n, r in BAD:
    print(f"  FAIL {n}: {r}")

s = hub.refresh(force=True)
print(f"还原后：effect={s.effect} bright={s.brightness} speed={s.speed} sleep={s.sleep}")
with hub.lock:
    hub._connect()
    page1 = bytes(hub.link.read_page())
print(f"配置页与开始时一致: {page1 == page0}")
hub.link and hub.link.close()
print("RESULT:", "ALL PASS" if not BAD else "HAS FAILURES")
