/* ============================================================
 * 手冲咖啡复盘台 —— 前端逻辑（原生 JS，无框架）
 * 阶段计算与 Python 端 diagnostics.py 保持一致：
 *   相邻节点间，水量增加=注水段(流速=增量/时长)，水量不变=停顿段；
 *   第 1 个注水段为闷蒸段。
 * ============================================================ */
"use strict";

/* ---------------- 常量与状态 ---------------- */

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 760, H = 400, M = { top: 24, right: 56, bottom: 44, left: 58 };
const IW = W - M.left - M.right;   // 绘图区宽
const IH = H - M.top - M.bottom;   // 绘图区高

const state = {
  brews: [],
  thresholds: null,
  flavorLabels: {},
  variableLabels: {},
  current: null,          // 正在编辑的 brew（含 nodes）
  selectedNode: null,     // 节点索引
  compareId: null,
  compareBrew: null,      // 叠加对照的完整 brew
  diagnosis: null,        // 服务端返回的规则/归因
  locked: new Set(),
  suggest: null,
  dirty: false,
  diagTimer: null,
  dragging: null,         // {index, moved}
};

const LOCK_VARS = [
  { key: "grind", label: "研磨" },
  { key: "temp", label: "水温" },
  { key: "dose", label: "粉量" },
  { key: "ratio", label: "粉水比" },
  { key: "bloom", label: "闷蒸" },
  { key: "flow", label: "注水节奏" },
];

/* ---------------- 小工具 ---------------- */

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  if (parent) parent.appendChild(node);
  return node;
}

function htmlEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmt(n, digits = 1) {
  if (n == null || Number.isNaN(n)) return "–";
  return Number(n).toFixed(digits).replace(/\.0$/, "");
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m ? `${m}:${String(s).padStart(2, "0")}` : `${s}s`;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.hidden = true; }, 2200);
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opt);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok) throw new Error((data && data.error) || `请求失败 ${resp.status}`);
  return data;
}

/* ---------------- 本地阶段计算（与后端一致，用于即时反馈） ---------------- */

function sanitizeNodes(nodes) {
  const clean = [];
  for (const n of nodes || []) {
    if (n == null || typeof n !== "object") continue;
    // 与后端一致：显式 null 或非数字字段的节点直接跳过，缺字段按 0 处理
    const rt = n.t === undefined ? 0 : Number(n.t);
    const rw = n.w === undefined ? 0 : Number(n.w);
    if (Number.isNaN(rt) || Number.isNaN(rw)) continue;
    let t = Math.max(0, Math.round(rt));
    let w = Math.max(0, Math.round(rw * 10) / 10);
    if (clean.length && t <= clean[clean.length - 1].t) t = clean[clean.length - 1].t + 1;
    if (clean.length && w < clean[clean.length - 1].w) w = clean[clean.length - 1].w;
    clean.push({ t, w });
  }
  if (!clean.length) clean.push({ t: 0, w: 0 });
  if (clean[0].t !== 0 || clean[0].w !== 0) clean.unshift({ t: 0, w: 0 });
  return clean;
}

function computeStages(nodes) {
  nodes = sanitizeNodes(nodes);
  const stages = [];
  for (let i = 1; i < nodes.length; i++) {
    const a = nodes[i - 1], b = nodes[i];
    const duration = b.t - a.t;
    const added = Math.round((b.w - a.w) * 10) / 10;
    const kind = added > 0 ? "pour" : "pause";
    stages.push({
      index: i - 1,
      kind,
      t0: a.t, t1: b.t, duration,
      w0: a.w, w1: b.w, added,
      flow: kind === "pour" && duration > 0 ? Math.round(added / duration * 100) / 100 : 0,
      bloom: i === 1 && a.w === 0 && added > 0,
    });
  }
  return { nodes, stages };
}

function localSummary(nodes, brew) {
  const { stages } = computeStages(nodes);
  const pours = stages.filter((s) => s.kind === "pour");
  const pauses = stages.filter((s) => s.kind === "pause");
  const flows = pours.map((s) => s.flow);
  const totalTime = nodes.length ? nodes[nodes.length - 1].t : 0;
  const totalWater = nodes.length ? nodes[nodes.length - 1].w : 0;
  const dose = Number(brew.dose) || 0;
  const sorted = [...flows].sort((x, y) => x - y);
  let median = null;
  if (sorted.length) {
    const m = Math.floor(sorted.length / 2);
    median = sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
  }
  return {
    totalTime,
    totalWater: Math.round(totalWater * 10) / 10,
    actualRatio: dose ? Math.round(totalWater / dose * 100) / 100 : 0,
    pours, pauses, median,
    bloom: pours.find((s) => s.bloom) || null,
  };
}

/* ---------------- 编辑标记 ---------------- */

function markDirty(dirty = true) {
  state.dirty = dirty;
  $("dirtyBadge").hidden = !dirty;
  $("saveBtn").classList.toggle("primary", dirty);
  $("saveBtn").textContent = dirty ? "保存修改 ●" : "已保存";
}

/* ---------------- 加载与选择 ---------------- */

async function init() {
  bindUI();
  try {
    const [{ brews }, meta, recipesData] = await Promise.all([
      api("GET", "/api/brews"),
      api("GET", "/api/thresholds"),
      api("GET", "/api/water/recipes").catch(() => ({ recipes: [] })),
    ]);
    state.brews = brews;
    state.thresholds = meta.thresholds;
    state.flavorLabels = meta.flavor_labels;
    state.variableLabels = meta.variable_labels;
    buildFlavorChecks();
    buildLockChecks();
    fillBrewSelectors();
    await WaterModule.init();
    WaterModule.refreshRecipes(recipesData.recipes);
    if (brews.length) {
      const full = await api("GET", `/api/brews/${brews[0].id}`);
      loadBrew(full.brew, { resetCompare: true });
    } else {
      toast("库里没有记录，可点击「恢复示例」载入示例数据");
    }
  } catch (err) {
    toast(err.message);
  }
}

