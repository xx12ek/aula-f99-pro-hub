# 贡献 / Contributing

中文或英文都行。English is fine too.

## 最有用的贡献：告诉我你的键盘能不能用

这个项目本来只为 **AULA F99 Pro** 写的（VID `258A:010C` / 2.4G `3554:FA09`）。
同一套 HID 协议的 AULA 键盘应该也能跑，但我手上只有一把，没法测。

**如果你有别的型号，请开一个 issue**，贴上：

1. 硬件 ID

   ```powershell
   Get-PnpDevice -Class HIDClass | Where-Object { $_.InstanceId -match "258A|3554" } |
     Select-Object FriendlyName, InstanceId
   ```

2. 自检输出

   ```powershell
   python tools/selfcheck.py
   ```

   它会逐项报告这台设备认哪些命令、不认哪些。**不认不是错误，是数据** ——
   正好告诉我这个型号少了什么。

3. 官方驱动的型号名（`Program Files (x86)\AULA\...` 里的 `KB.ini` 路径，
   或者你机器上那个 `Dev\kb\<型号>\KB.ini` 的名字）

拿到这些我就能判断要不要加一个型号配置。

## 提代码

1. Fork，开分支
2. **不要提交** `backup/`、`tmp/`、`build/`、`dist/`、`__pycache__`（`.gitignore` 已挡）
3. 改完跑一遍：

   ```powershell
   python -m py_compile app.py core/*.py
   python -c "import core.aula, core.misc, core.osd, core.sysvol, core.knob_router"
   node --check ui/app.js
   python tools/selfcheck.py
   ```

   **注意：`py_compile` 过了不代表能 import** —— 我删代码时曾把 `VK_VOL_UP`
   这类顶层常量一起删掉，`py_compile` 照样通过，`import` 才报 `NameError`。
   所以第二条命令必跑。

4. PR 里说明**你实测了什么**。这个项目的每一条协议结论都是真机验证出来的，
   `docs/PROTOCOL.md` 里标 ✅ 的条目不接受"看起来应该这样"的改动。

## 关于协议文档

`docs/PROTOCOL.md` 里每节都标了可信度：

- ✅ **实测** —— 真机发出去、读回来对上了，可以依赖
- ⚠️ **推断** —— 从行为推出来的，方向对但细节可能错
- ❓ **未知** —— 试过但没结论，别在上面盖东西

如果你推翻了一条 ✅，请附上你的实测数据（发了什么、回了什么），我会改。

## 不做的事

- 不改固件、不写内核驱动、不做需要管理员权限的事
- 不加网络请求（这个软件完全离线）
- 不为了"支持更多型号"而猜协议 —— 猜出来的字节写进设备可能把键盘搞坏
