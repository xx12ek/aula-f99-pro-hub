---
name: 我的键盘不能用 / 功能不对
about: 报告其他 AULA 型号的兼容性，或本型号某个功能异常
title: "[兼容性] 型号 + 简短描述"
labels: compatibility
---

<!-- 中文或英文都行 / 中文英文都可以 -->

## 键盘型号

<!-- 例如：狼蛛 F99 Pro / 狼蛛 F87 / AULA F75 -->

## 硬件 ID

在 PowerShell 里跑这条，把输出贴上来：

```powershell
Get-PnpDevice -Class HIDClass | Where-Object { $_.InstanceId -match "258A|3554" } |
  Select-Object FriendlyName, InstanceId
```

```

```

## 自检输出

在项目目录里跑：

```powershell
python tools/selfcheck.py
```

把完整输出贴在下面（它会告诉你哪些命令这台设备认、哪些不认）：

```

```

## 具体哪里不行

<!-- 比如说：读不到电量 / 改键写不进去 / 点灯效没反应 / 软件打不开 -->

## 官方驱动版本

<!-- 如果有的话，例如 V3-F99Pro_20240508.exe，或者包装盒/说明书上的型号 -->