function fillBrewSelectors() {
  const sel = $("brewSelect");
  sel.innerHTML = "";
  const cmp = $("compareSelect");
  const cmpKeep = cmp.value;
  cmp.innerHTML = '<option value="">（不叠加）</option>';

  for (const b of state.brews) {
    const opt = htmlEl("option", null,
      `[V${b.version}] ${b.name}（${b.brew_date}）`);
    opt.value = b.id;
    sel.appendChild(opt);

    const copt = htmlEl("option", null,
      `[V${b.version}] ${b.name}（${b.brew_date}）`);
    copt.value = b.id;
    cmp.appendChild(copt);
  }
  if (state.current) sel.value = state.current.id;
  if (cmpKeep) cmp.value = cmpKeep;
}

function loadBrew(brew, opts = {}) {
  state.current = JSON.parse(JSON.stringify(brew));
  state.current.nodes = sanitizeNodes(state.current.nodes);
  state.selectedNode = null;
  state.diagnosis = null;
  state.suggest = null;
  $("suggestBox").classList.remove("show");
  $("brewSelect").value = brew.id;

  if (opts.resetCompare || (state.compareBrew && state.compareBrew.id === brew.id)) {
    state.compareId = null;
    state.compareBrew = null;
    $("compareSelect").value = "";
  }
  state.locked = new Set();
  document.querySelectorAll("#lockChecks input").forEach((i) => { i.checked = false; });
  document.querySelectorAll("#lockChecks label").forEach((l) => l.classList.remove("on"));

  fillForm();
  renderAll();
  renderVersions();
  markDirty(false);
  scheduleDiagnosis();
  if (typeof WaterModule !== "undefined") {
    WaterModule.onBrewLoaded(state.current);
  }
}

/* ---------------- 参数表单 ---------------- */

function fillForm() {
  const b = state.current;
  $("fName").value = b.name || "";
  $("fDate").value = b.brew_date || "";
  $("fDose").value = b.dose;
  $("fTemp").value = b.temp;
  $("fGrind").value = b.grind;
  $("fTargetRatio").value = b.target_ratio;
  $("fWater").value = b.water;
  $("fNotes").value = b.notes || "";
  document.querySelectorAll("#flavorChecks input").forEach((i) => {
    i.checked = (b.flavors || []).includes(i.value);
    i.closest("label").classList.toggle("on", i.checked);
  });
}

function buildFlavorChecks() {
  const box = $("flavorChecks");
  box.innerHTML = "";
  for (const [key, label] of Object.entries(state.flavorLabels)) {
    const lab = htmlEl("label");
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.value = key;
    inp.hidden = true;
    inp.addEventListener("change", () => {
      lab.classList.toggle("on", inp.checked);
      const flavors = state.current.flavors || [];
      state.current.flavors = inp.checked
        ? [...new Set([...flavors, key])]
        : flavors.filter((f) => f !== key);
      markDirty();
      renderFlavorMap();
      scheduleDiagnosis();
    });
    lab.append(inp, document.createTextNode(label));
    box.appendChild(lab);
  }
}

function buildLockChecks() {
  const box = $("lockChecks");
  box.innerHTML = "";
  for (const v of LOCK_VARS) {
    const lab = htmlEl("label");
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.value = v.key;
    inp.hidden = true;
    inp.addEventListener("change", () => {
      lab.classList.toggle("on", inp.checked);
      if (inp.checked) state.locked.add(v.key); else state.locked.delete(v.key);
    });
    lab.append(inp, document.createTextNode(v.label));
    box.appendChild(lab);
  }
}

function bindFormEvents() {
  const numMap = {
    fDose: "dose", fTemp: "temp", fGrind: "grind",
    fTargetRatio: "target_ratio", fWater: "water",
  };
  for (const [id, key] of Object.entries(numMap)) {
    $(id).addEventListener("input", () => {
      state.current[key] = parseFloat($(id).value) || 0;
      onEditorChange();
    });
  }
  $("fName").addEventListener("input", () => {
    state.current.name = $("fName").value;
    markDirty();
  });
  $("fDate").addEventListener("change", () => {
    state.current.brew_date = $("fDate").value;
    markDirty();
  });
  $("fNotes").addEventListener("input", () => {
    state.current.notes = $("fNotes").value;
    markDirty();
  });
}

function onEditorChange() {
  markDirty();
  renderAll({ skipChartDrag: true });
  scheduleDiagnosis();
}

/* ---------------- 诊断（防抖调用后端权威规则） ---------------- */

function scheduleDiagnosis() {
  if (!state.current) return;
  clearTimeout(state.diagTimer);
  state.diagTimer = setTimeout(runDiagnosis, 250);
}

async function runDiagnosis() {
  if (!state.current) return;
  try {
    const data = await api("POST", "/api/diagnose", { brew: state.current });
    // 请求期间用户可能已切换记录
    if (!state.current) return;
    state.diagnosis = data.diagnosis;
    renderRules();
    renderFlavorMap();
    renderChartDecorations();
    renderStageFlags();
  } catch (err) {
    // 后端不可用时不阻塞编辑
    console.warn("诊断失败：", err);
  }
}

/* ============================================================
 * SVG 曲线
 * ============================================================ */

function chartScales(nodes, overlayNodes) {
  const maxT = Math.max(
    ...nodes.map((n) => n.t),
    ...(overlayNodes || []).map((n) => n.t),
    120
  );
  const maxW = Math.max(
    ...nodes.map((n) => n.w),
    ...(overlayNodes || []).map((n) => n.w),
    100
  );
  const tMax = Math.ceil(maxT / 30) * 30;
  const wMax = Math.ceil(maxW / 50) * 50;
  const x = (t) => M.left + (t / tMax) * IW;
  const y = (w) => M.top + IH - (w / wMax) * IH;
  return { tMax, wMax, x, y };
}

