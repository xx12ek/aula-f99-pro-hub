/* AULA Hub 前端逻辑 */
const R_IN = 540.35; // 2 * PI * 86
const IS_DEMO = new URLSearchParams(location.search).has("demo");

const MOCK = {
  connected: true, mode: "wired", modeLabel: "有线连接",
  model: "AULA F99 Pro", firmware: 24, battery: 100, charging: true,
  effect: 1, effectLabel: "常亮", brightness: 9, brightnessMax: 9, speed: 4, speedMax: 4, color: 0, sleep: 10, error: "",
  effects: [
    {ui:1,hw:1,name:"常亮"},{ui:2,hw:3,name:"呼吸"},{ui:3,hw:2,name:"梦幻彩虹"},
    {ui:4,hw:19,name:"一触即发"},{ui:5,hw:15,name:"雨中漫步"},{ui:6,hw:13,name:"彩虹轮盘"},
    {ui:7,hw:20,name:"涟漪扩散"},{ui:8,hw:16,name:"繁星点点"},{ui:9,hw:18,name:"踏雪无痕"},
    {ui:10,hw:5,name:"川流不息"},{ui:11,hw:7,name:"随波逐流"},{ui:12,hw:17,name:"如影随形"},
    {ui:13,hw:12,name:"正弦光波"},{ui:14,hw:8,name:"左右扫描"},{ui:15,hw:28,name:"旋转风车"},
    {ui:16,hw:30,name:"七彩瀑布"},{ui:17,hw:14,name:"花开富贵"},{ui:18,hw:29,name:"旋转风暴"},
    {ui:19,hw:0,name:"自定义"},{ui:0,hw:0,name:"关闭"},
  ],
  colors: ["#ff3b30","#34c759","#0a84ff","#ffd60a","#ff2d92","#32d5f5","#f2f5fa"],
  colorNames: ["红","绿","蓝","黄","品红","青","白","随机"],
  sleepOptions: [60,90,120,180,240,300,600,900,1200],
};

const MOCK_SETTINGS = { close_to_tray: true, autostart: false, poll_seconds: 5 };
let appSettings = { ...MOCK_SETTINGS };
let pollTimer = null;

// 单键调色页的状态（必须声明在 boot() 之前：demo 模式下 boot() 在脚本主体就执行，
// 放到文件后半段的 const 会因为暂时性死区抛 ReferenceError，把整页渲染一起打断）
let rgbTable = null;        // 126 个 [r,g,b]
let rgbSel = new Set();     // 选中的槽号
let rgbGeo = null;          // keymap: {idx: {x,y,w,h,label}}
let rgbLoaded = false;

// 备份文件名的中文名与「还原会动哪些部分」的说明。
// 必须放在脚本顶部：demo 下 boot() 在脚本主体就执行，loadSettings() 的同步段
// 会一路走到 renderRestoreWill()，const 留在后半段就是 TDZ ReferenceError。
const BK_KIND_CN = {
  "snapshot-L0": "默认层", "snapshot-L1": "FN1 层", "snapshot-L2": "FN2 层",
  "snapshot-L3": "Tap 层", "snapshot-macro": "宏库", "snapshot-page": "灯效设置",
  "snapshot-rgb": "逐键颜色", "matrix-L0": "默认层", "matrix-L1": "FN1 层",
  "matrix-L2": "FN2 层", "matrix-L3": "Tap 层", "macro": "宏库",
  "rgb-table": "逐键颜色", "rgb-before": "逐键颜色",
};
const BK_PART_CN = {
  L0: "默认层", L1: "FN1 层", L2: "FN2 层", L3: "Tap 层",
  macro: "宏库", page: "灯效设置", rgb: "逐键颜色",
};

// 色盘状态放在这里：syncRgbColorUi() 会在绑定阶段被调用，声明放后半段会命中 TDZ
const WHEEL_PX = 720;   // canvas 内部分辨率，CSS 显示 216px
let rgbHsv = { h: 270, s: 0.55, v: 1 };
let rgbWheelInited = false;
let rgbWheelBase = null;              // 只含色相的底图
let rgbWheelDrag = false;
// 色盘弹层是 body 级浮层、两个页签共用，靠这个记住这次是谁唤出来的（定位用）
let rgbWheelAnchor = null;
const RGB_PRESETS = [
  "#ff3b30", "#ff9500", "#ffd60a", "#34c759", "#00d4c8",
  "#0a84ff", "#8b5cf6", "#ff2d92", "#ffffff",
];

let state = null;
let api = null;
let pending = false;
// 音乐律动状态：这些必须在脚本顶部声明 —— bindMusicUi() 在 boot() 里就会跑，
// 放文件后半段会被 TDZ 拦住（和 rgb 那批变量同一个坑）。
let musicInfo = null;
let musicTimer = null;
// 旋钮绑定状态：同样必须放顶部 —— bindKnobBindUi() 在 boot() 里就跑了
let knobBind = null;
const $ = (id) => document.getElementById(id);

function fmtSleep(sec) {
  // 90 秒曾经被 Math.round 显示成「2 分钟」，和 120 秒撞名，所以不能四舍五入。
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) {
    const m = sec / 60;
    return Number.isInteger(m) ? `${m} 分钟` : `${Math.floor(m)} 分 ${sec % 60} 秒`;
  }
  return `${(sec / 3600).toFixed(0)} 小时`;
}

function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2200);
}

/* ---------- 渲染 ---------- */
function render(s) {
  state = s;
  if (!s.catalog && window.AULA_CATALOG) s.catalog = window.AULA_CATALOG;
  $("modelName").textContent = s.model || "AULA F99 Pro";
  $("modeLabel").textContent = s.modeLabel;
  $("statMode").textContent = s.mode === "wired" ? "USB 有线" : s.mode === "wireless" ? "2.4G" : "—";
  $("statFw").textContent = "v" + s.firmware;

  const pill = $("modePill");
  pill.classList.toggle("offline", !s.connected);
  pill.classList.toggle("busy", s.connected && s.battery < 0);
  document.body.classList.toggle("offline", !s.connected);

  // 电量
  const pct = s.battery >= 0 ? Math.max(0, Math.min(100, s.battery)) : 0;
  $("batteryNum").textContent = s.battery >= 0 ? pct : "--";
  $("ringValue").style.strokeDashoffset = String(R_IN * (1 - pct / 100));
  const bstate = $("batteryState");
  const charging = !!s.charging || s.mode === "wired";
  bstate.classList.toggle("charging", charging);
  bstate.innerHTML = s.connected
    ? (charging
        ? '<span class="bolt">⚡</span> ' + (s.mode === "wired" ? "已接通电源" : "充电中")
        : "电池供电")
    : "设备未连接";

  // 同步
  const age = typeof s.age === "number" ? s.age : 0;
  const when = age < 3 ? "刚刚同步" : age < 60 ? Math.round(age) + " 秒前同步" : Math.round(age / 60) + " 分钟前同步";
  $("syncText").textContent = s.connected
    ? when + " · " + (s.mode === "wired" ? "USB 数据线" : "2.4G 接收器")
    : (s.error || "未找到设备");
  $("syncPulse").classList.toggle("stale", !s.connected);

  // 灯效标题
  const cur = (s.effects || []).find(e => e.ui === s.effect);
  $("effectTitle").textContent = cur ? cur.name : "—";

  renderEffects(s);
  renderColors(s);
  renderSegs(s);
  renderSleep(s);
  segAutoSync();     // 音乐灵敏度 / 刷新率 / 色盘明度：同样跟手 + 吸附
  $("statusMsg").textContent = s.connected
    ? "用户态 HID · 无需安装驱动 · 设置实时写入键盘"
    : "等待键盘连接…";
}

function renderEffects(s) {
  const box = $("effects");
  const sig = (s.effects || []).map(e => e.ui + e.name).join("|");
  if (box.dataset.sig === sig) { markActive(s); return; }
  box.dataset.sig = sig;
  box.innerHTML = "";
  (s.effects || []).forEach(e => {
    const el = document.createElement("div");
    el.className = "effect";
    el.dataset.ui = e.ui;
    // 卡片小字原本是硬件灯效号（#01 #03…），对用户没意义，换成「这个灯效能调什么」
    const sub = Number(e.ui) === 1 ? "亮度" : Number(e.ui) === 19 ? "逐键上色" : "亮度 · 速度";
    el.title = `${e.name}（硬件灯效 ${e.hw}）`;
    el.innerHTML = `<div class="dot"></div><div class="name">${e.name}</div><div class="sub">${sub}</div>`;
    el.onclick = () => apply("set_effect", e.ui, `已切换到 ${e.name}`);
    box.appendChild(el);
  });
  markActive(s);
}

function markActive(s) {
  document.querySelectorAll(".effect").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.ui) === Number(s.effect));
  });
}

function renderColors(s) {
  const box = $("colors");
  // 判断「固定色是否已建过」必须看有没有 data-idx 的子节点，
  // 不能用 childElementCount === 0：第 8 颗自定义色圆片也在这个容器里，
  // 一旦它先被建出来，childElementCount 就永远不为 0，7 颗固定色再也建不出来。
  if (!box.querySelector(".swatch[data-idx]")) {
    // 颜色名 8 项、十六进制只有 7 项：第 8 项「随机」没有固定色，用 conic 渐变画。
    // 之前按 hex 数组遍历，随机那颗永远不生成，色板少一格。
    const names = s.colorNames || [];
    const hexes = s.colors || [];
    const n = Math.max(names.length, hexes.length);
    for (let i = 0; i < n; i++) {
      const el = document.createElement("div");
      el.className = "swatch" + (i === 7 ? " random" : "");
      if (i !== 7) el.style.background = hexes[i] || "#ffffff";
      el.title = names[i] || "";
      el.dataset.idx = i;
      el.onclick = () => apply("set_color", i, "颜色：" + (names[i] || ""));
      box.appendChild(el);
    }
  }
  // 第 8 颗：自定义色圆片。必须追加在 7 颗固定色之后，所以放在这里建。
  // 它没有 data-idx，别混进下面那句 active 判断。
  let swc = $("swCustom");
  if (!swc) {
    swc = document.createElement("button");
    swc.type = "button";
    swc.id = "swCustom";
    swc.className = "swatch custom";
    box.appendChild(swc);
  } else if (swc.parentElement !== box) {
    box.appendChild(swc);
  }
  box.querySelectorAll(".swatch").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.idx) === Number(s.color));
  });
  // 第 8 颗是「自定义色」圆片：中心显示当前画笔颜色，自定义灯效时描边高亮。
  // 注意不要把它算进 .swatch 的 active 判断（它没有 data-idx）。
  const sw = $("swCustom");
  if (sw) {
    sw.style.setProperty("--pick", $("rgbColor").value || "#7c4dff");
    sw.classList.toggle("active", Number(s.effect) === 19);
    sw.title = Number(s.effect) === 19
      ? "当前是自定义灯效。点这里从色盘挑任意颜色，整块键盘都会变成那个颜色"
      : "从色盘里挑任意颜色，整块键盘会切到自定义灯效并刷成这个颜色";
  }
}

/* ---------- 亮度 / 速度：连续跟手 + 松手缓动吸附 ----------
   两件事必须同时成立：

   1) 跟手。原生 <input type="range"> 自己就会跟手，问题是它的 step 一旦是 1，
      拖动时拇指只能一格格跳，观感很生硬。所以 step 设成 0.01，
      pointerdown 到 pointerup 之间 el.value 是连续的浮点数，拇指严格贴住鼠标。

   2) 松手吸附。松手后不立刻跳，而是用 requestAnimationFrame 把 value
      从当前浮点值缓动到最近的整数挡位（260ms，easeOutCubic），
      动画结束再提交给固件。这样「拉过去」和「落下来」是两段不同的手感。

   设备写入必须节流：实测裸写一次 78ms，带校验的 set_* 一次 253ms。
   拖动时每帧都写会把界面拖成幻灯片，所以拖动中只做「节流预览」
   （最小间隔 90ms，且只在整数挡位真正变化时才发），松手后再正式提交一次。

   亮度上限取 9 不是拍脑袋：固件出场配置页 19 个槽只用过 [4,5,7,9]，
   自定义亮度也是 9，说明它本来就按 0..9 十档在跑。官方 UI 压成 0..4
   才是丢信息的那一步（老代码还 min(4,…) 把出场的 9 读成 4，是个真 bug）。 */
const SEG_CFG = [
  { id: "brightnessSeg", key: "brightness", maxKey: "brightMax", fn: "set_brightness",
    preview: "preview_brightness", label: "亮度", fallbackMax: 9 },
  { id: "speedSeg", key: "speed", maxKey: "speedMax", fn: "set_speed",
    preview: "preview_speed", label: "速度", fallbackMax: 4 },
];
const SEG_N = 5;              // 老代码遗留：speed 的默认挡位数，只当兜底
const segDragging = {};       // id -> true 表示正在拖，轮询不许覆盖
const segLive = {};           // id -> 拖动中的浮动值（可能带小数）
const segAnim = {};           // id -> rAF 句柄，吸附动画
const segMax = {};            // id -> 当前这条滑轨的实际上限（来自后端）
const segLastSent = {};       // id -> 上次预览发出的整数挡位
const segLastAt = {};         // id -> 上次预览发出的时间戳
const segCommitted = {};      // id -> 上次正式提交的挡位（去重，change+pointerup 会连发）
const SEG_SEND_GAP = 90;      // 预览最小间隔 ms（裸写一次 78ms，留点余量）
const SEG_SNAP_MS = 260;      // 吸附缓动时长

function segLimit(el) {
  return segMax[el.id] || Number(el.max) || (SEG_N - 1);
}

function segPaint(el, v) {
  // --p 是 0..1 的「走了几成」，CSS 里换算成「拇指中心所在的像素」：
  //   7px + (100% - 14px) * p
  // 7px 是 14px 拇指的半个宽度。这样满档时填充右端正好压在拇指中心，
  // 视觉上就是「顶到头」，不会像按百分比铺满那样留一截尾巴。
  const n = segLimit(el);
  const p = n <= 0 ? 0 : Math.min(n, Math.max(0, Number(v))) / n;
  el.style.setProperty("--p", p.toFixed(4));
  el.dataset.value = String(v);
}

const segOriginStep = {};     // id -> 原始 step（吸附动画会临时改成 any，必须记住原值）

/* 松手吸附：把 value 从当前浮点缓动到最近挡位，结束时回调 */
function segSnapTo(el, target, done) {
  if (segAnim[el.id]) cancelAnimationFrame(segAnim[el.id]);
  // 有些滑条 step 是 5（音乐灵敏度）或 1，插值出来的小数会被浏览器
  // 按 step 规整回去，动画就变成一格一格跳。动画期间临时放开 step，
  // 结束后再还原，这样缓动才是连续的。
  //
  // 坑：change 和 pointerup 会连着各触发一次 commit，第二次进来时
  // el.step 已经是第一次动画临时写的 "any"，直接存下来当「原始值」
  // 就会把 step 永久变成 any（之后拖动不再按挡位取整）。所以原始
  // step 只在第一次见到时记一次，之后一直用记忆里的值。
  if (segOriginStep[el.id] == null && el.step !== "any") {
    segOriginStep[el.id] = el.step;
  }
  const oldStep = segOriginStep[el.id] ?? "";
  const from = Number(el.value);
  const to = Number(target);
  const dist = to - from;
  const finish = (v) => {
    if (oldStep) el.step = oldStep;
    el.value = String(v);
    segPaint(el, v);
    segAnim[el.id] = null;
    done(v);
  };
  if (Math.abs(dist) < 0.004) { finish(to); return; }
  el.step = "any";
  const t0 = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - t0) / SEG_SNAP_MS);
    // easeOutCubic：起步快、收尾稳，像被磁铁吸过去而不是弹过去
    const e = 1 - Math.pow(1 - k, 3);
    const v = from + dist * e;
    el.value = String(v);
    segPaint(el, v);
    if (k < 1) {
      segAnim[el.id] = requestAnimationFrame(step);
    } else {
      finish(to);
    }
  };
  segAnim[el.id] = requestAnimationFrame(step);
}

