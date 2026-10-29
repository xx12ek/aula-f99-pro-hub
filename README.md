# AULA Hub

[![License: MIT](https://img.shields.io/badge/license-MIT-7c4dff.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-0ea5e9.svg)](#安装)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](requirements.txt)
[![Self-check](https://img.shields.io/badge/self--check-25%2F25%20PASS-22c55e.svg)](docs/SELF-CHECK.md)

**一个现代化、开源的狼蛛 AULA F99 Pro 键盘控制中心 —— 替代那个 2019 年风格、还不显示电量的官方驱动。**

> **直接下载**：[AULA-Hub-portable.zip](https://github.com/xx12ek/aula-f99-pro-hub/releases/latest) · 免安装、不需要 Python、不需要管理员权限

原厂驱动的问题：界面停留在 Windows 7 年代、三个型号配置里 `ShowPower=0` 导致**永远看不到电量**、改键要做一堆反人类的点击、没有命令行、没有 Linux 支持。

这个项目用**纯用户态 HID**（不装驱动、不改系统、不碰固件）重写了一遍，把原厂藏起来的能力全部挖出来并补齐。

---

## 界面

<table>
<tr>
<td width="50%"><img src="docs/screenshots/lighting.png" alt="灯光页"><br><b>灯光</b> — 20 种灯效、亮度/速度无极拖动、逐键调色、10 种音乐律动</td>
<td width="50%"><img src="docs/screenshots/remap.png" alt="改键页"><br><b>改键 / 宏</b> — 4 层、8 类功能、每颗键显示真实中文功能名</td>
</tr>
<tr>
<td><img src="docs/screenshots/settings.png" alt="设置页"><br><b>设置</b> — 设备信息、托盘行为、开机自启、备份还原</td>
<td><img src="docs/screenshots/misc.png" alt="杂项页"><br><b>杂项</b> — 配置导入导出、去抖诊断、原始配置页、恢复出厂</td>
</tr>
</table>

> 截图是 `?demo=1` 的离线演示模式（不需要接键盘也能看界面）。

## 它现在能做什么

### 灯光
- **20 种灯效**（含「关闭」），与键盘固件一一对应
- 亮度（0–9 档）、速度（0–4 档）**无极拖动 + 松手缓动吸附**，改完立即生效
- **逐键颜色**：126 个灯位单独调色，内置色盘（HSV 圆环 + 明度条）
- **10 种音乐律动**：走 WASAPI 环回采集系统正在播放的声音，FFT 后按节奏刷灯
- 颜色组与灯效槽位按原厂 `LedOptN` 表精确映射（不是猜的）

### 改键 / 宏
- **4 层**：默认按键层 / FN1 / FN2 / Tap
- **8 个功能分类**：键盘、鼠标、多媒体、常用命令、组合键、修饰键、键盘内建、软件功能
- 每颗键直接显示**真实中文功能名**（不再是一排「特殊」）
- 宏录制：按键序列 + 延时，存进键盘本体的 10 KB 宏库
- 每一个写操作都**读回校验**，写不进去会明确报错而不是假装成功

### 旋钮
- **15 种可绑定动作**（系统音量 / 屏幕亮度 / 键盘背光 / 灯效切换 / 音乐律动…）
- **可增删改的槽位**：长按旋钮在自定义槽位之间轮回切换，槽位内容和数量都由你定
- 音量走**系统原生**通道（放行真键 → Windows 自己画那条胶囊提示条）
- 其余动作走**自绘 OSD**，尺寸/圆角/图标/强调色逐像素对齐 Windows 11 实测值

### 其它
- **电量**：数字 + 环形进度 + 充电状态（原厂藏了这个功能）
- **2.4G 无线支持**：读取全部状态、改灯效/亮度/速度/休眠；写改键与逐键颜色需要插线（协议层限制，不是偷懒）
- **固件模式守护**：长按旋钮会让键盘进入固件自己的模式（旋钮只调背光、不再发任何按键），后台自动把它拉回来
- 托盘常驻、关窗不退出、开机自启（可选）、电量图标
- 备份 / 还原：每次写设备前自动存一份快照，一键回到上一版

---

## 安装

### 方式一：直接下载（推荐，普通用户）

到 [Releases](https://github.com/xx12ek/aula-f99-pro-hub/releases) 下载 `AULA-Hub-portable.zip`，解压到任意目录，双击 `AULA-Hub.exe`。

- 免安装、不需要管理员权限、不需要装 Python
- 只支持 **Windows 10 / 11 x64**

### 方式二：从源码跑（开发者）

```bash
git clone https://github.com/xx12ek/aula-f99-pro-hub.git
cd aula-hub
pip install -r requirements.txt
python app.py
```

需要 Python 3.10+ 和 Edge WebView2（Win11 自带，Win10 可能要装一次）。

---

## 支持哪些键盘

目前**只针对 AULA F99 Pro** 验证过。判断你的键盘能不能用：

| VID | PID | 型号 |
|---|---|---|
| `0x258A` | `0x010C` | 有线 |
| `0x3554` | `0xFA09` | 2.4G 接收器 |

在 PowerShell 里跑这句能看到你的设备：

```powershell
Get-PnpDevice -Class HIDClass | Where-Object { $_.InstanceId -match "258A|3554" } | Select-Object FriendlyName, InstanceId
```

AULA 的其他型号（F75 / F87 / F99 非 Pro）大概率共用同一套协议，但**没有实测数据**，能用或不能用都欢迎开 issue 告诉我。

---

## 已知做不到的事

我宁愿写清楚，也不想让你点半天才发现：

| 做不到 | 原因 |
|---|---|
| 2.4G 下写改键 / 写逐键颜色 / 写宏 | 固件的无线通道只开放了读命令。实测 24 + 18 组命令组合全部无效 |
| 2.4G 下改旋钮槽位 | 同上，读不到配置页 |
| 让软件唤出 Windows 原生的亮度条 | 试过 WASAPI / SendInput / keybd_event / 扫描码 / WM_APPCOMMAND / WMI / Dxva2，**全部不弹**。只有真实硬件按键才会触发 |
| 从软件升级固件 | 原厂 V3 工具是烧录器，本体不含 `.hex`。刷坏就变砖 |
| 关掉「长按旋钮切固件模式」 | 那是固件行为，用户态关不掉。只能让它切、然后立刻接管回来 |

---

## 这是怎么逆向出来的

整个项目建立在**对原厂驱动 `OemDrv.exe` 与键盘固件协议的逆向**之上。核心发现（完整记录见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)）：

- **有线通道**：HID feature report，report id `6`，520 字节（8 字节头 + 512 数据）
- **2.4G 通道**：20 字节中断包，report id `0x13`，末字节是 `sum(all)&0xFF` 校验
- **配置页**：128 字节，尾标记 `5A A5`，灯效表从 `0x3A` 起每槽 2 字节
- **矩阵记录**：每键 4 字节，`b0` 是功能类别（普通键/命令/Consumer/宏/鼠标/固件内建…）
- **逐键颜色**：不是 RGBRGB 交错，而是 **R×126 + G×126 + B×126 三个平面**传输 —— 搞错会让整块颜色乱套

**没有任何固件被修改。** 所有操作都是驱动本来就支持、只是没在界面上给你的命令。

---

## 项目结构

```
aula-f99-pro-hub/
├── app.py              主程序 + pywebview 窗口 + JS↔Python 桥
├── core/
│   ├── aula.py         HID 协议层（有线 / 2.4G / 配置页 / 矩阵 / RGB / 宏）
│   ├── knob_router.py  旋钮接管（低级键盘钩子 + 槽位 + OSD）
│   ├── knob_mode.py    长按旋钮识别（配置页指纹）
│   ├── mode_guard.py   固件模式自动拉回
│   ├── osd.py          自绘 OSD（纯 Win32 layered window）
│   ├── sysvol.py       WASAPI 音量 + WMI 屏幕亮度
│   ├── music.py        音乐律动引擎（WASAPI loopback + FFT）
│   ├── macro.py        宏编码/解码
│   ├── misc.py         导入导出 / 恢复出厂 / 原始配置页
│   ├── settings.py     应用设置
│   ├── tray.py         系统托盘
│   └── chrome.py       Windows 深色标题栏
├── ui/                 前端（原生 HTML/CSS/JS，无框架无构建）
├── tools/selfcheck.py  25 项功能自检脚本
└── docs/               协议文档、自检报告
```

---

## 自检

想确认在你机器上一切正常，跑：

```bash
python tools/selfcheck.py
```

会逐项验证 25 个能力并打印 PASS/FAIL，所有写操作写完都会还原。最后一次结果见 [`docs/SELF-CHECK.md`](docs/SELF-CHECK.md)。

---

## 安全性

- **不装内核驱动**、不需要管理员权限、不写系统目录
- **不改固件**，所有命令都在原厂驱动已有的能力范围内
- 唯一的全局钩子是 `WH_KEYBOARD_LL`，**只截三个旋钮键**（音量+/-/静音）和 `F13`–`F20`，其它按键一律原样放行
- 网络：**完全离线**，不联网、不上传、不检查更新

源码全部公开，欢迎审计。

---

## 贡献

如果你有 AULA 的其他型号并且愿意帮忙测试，非常欢迎：

1. 用 `tools/selfcheck.py` 跑一遍，把输出贴到 issue
2. 或者直接开 issue 告诉我型号 + VID/PID + 哪些功能能用

协议文档（`docs/PROTOCOL.md`）里标了哪些结论是**实测确认**、哪些是**推测未验证**，请以实测为准。

---

## 许可

MIT。字体与图标均为自制，无第三方素材。