function renderChart() {
  const svg = $("chart");
  svg.innerHTML = "";

  const b = state.current;
  const nodes = sanitizeNodes(b.nodes);
  const overlayNodes = state.compareBrew ? sanitizeNodes(state.compareBrew.nodes) : null;
  const sc = chartScales(nodes, overlayNodes);

  // 网格与坐标轴
  const grid = el("g", { class: "grid" }, svg);
  const axis = el("g", { class: "axis" }, svg);

  const tStep = sc.tMax <= 180 ? 30 : 60;
  for (let t = 0; t <= sc.tMax; t += tStep) {
    el("line", { x1: sc.x(t), y1: M.top, x2: sc.x(t), y2: M.top + IH }, grid);
    const txt = el("text", { x: sc.x(t), y: M.top + IH + 16, "text-anchor": "middle", text: `${t}s` }, axis);
  }
  const wStep = sc.wMax <= 250 ? 50 : 100;
  for (let w = 0; w <= sc.wMax; w += wStep) {
    el("line", { x1: M.left, y1: sc.y(w), x2: M.left + IW, y2: sc.y(w) }, grid);
    el("text", { x: M.left - 8, y: sc.y(w) + 3.5, "text-anchor": "end", text: `${w}g` }, axis);
  }
  el("line", { x1: M.left, y1: M.top + IH, x2: M.left + IW, y2: M.top + IH }, axis);
  el("line", { x1: M.left, y1: M.top, x2: M.left, y2: M.top + IH }, axis);
  el("text", {
    class: "axis-label", x: M.left + IW / 2, y: H - 8,
    "text-anchor": "middle", text: "时间（秒）",
  }, axis);
  el("text", {
    class: "axis-label", x: 14, y: M.top + IH / 2,
    "text-anchor": "middle",
    transform: `rotate(-90 14 ${M.top + IH / 2})`,
    text: "累计水量（g）",
  }, axis);

  // 闷蒸底色区（闷蒸段 + 其后一个停顿，取并集）
  const { stages } = computeStages(nodes);
  const bloom = stages.find((s) => s.bloom);
  if (bloom) {
    let x1 = sc.x(bloom.t0);
    let end = bloom.t1;
    const after = stages.find((s) => s.index === bloom.index + 1 && s.kind === "pause");
    if (after) end = after.t1;
    el("rect", {
      class: "bloom-band",
      x: x1, y: M.top,
      width: Math.max(2, sc.x(end) - x1), height: IH, rx: 3,
    }, svg);
  }

  // 目标水量虚线
  const targetWater = (Number(b.dose) || 0) * (Number(b.target_ratio) || 0);
  if (targetWater > 0 && targetWater <= sc.wMax * 1.15) {
    const ty = sc.y(Math.min(targetWater, sc.wMax));
    el("line", { class: "target-line", x1: M.left, y1: ty, x2: M.left + IW, y2: ty }, svg);
    el("text", {
      class: "target-label", x: M.left + IW - 4, y: ty - 4,
      "text-anchor": "end", text: `目标 ${fmt(targetWater, 0)}g`,
    }, svg);
  }

  // 面积填充
  el("path", {
    class: "area-fill",
    d: `M ${sc.x(nodes[0].t)} ${sc.y(0)}` +
       nodes.map((n) => ` L ${sc.x(n.t)} ${sc.y(n.w)}`).join("") +
       ` L ${sc.x(nodes[nodes.length - 1].t)} ${sc.y(0)} Z`,
  }, svg);

  // 叠加曲线
  if (overlayNodes) {
    const d = overlayNodes.map((n, i) =>
      `${i ? "L" : "M"} ${sc.x(n.t)} ${sc.y(n.w)}`).join(" ");
    el("path", { class: "overlay-curve", d }, svg);
    overlayNodes.forEach((n) => {
      el("circle", { class: "node-dot overlay-node", cx: sc.x(n.t), cy: sc.y(n.w), r: 3.2 }, svg);
    });
    // 叠加曲线名称
    el("text", {
      class: "target-label", x: M.left + 6, y: M.top + 12,
      fill: "#3d7a8c",
      text: `对照：${state.compareBrew.name}`,
    }, svg);
    $("overlayLegend").hidden = false;
  } else {
    $("overlayLegend").hidden = true;
  }

  // 分段折线（注水实线 / 停顿虚线）+ 段标签
  const segLayer = el("g", { class: "segments" }, svg);
  stages.forEach((s) => {
    el("line", {
      class: `curve-line seg-${s.kind}`,
      x1: sc.x(s.t0), y1: sc.y(s.w0),
      x2: sc.x(s.t1), y2: sc.y(s.w1),
    }, segLayer);
    renderSegmentTag(svg, s, sc);
  });

  // 节点（先放大透明热区，再画可见圆点）
  const nodeLayer = el("g", { class: "nodes" }, svg);
  nodes.forEach((n, i) => {
    const hit = el("circle", {
      class: "node-hit",
      cx: sc.x(n.t), cy: sc.y(n.w), r: 13,
      "data-index": i,
    }, nodeLayer);
    let dotClass = "node-dot";
    const stageBefore = stages.find((s) => s.index === i - 1);
    if (i === 0) dotClass += " bloom";
    if (stageBefore) {
      if (stageBefore.bloom) dotClass += " bloom";
      else if (stageBefore.kind === "pause") dotClass += " pause-flat";
    }
    if (state.selectedNode === i) dotClass += " sel";
    const dot = el("circle", { class: dotClass, cx: sc.x(n.t), cy: sc.y(n.w), r: 5.5 }, nodeLayer);

    hit.addEventListener("mouseenter", () => showTip(hit, n, i));
    hit.addEventListener("mouseleave", hideTip);
    hit.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      deleteNode(i);
    });
  });

  bindChartEvents(svg);

  // 键盘微调
  svg.tabIndex = 0;
  svg.onkeydown = (e) => {
    if (state.selectedNode == null) return;
    const i = state.selectedNode;
    const n = nodes[i];
    const step = e.shiftKey ? 5 : 1;
    let { t, w: wv } = n;
    if (e.key === "ArrowLeft") t -= step;
    else if (e.key === "ArrowRight") t += step;
    else if (e.key === "ArrowUp") wv += step;
    else if (e.key === "ArrowDown") wv -= step;
    else return;
    e.preventDefault();
    updateNode(i, t, wv);
  };

  renderChartDecorations();
}