/* 滑条提交用的串行发送器。
   不能复用 apply()：它开头 `if (pending) return;` 是全局互斥，
   一次正式写入要 253ms，用户快速松手两次时第二次会被静默丢掉，
   界面就在下次轮询时「自己跳回旧值」。
   这里改成「始终发最新值」的串行队列：正在写就把最新意图记下来，写完再补发。 */
const segQueue = {};   // id -> { busy, want }

async function segSend(cfg, el, v) {
  if (!api) return;
  const q = segQueue[el.id] || (segQueue[el.id] = { busy: false, want: null });
  q.want = v;
  if (q.busy) return;
  q.busy = true;
  try {
    while (q.want !== null) {
      const val = q.want;
      q.want = null;
      try {
        const next = await api[cfg.fn](val);
        if (next) render(next);
      } catch (e) {
        toast("写入失败：" + (e && e.message ? e.message : e), true);
      }
    }
  } finally {
    q.busy = false;
  }
}

function segBind(el) {
  if (el.dataset.bound === "1") return;
  el.dataset.bound = "1";

  el.addEventListener("pointerdown", () => {
    if (segAnim[el.id]) { cancelAnimationFrame(segAnim[el.id]); segAnim[el.id] = null; }
    segDragging[el.id] = true;
    segLastSent[el.id] = null;
    segLastAt[el.id] = 0;
  });

  el.addEventListener("input", () => {
    const v = Number(el.value);
    segLive[el.id] = v;
    segPaint(el, v);
    const cfg = SEG_CFG.find(c => c.id === el.id);
    if (cfg) el.title = `${cfg.label} ${v.toFixed(1)} / ${segLimit(el)}`;
    if (!segDragging[el.id]) return;   // 键盘方向键也会冒 input，那种没拖动
    // 节流预览：只在整数挡位真变了、且距上次发送够久时才写设备
    const lv = Math.round(v);
    const now = performance.now();
    if (lv !== segLastSent[el.id] && (now - (segLastAt[el.id] || 0)) >= SEG_SEND_GAP) {
      segLastSent[el.id] = lv;
      segLastAt[el.id] = now;
      if (api && cfg && cfg.preview) {
        try { api[cfg.preview](lv); } catch (_) { /* 预览失败不影响松手正式提交 */ }
      }
    }
  });

  const commit = () => {
    const cfg = SEG_CFG.find(c => c.id === el.id);
    if (!cfg) return;
    segDragging[el.id] = false;
    const limit = segLimit(el);
    const raw = Math.min(limit, Math.max(0, Number(el.value)));
    const target = Math.round(raw);      // 吸附到最近的整数挡位
    segLive[el.id] = null;
    segSnapTo(el, target, (v) => {
      if (state && Number(state[cfg.key]) === v) { renderSegs(state); return; }
      if (segCommitted[el.id] === v) return;   // change/pointerup 会连发两次，去掉重复写
      segCommitted[el.id] = v;
      segSend(cfg, el, v);
    });
  };
  el.addEventListener("change", commit);     // 松手 / 键盘方向键
  el.addEventListener("pointerup", commit);
  el.addEventListener("pointercancel", commit);
  el.addEventListener("keyup", commit);
}

function renderSegs(s) {
  SEG_CFG.forEach(cfg => {
    const el = $(cfg.id);
    if (!el) return;
    segBind(el);
    // 上限跟着后端走（亮度 0..9、速度 0..4）。只在变化时才改 max，
    // 因为改 max 会重置 value，轮询每 5 秒来一次会打断正在进行的拖动。
    const limit = Number(s[cfg.maxKey] ?? cfg.fallbackMax);
    if (Number(el.max) !== limit) { el.max = String(limit); segMax[el.id] = limit; }
    const disabled = cfg.key === "speed" && s.effect === 1;
    el.classList.toggle("off", disabled);
    el.disabled = disabled;
    // 拖动 / 吸附动画进行中，轮询不许碰 value
    if (segDragging[el.id] || segAnim[el.id]) { segPaint(el, segLive[el.id] ?? Number(el.value)); return; }
    const v = Number(s[cfg.key]);
    el.title = disabled ? "常亮模式下没有速度可调" : `${cfg.label} ${v} / ${limit}`;
    if (Number(el.value) !== v) el.value = String(v);
    segPaint(el, v);
  });
}


function renderSleep(s) {
  const sel = $("sleepSelect");
  if (sel.childElementCount === 0) {
    (s.sleepOptions || []).forEach(v => {
      const o = document.createElement("option");
      o.value = v; o.textContent = fmtSleep(v);
      sel.appendChild(o);
    });
    sel.onchange = () => apply("set_sleep", Number(sel.value), "自动休眠已更新");
  }
  const cur = (s.sleepOptions || []).find(v => ({2:60,3:90,4:120,6:180,8:240,10:300,20:600,30:900,40:1200})[s.sleep] === v);
  if (cur) sel.value = String(cur);
}

/* ---------- 通用滑条：任何带 .seg 的 range 都自动跟手 + 吸附 ----------
   第一页的亮度/速度有专属逻辑（要走设备预览），这里处理剩下的普通滑条：
   音乐卡的灵敏度/刷新率、色盘的明度条。它们只改本地状态，
   但同样需要「拖动跟手、松手缓动落到最近挡位」这套手感，
   否则同一界面里两种滑条手感不一致，用起来很割裂。 */