/* 段中部标签：注水段标流速，停顿段标停顿时长；命中规则标红 */
function renderSegmentTag(svg, s, sc) {
  const flaggedStages = flaggedStageSet();
  const isPause = s.kind === "pause";
  const mx = (sc.x(s.t0) + sc.x(s.t1)) / 2;
  const my = (sc.y(s.w0) + sc.y(s.w1)) / 2;
  const label = isPause ? `${s.duration}s` : `${fmt(s.flow, 2)} g/s`;
  // 太短的段不画标签，避免重叠
  if (s.t1 - s.t0 < (isPause ? 6 : 8)) return;

  const flagged = flaggedStages.has(s.index);
  const g = el("g", { "data-stage": s.index, style: "cursor:pointer" }, svg);
  const pillW = isPause ? 30 : 46, pillH = 13;
  el("rect", {
    class: `tag-bg${isPause ? " pause" : ""}${flagged ? " flag" : ""}`,
    x: mx - pillW / 2, y: my - pillH / 2 - 8,
    width: pillW, height: pillH, rx: 6.5,
  }, g);
  el("text", {
    class: "seg-tag", x: mx, y: my - 8,
    text: label,
  }, g);
  g.addEventListener("click", () => focusStage(s.index));
}

/* 规则命中的 ⚠ 标记，挂到对应段中点 */
function renderChartDecorations() {
  const svg = $("chart");
  if (!svg) return;
  svg.querySelectorAll(".rule-marker-g").forEach((n) => n.remove());
  if (!state.diagnosis) return;

  const nodes = sanitizeNodes(state.current.nodes);
  const sc = chartScales(nodes, state.compareBrew ? sanitizeNodes(state.compareBrew.nodes) : null);
  const flagged = new Map();
  for (const r of state.diagnosis.rules) {
    for (const si of r.stages) {
      if (!flagged.has(si)) flagged.set(si, []);
      flagged.get(si).push(r);
    }
  }
  const { stages } = computeStages(nodes);
  flagged.forEach((_rules, si) => {
    const s = stages.find((x) => x.index === si);
    if (!s) return;
    const mx = (sc.x(s.t0) + sc.x(s.t1)) / 2;
    const my = (sc.y(s.w0) + sc.y(s.w1)) / 2;
    const g = el("g", { class: "rule-marker-g", style: "cursor:pointer" }, svg);
    el("text", { class: "rule-marker", x: mx, y: my - 24, text: "⚠" }, g);
    g.addEventListener("click", () => focusStage(si));
  });
}

function flaggedStageSet() {
  const set = new Set();
  if (state.diagnosis) {
    for (const r of state.diagnosis.rules) r.stages.forEach((i) => set.add(i));
  }
  return set;
}

/* ---------------- 图表交互（事件只绑定一次） ---------------- */

// 图表级事件在第一次渲染时绑定，之后复用；坐标换算时即时取最新节点
let chartEventsBound = false;

function svgPoint(svg, evt) {
  const rect = svg.getBoundingClientRect();
  return {
    x: (evt.clientX - rect.left) * (W / rect.width),
    y: (evt.clientY - rect.top) * (H / rect.height),
  };
}

function bindChartEvents(svg) {
  if (chartEventsBound) return;
  chartEventsBound = true;

  svg.addEventListener("pointerdown", (e) => {
    const hit = e.target.closest ? e.target.closest(".node-hit") : null;
    if (!hit) return;
    const index = Number(hit.dataset.index);
    state.dragging = { index };
    state.selectedNode = index;
    svg.setPointerCapture(e.pointerId);
    hideTip();
    updateNodeEditor();
    renderAll();
  });

  svg.addEventListener("pointermove", (e) => {
    if (!state.dragging) return;
    const { index } = state.dragging;
    const sc = currentScales();
    const pt = svgPoint(svg, e);
    let t = Math.round((pt.x - M.left) / IW * sc.tMax);
    let w = Math.round((M.top + IH - pt.y) / IH * sc.wMax);
    // 末节点向右/向上拖时允许坐标轴随之扩展
    t = Math.max(0, Math.min(sc.tMax + 30, t));
    w = Math.max(0, Math.min(sc.wMax + 30, w));

    const cur = state.current.nodes[index];
    if (!cur) return;
    if (cur.t !== t || cur.w !== w) {
      updateNode(index, t, w, true);
    }
  });

  const endDrag = () => {
    if (state.dragging) {
      state.dragging = null;
      state.current.nodes = sanitizeNodes(state.current.nodes);
      renderAll();
      scheduleDiagnosis();
    }
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);

  svg.addEventListener("dblclick", (e) => {
    if (e.target.classList && e.target.classList.contains("node-hit")) return;
    const sc = currentScales();
    const pt = svgPoint(svg, e);
    if (pt.x < M.left || pt.x > M.left + IW || pt.y < M.top || pt.y > M.top + IH) return;
    const t = Math.max(0, Math.round((pt.x - M.left) / IW * sc.tMax));
    const w = Math.max(0, Math.round((M.top + IH - pt.y) / IH * sc.wMax));
    insertNodeAt(t, w);
  });
}

function currentScales() {
  const nodes = sanitizeNodes(state.current.nodes);
  const overlayNodes = state.compareBrew ? sanitizeNodes(state.compareBrew.nodes) : null;
  return chartScales(nodes, overlayNodes);
}

/* ---------------- 节点增删改 ---------------- */

function updateNode(index, t, w, fromDrag = false) {
  const nodes = state.current.nodes;
  if (index < 0 || index >= nodes.length) return;
  t = Math.max(0, Math.round(t));
  w = Math.max(0, Math.round(w));
  if (index === 0) { t = 0; w = 0; }
  // 不允许越过相邻节点的时间
  if (index > 0) t = Math.max(t, nodes[index - 1].t + 1);
  if (index < nodes.length - 1) t = Math.min(t, nodes[index + 1].t - 1);
  nodes[index] = { t, w };
  markDirty();
  if (!fromDrag) state.current.nodes = sanitizeNodes(nodes);
  renderAll({ skipChartDrag: true });
  updateNodeEditor();
  scheduleDiagnosis();
}

function insertNodeAt(t, w) {
  const nodes = state.current.nodes;
  let k = 0;
  while (k < nodes.length && nodes[k].t < t) k++;
  // 与既有节点过近则不插入
  const near = nodes.find((n) => Math.abs(n.t - t) <= 2);
  if (near) return;
  nodes.splice(k, 0, { t, w });
  state.selectedNode = k;
  state.current.nodes = sanitizeNodes(nodes);
  markDirty();
  renderAll();
  updateNodeEditor();
  scheduleDiagnosis();
}

function deleteNode(index) {
  const nodes = state.current.nodes;
  if (index <= 0 || index >= nodes.length - 1) {
    toast("首节点和末节点不能删除");
    return;
  }
  nodes.splice(index, 1);
  state.selectedNode = null;
  markDirty();
  renderAll();
  updateNodeEditor();
  scheduleDiagnosis();
}

function updateNodeEditor() {
  const i = state.selectedNode;
  const selT = $("selT"), selW = $("selW");
  if (i == null || !state.current.nodes[i]) {
    selT.value = ""; selW.value = "";
    $("selInfo").textContent = "（点击曲线上的圆点选择）";
    $("delNodeBtn").disabled = true;
    return;
  }
  const n = state.current.nodes[i];
  selT.value = n.t;
  selW.value = n.w;
  $("selInfo").textContent = `节点 ${i + 1}/${state.current.nodes.length}`;
  $("delNodeBtn").disabled = (i === 0 || i === state.current.nodes.length - 1);
}

function focusStage(stageIndex) {
  // 高亮对应阶段行并滚动
  const row = document.querySelector(`#stageTable tr[data-stage="${stageIndex}"]`);
  if (row) {
    row.scrollIntoView({ behavior: "smooth", block: "nearest" });
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1200);
  }
}

/* ---------------- tooltip ---------------- */

function showTip(hit, n, index) {
  if (state.dragging) return;
  const { stages } = computeStages(state.current.nodes);
  const lines = [`<b>节点 ${index + 1}</b>：${n.t}s · ${fmt(n.w)}g`];
  if (index > 0) {
    const s = stages.find((x) => x.index === index - 1);
    if (s) {
      lines.push(s.kind === "pour"
        ? `上段：+${fmt(s.added)}g / ${s.duration}s = ${fmt(s.flow, 2)} g/s`
        : `上段：停顿 ${s.duration}s`);
      if (s.bloom) lines.push("🌱 闷蒸段");
    }
  }
  const tip = $("chartTip");
  tip.innerHTML = lines.join("<br>");
  tip.hidden = false;
  const svgRect = $("chart").getBoundingClientRect();
  const wrapRect = $("chart").parentElement.getBoundingClientRect();
  const x = parseFloat(hit.getAttribute("cx"));
  const y = parseFloat(hit.getAttribute("cy"));
  tip.style.left = `${svgRect.left - wrapRect.left + x * (svgRect.width / W)}px`;
  tip.style.top = `${svgRect.top - wrapRect.top + y * (svgRect.height / H)}px`;
}

function hideTip() {
  $("chartTip").hidden = true;
}

/* ============================================================
 * 指标条 / 阶段表
 * ============================================================ */

function renderStats() {
  const b = state.current;
  const nodes = sanitizeNodes(b.nodes);
  const sum = localSummary(nodes, b);
  $("statTime").textContent = fmtTime(sum.totalTime);
  $("statWater").textContent = `${fmt(sum.totalWater)}g`;
  $("statRatio").textContent = sum.actualRatio ? `1:${fmt(sum.actualRatio, 2)}` : "–";
  $("statBloom").textContent = sum.bloom
    ? `${fmt(sum.bloom.w1)}g/${sum.bloom.duration}s`
    : "无";
  $("statFlow").textContent = sum.median != null ? `${fmt(sum.median, 2)} g/s` : "–";
  $("statCounts").textContent = `${sum.pours.length} / ${sum.pauses.length}`;
}

function renderStageTable() {
  const b = state.current;
  const { stages } = computeStages(b.nodes);
  const tbody = $("stageTable").querySelector("tbody");
  tbody.innerHTML = "";
  const flagged = flaggedStageSet();
  const selectedFlavors = new Set(b.flavors || []);

  // 选中的风味对应的可疑阶段也要高亮
  if (state.diagnosis) {
    for (const f of selectedFlavors) {
      for (const item of state.diagnosis.flavors[f] || []) {
        item.stages.forEach((i) => flagged.add(i));
      }
    }
  }

  stages.forEach((s, i) => {
    const tr = htmlEl("tr");
    tr.dataset.stage = s.index;
    if (flagged.has(s.index)) tr.classList.add("suspicious");

    const kindLabel = s.bloom ? "闷蒸" : s.kind === "pour" ? "注水" : "停顿";
    const kindCls = s.bloom ? "bloom" : s.kind;
    tr.innerHTML =
      `<td>${i + 1}</td>` +
      `<td style="text-align:center"><span class="kind-pill ${kindCls}">${kindLabel}</span></td>` +
      `<td>${s.t0}–${s.t1}</td>` +
      `<td>${s.duration}s</td>` +
      `<td>${fmt(s.w0)}</td>` +
      `<td>${fmt(s.w1)}</td>` +
      `<td>${s.kind === "pour" ? fmt(s.added) : "—"}</td>` +
      `<td>${s.kind === "pour" ? fmt(s.flow, 2) : "—"}</td>`;
    const flagTd = htmlEl("td", "flag-cell");
    const marks = [];
    if (state.diagnosis) {
      for (const r of state.diagnosis.rules) {
        if (r.stages.includes(s.index)) marks.push("⚠");
      }
    }
    flagTd.textContent = marks.join(" ");
    flagTd.title = "点击曲线段可定位";
    tr.appendChild(flagTd);
    tr.addEventListener("click", () => focusStage(s.index));
    tbody.appendChild(tr);
  });
  $("stageCount").textContent = `共 ${stages.length} 段`;
}

function renderStageFlags() {
  renderStageTable();
}

/* ============================================================
 * 规则列表 / 风味归因
 * ============================================================ */