function segAutoLimit(el) {
  return Number(el.max) || 100;
}
function segAutoBind(el) {
  if (el.dataset.auto === "1") return;
  el.dataset.auto = "1";
  // 拖动全程 value 是连续的（step 已改成 0.01 或本来就很细），
  // 所以拇指会严格贴着指针走，不会一格一格跳。
  el.addEventListener("input", () => {
    segPaint(el, Number(el.value));
  });
  const snap = () => {
    const step = Number(el.step) || 1;
    const raw = Number(el.value);
    const target = Math.round(raw / step) * step;
    segSnapTo(el, Math.min(segAutoLimit(el), Math.max(0, target)), () => {
      // 吸附完成后补一次 input/change，让原来的业务处理拿到最终整数
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  el.addEventListener("pointerup", snap);
  el.addEventListener("pointercancel", snap);
  // change 已在上面被 snap 手动派发，这里不再绑，避免二次触发
  el.addEventListener("keyup", (e) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(e.key)) snap();
  });
}
function segAutoSync() {
  document.querySelectorAll("input.seg").forEach(el => {
    if (SEG_CFG.some(c => c.id === el.id)) return;   // 亮度/速度走专属逻辑
    segAutoBind(el);
    if (!segDragging[el.id] && !segAnim[el.id]) segPaint(el, Number(el.value));
  });
}

/* ---------- 交互 ---------- */
async function apply(method, value, msg) {
  if (!api || pending) return;
  pending = true;
  try {
    const next = await api[method](value);
    render(next);
    if (msg) toast(msg);
  } catch (e) {
    toast("写入失败：" + (e && e.message ? e.message : e), true);
  } finally {
    pending = false;
  }
}

async function refresh(manual) {
  if (!api) return;
  const btn = $("refreshBtn");
  if (manual) { btn.classList.add("spin"); setTimeout(() => btn.classList.remove("spin"), 700); }
  try {
    const next = await api.state(!!manual);
    render(next);
    if (!$("setLayout").hidden) renderInfo(next);
    if (!$("rgbLayout").hidden) { renderRgbKbd(); updateRgbTip(); }
  } catch (e) {
    toast("读取失败：" + e, true);
  }
}

/* ---------- 启动 ---------- */
function boot() {
  bindKeysUi();
  if (IS_DEMO) {
    api = {
      state: async () => MOCK,
      set_effect: async () => MOCK, set_color: async () => MOCK,
      set_brightness: async () => MOCK, set_speed: async () => MOCK,
      set_sleep: async () => MOCK,
      misc_info: async () => ({
        ok: true, link: "wired", effect: 11, effectOff: false,
        canWriteMatrix: true, canWriteRgb: true, canWriteMacro: true,
        canReset: true, debounceDefault: 0x04010101,
        backupDir: "", notes: [],
      }),
      misc_effect_off: async () => ({ ok: true, effect: 0 }),
      misc_probe_time: async () => ({ ok: true, moving: [] }),
      misc_read_debounce: async () => ({
        ok: true, known: false,
        page_nonzero: [[9, 0], [10, 11], [24, 40], [56, 255]],
        hint: "demo 模式：这里是假数据",
      }),
      misc_export: async () => ({
        ok: true, path: "C:/demo/aula-config.json", bytes: 20480,
        layers: 4, hasRgb: true, hasMacros: true,
      }),
      misc_import: async () => ({ ok: true, done: ["矩阵第 0 层", "配置页"], skipped: [] }),
      misc_pick_import: async () => ({ ok: true, done: ["矩阵第 0 层", "配置页"], skipped: [] }),
      misc_reset_all: async () => ({ ok: true }),
      misc_read_page: async () => ({
        ok: true,
        hex: "00".repeat(128),
        nonzero: [[1, 3], [2, 3], [3, 2], [10, 11]],
      }),
      misc_write_page: async () => ({ ok: true, match: true }),
      get_settings: async () => ({ ...MOCK_SETTINGS }),
      set_settings: async (j) => ({ ...MOCK_SETTINGS, ...JSON.parse(j) }),
      list_backups: async () => ([
        { name: "snapshot-rgb-20261022-162333-demo01.bin", tag: "snapshot-rgb", kind: "snapshot-rgb", before: false, group: "demo01", time: "10-22 16:23:33", size: 378, mtime: 3 },
        { name: "snapshot-page-20261022-162333-demo01.bin", tag: "snapshot-page", kind: "snapshot-page", before: false, group: "demo01", time: "10-22 16:23:33", size: 128, mtime: 3 },
        { name: "snapshot-macro-20261022-162333-demo01.bin", tag: "snapshot-macro", kind: "snapshot-macro", before: false, group: "demo01", time: "10-22 16:23:33", size: 10240, mtime: 3 },
        { name: "snapshot-L3-20261022-162332-demo01.bin", tag: "snapshot-L3", kind: "snapshot-L3", before: false, group: "demo01", time: "10-22 16:23:32", size: 512, mtime: 2 },
        { name: "snapshot-L2-20261022-162332-demo01.bin", tag: "snapshot-L2", kind: "snapshot-L2", before: false, group: "demo01", time: "10-22 16:23:32", size: 512, mtime: 2 },
        { name: "snapshot-L1-20261022-162332-demo01.bin", tag: "snapshot-L1", kind: "snapshot-L1", before: false, group: "demo01", time: "10-22 16:23:32", size: 512, mtime: 2 },
        { name: "snapshot-L0-20261022-162332-demo01.bin", tag: "snapshot-L0", kind: "snapshot-L0", before: false, group: "demo01", time: "10-22 16:23:32", size: 512, mtime: 2 },
        { name: "matrix-L2-before-20261022-154056.bin", tag: "matrix-L2", kind: "matrix-L2", before: true, group: "", time: "10-22 15:40:56", size: 512, mtime: 1 },
      ]),
      backup_now: async () => ({ files: 5 }),
      open_backup_dir: async () => ({ ok: true }),
      get_keymap: async () => ({ keys: window.AULA_KEYMAP || {}, slots: 126 }),
      get_rgb: async () => {
        // 复刻真机实测表：9 个纯蓝槽（Esc/A/W/S/D/←↑↓→），其余全黑
        const slots = Array.from({ length: 126 }, () => [0, 0, 0]);
        [0, 9, 14, 15, 21, 83, 88, 89, 95].forEach(i => { slots[i] = [0, 0, 255]; });
        return { ok: true, slots };
      },
      set_key_color: async () => ({ ok: true }),
      set_key_colors: async () => ({ ok: true }),
      use_custom_rgb: async () => ({ ok: true }),
      // 旋钮（矩阵第 0 层 #108）。demo 用真机实测的出厂记录。
      // quick/verified 与后端 core.aula.KNOB_VERIFIED 保持同一份清单。
      get_knob: async () => ({
        idx: 108,
        record: "07 00 00 1d",
        action: { kind: "oem", grp: 7, code: 29, name: "旋钮 / 模式键" },
        factory: true,
        factoryRecord: "07 00 00 1d",
        verified: true,
        quick: [
          { label: "播放 / 暂停", action: { type: "media", name: "播放/暂停" } },
          { label: "静音", action: { type: "media", name: "静音" } },
          { label: "下一曲", action: { type: "media", name: "下一曲" } },
          { label: "上一曲", action: { type: "media", name: "上一曲" } },
          { label: "停止", action: { type: "media", name: "停止" } },
          { label: "音量 +", action: { type: "media", name: "音量+" } },
          { label: "音量 −", action: { type: "media", name: "音量-" } },
        ],
      }),
      set_knob: async () => ({ ok: true }),
      reset_knob: async () => ({ ok: true }),
      // 旋钮双向接管（demo 只做界面）
      knob_bind: async () => ({
        running: false, installed: false, hits: 0, writes: 0, softHits: 0, lastAction: "", error: "",
        binding: { cw: "bright_up", ccw: "bright_down", press: "mute_toggle", step: 1, enabled: false },
        actions: [
          { key: "system", label: "系统默认（音量 / 静音，弹系统原生条）" },
          { key: "bright_up", label: "键盘亮度 +" },
          { key: "bright_down", label: "键盘亮度 −" },
          { key: "speed_up", label: "灯效速度 +" },
          { key: "speed_down", label: "灯效速度 −" },
          { key: "effect_next", label: "下一个灯效" },
          { key: "effect_prev", label: "上一个灯效" },
          { key: "vol_up", label: "系统音量 +" },
          { key: "vol_down", label: "系统音量 −" },
          { key: "mute_toggle", label: "系统静音开关" },
          { key: "screen_up", label: "屏幕亮度 +" },
          { key: "screen_down", label: "屏幕亮度 −" },
          { key: "custom_rgb", label: "切到自定义灯效" },
          { key: "music_toggle", label: "音乐律动 开 / 关" },
          { key: "none", label: "什么都不做" },
        ],
        slots: [
          { id: "sound", label: "声音", cw: "system", ccw: "system", press: "system" },
          { id: "bright", label: "屏幕亮度", cw: "screen_up", ccw: "screen_down", press: "system" },
          { id: "kblight", label: "键盘亮度", cw: "bright_up", ccw: "bright_down", press: "system" },
          { id: "effect", label: "灯效", cw: "effect_next", ccw: "effect_prev", press: "system" },
        ],
        active: 0, slotLabel: "声音",
        slotWatch: { running: true, enabled: true, hits: 0, samples: 0, reason: "", error: "" },
        screenAvailable: true,
        modeGuard: { running: true, enabled: true, rescued: 0, samples: 0, error: "" },
        brightMax: 9, speedMax: 4,
      }),
      knob_set_slot: async (which) => {
        const n = Number(which);
        const list = (knobBind && knobBind.slots) || [];
        return Object.assign({}, knobBind, {
          active: (Number.isFinite(n) && n >= 0 && n < list.length) ? n : 0
        });
      },
      knob_cycle_slot: async () => {
        const list = (knobBind && knobBind.slots) || [];
        const n = ((Number(knobBind && knobBind.active) || 0) + 1) % Math.max(1, list.length);
        return Object.assign({}, knobBind, { active: n });
      },
      knob_save_slots: async (raw) => {
        const d = JSON.parse(raw);
        return Object.assign({}, knobBind, { active: d.active, slots: d.slots });
      },
      knob_reset_slots: async () => Object.assign({}, knobBind, {
        active: 0, slots: knobBind.slots.slice(0, 4)
      }),
      knob_cycle_mode: async () => api.knob_cycle_slot(),
      knob_set_bind: async () => ({
        running: true, installed: true, hits: 0, writes: 0, lastAction: "", error: "",
        binding: { cw: "bright_up", ccw: "bright_down", press: "mute_toggle", step: 1, enabled: true },
        actions: [
          { key: "system", label: "系统默认（音量 / 静音，弹系统原生条）" },
          { key: "bright_up", label: "键盘亮度 +" },
          { key: "bright_down", label: "键盘亮度 −" },
          { key: "speed_up", label: "灯效速度 +" },
          { key: "speed_down", label: "灯效速度 −" },
          { key: "effect_next", label: "下一个灯效" },
          { key: "effect_prev", label: "上一个灯效" },
          { key: "vol_up", label: "系统音量 +" },
          { key: "vol_down", label: "系统音量 −" },
          { key: "mute_toggle", label: "系统静音开关" },
          { key: "screen_up", label: "屏幕亮度 +" },
          { key: "screen_down", label: "屏幕亮度 −" },
          { key: "custom_rgb", label: "切到自定义灯效" },
          { key: "music_toggle", label: "音乐律动 开 / 关" },
          { key: "none", label: "什么都不做" },
        ],
        slots: [
          { id: "sound", label: "声音", cw: "system", ccw: "system", press: "system" },
          { id: "bright", label: "屏幕亮度", cw: "screen_up", ccw: "screen_down", press: "system" },
          { id: "kblight", label: "键盘亮度", cw: "bright_up", ccw: "bright_down", press: "system" },
          { id: "effect", label: "灯效", cw: "effect_next", ccw: "effect_prev", press: "system" },
        ],
        active: 0, slotLabel: "声音",
        slotWatch: { running: true, enabled: true, hits: 0, samples: 0, reason: "", error: "" },
        screenAvailable: true,
        brightMax: 9, speedMax: 4,
      }),
      knob_takeover: async (on) => ({
        running: !!on, installed: !!on, hits: 0, writes: 0, lastAction: "", error: "",
        binding: { cw: "bright_up", ccw: "bright_down", press: "mute_toggle", step: 1, enabled: !!on },
        actions: [
          { key: "system", label: "系统默认（音量 / 静音，弹系统原生条）" },
          { key: "bright_up", label: "键盘亮度 +" },
          { key: "bright_down", label: "键盘亮度 −" },
          { key: "speed_up", label: "灯效速度 +" },
          { key: "speed_down", label: "灯效速度 −" },
          { key: "effect_next", label: "下一个灯效" },
          { key: "effect_prev", label: "上一个灯效" },
          { key: "vol_up", label: "系统音量 +" },
          { key: "vol_down", label: "系统音量 −" },
          { key: "mute_toggle", label: "系统静音开关" },
          { key: "screen_up", label: "屏幕亮度 +" },
          { key: "screen_down", label: "屏幕亮度 −" },
          { key: "custom_rgb", label: "切到自定义灯效" },
          { key: "music_toggle", label: "音乐律动 开 / 关" },
          { key: "none", label: "什么都不做" },
        ],
        slots: [
          { id: "sound", label: "声音", cw: "system", ccw: "system", press: "system" },
          { id: "bright", label: "屏幕亮度", cw: "screen_up", ccw: "screen_down", press: "system" },
          { id: "kblight", label: "键盘亮度", cw: "bright_up", ccw: "bright_down", press: "system" },
          { id: "effect", label: "灯效", cw: "effect_next", ccw: "effect_prev", press: "system" },
        ],
        active: 0, slotLabel: "声音",
        slotWatch: { running: true, enabled: true, hits: 0, samples: 0, reason: "", error: "" },
        screenAvailable: true,
        brightMax: 9, speedMax: 4,
      }),
      // 音乐律动（demo 下只做界面，不真的起线程）。
      // 注意 music_start/stop 里不能用 `this.music_status()`：它们是箭头函数，
      // this 绑定到 boot() 的词法作用域（window），会抛 TypeError。用局部函数。
      music_status: async () => mockMusicStatus(),
      music_start: async () => ({ ...mockMusicStatus(), running: true }),
      music_stop: async () => ({ ...mockMusicStatus(), running: false }),
      music_mood: async (m) => {
        const picked = mockMusicStatus().moods.find(x => x.key === m);
        return { ...mockMusicStatus(), mood: m, moodLabel: picked ? picked.label : m };
      },
      music_tune: async () => mockMusicStatus(),
      // 拖动预览：demo 下只改本地快照，让界面手感与真机一致
      preview_brightness: async (v) => { MOCK.brightness = v; return MOCK; },
      preview_speed: async (v) => { MOCK.speed = v; return MOCK; },
    };
    render(MOCK);
    bindMusicUi();
    const wantTab = new URLSearchParams(location.search).get("tab");
    if (wantTab === "set") switchTab("set");
    if (wantTab === "rgb") switchTab("rgb");
    if (wantTab === "misc") switchTab("misc");
    if (wantTab === "macros") switchTab("keys");
    // 注意：这里不能 switchTab("keys") —— `bindings` 在文件后面才用 let 声明，
    // boot() 又在顶层立即执行，此时访问会触发 TDZ ReferenceError，
    // 整个脚本主体（包括下面的 DOMContentLoaded 注册）都会中断。
    // 改键页的 demo 注入由文件末尾的 DOMContentLoaded 块负责。
    return;
  }
  api = window.pywebview.api;
  $("refreshBtn").onclick = () => refresh(true);
  bindKeysUi();
  bindMusicUi();
  render({
    connected: false, mode: "none", modeLabel: "正在连接…", model: "AULA F99 Pro",
    firmware: 24, battery: -1, charging: false, effect: 1,
    brightness: 4, brightnessMax: 9, speed: 4, speedMax: 4,
    color: 0, sleep: 10, effects: [], colors: [], colorNames: [],
    sleepOptions: [],
  });
  refresh(false);
  setInterval(() => refresh(false), 5000);
}

if (IS_DEMO) boot();
else {
  if (window.pywebview && window.pywebview.api) boot();
  else window.addEventListener("pywebviewready", boot);
}

/* ================= 改键 / 宏 ================= */
let bindings = null;          // {layers: {0: {idx: {x,y,w,h,rec}}}}
let curLayer = 0;
let keymap = {};              // idx -> geometry (来自 get_bindings，静态)
let editingKey = null;        // 当前弹窗的 idx
let macros = [];              // [{slot,name,steps}]
let meSteps = [];             // 编辑器步骤 [{name, delay, press}]

const LAYER_NAMES = ["默认按键层", "FN1 层", "FN2 层", "Tap 层"];

function switchTab(tab) {
  const light = tab === "light";
  const rgb = tab === "rgb";
  const keys = tab === "keys";
  const set = tab === "set";
  const misc = tab === "misc";
  $("tabLight").classList.toggle("active", light);
  $("tabRGB").classList.toggle("active", rgb);
  $("tabKeys").classList.toggle("active", keys);
  $("tabSet").classList.toggle("active", set);
  $("tabMisc").classList.toggle("active", misc);
  document.querySelector("main.layout:not(.keys-layout):not(.set-layout):not(.rgb-layout)").hidden = !light;
  $("rgbLayout").hidden = !rgb;
  $("keysLayout").hidden = !keys;
  $("setLayout").hidden = !set;
  $("miscLayout").hidden = !misc;
  if (misc) loadMisc();
  if (rgb) loadRgb();
  if (keys && !bindings && api && !IS_DEMO) loadBindings();
  if (keys) { loadKnob(); loadKnobBind(); }
  if (set) loadSettings();
  // 改键页隐藏时量不到 offsetWidth，字宽会按兜底值算；显示后重排一次
  if (keys) requestAnimationFrame(() => { if (bindings) renderKbd(true); });
}

function renderLayers() {
  document.querySelectorAll("#layerTabs button").forEach(b => {
    b.classList.toggle("active", Number(b.dataset.l) === curLayer);
    b.onclick = () => { curLayer = Number(b.dataset.l); renderLayers(); renderKbd(); };
  });
}

function recText(rec) {
  if (!rec) return "";
  if (rec.kind === "key") return "→" + rec.name;
  if (rec.kind === "combo") return rec.name;
  if (rec.kind === "mod") return rec.name;
  if (rec.kind === "media") return rec.name;
  if (rec.kind === "cmd") return rec.name;
  if (rec.kind === "mouse") return rec.name;
  if (rec.kind === "oem") return rec.name;
  if (rec.kind === "soft") return rec.name;
  if (rec.kind === "macro") return "宏 " + rec.slot;
  if (rec.kind === "fn") return "Fn";
  if (rec.kind === "special") return "未知 " + (rec.raw || "");
  return "";
}

/* 键帽下方小字宽度有限：长名字用缩写，悬浮 title 里给全名。
   catalog 里写的是好看的名字（「音量 +」），解码回来是紧凑名（「音量+」），
   查表前先去掉空白，两种写法都能命中。 */
const SHORT = {
  "播放/暂停": "播放", "停止": "停止", "上一曲": "上曲", "下一曲": "下曲",
  "音量+": "音+", "音量-": "音-", "静音": "静音",
  "屏幕亮度+": "亮+", "屏幕亮度-": "亮-", "亮度+": "亮+", "亮度-": "亮-",
  "计算器": "计算", "我的电脑": "电脑", "浏览器": "浏览", "收藏夹": "收藏",
  "媒体播放器": "播放器", "播放器": "播放器", "主页": "主页", "搜索": "搜索",
  "刷新": "刷新", "后退": "后退", "前进": "前进", "邮件": "邮件",
  "任务管理器": "任务", "关闭窗口": "关窗", "锁定计算机": "锁机",
  "运行命令": "运行", "显示桌面": "桌面", "放大": "放大", "缩小": "缩小",
  "撤销": "撤销", "保存": "保存", "锁Win键": "锁Win", "锁键盘": "锁键",
  "复位键盘": "复位", "恢复出厂设置": "出厂", "显示电量": "电量",
  "游戏模式": "游戏", "Windows系统模式": "Win", "Android系统模式": "安卓",
  "macOS系统模式": "Mac", "Linux系统模式": "Linux",
  "键盘亮度+": "键亮+", "键盘亮度-": "键亮-", "切换灯效": "灯效",
  "灯效速度+": "速+", "灯效速度-": "速-", "灯效速度复位": "速复",
  "切换灯效方向": "方向", "灯效方向切换": "方向",
  "2.4G通道": "2.4G", "蓝牙通道1": "BT1", "蓝牙通道2": "BT2", "蓝牙通道3": "BT3",
};

function shortName(name) {
  if (!name) return "";
  const key = String(name).replace(/\s+/g, "");
  if (SHORT[key]) return SHORT[key];
  const m = /^蓝牙通道(\d)$/.exec(key);
  if (m) return "BT" + m[1];
  const m2 = /^灯效(\d+)$/.exec(key);
  if (m2) return "灯" + m2[1];
  if (/^Ctrl\+/.test(key)) return "C+" + key.slice(5);
  if (/^Win\+/.test(key)) return "W+" + key.slice(4);
  if (/^Alt\+/.test(key)) return "A+" + key.slice(4);
  if (/^Shift\+/.test(key)) return "S+" + key.slice(6);
  return name;
}

let _measure = null;
function textWidth(str, fontPx) {
  if (!_measure) {
    const cv = document.createElement("canvas");
    _measure = cv.getContext("2d");
  }
  _measure.font = `${fontPx}px "Segoe UI", "Microsoft YaHei", sans-serif`;
  return _measure.measureText(str).width;
}

function fitText(text, widthPx, fontPx) {
  if (!text) return "";
  if (textWidth(text, fontPx) <= widthPx) return text;
  let out = "";
  for (const ch of text) {
    if (textWidth(out + ch, fontPx) > widthPx) break;
    out += ch;
  }
  return out || text.slice(0, 1);
}

function recKindClass(rec) {
  if (!rec) return "";
  if (rec.kind === "macro") return "macro";
  if (rec.kind === "mouse") return "mousekey";
  if (rec.kind === "key") return "remap";
  if (rec.kind === "soft") return "softkey";
  if (rec.kind === "fn") return "fnself";
  if (rec.kind === "empty" || rec.kind === "sentinel") return "";
  return "fnkey";
}

const LEGEND = [
  ["#0891b2", "普通改键 / 组合键", "这个键被换成了别的按键。例如 F1 改成「音量+」，或 Q 改成「Ctrl+C」。按键由电脑执行。"],
  ["#6d28d9", "键盘内建 / 多媒体 / 常用命令", "这个键不再发普通按键，而是让键盘（或系统）执行一个动作。例如切蓝牙通道、调灯效亮度、锁 Win、开任务管理器。"],
  ["#b45309", "鼠标按键", "按下这个键等于点鼠标左键 / 右键 / 中键。键盘自己发出一份鼠标报文，不经过电脑改键。"],
  ["#047857", "宏", "按下会依次重放录制好的一串按键，带每步延时。"],
  ["#c026d3", "软件功能", "切灯效、开关音乐律动这类键盘固件做不到的事。键盘发的是 F13～F20 虚拟键，由 AULA Hub 接住执行。"],
  ["#98a0b3", "无功能", "这个键在本层是空的，按下不产生任何输出。"],
];

function renderLegend() {
  const box = $("legendGrid");
  if (!box || box.childElementCount) return;
  LEGEND.forEach(([color, title, desc]) => {
    const d = document.createElement("div");
    d.className = "legend-item";
    d.innerHTML = `<div class="lg-t"><span class="lg-dot" style="background:${color}"></span>${esc(title)}</div>
      <div class="lg-d">${esc(desc)}</div>`;
    box.appendChild(d);
  });
}

/* 键帽印字与 HID 名字常不一致（Num7 / KP7、Bksp / Backspace、Ins / Insert）。
   判断「这个键还是它自己」时把两边归一化，否则每个数字键都会多出一行废话小字。 */
const NAME_ALIAS = {
  bksp: "backspace", ins: "insert", caps: "capslock", del: "delete",
  prtsc: "printscreen", scrlk: "scrolllock", numlk: "numlock",
  numenter: "kpenter", enter: "kpenter", "\\": "backslash", "`": "grave",
};
function normKeyName(s) {
  let n = String(s || "").toLowerCase().replace(/\s+/g, "");
  n = n.replace(/^num(?=[0-9*+\-/.]|$)/, "kp");
  return NAME_ALIAS[n] || n;
}

/* 小键盘那几个又窄又高的键（Num+ / Num- / Num* / Num/ / Num Enter）在原始布局里
   只有 20 单位宽，标签放不下会溢出边框。这里按宽度换短名，宽度够了不换。 */
const NARROW = {
  "Num+": "+", "Num-": "-", "Num*": "*", "Num/": "/", "Num.": ".",
  "Num0": "0", "Num1": "1", "Num2": "2", "Num3": "3", "Num4": "4",
  "Num5": "5", "Num6": "6", "Num7": "7", "Num8": "8", "Num9": "9",
  "NumLk": "Lock", "Enter": "Ent", "ScrLk": "Scr", "PrtSc": "Prt",
  "Bksp": "Bksp", "Caps": "Caps", "PgUp": "PgU", "PgDn": "PgD",
  "Calc": "Cal", "Home": "Hom", "Del": "Del",
};
function fitLabel(label, widthPx, fontPx) {
  if (textWidth(label, fontPx) <= widthPx - 2) return label;
  const alt = NARROW[label];
  if (alt && textWidth(alt, fontPx) <= widthPx - 2) return alt;
  return fitText(alt || label, widthPx - 2, fontPx);
}

function renderKbd(force) {
  const box = $("kbd");
  const layerKeys = bindings && bindings.layers ? bindings.layers[curLayer] : null;
  if (!layerKeys) return;
  if (force || box.dataset.layer !== String(curLayer) || box.childElementCount === 0) {
    box.innerHTML = "";
    box.dataset.layer = String(curLayer);
    const entries = Object.entries(layerKeys).map(([idx, g]) => ({ idx: Number(idx), ...g }));
    entries.sort((a, b) => a.idx - b.idx);
    let mx = 0, my = 0;
    entries.forEach(g => { mx = Math.max(mx, g.x + g.w); my = Math.max(my, g.y + g.h); });
    // 原始坐标 635×214：按容器宽度换算成像素；上限 2.6 让 1400px 窗口下键帽够大好看
    const wrap = box.parentElement;
    const avail = Math.max(420, (wrap ? wrap.clientWidth : 760) - 6);
    const s = Math.min(2.6, Math.max(0.6, avail / mx));
    box.style.transform = "none";
    box.style.width = Math.ceil(mx * s) + "px";
    box.style.height = Math.ceil(my * s) + "px";
    box.dataset.scale = String(s);
    entries.forEach(g => {
      const el = document.createElement("div");
      el.className = "key";
      el.style.left = Math.round(g.x * s) + "px";
      el.style.top = Math.round(g.y * s) + "px";
      el.style.width = Math.round(g.w * s) + "px";
      el.style.height = Math.round(g.h * s) + "px";
      el.style.borderRadius = Math.round(6 * s) + "px";
      el.dataset.idx = g.idx;
      const lab = g.label || ("#" + g.idx);
      el.dataset.label = lab;
      el.title = lab + " · 矩阵 #" + g.idx;
      el.onclick = () => openKeyModal(g.idx, g.label);
      box.appendChild(el);
      // 键名字号按实际宽度定；宽度小的键（Num+ / Num-）先缩字，再换短名
      const wpx0 = g.w * s;
      el._fs = Math.max(7, Math.min(12.5, (wpx0 - 4) / Math.max(1, lab.length) * 1.65));
    });
  }
  const s = Number(box.dataset.scale) || 1;
  box.querySelectorAll(".key").forEach(el => {
    const rec = layerKeys[el.dataset.idx] ? layerKeys[el.dataset.idx].rec : null;
    const label = el.dataset.label || "?";
    // 键映射回自己（默认层里的普通键）不算「改键」，不写小字，避免满屏 →Q →W
    const self = rec && rec.kind === "key" &&
      normKeyName(rec.name) === normKeyName(label);
    const t = self ? "" : recText(rec);
    el.classList.remove("remap", "macro", "fnkey");
    const kc = self ? "" : recKindClass(rec);
    if (kc) el.classList.add(kc);
    const fs = el._fs || 10;
    el.style.fontSize = fs + "px";
    const wpx = el.offsetWidth || 24;
    const subFs = Math.max(7, Math.min(9.5, fs - 2));
    const shown = t ? fitText(shortName(t), wpx - 3, subFs) : "";
    el.title = label + (t ? " → " + t : "") + " · 矩阵 #" + el.dataset.idx;
    const sub = shown ? `<span class="ksub" style="font-size:${subFs}px">${esc(shown)}</span>` : "";
    el.innerHTML = `<span class="klabel">${esc(fitLabel(label, wpx, fs))}</span>` + sub;
  });
}

function hidChar(u) {
  if (u == null) return "?";
  if (u >= 0x04 && u <= 0x1D) return String.fromCharCode(65 + (u - 0x04));
  if (u >= 0x1E && u <= 0x27) return String(u - 0x1D);          // 1..9,0
  return {0x28:"⏎",0x29:"Esc",0x2A:"⌫",0x2B:"Tab",0x2C:"␣",0x39:"Caps",
    0x3A:"F1",0x3B:"F2",0x3C:"F3",0x3D:"F4",0x3E:"F5",0x3F:"F6",0x40:"F7",
    0x41:"F8",0x42:"F9",0x43:"F10",0x44:"F11",0x45:"F12",0x49:"Ins",0x4A:"Home",
    0x4B:"PgUp",0x4C:"Del",0x4D:"End",0x4E:"PgDn",0x4F:"→",0x50:"←",0x51:"↓",0x52:"↑",
    0xE0:"Ctrl",0xE1:"Shift",0xE2:"Alt",0xE3:"Win",0xE5:"Shift"}[u] || "";
}

async function loadBindings() {
  try {
    toast("正在读取键盘布局…");
    bindings = await api.get_bindings();
    renderLayers(); renderKbd(); renderLegend();
    toast("布局已读取");
  } catch (e) {
    toast("读取失败：" + e, true);
  }
}

/* ---------- 键位弹窗 ---------- */
const CAT_LABELS = {
  media: "多媒体", soft: "软件功能", mouse: "鼠标", cmd: "常用命令",
  combo: "组合键", key: "键盘", mods: "修饰键", oem: "键盘内建",
};
const CAT_HINTS = {
  media: "多媒体 / 系统快捷：键盘直接把「音量+」「亮度+」这类系统指令发出去，不用记键码。绑成「亮度+ / 亮度−」时，Windows 会自己弹出系统原生亮度条（和笔记本自带亮度键一模一样）；这是我方唯一能唤起原生亮度条的办法，软件直接改亮度系统一律不弹。",
  soft: "软件功能：键盘自己做不到的事（切灯效、开关音乐律动），由本程序代你执行。键盘发出的是 F13～F20 这种虚拟键，只有 AULA Hub 在跑的时候才有反应。",
  mouse: "鼠标按键：按下这个键，电脑收到的是一次鼠标点击（左键 / 右键 / 中键）。",
  cmd: "常用命令：键盘固件自己执行（关窗口、锁屏、任务管理器等），走系统命令而不是按键。",
  combo: "组合键：一个修饰键加一个主键，例如 Ctrl+C 复制、Alt+Tab 切窗口。改完按一次就是整套组合。",
  key: "键盘：把这个键换成另一个普通按键，比如把 Caps 换成 Esc。",
  mods: "修饰键：单独一个 Ctrl / Shift / Alt / Win，按住它再配合别的键用。",
  oem: "键盘内建：切通道、切系统模式、锁 Win、调灯效亮度速度、显示电量。这些由键盘自己处理，不发给电脑。注意这里的「亮度」是键盘背光，不是屏幕亮度。",
};
let kmCat = "media";

function openKeyModal(idx, label) {
  editingKey = { idx, label, rec: null };
  const layerKeys = bindings && bindings.layers ? bindings.layers[curLayer] : null;
  const g = layerKeys ? layerKeys[idx] : null;
  editingKey.rec = g ? g.rec : null;
  $("kmTitle").textContent = "修改键位 · " + (label || ("#" + idx));
  $("kmGeo").textContent = LAYER_NAMES[curLayer] + " · 矩阵 #" + idx;
  $("kmCur").textContent = "当前功能：" + (recText(editingKey.rec) || "无功能");
  $("kmKey").value = "";
  document.querySelectorAll("#kmTabs button").forEach(b => {
    b.classList.toggle("active", b.dataset.cat === kmCat);
    b.onclick = () => { kmCat = b.dataset.cat; renderKmPanel(); markKmTabs(); };
  });
  renderKmPanel();
  renderKmMacros();
  $("keyModal").hidden = false;
  setTimeout(() => $("kmKey").focus(), 50);
}

function markKmTabs() {
  document.querySelectorAll("#kmTabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.cat === kmCat));
}

function renderKmPanel() {
  const box = $("kmPanel");
  box.innerHTML = "";
  const hint = $("kmHint");
  if (hint) hint.textContent = CAT_HINTS[kmCat] || "";
  const cat = (state && state.catalog) ? state.catalog[kmCat] : null;
  if (!cat) { box.innerHTML = '<div class="none">目录未加载</div>'; return; }
  cat.forEach(item => {
    const b = document.createElement("button");
    b.className = "km-chip";
    b.textContent = item.label;
    b.onclick = () => sendKeyAction(item.action, item.label);
    box.appendChild(b);
  });
}

function renderKmMacros() {
  const mBox = $("kmMacros");
  mBox.innerHTML = "";
  if (!macros.length) { mBox.innerHTML = '<div class="none">还没有宏，先在右侧新建。</div>'; return; }
  macros.forEach(m => {
    const b = document.createElement("button");
    b.className = "km-opt m";
    b.textContent = `宏 ${m.slot || "?"} · ${m.name}`;
    b.onclick = () => sendKeyAction({ type: "macro", slot: m.slot }, `绑宏「${m.name}」`);
    mBox.appendChild(b);
  });
}

async function sendKeyAction(action, label) {
  try {
    await api.set_key(curLayer, editingKey.idx, JSON.stringify(action));
    toast(`${editingKey.label || "#" + editingKey.idx} → ${label}（${LAYER_NAMES[curLayer]}）`);
    $("keyModal").hidden = true;
    await loadBindingsSilent();
  } catch (e) { toast("写入失败：" + e, true); }
}

async function loadBindingsSilent() {
  try {
    bindings = await api.get_bindings();
    renderKbd();
    if (editingKey && editingKey.idx === 108) await loadKnob();
  } catch (e) {}
}

async function applyKeyRemap() {
  const name = ($("kmKey").value || "").trim().toLowerCase();
  if (!name) return;
  try {
    await api.set_key(curLayer, editingKey.idx, JSON.stringify({ type: "key", name }));
    toast(`已改键 → ${name.toUpperCase()}（${LAYER_NAMES[curLayer]}）`);
    $("keyModal").hidden = true;
    await loadBindingsSilent();
  } catch (e) { toast("改键失败：" + e, true); }
}

async function applyKeyClear() {
  try {
    await api.set_key(curLayer, editingKey.idx, JSON.stringify({ type: "clear" }));
    toast(`已清除改键（${LAYER_NAMES[curLayer]}）`);
    $("keyModal").hidden = true;
    await loadBindingsSilent();
  } catch (e) { toast("清除失败：" + e, true); }
}

/* ---------- 宏编辑 ---------- */
function renderMacros() {
  const box = $("macroList");
  box.innerHTML = "";
  if (!macros.length) { box.innerHTML = '<div class="macro-empty">还没有宏。点「+ 新建宏」，或直接在下方录制步骤。</div>'; return; }
  macros.forEach((m, i) => {
    const el = document.createElement("div");
    el.className = "macro-item";
    const desc = m.steps.map(s => (s.press === false ? "↑" : "↓") + (s.name || "?")).join(" ");
    el.innerHTML = `<div><div class="mname"><span class="slot-tag">槽 ${m.slot || i + 1}</span>${esc(m.name)}</div>
      <div class="mdesc">${esc(desc)}</div></div>
      <button class="ghost-btn del">删除</button>`;
    el.querySelector(".del").onclick = async () => {
      macros.splice(i, 1);
      try { await pushMacros("已删除宏"); } catch (e) { toast("删除失败：" + e, true); }
    };
    box.appendChild(el);
  });
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function renderMeSteps() {
  const box = $("meSteps");
  box.innerHTML = "";
  meSteps.forEach((s, i) => {
    const c = document.createElement("span");
    c.className = "me-chip";
    c.innerHTML = `${s.press === false ? "↑" : "↓"} ${esc((s.name || "").toUpperCase())}${s.delay ? " ·" + s.delay + "ms" : ""} <span class="x">✕</span>`;
    c.querySelector(".x").onclick = () => { meSteps.splice(i, 1); renderMeSteps(); };
    box.appendChild(c);
  });
  if (!meSteps.length) box.innerHTML = '<span style="color:#98a0b3;font-size:12px">点击「+ 添加」拼步骤：↓按下 ↑释放</span>';
}

async function pushMacros(msg) {
  const payload = macros.map((m, i) => ({ name: m.name || ("宏" + (i + 1)), steps: m.steps }));
  await api.save_macros(JSON.stringify(payload));
  macros = await api.get_macros();
  renderMacros();
  toast(msg + "，已写入键盘");
}

function addMacro() {
  const n = macros.length + 1;
  macros.push({ name: "宏 " + n, steps: [] });
  $("macroEditor").hidden = false;
  $("meName").value = "宏 " + n;
  meSteps = macros[macros.length - 1].steps;
  renderMeSteps();
  $("macroTitle").textContent = `宏（${macros.length}）`;
}

async function saveMacro() {
  const name = ($("meName").value || "").trim() || "宏";
  const cur = macros[macros.length - 1];
  if (!cur) return;
  if (!meSteps.length) { toast("先添加至少一步", true); return; }
  cur.name = name; cur.steps = meSteps.slice();
  try {
    await pushMacros(`已保存「${name}」`);
    $("macroEditor").hidden = true;
    renderMacros();
  } catch (e) { toast("保存失败：" + e, true); }
}

async function addStep() {
  const name = ($("meKey").value || "").trim().toLowerCase();
  if (!name) { toast("输入键名，如 a / ctrl / f5", true); return; }
  const delay = Math.max(0, Math.min(1000, Number($("meDelay").value) || 0));
  meSteps.push({ name, delay, press: true });
  meSteps.push({ name, delay: 0, press: false });
  $("meKey").value = "";
  renderMeSteps();
  setTimeout(() => $("meKey").focus(), 30);
}


async function restoreAll() {
  if (!confirm("从最近的备份还原矩阵和宏库？")) return;
  try {
    await api.restore_all();
    toast("已还原，重新读取…");
    bindings = null; macros = [];
    await loadBindings();
    macros = await api.get_macros();
    renderMacros();
  } catch (e) { toast("还原失败：" + e, true); }
}

/* ================= 设置页 ================= */
function renderInfo(s) {
  const box = $("infoGrid");
  if (!box) return;
  const rows = [
    ["设备型号", s.model || "AULA F99 Pro", ""],
    ["连接通道", s.modeLabel || "未连接", ""],
    ["固件版本", s.firmware ? "v" + s.firmware : "—", ""],
    ["剩余电量", s.battery >= 0 ? s.battery + "%" + (s.charging ? " · 充电中" : "") : "读不到", ""],
    ["键盘矩阵", "128 键 · 4 层", ""],
    ["当前灯效", s.effects && s.effects.length ? (s.effects.find(e => e.ui === s.effect) || {}).name || "—" : "—", ""],
  ];
  box.innerHTML = rows.map(([k, v]) => `<div class="ig"><span>${esc(k)}</span><strong>${esc(String(v))}</strong></div>`).join("");
  $("setModeChip").textContent = s.connected ? (s.mode === "wired" ? "有线" : "2.4G") : "未连接";
  $("setSync").textContent = s.connected ? "刚刚同步" : (s.error || "等待键盘连接");
  $("setPulse").style.background = s.connected ? "var(--ok)" : "var(--muted-2)";
}

function renderSettings() {
  $("tgTray").setAttribute("aria-checked", appSettings.close_to_tray ? "true" : "false");
  $("tgAuto").setAttribute("aria-checked", appSettings.autostart ? "true" : "false");
  const sel = $("pollSelect");
  const want = String(appSettings.poll_seconds || 5);
  if (![...sel.options].some(o => o.value === want)) {
    const o = document.createElement("option");
    o.value = want; o.textContent = want + " 秒";
    sel.appendChild(o);
  }
  sel.value = want;
  startPolling(Number(want));
}

function startPolling(sec) {
  if (pollTimer) clearInterval(pollTimer);
  if (!sec) return;
  pollTimer = setInterval(() => { if (!$("setLayout").hidden) refresh(false); }, sec * 1000);
}

async function loadSettings() {
  renderInfo(state || {});
  if (IS_DEMO) { appSettings = { ...MOCK_SETTINGS }; renderSettings();
    renderBackups(await api.list_backups());
    renderRestoreWill({ ok: true, group: "demo01", parts: [
      { kind: "L0", time: "10-22 16:23:32" }, { kind: "L1", time: "10-22 16:23:32" },
      { kind: "L2", time: "10-22 16:23:32" }, { kind: "L3", time: "10-22 16:23:32" },
      { kind: "macro", time: "10-22 16:23:33" }, { kind: "page", time: "10-22 16:23:33" },
      { kind: "rgb", time: "10-22 16:23:33" }] });
    return; }
  try {
    appSettings = await api.get_settings();
    renderSettings();
    renderBackups(await api.list_backups());
    renderRestoreWill(await api.latest_backup());
  } catch (e) { toast("设置读取失败：" + e, true); }
}


function bkKindCn(kind) { return BK_KIND_CN[kind] || kind || "备份"; }

/* 把一堆文件按「同一次备份」折叠成行。
   带 group 号的是新版整组备份（同名 group 算一组）；
   老版本没有 group，就按「同一秒 + 同前缀」凑。 */
function groupBackups(items) {
  const map = new Map();
  for (const b of items || []) {
    const key = b.group
      ? "g:" + b.group
      : "t:" + (b.time || "") + ":" + (b.before ? "before" : "normal");
    if (!map.has(key)) {
      map.set(key, { time: b.time, mtime: b.mtime, before: !!b.before,
                     group: b.group || "", kinds: [], size: 0, count: 0, names: [] });
    }
    const g = map.get(key);
    g.kinds.push(bkKindCn(b.kind || b.tag));
    g.size += b.size || 0;
    g.count += 1;
    g.names.push(b.name);
    if ((b.mtime || 0) > (g.mtime || 0)) g.mtime = b.mtime;
  }
  // 组内按语义顺序排，别按写入顺序（同一秒里的先后是随机的）
  const ORDER = ["默认层", "FN1 层", "FN2 层", "Tap 层", "宏库", "灯效设置", "逐键颜色"];
  const rank = n => { const i = ORDER.indexOf(n); return i < 0 ? 99 : i; };
  for (const g of map.values()) g.kinds.sort((a, b) => rank(a) - rank(b));
  const rows = [...map.values()];
  rows.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  return rows;
}

function renderBackups(items) {
  const box = $("backupList");
  if (!box) return;
  if (!items || !items.length) {
    box.innerHTML = '<div class="backup-empty">还没有备份。改键或绑宏时才会自动生成。</div>';
    return;
  }
  const rows = groupBackups(items).slice(0, 8);
  box.innerHTML = rows.map(g => {
    const kinds = [...new Set(g.kinds)];
    const desc = kinds.length > 4
      ? kinds.slice(0, 4).join(" · ") + ` 等 ${kinds.length} 项`
      : kinds.join(" · ");
    const size = g.size >= 1024 ? (g.size / 1024).toFixed(1) + " KB" : g.size + " B";
    const tag = g.before
      ? '<span class="bk-flag warn">改动前</span>'
      : g.group ? '<span class="bk-flag">整组</span>' : "";
    return `<div class="backup-row" title="${esc(g.names.join("\n"))}">
      <span class="bk-time">${esc(g.time)}</span>
      ${tag}
      <span class="bk-desc">${esc(desc)}</span>
      <span class="bk-size">${esc(size)}</span>
    </div>`;
  }).join("");
}


/* 明确写出「点还原会发生什么」。以前按下去之前完全不知道会退回哪个时刻。 */
function renderRestoreWill(info) {
  const el = $("restoreWill");
  if (!el) return;
  if (!info || info.ok === false) {
    el.textContent = info && info.error ? "读不到备份信息：" + info.error : "";
    return;
  }
  const parts = (info.parts || []).map(p => BK_PART_CN[p.kind] || p.kind);
  if (!parts.length) {
    el.textContent = "还没有可还原的备份。先点右上角「立即备份一次」。";
    return;
  }
  const done = new Set(parts);
  // parts 已被 BK_PART_CN 翻成中文，缺项判断直接用中文名比对
  const missing = ["默认层", "FN1 层", "FN2 层", "Tap 层", "宏库"]
    .filter(n => !done.has(n));
  const when = (info.parts[0] || {}).time || "";
  el.textContent = `点还原会退回 ${when} 的状态：${parts.join(" · ")}` +
    (missing.length ? `（缺 ${missing.join("、")}，那部分保持现状）` : "");
  el.classList.toggle("warn", missing.length > 0);
}

async function patchSettings(patch) {
  if (IS_DEMO) { appSettings = { ...appSettings, ...patch }; renderSettings(); return; }
  try { appSettings = await api.set_settings(JSON.stringify(patch)); renderSettings(); }
  catch (e) { toast("保存失败：" + e, true); }
}

async function backupNow() {
  if (IS_DEMO) { toast("演示模式不写设备"); return; }
  try { const r = await api.backup_now(); toast(`已备份 ${r.files} 个文件`); renderBackups(await api.list_backups()); }
  catch (e) { toast("备份失败：" + e, true); }
}

function bindSettingsUi() {
  $("tabSet").onclick = () => switchTab("set");
  $("tgTray").onclick = () => patchSettings({ close_to_tray: !appSettings.close_to_tray });
  $("tgAuto").onclick = () => patchSettings({ autostart: !appSettings.autostart });
  $("pollSelect").onchange = (e) => patchSettings({ poll_seconds: Number(e.target.value) });
  $("backupNowBtn").onclick = backupNow;
  $("restoreLatestBtn").onclick = restoreAll;
  $("openBackupBtn").onclick = () => {
    if (IS_DEMO) { toast("演示模式没有备份目录"); return; }
    api.open_backup_dir().catch(e => toast("打不开：" + e, true));
  };
}

function bindKeysUi() {
  $("tabLight").onclick = () => switchTab("light");
  $("tabRGB").onclick = () => switchTab("rgb");
  $("tabKeys").onclick = () => switchTab("keys");
$("tabMisc").onclick = () => switchTab("misc");
  bindSettingsUi();
  $("kmClose").onclick = () => ($("keyModal").hidden = true);
  $("keyModal").addEventListener("click", e => { if (e.target === $("keyModal")) $("keyModal").hidden = true; });
  $("kmApply").onclick = applyKeyRemap;
  $("kmKey").addEventListener("keydown", e => { if (e.key === "Enter") applyKeyRemap(); });
  document.querySelector('.km-opt[data-act="clear"]').onclick = applyKeyClear;
  $("addMacroBtn").onclick = addMacro;
  $("meAddBtn").onclick = addStep;
  $("meKey").addEventListener("keydown", e => { if (e.key === "Enter") addStep(); });
  $("meSaveBtn").onclick = saveMacro;
  $("restoreBtn").onclick = restoreAll;
  bindRgbUi();
  bindKnobUi();
}

/* ================= 旋钮 / 模式键 ================= */
// 旋钮在键盘里就是矩阵第 0 层 #108 的一条 4 字节记录（工厂值 07 00 00 1d）。
// 它没有几何坐标，改键页画不出这颗键，只能在这张独立卡片里编辑。
//
// 快捷项优先用后端 get_knob().quick —— 那是 core.aula.KNOB_VERIFIED，
// 即上游 Linux 驱动实测能用的 7 个动作（+出厂标记共 8 条记录）。
// 下面的常量只是后端没给 quick 时的兜底，两边的 name 必须与
// core.aula.MEDIA 的原名一致（亮度+ 而不是「屏幕亮度+」），否则会抛
// 「不认识的功能」。
const KNOB_QUICK = [
  { label: "音量 +", action: { type: "media", name: "音量+" } },
  { label: "音量 −", action: { type: "media", name: "音量-" } },
  { label: "静音", action: { type: "media", name: "静音" } },
  { label: "亮度 +", action: { type: "media", name: "亮度+" } },
  { label: "亮度 −", action: { type: "media", name: "亮度-" } },
  { label: "下一曲", action: { type: "media", name: "下一曲" } },
  { label: "上一曲", action: { type: "media", name: "上一曲" } },
  { label: "播放 / 暂停", action: { type: "media", name: "播放/暂停" } },
];

let knobInfo = null;

async function loadKnob() {
  try {
    knobInfo = await api.get_knob();
    renderKnob();
  } catch (e) {
    const st = $("knobState");
    if (st) { st.textContent = "需插数据线"; st.className = "knob-state factory"; }
    const cur = $("knobCur");
    if (cur) cur.textContent = "当前功能：—（2.4G 能读矩阵但改不了，要看逐键功能请插数据线）";
    const rec = $("knobRec");
    if (rec) rec.textContent = "";
  }
}

function renderKnob() {
  if (!knobInfo) return;
  const st = $("knobState");
  const isFactory = !!knobInfo.factory;
  st.textContent = isFactory ? "出厂状态" : "已自定义";
  st.className = "knob-state" + (isFactory ? " factory" : "");
  $("knobCur").textContent = "当前功能：" +
    (recText(knobInfo.action) || "无功能");
  $("knobRec").textContent = "#108 · " + (knobInfo.record || "");

  // 当前记录不在「实测能用」的 8 条里时，明确提醒一句 ——
  // 设备会把记录原样存下，但旋钮转动时到底报不报得出来没人验证过。
  const note = $("knobUnverified");
  if (note) note.hidden = !!knobInfo.verified || isFactory;

  const box = $("knobPresets");
  box.innerHTML = "";
  const list = (knobInfo.quick && knobInfo.quick.length) ? knobInfo.quick : KNOB_QUICK;
  list.forEach(item => {
    const b = document.createElement("button");
    b.className = "km-chip";
    b.textContent = item.label;
    b.onclick = () => setKnobAction(item.action, item.label);
    box.appendChild(b);
  });
}

async function setKnobAction(action, label) {
  try {
    await api.set_knob(JSON.stringify(action));
    toast("旋钮 → " + label);
    await loadKnob();
  } catch (e) { toast("写入失败：" + e, true); }
}

// 「改成其他功能…」复用改键弹窗：把 editingKey 指向 #108，
// 提交时 sendKeyAction 会调 api.set_key(0, 108, ...) —— 与旋钮是同一条记录。
function openKnobModal() {
  editingKey = { idx: 108, label: "旋钮", rec: knobInfo ? knobInfo.action : null };
  $("kmTitle").textContent = "修改旋钮功能";
  $("kmGeo").textContent = "默认层 · 矩阵 #108（旋钮）";
  $("kmCur").textContent = "当前功能：" + (recText(editingKey.rec) || "无功能");
  $("kmKey").value = "";
  document.querySelectorAll("#kmTabs button").forEach(b => {
    b.classList.toggle("active", b.dataset.cat === kmCat);
    b.onclick = () => { kmCat = b.dataset.cat; renderKmPanel(); markKmTabs(); };
  });
  renderKmPanel();
  renderKmMacros();
  $("keyModal").hidden = false;
  setTimeout(() => $("kmKey").focus(), 50);
}

function bindKnobUi() {
  const edit = $("knobEdit");
  if (edit) edit.onclick = openKnobModal;
  const reset = $("knobReset");
  if (reset) reset.onclick = async () => {
    try {
      await api.reset_knob();
      toast("旋钮已恢复出厂");
      await loadKnob();
    } catch (e) { toast("恢复失败：" + e, true); }
  };
  bindKnobBindUi();
  bindMiscUi();
}

/* ---- 旋钮：顺时针 / 逆时针 / 按下 分开绑 ----
   出厂固件已经把方向翻成 VK_VOLUME_UP / DOWN / MUTE（真机实测），
   所以软件层拦这三个键，就能让两个方向各干各的。 */

async function loadKnobBind() {
  try {
    knobBind = await api.knob_bind();
    renderKnobBind();
  } catch (e) {
    const tip = $("knobBindTip");
    if (tip) tip.textContent = "读不到旋钮绑定：" + e;
  }
}

function renderKnobBind() {
  if (!knobBind) return;
  const b = knobBind.binding || {};
  const acts = knobBind.actions || [];
  // 这三个下拉框改的是「当前槽位」的绑定，永远可改。
  [["knobCw", b.cw], ["knobCcw", b.ccw], ["knobPress", b.press]]
    .forEach(([id, cur]) => {
      const sel = $(id);
      if (!sel) return;
      sel.innerHTML = "";
      acts.forEach(a => {
        const o = document.createElement("option");
        o.value = a.key;
        o.textContent = a.label;
        if (a.key === cur) o.selected = true;
        sel.appendChild(o);
      });
      sel.disabled = false;
      const row = sel.closest(".knob-row");
      if (row) {
        row.classList.remove("locked");
        const tag = row.querySelector(".knob-by-mode");
        if (tag) tag.remove();
      }
      sel.title = "";
    });
  const stepSel = $("knobStep");
  if (stepSel) stepSel.value = String(b.step || 1);

  const tg = $("knobTakeover");
  if (tg) tg.checked = !!b.enabled;

  const tip = $("knobBindTip");
  if (tip) {
    const on = !!b.enabled;
    tip.classList.toggle("on", on);
    tip.textContent = on
      ? "已接管：转旋钮按当前槽位绑的动作走。关掉就完全恢复系统音量。"
      : "打开「接管旋钮」后，转旋钮按当前槽位绑的动作走；关掉就是系统音量。";
  }
  const hits = $("knobHits");
  if (hits) {
    if (!b.enabled) {
      hits.textContent = "";
    } else {
      const h = knobBind.hits || 0;
      const w = knobBind.writes || 0;
      const tail = knobBind.lastAction ? " · 最近：" + actLabel(knobBind.lastAction) : "";
      // 显示「转了多少格 / 实际写了多少次」：差距越大说明防抖吃得越多，
      // 快速转时不会一下一下地闪（一次写入 79ms，固件收到会整块重载灯效）。
      hits.textContent = `（转了 ${h} 格 · 写设备 ${w} 次${tail}）`;
    }
  }
  // 软件动作键（矩阵里的 F13..F20）靠同一个钩子，跟旋钮接管开关无关，
  // 所以次数单独显示一行，别和「转了 N 格」混在一起看。
  const sh = $("knobSoftHits");
  if (sh) {
    const n = knobBind.softHits || 0;
    sh.textContent = n ? "软件功能键按了 " + n + " 次" : "";
  }
  // 固件模式守护：键盘长按会把自己切进「固件模式」，那时旋钮由固件
  // 直接调背光、软件一条按键都收不到。守护线程每 2 秒检查并写回。
  const gh = $("knobGuardHint");
  if (gh) {
    const mg = knobBind.modeGuard || {};
    let txt = "";
    if (mg.error) {
      txt = "固件模式守护：" + mg.error;
      gh.classList.add("warn");
    } else {
      gh.classList.remove("warn");
      const r = mg.rescued || 0;
      if (r > 0) {
        txt = "已自动把键盘从固件模式拉回 " + r +
              " 次（长按旋钮会让键盘进那个模式，进去后旋钮只调键盘背光）";
      }
    }
    gh.textContent = txt;
  }
  if (knobBind.error && knobBind.binding && knobBind.binding.enabled) {
    const tip2 = $("knobBindTip");
    if (tip2) { tip2.textContent = "接管没生效：" + knobBind.error; tip2.classList.add("on"); }
  }
  // 屏幕亮度只对笔记本内置屏有效（WMI）。外接屏走 DDC/CI，这台机器实测拿不到能力，
  // 所以不可用时要明说，别让人绑了发现没反应。
  renderKnobSlots();
  const sh2 = $("knobScreenHint");
  if (sh2) {
    const binds = [b.cw, b.ccw, b.press].map(String);
    const uses = binds.some(v => v === "screen_up" || v === "screen_down");
    if (knobBind.screenAvailable === false && uses) {
      sh2.textContent = "注意：这台机器调不了屏幕亮度（只支持笔记本内置屏，外接屏的 DDC/CI 这条路不通），绑了也不会有反应。";
      sh2.classList.add("show");
    } else {
      sh2.classList.remove("show");
      sh2.textContent = "";
    }
  }
}

/* ================= 旋钮槽位（长按旋钮在这些槽里轮回切换） ================= */

let slotEditIndex = -1;   // -1 = 不在编辑；>=0 = 正在编辑第几槽

function slotList() {
  return (knobBind && Array.isArray(knobBind.slots)) ? knobBind.slots : [];
}

function activeSlot() {
  const n = Number(knobBind && knobBind.active);
  const list = slotList();
  if (!list.length) return null;
  return list[(Number.isFinite(n) && n >= 0 && n < list.length) ? n : 0];
}

function renderKnobSlots() {
  if (!knobBind) return;
  const list = slotList();
  const cur = Number(knobBind.active) || 0;

  const box = $("knobSlotChips");
  if (box) {
    box.textContent = "";
    list.forEach((sl, idx) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "knob-slot-chip" + (idx === cur ? " on" : "");
      b.dataset.idx = String(idx);
      const n = document.createElement("span");
      n.className = "num";
      n.textContent = String(idx + 1);
      const t = document.createElement("span");
      t.className = "tx";
      t.textContent = sl.label || ("槽 " + (idx + 1));
      b.appendChild(n);
      b.appendChild(t);
      b.onclick = () => pickSlot(idx);
      box.appendChild(b);
    });
  }

  const robin = $("knobSlotsRobin");
  if (robin) {
    const sw = knobBind.slotWatch || {};
    if (sw.hits) {
      robin.textContent = "长按已切换 " + sw.hits + " 次";
      robin.classList.add("ok");
    } else if (sw.running === false) {
      robin.textContent = "长按监听没在跑";
      robin.classList.add("bad");
    } else if (state && state.mode && state.mode !== "wired") {
      robin.textContent = "";
      robin.classList.remove("ok");
    } else {
      robin.textContent = "";
      robin.classList.remove("ok", "bad");
    }
  }

  const hint = $("knobSlotHint");
  if (hint) {
    const sl = activeSlot();
    hint.textContent = list.length > 1
      ? "现在停在第 " + (cur + 1) + " 槽「" + ((sl && sl.label) || "") + "」。长按旋钮，按 1→2→…→" + list.length + "→1 轮回。"
      : "只有一个槽。在下面「+ 新增槽位」里加几个，长按旋钮就能在这几个之间轮回。";
  }
}

async function pickSlot(idx) {
  try {
    knobBind = await api.knob_set_slot(Number(idx));
    if (knobBind && knobBind.error) { toast("切换失败：" + knobBind.error, true); return; }
    renderKnobBind();
    const sl = activeSlot();
    toast("已切到第 " + (idx + 1) + " 槽「" + ((sl && sl.label) || "") + "」");
  } catch (e) { toast("切换失败：" + e, true); }
}

/* 槽位下拉框里的动作（和主绑定那三个共用同一份 ACTIONS） */
function fillSlotSelect(sel, value) {
  if (!sel) return;
  const acts = (knobBind && knobBind.actions) || [];
  sel.textContent = "";
  acts.forEach(a => {
    const o = document.createElement("option");
    o.value = a.key;
    o.textContent = a.label;
    if (a.key === value) o.selected = true;
    sel.appendChild(o);
  });
}

function openSlotForm(kind) {
  const form = $("knobSlotForm");
  if (!form) return;
  const list = slotList();
  const cur = Number(knobBind.active) || 0;

  if (kind === "add") {
    slotEditIndex = list.length;      // 追加到末尾
    if ($("knobSlotName")) $("knobSlotName").value = "";
    fillSlotSelect($("knobSlotCw"), "system");
    fillSlotSelect($("knobSlotCcw"), "system");
    fillSlotSelect($("knobSlotPress"), "system");
  } else {
    const sl = list[cur];
    if (!sl) return;
    slotEditIndex = cur;
    if ($("knobSlotName")) $("knobSlotName").value = sl.label || "";
    fillSlotSelect($("knobSlotCw"), sl.cw || "system");
    fillSlotSelect($("knobSlotCcw"), sl.ccw || "system");
    fillSlotSelect($("knobSlotPress"), sl.press || "system");
  }
  form.hidden = false;
}

function closeSlotForm() {
  const form = $("knobSlotForm");
  if (form) form.hidden = true;
  slotEditIndex = -1;
}

async function saveSlotForm() {
  const list = slotList().map(x => Object.assign({}, x));
  const name = (($("knobSlotName") || {}).value || "").trim();
  if (!name) { toast("先给这个槽起个名字", true); return; }
  const fresh = {
    id: "c" + Date.now().toString(36),
    label: name,
    cw: ($("knobSlotCw") || {}).value || "system",
    ccw: ($("knobSlotCcw") || {}).value || "system",
    press: ($("knobSlotPress") || {}).value || "system"
  };
  if (slotEditIndex >= 0 && slotEditIndex < list.length) {
    fresh.id = list[slotEditIndex].id || fresh.id;
    list[slotEditIndex] = fresh;
  } else {
    list.push(fresh);
  }
  try {
    knobBind = await api.knob_save_slots(JSON.stringify({
      active: Number(knobBind.active) || 0, slots: list
    }));
    if (knobBind && knobBind.error) { toast("保存失败：" + knobBind.error, true); return; }
    closeSlotForm();
    renderKnobBind();
    toast("已保存槽位「" + name + "」");
  } catch (e) { toast("保存失败：" + e, true); }
}

async function deleteSlot() {
  const list = slotList().map(x => Object.assign({}, x));
  if (list.length <= 1) { toast("至少要留一个槽", true); return; }
  const cur = Number(knobBind.active) || 0;
  const sl = list[cur];
  if (!sl) return;
  if (!confirm("删掉第 " + (cur + 1) + " 槽「" + (sl.label || "") + "」？")) return;
  list.splice(cur, 1);
  const nextActive = Math.min(cur, list.length - 1);
  try {
    knobBind = await api.knob_save_slots(JSON.stringify({ active: nextActive, slots: list }));
    if (knobBind && knobBind.error) { toast("删除失败：" + knobBind.error, true); return; }
    closeSlotForm();
    renderKnobBind();
    toast("已删除该槽位");
  } catch (e) { toast("删除失败：" + e, true); }
}

async function resetSlots() {
  if (!confirm("恢复出厂四个槽位（声音 / 屏幕亮度 / 键盘亮度 / 灯效）？你自建的槽会没掉。")) return;
  try {
    knobBind = await api.knob_reset_slots();
    if (knobBind && knobBind.error) { toast("恢复失败：" + knobBind.error, true); return; }
    closeSlotForm();
    renderKnobBind();
    toast("已恢复出厂槽位");
  } catch (e) { toast("恢复失败：" + e, true); }
}

function bindKnobSlotUi() {
  const add = $("knobSlotAdd");
  if (add) add.onclick = () => openSlotForm("add");
  const ed = $("knobSlotEdit");
  if (ed) ed.onclick = () => openSlotForm("edit");
  const dl = $("knobSlotDel");
  if (dl) dl.onclick = deleteSlot;
  const rs = $("knobSlotReset");
  if (rs) rs.onclick = resetSlots;
  const sv = $("knobSlotSave");
  if (sv) sv.onclick = saveSlotForm;
  const cc = $("knobSlotCancel");
  if (cc) cc.onclick = closeSlotForm;
}

function actLabel(key) {
  const list = (knobBind && knobBind.actions) || [];
  const hit = list.find(a => a.key === key);
  return hit ? hit.label : key;
}

async function patchKnobBind(patch, okMsg) {
  try {
    knobBind = await api.knob_set_bind(JSON.stringify(patch));
    renderKnobBind();
    if (okMsg) toast(okMsg);
  } catch (e) { toast("保存失败：" + e, true); }
}

function bindKnobBindUi() {
  bindKnobSlotUi();
  const tg = $("knobTakeover");
  if (tg) tg.onchange = async () => {
    try {
      knobBind = await api.knob_takeover(tg.checked);
      renderKnobBind();
      toast(tg.checked ? "旋钮已接管，方向各自生效" : "已交还给系统，旋钮恢复调音量");
    } catch (e) { toast("切换失败：" + e, true); tg.checked = !tg.checked; }
  };
  ["knobCw", "knobCcw", "knobPress"].forEach(id => {
    const sel = $(id);
    if (!sel) return;
    sel.onchange = () => {
      const key = { knobCw: "cw", knobCcw: "ccw", knobPress: "press" }[id];
      patchKnobBind({ [key]: sel.value }, "已保存：" + actLabel(sel.value));
    };
  });
  const stepSel = $("knobStep");
  if (stepSel) stepSel.onchange = () => patchKnobBind({ step: Number(stepSel.value) }, "每次跳 " + stepSel.value + " 档");
}

/* ================= 杂项（原版有、之前漏掉的功能） ================= */

let miscInfo = null;

function miscMsg(el, text, kind) {
  if (!el) return;
  el.hidden = false;
  el.textContent = text;
  el.classList.remove("ok", "bad");
  if (kind) el.classList.add(kind);
}

async function loadMisc() {
  const lead = $("miscLead");
  try {
    miscInfo = await api.misc_info();
  } catch (e) {
    if (lead) lead.textContent = "读不到杂项状态：" + e;
    return;
  }
  if (!miscInfo || miscInfo.error) {
    if (lead) lead.textContent = "读不到杂项状态：" + ((miscInfo && miscInfo.error) || "未知错误");
    return;
  }
  const chip = $("miscLinkChip");
  if (chip) chip.textContent = miscInfo.link === "wired" ? "有线连接" : (miscInfo.link === "wireless" ? "2.4G 无线" : "未连接");

  if (lead) {
    const t = [];
    t.push(miscInfo.link === "wired"
      ? "现在是有线连接，下面每一项都能用。"
      : "现在是 2.4G：灯效关闭、导入导出、看配置页都能用；写矩阵 / 写逐键颜色 / 写宏 / 恢复出厂做不了。");
    lead.textContent = t.join(" ");
  }

  const off = $("miscEffectOff");
  if (off) off.disabled = false;

  const reset = $("miscReset");
  if (reset) {
    reset.disabled = !miscInfo.canReset;
    reset.title = miscInfo.canReset ? "" : "恢复出厂只有有线连接能做";
  }
  const imp = $("miscImport");
  if (imp) imp.disabled = false;

  const foot = $("miscFoot");
  if (foot) {
    foot.textContent = miscInfo.link === "wired"
      ? "这些功能都会直接改设备，改之前建议先去「设置 → 备份」存一份。"
      : "2.4G 下写矩阵 / 写颜色 / 写宏在协议层就不存在（24 + 18 组命令组合实测全部无效），要改请插数据线。";
  }
  renderMiscSleep();
}

async function miscEffectOff() {
  try {
    const r = await api.misc_effect_off();
    if (r && r.error) { toast("关灯失败：" + r.error, true); return; }
    toast("灯效已关闭（原版 tc_kb_led20）");
    await refreshState(true);
  } catch (e) { toast("关灯失败：" + e, true); }
}

async function miscExport() {
  try {
    const r = await api.misc_export();
    if (r && r.error) { toast("导出失败：" + r.error, true); return; }
    toast("已导出 " + (r.path || "").split(/[\\/]/).pop() + "（" + Math.round((r.bytes || 0) / 1024) + " KB）");
  } catch (e) { toast("导出失败：" + e, true); }
}

async function miscImport() {
  try {
    const r = await api.misc_pick_import();
    if (r && r.error) { toast("导入失败：" + r.error, true); return; }
    const done = (r.done || []).length;
    const skip = (r.skipped || []).length;
    toast(skip ? ("写入 " + done + " 项，跳过 " + skip + " 项（" + r.skipped[0] + "）") : ("已写入 " + done + " 项"));
    await refreshState(true);
  } catch (e) { toast("导入失败：" + e, true); }
}

async function miscDebounce() {
  const el = $("miscDebounceOut");
  try {
    const r = await api.misc_read_debounce();
    if (r && r.error) { miscMsg(el, "读取失败：" + r.error, "bad"); return; }
    const nz = (r.page_nonzero || []).map(x => "0x" + x[0].toString(16).padStart(2, "0") + "=" + x[1]);
    miscMsg(el, (r.hint || "") + "\n\n配置页非零字节：" + nz.join("  "), "");
  } catch (e) { miscMsg(el, "读取失败：" + e, "bad"); }
}

function renderMiscSleep() {
  const sel = $("miscSleep");
  const hint = $("miscSleepHint");
  if (!sel) return;
  const opts = (state.sleepOptions && state.sleepOptions.length)
    ? state.sleepOptions : [60, 90, 120, 180, 240, 300, 600, 900, 1200];
  if (sel.childElementCount === 0) {
    opts.forEach(v => {
      const o = document.createElement("option");
      o.value = v; o.textContent = fmtSleep(v);
      sel.appendChild(o);
    });
    sel.onchange = () => miscSetSleep(Number(sel.value));
  }
  const map = { 2: 60, 3: 90, 4: 120, 6: 180, 8: 240, 10: 300, 20: 600, 30: 900, 40: 1200 };
  const cur = map[state.sleep];
  if (cur) sel.value = String(cur);
  if (hint) {
    hint.textContent = cur
      ? "当前：" + fmtSleep(cur) + "（设备里存的是档位 " + state.sleep + "）"
      : "读不到当前档位";
  }
}

async function miscSetSleep(sec) {
  try {
    await api.set_sleep(Number(sec));
    await refresh(true);
    renderMiscSleep();
    miscMsg("自动休眠已改为 " + fmtSleep(Number(sec)), true);
  } catch (e) {
    miscMsg("改休眠失败：" + e, false);
  }
}

async function miscProbeTime() {
  const el = $("miscTimeOut");
  miscMsg(el, "正在连读三次配置页…", "");
  try {
    const r = await api.misc_probe_time();
    if (r && r.error) { miscMsg(el, "探测失败：" + r.error, "bad"); return; }
    const mv = r.moving || [];
    miscMsg(el, mv.length
      ? ("这些偏移在 1.2 秒里自己变了（固件可能在走秒）：\n" + mv.map(i => "0x" + i.toString(16).padStart(2, "0")).join("  "))
      : "没有任何偏移自己变化 → 这个固件不在配置页里存时间。", mv.length ? "ok" : "");
  } catch (e) { miscMsg(el, "探测失败：" + e, "bad"); }
}

async function miscPageRead() {
  const box = $("miscPageHex");
  const out = $("miscPageOut");
  try {
    const r = await api.misc_read_page();
    if (r && r.error) { miscMsg(out, "读取失败：" + r.error, "bad"); return; }
    if (box) box.value = r.hex || "";
    miscMsg(out, "已读出 128 字节；非零 " + ((r.nonzero || []).length) + " 个。", "ok");
  } catch (e) { miscMsg(out, "读取失败：" + e, "bad"); }
}

async function miscPageWrite() {
  const box = $("miscPageHex");
  const out = $("miscPageOut");
  const hex = box ? box.value.replace(/\s+/g, "") : "";
  if (hex.length !== 256) { miscMsg(out, "必须是 128 字节（256 个十六进制字符），现在 " + (hex.length / 2) + " 字节。", "bad"); return; }
  if (!confirm("把这一整页写回设备？写错可能让键盘行为异常，但不会变砖（配置页随时能再写回来）。")) return;
  try {
    const r = await api.misc_write_page(hex);
    if (r && r.error) { miscMsg(out, "写入失败：" + r.error, "bad"); return; }
    miscMsg(out, r.match ? "写入成功，回读与写入完全一致。" : "写入完成，但回读不一致 —— 设备可能改了某些字节。", r.match ? "ok" : "bad");
    await refreshState(true);
  } catch (e) { miscMsg(out, "写入失败：" + e, "bad"); }
}

async function miscReset() {
  if (!confirm("恢复出厂会把灯效、改键、宏全部清掉，且不可撤销。\n建议先在「设置 → 备份」存一份。\n\n确定继续？")) return;
  if (!confirm("再确认一次：真的要恢复出厂？")) return;
  try {
    const r = await api.misc_reset_all();
    if (r && r.error) { toast("恢复失败：" + r.error, true); return; }
    toast("已发送恢复出厂命令，键盘会重连一下");
    await refreshState(true);
    loadMisc();
  } catch (e) { toast("恢复失败：" + e, true); }
}

function bindMiscUi() {
  const b1 = $("miscEffectOff");
  if (b1) b1.onclick = miscEffectOff;
  const b2 = $("miscExport");
  if (b2) b2.onclick = miscExport;
  const b3 = $("miscImport");
  if (b3) b3.onclick = miscImport;
  const b4 = $("miscDebounce");
  if (b4) b4.onclick = miscDebounce;
  const b5 = $("miscProbeTime");
  if (b5) b5.onclick = miscProbeTime;
  const b6 = $("miscPageRead");
  if (b6) b6.onclick = miscPageRead;
  const b7 = $("miscPageWrite");
  if (b7) b7.onclick = miscPageWrite;
  const b8 = $("miscReset");
  if (b8) b8.onclick = miscReset;
}

/* ================= 单键调色 ================= */
function rgbToHex(c) {
  return "#" + c.map(v => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
}

function hexToRgb(h) {
  h = String(h).replace("#", "");
  if (h.length === 3) h = h.split("").map(c => c + c).join("");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function isDark(c) {
  return c[0] + c[1] + c[2] < 24;
}

/* 相对亮度（0..1）：决定键帽上文字用黑还是白。
   纯蓝 (0,0,255) 的加权亮度只有 0.07 → 必须用白字，不能用「255-v」那套反色技巧。 */
function luma(c) {
  return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
}

async function loadRgb(force) {
  if (!api) return;
  if (rgbLoaded && !force) { renderRgbKbd(); return; }
  try {
    const st = state || {};
    if (st.mode !== "wired") {
      $("rgbTip").textContent = "现在走的是 2.4G。逐键颜色在 2.4G 下读得到、改不了（固件只开放读命令），要改请插数据线。";
    }
    if (!rgbGeo) {
      const km = await api.get_keymap();
      rgbGeo = km.keys || {};
    }
    const res = await api.get_rgb();
    rgbTable = (res.slots || []).map(c => [c[0], c[1], c[2]]);
    rgbLoaded = true;
    renderRgbKbd();
    renderRgbSel();
    updateRgbTip();
  } catch (e) {
    rgbLoaded = false;
    $("rgbTip").textContent = "读取失败：" + e;
    toast("读取颜色失败：" + e, true);
  }
}

/* 提示随状态变化：不是有线 / 不是自定义灯效 / 一切就绪 */
function updateRgbTip() {
  const st = state || {};
  const tip = $("rgbTip");
  const btn = $("rgbUseCustom");
  if (!tip) return;
  if (st.mode !== "wired") {
    tip.textContent = "现在走的是 2.4G。逐键颜色在 2.4G 下读得到、改不了（固件只开放读命令），要改请插数据线。";
    if (btn) btn.hidden = true;
    return;
  }
  const custom = Number(st.effect) === 19;
  if (!custom) {
    tip.textContent = `当前灯效是「${st.effectLabel || "内置灯效"}」，单键颜色不会显示。点右边的按钮切到自定义灯效。`;
    if (btn) btn.hidden = false;
  } else {
    tip.textContent = "点一个键 → 调色 → 刷到键盘。现在已经是自定义灯效，改完立刻就能看到。";
    if (btn) btn.hidden = true;
  }
}

function rgbScale() {
  const box = $("rgbKbd");
  const wrap = box.parentElement;
  let mx = 0, my = 0;
  Object.values(rgbGeo || {}).forEach(g => { mx = Math.max(mx, g.x + g.w); my = Math.max(my, g.y + g.h); });
  const avail = Math.max(420, (wrap ? wrap.clientWidth : 760) - 6);
  return { s: Math.min(2.6, Math.max(0.6, avail / Math.max(1, mx))), mx, my };
}

function renderRgbKbd() {
  const box = $("rgbKbd");
  if (!rgbGeo) return;
  if (!box.childElementCount) {
    const { s, mx, my } = rgbScale();
    box.style.width = Math.ceil(mx * s) + "px";
    box.style.height = Math.ceil(my * s) + "px";
    box.dataset.scale = String(s);
    Object.entries(rgbGeo).forEach(([idx, g]) => {
      const el = document.createElement("div");
      el.className = "key";
      el.style.left = Math.round(g.x * s) + "px";
      el.style.top = Math.round(g.y * s) + "px";
      el.style.width = Math.round(g.w * s) + "px";
      el.style.height = Math.round(g.h * s) + "px";
      el.style.borderRadius = Math.round(6 * s) + "px";
      el.dataset.idx = idx;
      el.dataset.label = g.label || ("#" + idx);
      el.onclick = (ev) => toggleRgbSel(Number(idx), ev);
      box.appendChild(el);
    });
  }
  paintRgbKeys();
}

function paintRgbKeys() {
  const box = $("rgbKbd");
  if (!box) return;
  const s = Number(box.dataset.scale) || 1;
  box.querySelectorAll(".key").forEach(el => {
    const idx = Number(el.dataset.idx);
    const label = el.dataset.label || "?";
    const c = rgbTable && rgbTable[idx] ? rgbTable[idx] : [0, 0, 0];
    const lit = !isDark(c);
    el.style.background = lit
      ? `linear-gradient(180deg, ${rgbToHex(c.map(v => Math.min(255, Math.round(v * 1.12 + 22))))}, ${rgbToHex(c)})`
      : "linear-gradient(180deg, #ffffff, #f1f3fa)";
    el.style.borderColor = lit ? "rgba(16,24,40,.18)" : "#dde1ee";
    el.classList.toggle("sel", rgbSel.has(idx));
    const wpx = Math.round((rgbGeo[idx] ? rgbGeo[idx].w : 20) * s);
    const fs = Math.max(7, Math.min(12.5, (wpx - 4) / Math.max(1, label.length) * 1.65));
    el.style.fontSize = fs + "px";
    // 亮底配深字、暗底配浅字；蓝/紫这类深彩色亮度低，仍用浅字
    el.style.color = lit ? (luma(c) > 0.62 ? "#1a1e2b" : "#f7f9ff") : "#3a4157";
    const tip = lit ? ` · ${rgbToHex(c)}` : " · 未点亮";
    el.title = `${label} · 矩阵 #${idx}${tip}`;
    el.innerHTML = `<span class="klabel">${esc(fitLabel(label, wpx, fs))}</span>`;
  });
}

function toggleRgbSel(idx, ev) {
  if (ev && (ev.ctrlKey || ev.metaKey)) {
    rgbSel.has(idx) ? rgbSel.delete(idx) : rgbSel.add(idx);
  } else if (ev && ev.shiftKey) {
    rgbSel.add(idx);
  } else {
    if (rgbSel.size === 1 && rgbSel.has(idx)) rgbSel.clear();
    else { rgbSel.clear(); rgbSel.add(idx); }
  }
  paintRgbKeys();
  renderRgbSel();
}

function renderRgbSel() {
  const n = rgbSel.size;
  $("rgbCount").textContent = `已选 ${n} 键`;
  if (n === 0) {
    $("rgbSelTitle").textContent = "未选中";
    $("rgbSelInfo").textContent = "点一个键，这里会显示它当前的颜色。";
    return;
  }
  if (n === 1) {
    const idx = [...rgbSel][0];
    const g = rgbGeo && rgbGeo[idx];
    const c = rgbTable && rgbTable[idx] ? rgbTable[idx] : [0, 0, 0];
    $("rgbSelTitle").textContent = g ? g.label : ("#" + idx);
    $("rgbSelInfo").innerHTML = `矩阵 <b>#${idx}</b><br>当前颜色 <b>${isDark(c) ? "未点亮" : rgbToHex(c)}</b>`;
    $("rgbSingleColor").value = rgbToHex(c);
  } else {
    $("rgbSelTitle").textContent = `${n} 个键`;
    const idxs = [...rgbSel].sort((a, b) => a - b);
    $("rgbSelInfo").innerHTML = `矩阵号：<b>${idxs.slice(0, 18).join(", ")}</b>${idxs.length > 18 ? " …" : ""}`;
  }
}

async function paintRgb(colorHex) {
  if (!rgbSel.size) { toast("先点几个键再上色", true); return; }
  const c = hexToRgb(colorHex);
  const mapping = {};
  [...rgbSel].forEach(i => { mapping[i] = c; });
  try {
    toast(`正在给 ${rgbSel.size} 个键上色…`);
    await api.set_key_colors(JSON.stringify(mapping));
    if (!rgbTable) rgbTable = Array.from({ length: 126 }, () => [0, 0, 0]);
    Object.keys(mapping).forEach(i => { rgbTable[Number(i)] = c; });
    paintRgbKeys();
    renderRgbSel();
    toast(`已刷 ${rgbSel.size} 个键`);
  } catch (e) {
    toast("上色失败：" + e, true);
  }
}

async function paintOne(colorHex) {
  const idx = [...rgbSel][0];
  if (idx == null) { toast("先点一个键", true); return; }
  const c = hexToRgb(colorHex);
  try {
    await api.set_key_color(idx, c[0], c[1], c[2]);
    if (!rgbTable) rgbTable = Array.from({ length: 126 }, () => [0, 0, 0]);
    rgbTable[idx] = c;
    paintRgbKeys();
    renderRgbSel();
    toast(`矩阵 #${idx} 已改色`);
  } catch (e) {
    toast("改色失败：" + e, true);
  }
}

async function fillAllRgb(colorHex) {
  const c = hexToRgb(colorHex);
  try {
    toast("正在整块上色…");
    const mapping = {};
    Object.keys(rgbGeo || {}).forEach(k => { mapping[k] = c; });
    await api.set_key_colors(JSON.stringify(mapping));
    if (!rgbTable) rgbTable = Array.from({ length: 126 }, () => [0, 0, 0]);
    Object.keys(mapping).forEach(k => { rgbTable[Number(k)] = c; });
    paintRgbKeys();
    toast("整块已上色");
  } catch (e) {
    toast("上色失败：" + e, true);
  }
}

async function clearAllRgb() {
  try {
    const mapping = {};
    Object.keys(rgbGeo || {}).forEach(k => { mapping[k] = [0, 0, 0]; });
    await api.set_key_colors(JSON.stringify(mapping));
    if (rgbTable) rgbTable = rgbTable.map(() => [0, 0, 0]);
    paintRgbKeys();
    toast("已全部熄灭");
  } catch (e) {
    toast("熄灭失败：" + e, true);
  }
}

function renderRgbPresets() {
  const box = $("rgbPresets");
  if (!box || box.childElementCount) return;
  RGB_PRESETS.forEach(hex => {
    const b = document.createElement("button");
    b.className = "rgb-preset";
    b.style.background = hex;
    b.dataset.hex = hex;
    b.title = hex;
    b.onclick = () => {
      $("rgbColor").value = hex;
      syncRgbColorUi();
      const tgt = $("wheelTarget") ? $("wheelTarget").value : "keys";
      if (tgt === "light") { applyLightColor(hex); return; }
      if (rgbSel.size) paintRgb(hex); else toast("已选颜色，点键后自动应用", false);
    };
    box.appendChild(b);
  });
}

function syncRgbColorUi() {
  const hex = $("rgbColor").value;
  $("rgbSwatchDot").style.background = hex;
  $("rgbHex").textContent = hex.toUpperCase();
  document.querySelectorAll(".rgb-preset").forEach(b =>
    b.classList.toggle("on", b.dataset.hex.toLowerCase() === hex.toLowerCase()));
  const sw = $("swCustom");
  if (sw) sw.style.setProperty("--pick", hex);
  // 色盘侧同步（从快捷色 / 十六进制输入进来时也要跟上）
  if (rgbWheelInited) {
    setRgbPickFromHex(hex, true);
    const pv = $("rgbPreview");
    if (pv) pv.style.background = hex;
    const hi = $("rgbHexInput");
    if (hi && document.activeElement !== hi) hi.value = hex.toUpperCase();
    const vn = $("rgbValNum");
    if (vn) vn.textContent = String(Math.round(rgbHsv.v * 100));
    const vs = $("rgbVal");
    if (vs) vs.value = String(Math.round(rgbHsv.v * 100));
  }
}

/* ---------------------------------------------------------------- 色盘
   角度 = 色相，半径 = 饱和度（圆心是白），下面的滑轨 = 明度。
   canvas 画一次 720x720 底图（retina），之后每次只重绘指针和明度蒙版。
   完全自己算 HSV，不依赖系统取色对话框。                                        */

function hsvToRgb(h, s, v) {
  h = ((h % 360) + 360) % 360;
  const c = v * s;
  const hp = h / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let r = 0, g = 0, b = 0;
  if (hp < 1) { r = c; g = x; }
  else if (hp < 2) { r = x; g = c; }
  else if (hp < 3) { g = c; b = x; }
  else if (hp < 4) { g = x; b = c; }
  else if (hp < 5) { r = x; b = c; }
  else { r = c; b = x; }
  const m = v - c;
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

function rgbToHsv(c) {
  const r = c[0] / 255, g = c[1] / 255, b = c[2] / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const d = mx - mn;
  let h = 0;
  if (d > 1e-6) {
    if (mx === r) h = 60 * (((g - b) / d) % 6);
    else if (mx === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  if (h < 0) h += 360;
  return { h: h, s: mx <= 1e-6 ? 0 : d / mx, v: mx };
}

// 给定色相+明度，画出整块圆盘；saturation 由半径决定，与颜色本身的 s 无关
function buildWheelBase() {
  const cv = document.createElement("canvas");
  cv.width = WHEEL_PX; cv.height = WHEEL_PX;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(WHEEL_PX, WHEEL_PX);
  const d = img.data;
  const R = WHEEL_PX / 2;
  for (let y = 0; y < WHEEL_PX; y++) {
    for (let x = 0; x < WHEEL_PX; x++) {
      const dx = x - R + 0.5, dy = y - R + 0.5;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const i = (y * WHEEL_PX + x) * 4;
      if (dist > R) { d[i + 3] = 0; continue; }
      const hue = (Math.atan2(dy, dx) * 180) / Math.PI;
      const sat = Math.min(1, dist / R);
      const rgb = hsvToRgb(hue, sat, 1);   // 满明度底图，明度用蒙版压暗
      d[i] = rgb[0]; d[i + 1] = rgb[1]; d[i + 2] = rgb[2];
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv;
}

function paintWheel() {
  const cv = $("rgbWheel");
  if (!cv || !rgbWheelBase) return;
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, WHEEL_PX, WHEEL_PX);
  ctx.drawImage(rgbWheelBase, 0, 0);
  // 明度蒙版：只在圆盘内部压暗
  if (rgbHsv.v < 0.999) {
    ctx.globalCompositeOperation = "source-atop";
    ctx.fillStyle = "rgba(0,0,0," + (1 - rgbHsv.v).toFixed(3) + ")";
    ctx.fillRect(0, 0, WHEEL_PX, WHEEL_PX);
    ctx.globalCompositeOperation = "source-over";
  }
  // 指针：色相角 + 饱和半径
  const R = WHEEL_PX / 2;
  const a = (rgbHsv.h * Math.PI) / 180;
  const rad = rgbHsv.s * R;
  const cx = R + Math.cos(a) * rad;
  const cy = R + Math.sin(a) * rad;
  ctx.lineWidth = 9; ctx.strokeStyle = "rgba(255,255,255,.95)";
  ctx.beginPath(); ctx.arc(cx, cy, 11, 0, Math.PI * 2); ctx.stroke();
  ctx.lineWidth = 4; ctx.strokeStyle = "rgba(16,24,40,.55)";
  ctx.beginPath(); ctx.arc(cx, cy, 14.5, 0, Math.PI * 2); ctx.stroke();
}

function rgbHexNow() {
  const c = hsvToRgb(rgbHsv.h, rgbHsv.s, rgbHsv.v);
  return rgbToHex(c);
}

// 把当前 HSV 写回 #rgbColor，并刷新所有显示
function commitWheelColor(applyToKeys) {
  const hex = rgbHexNow();
  $("rgbColor").value = hex;
  syncRgbColorUi();
  paintWheel();
  if (!applyToKeys) return;
  // 目标决定落点：整块键盘（灯光页）/ 选中的键（单键调色页）
  if ($("wheelTarget") && $("wheelTarget").value === "light") {
    applyLightColor(hex);
    return;
  }
  if (rgbSel.size) paintRgb(hex);
}

/* 灯光页整块上色。
   配置页的颜色字段只有 4bit（0..7 = 7 色 + 随机），固件存不下任意色，
   所以任意色只能写到单键 RGB 表；而单键色只在「自定义灯效 + 0x09=1」时
   显示，所以这里必须先切自定义，再整块刷。 */
async function applyLightColor(hex) {
  if (!api) return;
  if (state && state.mode !== "wired") {
    toast("2.4G 下逐键颜色只读不写，要改请插数据线", true);
    return;
  }
  if (!rgbGeo) {
    try {
      const km = await api.get_keymap();
      rgbGeo = km.keys || {};
    } catch (e) {
      toast("读取键位失败：" + e, true);
      return;
    }
  }
  try {
    await api.use_custom_rgb();
    await fillAllRgb(hex);
    refresh(true);
  } catch (e) {
    toast("刷色失败：" + e, true);
  }
}

function setRgbPickFromHex(hex, keepV) {
  const c = hexToRgb(hex);
  if (!c) return;
  const hsv = rgbToHsv(c);
  // 纯黑/纯白时 rgbToHsv 的色相无意义，保留旧色相免得指针乱跳
  rgbHsv.h = hsv.s <= 0.001 ? rgbHsv.h : hsv.h;
  rgbHsv.s = hsv.s;
  rgbHsv.v = keepV ? Math.max(hsv.v, 0.06) : hsv.v;
  paintWheel();
}

function pickFromWheelEvent(e) {
  const cv = $("rgbWheel");
  const box = cv.getBoundingClientRect();
  let nx = ((e.clientX - box.left) / box.width) * 2 - 1;
  let ny = ((e.clientY - box.top) / box.height) * 2 - 1;
  let r = Math.sqrt(nx * nx + ny * ny);
  if (r > 1) { nx /= r; ny /= r; r = 1; }
  rgbHsv.h = (Math.atan2(ny, nx) * 180) / Math.PI;
  rgbHsv.s = Math.min(1, r);
  commitWheelColor(false);
}

function bindRgbWheel() {
  if (rgbWheelInited) return;
  rgbWheelInited = true;
  rgbWheelBase = buildWheelBase();

  const btn = $("rgbPickBtn"), pop = $("rgbPop");
  const closePop = () => {
    pop.hidden = true;
    rgbWheelAnchor = null;
    btn.setAttribute("aria-expanded", "false");
    const sc = $("swCustom");
    if (sc) sc.setAttribute("aria-expanded", "false");
  };
  // 弹层是 fixed 定位，得自己算位置；锚点下方放不下就翻到上方。
  const positionPop = (anchor) => {
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth || 452;
    const h = pop.offsetHeight || 320;
    let left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
    let top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) {
      const above = r.top - h - 8;
      top = above >= 8 ? above : Math.max(8, window.innerHeight - h - 8);
    }
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  };
  // target: "keys"（单键调色页）| "light"（灯光页整块）
  const openPop = (anchor, target) => {
    rgbWheelAnchor = anchor || btn;
    $("wheelTarget").value = target || "keys";
    const isLight = $("wheelTarget").value === "light";
    const ap = $("rgbPopApply"), nt = $("rgbPopNote");
    if (ap) ap.hidden = !isLight;
    if (nt) {
      nt.textContent = isLight
        ? "选好颜色点上面的按钮，整块键盘会切到自定义灯效并刷成这个颜色。"
        : "选中的键会立刻变成这个颜色；没选键时只改画笔。";
    }
    pop.hidden = false;
    positionPop(rgbWheelAnchor);
    if (anchor) anchor.setAttribute("aria-expanded", "true");
    setRgbPickFromHex($("rgbColor").value, true);
    paintWheel();
    $("rgbPreview").style.background = $("rgbColor").value;
    $("rgbHexInput").value = $("rgbColor").value.toUpperCase();
    $("rgbVal").value = String(Math.round(rgbHsv.v * 100));
    $("rgbValNum").textContent = String(Math.round(rgbHsv.v * 100));
  };
  btn.setAttribute("aria-expanded", "false");
  btn.onclick = (e) => {
    e.stopPropagation();
    if (pop.hidden) openPop(btn, "keys"); else closePop();
  };
  // 灯光页那颗「自定义色」圆片由 renderColors() 动态追加，绑定时机不确定，
  // 所以用 capture 阶段的事件委托：既能在「点外面关闭」那条 document 监听之前
  // 抢到事件，又不受初始化顺序影响。
  document.addEventListener("click", (e) => {
    const swEl = e.target && e.target.closest ? e.target.closest("#swCustom") : null;
    if (!swEl) return;
    e.stopPropagation();
    e.preventDefault();
    if (pop.hidden) openPop(swEl, "light"); else closePop();
  }, true);
  window.addEventListener("resize", () => {
    if (!pop.hidden && rgbWheelAnchor) positionPop(rgbWheelAnchor);
  });
  pop.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { if (!pop.hidden) closePop(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) closePop(); });

  const cv = $("rgbWheel");
  cv.addEventListener("pointerdown", (e) => {
    rgbWheelDrag = true;
    try { cv.setPointerCapture(e.pointerId); } catch (_) {}
    pickFromWheelEvent(e);
  });
  cv.addEventListener("pointermove", (e) => { if (rgbWheelDrag) pickFromWheelEvent(e); });
  const endDrag = (e) => {
    if (!rgbWheelDrag) return;
    rgbWheelDrag = false;
    pickFromWheelEvent(e);
    commitWheelColor(true);
  };
  cv.addEventListener("pointerup", endDrag);
  cv.addEventListener("pointercancel", () => { rgbWheelDrag = false; });

  $("rgbVal").addEventListener("input", (e) => {
    rgbHsv.v = Math.max(0.02, Number(e.target.value) / 100);
    $("rgbValNum").textContent = e.target.value;
    commitWheelColor(false);
  });
  $("rgbVal").addEventListener("change", () => commitWheelColor(true));

  $("rgbHexInput").addEventListener("input", (e) => {
    let v = e.target.value.trim();
    if (v && v[0] !== "#") v = "#" + v;
    if (/^#[0-9a-fA-F]{6}$/.test(v)) {
      $("rgbColor").value = v.toLowerCase();
      setRgbPickFromHex(v, true);
      $("rgbPreview").style.background = v;
      $("rgbSwatchDot").style.background = v;
      $("rgbHex").textContent = v.toUpperCase();
    }
  });
  $("rgbHexInput").addEventListener("change", (e) => {
    let v = e.target.value.trim();
    if (v && v[0] !== "#") v = "#" + v;
    if (!/^#[0-9a-fA-F]{6}$/.test(v)) { e.target.value = $("rgbColor").value.toUpperCase(); return; }
    $("rgbColor").value = v.toLowerCase();
    syncRgbColorUi();
    commitWheelColor(true);
  });

  paintWheel();
}

function bindRgbUi() {
  renderRgbPresets();
  bindRgbWheel();
  syncRgbColorUi();
  const popApply = $("rgbPopApply");
  if (popApply) {
    popApply.onclick = async () => {
      await applyLightColor($("rgbColor").value);
      $("rgbPop").hidden = true;
    };
  }
  $("rgbApplyAll").onclick = () => fillAllRgb($("rgbColor").value);
  $("rgbClearAll").onclick = clearAllRgb;
  $("rgbReload").onclick = () => loadRgb(true);
  $("rgbSingleApply").onclick = () => paintOne($("rgbSingleColor").value);
  $("rgbUseCustom").onclick = async () => {
    try {
      await api.use_custom_rgb();
      toast("已切到自定义灯效，单键颜色现在会显示");
      refresh(false);
    } catch (e) { toast("切换失败：" + e, true); }
  };
  // demo 截图用：?demo=1&tab=rgb&wheel=1 自动展开色盘
  const wq = IS_DEMO ? new URLSearchParams(location.search).get("wheel") : null;
  if (wq) {
    window.addEventListener("DOMContentLoaded", () => setTimeout(() => {
      if (wq === "light") {
        // 灯光页的入口是 .colors 里动态追加的第 8 颗
        const st = $("swCustom");
        if (st) st.click();
      } else {
        const b = $("rgbPickBtn");
        if (b) b.click();
      }
    }, 300));
  }
}

/* ================= 音乐律动 ================= */
/* 后端 core/music.py：WASAPI 环回采系统声音 → FFT → 按频谱写 0x06 RGB 表。
   停止时会把开始前的单键颜色写回去，所以来回开关是安全的。 */

function mockMusicStatus() {
  return {
    running: false, mood: "cloud_snow", moodLabel: "云起雪飞 · 常规", fps: 22, gain: 0.92,
    frames: 0, peak: 0.42, error: null,
    // 与 core/music.py 的 PRESETS 一致（十种，名字照抄原版 tc_music_mode1..10）
    moods: [
      { key: "light_dance", label: "音频轻舞 · 轻柔" },
      { key: "dazzle", label: "光彩夺目 · 摇滚" },
      { key: "cloud_snow", label: "云起雪飞 · 常规" },
      { key: "light_field", label: "光场变化 · 人声" },
      { key: "stream", label: "溪水潺潺 · 常规" },
      { key: "bloom", label: "百花争艳 · 激情" },
      { key: "pearls", label: "珠落玉盘 · 摇滚" },
      { key: "moon", label: "彩云逐月 · 激情" },
      { key: "mountain", label: "高山流水 · 激情" },
      { key: "rain_silk", label: "飘雨如丝 · 常规" },
    ],
  };
}

function renderMusicMoods(info) {
  const box = $("musicMoods");
  if (!box) return;
  box.innerHTML = "";
  (info.moods || []).forEach(m => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "km-chip" + (m.key === info.mood ? " active" : "");
    b.textContent = m.label;
    b.onclick = async () => {
      try {
        musicInfo = await api.music_mood(m.key);
        renderMusic(musicInfo);
        toast("律动风格 → " + m.label);
      } catch (e) { toast("切换失败：" + e, true); }
    };
    box.appendChild(b);
  });
}

function renderMusic(info) {
  if (!info) return;
  musicInfo = info;
  const st = $("musicState");
  st.textContent = info.running ? "律动中" : "待机";
  st.classList.toggle("on", !!info.running);

  const btn = $("musicToggle");
  btn.textContent = info.running ? "停止律动" : "开始律动";
  btn.classList.toggle("primary-btn", !info.running);
  btn.classList.toggle("ghost-btn", !!info.running);

  $("musicGain").value = String(Math.round((info.gain || 1) * 100));
  $("musicGainVal").textContent = (info.gain || 1).toFixed(2) + "×";
  $("musicFps").value = String(info.fps || 22);
  $("musicFpsVal").textContent = (info.fps || 22) + " fps";

  const lvl = Math.max(0, Math.min(1, info.peak || 0));
  $("musicMeterFill").style.width = (lvl * 100).toFixed(0) + "%";

  const hint = $("musicHint");
  if (info.error) {
    hint.textContent = info.error;
  } else if (info.running) {
    hint.textContent = "已写 " + info.frames + " 帧 · 停止会还原原色";
  } else {
    hint.textContent = "停止时会把原来的单键颜色写回去";
  }

  renderMusicMoods(info);
}

async function musicPoll() {
  if (!api) return;
  // 不能只在 running 时轮询：musicInfo 初始是「待机」，一旦律动由别处（例如
  // Api 直接调用）启动，这里就永远不刷新，界面会一直卡在「待机」。
  try {
    renderMusic(await api.music_status());
  } catch (_) { /* 轮询失败不打扰用户 */ }
}

async function musicToggle() {
  if (!api) return;
  const btn = $("musicToggle");
  btn.disabled = true;
  try {
    if (musicInfo && musicInfo.running) {
      renderMusic(await api.music_stop());
      toast("律动已停止，原色已写回");
    } else {
      renderMusic(await api.music_start());
      toast("律动已开始");
    }
  } catch (e) {
    toast("" + (e && e.message ? e.message : e), true);
  } finally {
    btn.disabled = false;
  }
}

async function musicTune(patch) {
  if (!api) return;
  try {
    renderMusic(await api.music_tune(patch.fps, patch.gain));
  } catch (e) { toast("" + e, true); }
}

function bindMusicUi() {
  if (!$("musicToggle")) return;
  $("musicToggle").onclick = musicToggle;
  $("musicGain").oninput = () => {
    const g = Number($("musicGain").value) / 100;
    $("musicGainVal").textContent = g.toFixed(2) + "×";
  };
  $("musicGain").onchange = () => musicTune({ gain: Number($("musicGain").value) / 100 });
  $("musicFps").oninput = () => { $("musicFpsVal").textContent = $("musicFps").value + " fps"; };
  $("musicFps").onchange = () => musicTune({ fps: Number($("musicFps").value) });

  api.music_status().then(renderMusic).catch(() => {});
  if (musicTimer) clearInterval(musicTimer);
  musicTimer = setInterval(musicPoll, 700);
}

/* demo 模式：URL 带 &tab=keys 时切到改键页并注入假布局 */if (IS_DEMO && new URLSearchParams(location.search).get("tab") === "keys") {
  window.addEventListener("DOMContentLoaded", () => {
    switchTab("keys");
    const geo = window.AULA_KEYMAP || {};
    const demoKeys = {};
    Object.entries(geo).forEach(([idx, g]) => {
      demoKeys[idx] = { k: g.k, x: g.x, y: g.y, w: g.w, h: g.h, label: g.label, rec: { kind: "empty" } };
    });
    // 几个真实解码样例：Q 改成 Ctrl+C、F1 音量+、F2 亮度+、F3 任务管理器、F5 蓝牙通道1
    const samples = {
      20: { kind: "combo", mods: 1, usage: 6, name: "Ctrl+C" },
      12: { kind: "media", usage: 0x00E9, name: "音量+" },
      18: { kind: "media", usage: 0x006F, name: "亮度+" },
      24: { kind: "cmd", cmd: 7, name: "任务管理器" },
      36: { kind: "oem", grp: 7, code: 5, name: "蓝牙通道 1" },
      30: { kind: "macro", slot: 1, mode: 1, name: "宏 1" },
    };
    Object.entries(samples).forEach(([k, v]) => { if (demoKeys[k]) demoKeys[k].rec = v; });
    bindings = { layers: { 0: demoKeys, 1: demoKeys, 2: demoKeys, 3: demoKeys } };
    curLayer = 0;
    macros = [{ slot: 1, name: "示例宏", steps: [{ name: "a", delay: 20, press: true }, { name: "a", delay: 0, press: false }] }];
    renderLayers(); renderKbd(); renderMacros(); renderLegend();
    const qm = new URLSearchParams(location.search).get("km");
    if (qm) { kmCat = qm; openKeyModal(20, "Q"); markKmTabs(); }
  });
}