function renderRules() {
  const ul = $("ruleList");
  ul.innerHTML = "";
  const rules = state.diagnosis ? state.diagnosis.rules : [];
  const count = $("ruleCount");
  count.textContent = rules.length;
  count.classList.toggle("zero", rules.length === 0);

  if (!rules.length) {
    ul.appendChild(htmlEl("li", "empty-note",
      state.diagnosis ? "未发现明显问题，节奏与参数都在参考范围内 ✓" : "诊断计算中…"));
    return;
  }
  const order = { bad: 0, warning: 1, info: 2 };
  [...rules].sort((a, b) => order[a.level] - order[b.level]).forEach((r) => {
    const li = htmlEl("li", `rule-item ${r.level}`);
    const head = htmlEl("div", "rule-head");
    const icon = r.level === "bad" ? "⛔" : r.level === "warning" ? "⚠️" : "ℹ️";
    head.appendChild(htmlEl("span", null, `${icon} ${r.label}`));
    li.appendChild(head);
    li.appendChild(htmlEl("div", "rule-msg", r.message));
    if (r.stages.length) {
      const s = htmlEl("span", "rule-stage",
        `→ 定位到第 ${r.stages.map((i) => i + 1).join("、")} 段`);
      s.addEventListener("click", () => focusStage(r.stages[0]));
      li.appendChild(s);
    }
    ul.appendChild(li);
  });
}

function renderFlavorMap() {
  const box = $("flavorMap");
  box.innerHTML = "";
  const b = state.current;
  const selected = new Set(b.flavors || []);
  const flavors = state.diagnosis ? state.diagnosis.flavors : {};
  const allKeys = Object.keys(state.flavorLabels);

  allKeys.forEach((key) => {
    const items = flavors[key] || [];
    const isOn = selected.has(key);
    const block = htmlEl("div", `fmap-block${isOn ? "" : " off"}`);
    const head = htmlEl("div", "fmap-head");
    head.appendChild(htmlEl("span", null, `${isOn ? "▾" : "▸"} ${state.flavorLabels[key]}`));
    head.appendChild(htmlEl("span", "count", isOn ? `${items.length} 条线索` : "未勾选"));
    block.appendChild(head);

    const body = htmlEl("div", "fmap-body");
    if (!isOn) {
      body.style.display = "none";
    } else if (!items.length) {
      body.appendChild(htmlEl("div", "fmap-none",
        "未从参数/曲线中定位到典型原因，可结合杯测再观察。"));
    } else {
      items.forEach((item) => {
        const row = htmlEl("div", "fmap-reason", item.reason);
        if (item.stages.length) {
          const link = htmlEl("span", "jump-stage",
            `→ 第 ${item.stages.map((i) => i + 1).join("、")} 段`);
          link.addEventListener("click", () => focusStage(item.stages[0]));
          row.appendChild(link);
        }
        body.appendChild(row);
      });
    }
    head.addEventListener("click", () => {
      body.style.display = body.style.display === "none" ? "block" : "none";
    });
    block.appendChild(body);
    box.appendChild(block);
  });
}

/* ============================================================
 * 叠加对照 / 建议 / 版本
 * ============================================================ */

function renderCompareDiff() {
  const box = $("compareDiff");
  box.innerHTML = "";
  const a = state.current;
  const b = state.compareBrew;
  if (!b) {
    box.appendChild(htmlEl("div", "empty-note", "选择另一杯叠加曲线，对比参数与节奏差异。"));
    return;
  }
  const rows = [
    ["粉量 (g)", a.dose, b.dose],
    ["水温 (℃)", a.temp, b.temp],
    ["研磨刻度", a.grind, b.grind],
    ["目标粉水比", a.target_ratio, b.target_ratio],
    ["登记水量 (g)", a.water, b.water],
  ];
  rows.forEach(([k, va, vb]) => {
    const line = htmlEl("div", "diff-line");
    line.appendChild(htmlEl("span", "dl-k", k));
    const val = htmlEl("span", "dl-v", `${vb} → ${va}`);
    const numA = parseFloat(va), numB = parseFloat(vb);
    if (numA > numB) val.classList.add("up");
    else if (numA < numB) val.classList.add("down");
    line.appendChild(val);
    box.appendChild(line);
  });
  const sa = localSummary(sanitizeNodes(a.nodes), a);
  const sb = localSummary(sanitizeNodes(b.nodes), b);
  const extra = [
    ["总时长", fmtTime(sa.totalTime), fmtTime(sb.totalTime)],
    ["实际粉水比", `1:${fmt(sa.actualRatio, 2)}`, `1:${fmt(sb.actualRatio, 2)}`],
    ["中位流速", sa.median != null ? `${fmt(sa.median, 2)}` : "–",
     sb.median != null ? `${fmt(sb.median, 2)}` : "–"],
  ];
  extra.forEach(([k, va, vb]) => {
    const line = htmlEl("div", "diff-line");
    line.appendChild(htmlEl("span", "dl-k", k));
    line.appendChild(htmlEl("span", "dl-v", `${vb} → ${va}`));
    box.appendChild(line);
  });
  renderWaterCompare();
}

/* 两杯并排水配方差异（WaterModule 提供） */
function renderWaterCompare() {
  if (typeof WaterModule === "undefined") return;
  if (state.current && state.compareBrew) {
    WaterModule.renderCompare(state.current, state.compareBrew);
  } else {
    const box = document.getElementById("waterCompare");
    if (box) box.hidden = true;
  }
}

async function generateSuggestion() {
  if (!state.current) return;
  try {
    const data = await api("POST", "/api/suggest", {
      anchor: state.current,
      baseline: state.compareBrew || null,
      locked: [...state.locked],
    });
    state.suggest = data;
    renderSuggestions();
    toast("已生成下一次调整建议");
  } catch (err) {
    toast(err.message);
  }
}

function renderSuggestions() {
  const box = $("suggestBox");
  box.innerHTML = "";
  if (!state.suggest) return;
  box.classList.add("show");
  state.suggest.suggestions.forEach((s) => {
    const item = htmlEl("div", "sg-item");
    const title = htmlEl("div", "sg-title");
    title.appendChild(document.createTextNode(s.title));
    const tag = htmlEl("span", "sg-var",
      state.variableLabels[s.variable] || s.variable);
    title.appendChild(tag);
    item.appendChild(title);
    item.appendChild(htmlEl("div", "sg-detail", s.detail));
    if (s.stages.length) {
      const link = htmlEl("span", "sg-stage",
        `→ 曲线上第 ${[...new Set(s.stages)].map((i) => i + 1).join("、")} 段`);
      link.addEventListener("click", () => focusStage(s.stages[0]));
      item.appendChild(link);
    }
    box.appendChild(item);
  });
  box.appendChild(htmlEl("div", "sg-note", state.suggest.note));
}

function renderVersions() {
  const ul = $("versionList");
  ul.innerHTML = "";
  if (!state.current) return;
  const group = state.brews
    .filter((b) => b.group_id === state.current.group_id)
    .sort((a, b) => b.version - a.version);
  if (!group.length) {
    ul.appendChild(htmlEl("li", "empty-note", "尚无版本。"));
    return;
  }
  group.forEach((b) => {
    const li = htmlEl("li", `ver-item${b.id === state.current.id ? " current" : ""}`);
    const main = htmlEl("div", "ver-main");
    const name = htmlEl("div", "ver-name");
    name.appendChild(htmlEl("span", "ver-v", `V${b.version}`));
    name.appendChild(document.createTextNode(b.name));
    main.appendChild(name);
    main.appendChild(htmlEl("div", "ver-meta",
      `${b.brew_date} · ${fmt(b.dose, 0)}g粉 / ${fmt(b.water, 0)}g水 · ${fmt(b.temp, 0)}℃`));
    if (b.water_recipe_id) {
      const wm = htmlEl("div", "ver-water", "💧 已关联水配方");
      main.appendChild(wm);
    }
    main.addEventListener("click", () => switchBrew(b.id));
    li.appendChild(main);

    const del = htmlEl("button", "ver-del", "✕");
    del.title = "删除该版本";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`确定删除「${b.name}」？此操作不可撤销。`)) return;
      try {
        await api("DELETE", `/api/brews/${b.id}`);
        state.brews = state.brews.filter((x) => x.id !== b.id);
        if (state.current.id === b.id) state.current = null;
        fillBrewSelectors();
        if (state.current) renderVersions();
        else location.reload();
      } catch (err) {
        toast(err.message);
      }
    });
    li.appendChild(del);
    ul.appendChild(li);
  });
}

async function switchBrew(id) {
  if (state.dirty && !confirm("当前有未保存的修改，切换将丢失这些修改。确定切换？")) return;
  try {
    const full = await api("GET", `/api/brews/${id}`);
    loadBrew(full.brew, { resetCompare: false });
  } catch (err) {
    toast(err.message);
  }
}

/* ============================================================
 * 保存 / 新建 / 恢复示例 / 打印
 * ============================================================ */

async function saveCurrent() {
  if (!state.current) return;
  const b = state.current;
  if (!b.name || !b.name.trim()) {
    toast("请先填写方案名称");
    $("fName").focus();
    return;
  }
  const payload = {
    name: b.name.trim(),
    brew_date: b.brew_date,
    dose: Number(b.dose),
    water: Number(b.water),
    temp: Number(b.temp),
    grind: Number(b.grind),
    target_ratio: Number(b.target_ratio),
    flavors: b.flavors,
    notes: b.notes,
    nodes: sanitizeNodes(b.nodes),
    water_recipe_id: b.water_recipe_id ?? null,
  };
  try {
    const data = await api("PUT", `/api/brews/${b.id}`, payload);
    // 更新列表项
    const idx = state.brews.findIndex((x) => x.id === b.id);
    if (idx >= 0) {
      state.brews[idx] = {
        ...state.brews[idx],
        name: data.brew.name, brew_date: data.brew.brew_date,
        dose: data.brew.dose, water: data.brew.water, temp: data.brew.temp,
        grind: data.brew.grind, target_ratio: data.brew.target_ratio,
        flavors: data.brew.flavors,
      };
    }
    state.current = { ...state.current, ...data.brew };
    fillBrewSelectors();
    renderVersions();
    markDirty(false);
    toast("已保存 ✓");
  } catch (err) {
    toast(err.message);
  }
}

async function saveAsNewVersion() {
  if (!state.current) return;
  if (state.dirty && !confirm("当前修改还没保存。新版本将包含这些未保存修改，继续？")) return;
  const b = state.current;
  try {
    const data = await api("POST", "/api/brews", {
      group_id: b.group_id,
      name: b.name,
      brew_date: new Date().toISOString().slice(0, 10),
      dose: Number(b.dose),
      water: Number(b.water),
      temp: Number(b.temp),
      grind: Number(b.grind),
      target_ratio: Number(b.target_ratio),
      flavors: b.flavors,
      notes: b.notes,
      nodes: sanitizeNodes(b.nodes),
    });
    state.brews.push(data.brew);
    fillBrewSelectors();
    loadBrew(data.brew, { resetCompare: true });
    toast(`已存为 V${data.brew.version} ✓`);
  } catch (err) {
    toast(err.message);
  }
}

function newBrew() {
  if (state.dirty && !confirm("当前有未保存的修改，新建将丢失这些修改。确定？")) return;
  const today = new Date().toISOString().slice(0, 10);
  state.current = {
    id: null,
    group_id: null,
    name: "",
    brew_date: today,
    dose: 15, water: 225, temp: 92, grind: 22, target_ratio: 15,
    flavors: [],
    notes: "",
    water_recipe_id: null,
    nodes: [
      { t: 0, w: 0 },
      { t: 25, w: 35 },
      { t: 55, w: 35 },
      { t: 90, w: 130 },
      { t: 100, w: 130 },
      { t: 140, w: 185 },
      { t: 147, w: 185 },
      { t: 180, w: 225 },
    ],
  };
  state.compareId = null;
  state.compareBrew = null;
  $("compareSelect").value = "";
  state.diagnosis = null;
  fillForm();
  renderAll();
  if (typeof WaterModule !== "undefined") WaterModule.onBrewLoaded(state.current);
  $("fName").focus();
  markDirty(true);
  $("saveBtn").textContent = "保存为新记录";
  toast("填好参数与曲线后点击「保存为新记录」");
}

async function saveNewFromScratch() {
  // newBrew 产生的未落库记录
  if (!state.current || state.current.id != null) {
    return saveCurrent();
  }
  const b = state.current;
  if (!b.name || !b.name.trim()) {
    toast("请先填写方案名称");
    $("fName").focus();
    return;
  }
  try {
    const data = await api("POST", "/api/brews", {
      name: b.name.trim(),
      brew_date: b.brew_date,
      dose: Number(b.dose),
      water: Number(b.water),
      temp: Number(b.temp),
      grind: Number(b.grind),
      target_ratio: Number(b.target_ratio),
      flavors: b.flavors,
      notes: b.notes,
      nodes: sanitizeNodes(b.nodes),
    });
    state.brews.push(data.brew);
    fillBrewSelectors();
    loadBrew(data.brew, { resetCompare: true });
    toast(`已创建 V${data.brew.version} ✓`);
  } catch (err) {
    toast(err.message);
  }
}

async function resetSamples() {
  if (!confirm("将删除标记为示例的记录并重新载入两份示例，自定义记录不受影响。继续？")) return;
  try {
    const data = await api("POST", "/api/samples/reset", {});
    state.brews = (await api("GET", "/api/brews")).brews;
    if (typeof WaterModule !== "undefined") {
      const rd = await api("GET", "/api/water/recipes");
      WaterModule.refreshRecipes(rd.recipes);
    }
    fillBrewSelectors();
    if (data.brews.length) {
      const full = await api("GET", `/api/brews/${data.brews[0].id}`);
      loadBrew(full.brew, { resetCompare: true });
    }
    toast("示例数据已恢复 ✓");
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------- 打印 ---------------- */

function preparePrint() {
  const b = state.current;
  if (!b) return;
  $("printName").textContent = `方案：${b.name}`;
  $("printVersion").textContent =
    `V${b.version}${b.group_id ? ` · 组 #${b.group_id}` : ""}`;
  $("printDate").textContent = `日期：${b.brew_date}`;
  // 若建议尚未生成，打印前用已选风味/锁定静默生成一次
  if (!state.suggest) {
    generateSuggestion().then(() => window.print());
  } else {
    window.print();
  }
}

/* ============================================================
 * 总渲染 & 事件绑定
 * ============================================================ */

function renderAll(opts = {}) {
  if (!state.current) return;
  renderStats();
  renderChart();
  renderStageTable();
  renderCompareDiff();
  updateNodeEditor();
  updatePrintFields();
}

function updatePrintFields() {
  const b = state.current;
  if (!b) return;
  $("printName").textContent = `方案：${b.name || "（未命名）"}`;
  $("printVersion").textContent = b.id ? `V${b.version}` : "未保存";
  $("printDate").textContent = `日期：${b.brew_date}`;
}

function bindUI() {
  bindFormEvents();

  $("brewSelect").addEventListener("change", (e) => switchBrew(Number(e.target.value)));
  $("newBrewBtn").addEventListener("click", newBrew);
  $("resetSamplesBtn").addEventListener("click", resetSamples);
  $("saveBtn").addEventListener("click", () => {
    if (state.current && state.current.id == null) saveNewFromScratch();
    else saveCurrent();
  });
  $("printBtn").addEventListener("click", preparePrint);

  $("selT").addEventListener("change", () => {
    if (state.selectedNode != null) {
      updateNode(state.selectedNode, Number($("selT").value), state.current.nodes[state.selectedNode].w);
    }
  });
  $("selW").addEventListener("change", () => {
    if (state.selectedNode != null) {
      updateNode(state.selectedNode, state.current.nodes[state.selectedNode].t, Number($("selW").value));
    }
  });
  $("delNodeBtn").addEventListener("click", () => {
    if (state.selectedNode != null) deleteNode(state.selectedNode);
  });

  $("compareSelect").addEventListener("change", async (e) => {
    const id = Number(e.target.value);
    if (!id) {
      state.compareId = null;
      state.compareBrew = null;
      renderChart();
      renderCompareDiff();
      renderWaterCompare();
      return;
    }
    try {
      const full = await api("GET", `/api/brews/${id}`);
      state.compareId = id;
      state.compareBrew = full.brew;
      renderChart();
      renderCompareDiff();
      renderWaterCompare();
    } catch (err) {
      toast(err.message);
    }
  });
  $("clearCompareBtn").addEventListener("click", () => {
    state.compareId = null;
    state.compareBrew = null;
    $("compareSelect").value = "";
    renderChart();
    renderCompareDiff();
    renderWaterCompare();
  });
  $("suggestBtn").addEventListener("click", generateSuggestion);
  $("newVersionBtn").addEventListener("click", saveAsNewVersion);

  // Ctrl/Cmd+S 保存
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (state.current && state.current.id == null) saveNewFromScratch();
      else saveCurrent();
    }
  });

  // 离开页面前提示
  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

document.addEventListener("DOMContentLoaded", init);

/* ---------------- 供 water.js 调用的接口 ---------------- */

window.App = {
  currentBrewId() {
    return state.current ? state.current.id : null;
  },
  getCurrentBrew() {
    return state.current;
  },
  async updateBrewField(field, value) {
    if (!state.current || state.current.id == null) return;
    const data = await api("PUT", `/api/brews/${state.current.id}`, { [field]: value });
    state.current = { ...state.current, ...data.brew };
    const idx = state.brews.findIndex((x) => x.id === state.current.id);
    if (idx >= 0) state.brews[idx] = { ...state.brews[idx], ...data.brew };
    fillBrewSelectors();
    renderVersions();
  },
  async unlinkWaterFromCurrent() {
    if (state.current && state.current.id != null && state.current.water_recipe_id != null) {
      await this.updateBrewField("water_recipe_id", null);
    }
    if (state.current) state.current.water_recipe_id = null;
  },
};
