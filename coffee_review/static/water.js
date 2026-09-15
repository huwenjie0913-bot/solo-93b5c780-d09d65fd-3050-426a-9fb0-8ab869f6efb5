/* ============================================================
 * 冲煮水配方模块 —— 原生 JS
 * 计算权威在 Python 端 water.py（/api/water/*）；本文件只负责
 * 原水/目标水/储备液编辑、防抖请求、结果与冲突渲染、配方存取、
 * 按新体积等比换算，以及两杯对照时的水配方并排差异。
 * ============================================================ */
"use strict";

const WaterModule = (() => {
  /* ---------------- 状态 ---------------- */

  // 六支盐按固定顺序（与后端 meta 一致时以后端为准）
  const SALT_KEYS = ["cacl2_2h2o", "mgso4_7h2o", "nahco3",
    "caso4_2h2o", "mgcl2_6h2o", "khco3"];

  const state = {
    meta: null,            // /api/water/meta
    recipes: [],           // 已保存配方
    editor: defaultEditor(),
    savedId: null,         // 当前编辑器载入的已保存配方 id
    result: null,          // 最近一次后端计算结果
    brewId: null,          // 当前关联的冲煮 id（仅显示用）
    dirty: false,
    timer: null,
    bound: false,
  };

  function defaultEditor() {
    return {
      name: "",
      volume_ml: 1000,
      source: { ca: 0, mg: 0, na: 0, hco3: 0 },
      target: { ca: 55, mg: 10, hco3: 45 },
      limits: { cl_max: 100, so4_max: 150, resolution_ml: 0.1 },
      stocks: {
        cacl2_2h2o: { enabled: true, conc_g_l: 50 },
        mgso4_7h2o: { enabled: true, conc_g_l: 50 },
        nahco3: { enabled: true, conc_g_l: 25 },
        caso4_2h2o: { enabled: false, conc_g_l: 50 },
        mgcl2_6h2o: { enabled: false, conc_g_l: 50 },
        khco3: { enabled: false, conc_g_l: 25 },
      },
    };
  }

  /* ---------------- 小工具（复用 app.js 的 $/api/toast/htmlEl/fmt） ---------------- */

  const numFields = {
    wsCa: ["source", "ca"], wsMg: ["source", "mg"], wsNa: ["source", "na"], wsHco3: ["source", "hco3"],
    wtCa: ["target", "ca"], wtMg: ["target", "mg"], wtHco3: ["target", "hco3"],
    wlCl: ["limits", "cl_max"], wlSo4: ["limits", "so4_max"], wlRes: ["limits", "resolution_ml"],
  };

  function setDeep(obj, path, val) {
    for (let i = 0; i < path.length - 1; i++) obj = obj[path[i]];
    obj[path[path.length - 1]] = val;
  }

  function markDirty(d = true) {
    state.dirty = d;
    const btn = $("wfSaveBtn");
    if (btn) btn.classList.toggle("primary", d);
    btn.textContent = d ? "保存配方 ●" : "保存配方";
  }

  /* ---------------- 初始化 ---------------- */

  async function init() {
    bindUI();
    try {
      const [meta, data] = await Promise.all([
        api("GET", "/api/water/meta"),
        api("GET", "/api/water/recipes"),
      ]);
      state.meta = meta;
      state.recipes = data.recipes;
    } catch (err) {
      console.warn("水模块元数据加载失败：", err);
    }
    buildStockRows();
    fillSavedSelect();
    fillInputs();
    refreshLinkTag();
    scheduleCalculate();
  }

  function buildStockRows() {
    const tbody = $("stockTable").querySelector("tbody");
    tbody.innerHTML = "";
    SALT_KEYS.forEach((key) => {
      const info = saltInfo(key);
      const tr = htmlEl("tr");
      tr.dataset.salt = key;

      const tdCheck = htmlEl("td", "stock-edit");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "salt-enabled";
      tdCheck.appendChild(cb);

      const tdConc = htmlEl("td", "stock-edit");
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = "0";
      inp.step = "0.1";
      inp.className = "salt-conc";
      tdConc.appendChild(inp);

      cb.addEventListener("change", () => {
        state.editor.stocks[key].enabled = cb.checked;
        markDirty();
        tr.classList.toggle("disabled", !cb.checked);
        scheduleCalculate();
      });
      inp.addEventListener("input", () => {
        state.editor.stocks[key].conc_g_l = parseFloat(inp.value) || 0;
        markDirty();
        scheduleCalculate();
      });

      tr.appendChild(tdCheck);
      tr.appendChild(htmlEl("td", "salt-label")).textContent = info.label;
      tr.appendChild(htmlEl("td", "salt-formula")).textContent = info.formula;
      tr.appendChild(tdConc);
      tr.appendChild(htmlEl("td", "dose-mg"));
      const mlCell = htmlEl("td", "dose-ml-cell");
      mlCell.appendChild(htmlEl("span", "dose-ml"));
      tr.appendChild(mlCell);
      tr.appendChild(htmlEl("td", "salt-ions"));
      tbody.appendChild(tr);
    });
  }

  function saltInfo(key) {
    const s = state.meta && state.meta.salts.find((x) => x.key === key);
    if (s) return s;
    // meta 未就绪时的兜底文案
    const fallback = {
      cacl2_2h2o: ["氯化钙（二水）", "CaCl₂·2H₂O"],
      mgso4_7h2o: ["硫酸镁（七水·泻盐）", "MgSO₄·7H₂O"],
      nahco3: ["小苏打", "NaHCO₃"],
      caso4_2h2o: ["石膏（二水）", "CaSO₄·2H₂O"],
      mgcl2_6h2o: ["氯化镁（六水）", "MgCl₂·6H₂O"],
      khco3: ["碳酸氢钾", "KHCO₃"],
    }[key];
    return { key, label: fallback[0], formula: fallback[1], ions: {} };
  }

  /* ---------------- 表单回填 / 收集 ---------------- */

  function fillInputs() {
    const e = state.editor;
    $("wfName").value = e.name || "";
    $("wfVolume").value = e.volume_ml;
    for (const [id, path] of Object.entries(numFields)) {
      $(id).value = path[0] === "source" || path[0] === "target"
        ? e[path[0]][path[1]]
        : e.limits[path[1]];
    }
    document.querySelectorAll("#stockTable tr[data-salt]").forEach((tr) => {
      const key = tr.dataset.salt;
      const cfg = e.stocks[key];
      tr.querySelector(".salt-enabled").checked = !!cfg.enabled;
      tr.querySelector(".salt-conc").value = cfg.conc_g_l;
      tr.classList.toggle("disabled", !cfg.enabled);
    });
  }

  function collectSpec() {
    return {
      volume_ml: Number(state.editor.volume_ml) || 0,
      source: { ...state.editor.source },
      target: { ...state.editor.target },
      stocks: JSON.parse(JSON.stringify(state.editor.stocks)),
      limits: { ...state.editor.limits },
    };
  }

  /* ---------------- 计算（防抖） ---------------- */

  function scheduleCalculate() {
    clearTimeout(state.timer);
    state.timer = setTimeout(runCalculate, 220);
  }

  async function runCalculate() {
    const spec = collectSpec();
    if (spec.volume_ml <= 0) {
      renderInvalidVolume();
      return;
    }
    try {
      const data = await api("POST", "/api/water/calculate", { params: spec });
      state.result = data.result;
      renderResult();
    } catch (err) {
      console.warn("水配方计算失败：", err);
      const ul = $("waterConflicts");
      ul.innerHTML = "";
      ul.appendChild(htmlEl("li", "wc-item error",
        `计算失败：${err.message}`));
    }
  }

  function renderInvalidVolume() {
    state.result = null;
    ["wrCa", "wrMg", "wrNa", "wrCl", "wrSo4", "wrHco3", "wrGH", "wrAdd"]
      .forEach((id) => { $(id).textContent = "–"; });
    $("wrKBox").hidden = true;
    document.querySelectorAll("#stockTable .dose-mg, #stockTable .dose-ml, #stockTable .salt-ions")
      .forEach((n) => { n.textContent = ""; });
    const ul = $("waterConflicts");
    ul.innerHTML = "";
    ul.appendChild(htmlEl("li", "wc-item error", "请先填写大于 0 的成品体积。"));
    $("waterDeviation").innerHTML = "";
  }

  /* ---------------- 结果渲染 ---------------- */

  const ION_UNITS = { ca: "Ca²⁺", mg: "Mg²⁺", na: "Na⁺", k: "K⁺", cl: "Cl⁻", so4: "SO₄²⁻", hco3: "HCO₃⁻" };

  function renderResult() {
    const r = state.result;
    if (!r) return;

    // 成品离子
    const f = r.final;
    const caps = { cl: state.editor.limits.cl_max, so4: state.editor.limits.so4_max };
    setIon("wrCa", f.ca);
    setIon("wrMg", f.mg);
    setIon("wrNa", f.na);
    setIon("wrCl", f.cl, caps.cl);
    setIon("wrSo4", f.so4, caps.so4);
    setIon("wrHco3", f.hco3);
    $("wrGH").textContent = `${fmt(r.hardness.as_caco3, 0)} / ${fmt(r.hardness.dh, 1)}°dH`;
    $("wrAdd").textContent = `${fmt(r.total_add_ml, 2)} mL`;
    if (f.k >= 0.05) {
      $("wrKBox").hidden = false;
      setIon("wrK", f.k);
    } else {
      $("wrKBox").hidden = true;
    }

    // 各盐剂量
    document.querySelectorAll("#stockTable tr[data-salt]").forEach((tr) => {
      const key = tr.dataset.salt;
      const d = r.doses.find((x) => x.key === key);
      tr.classList.toggle("disabled", !d.enabled);
      tr.classList.toggle("dose-warn", d.below_resolution);
      tr.querySelector(".dose-mg").textContent =
        d.salt_mg > 0 ? fmt(d.salt_mg, 1) : d.enabled ? "0" : "—";
      const ml = tr.querySelector(".dose-ml");
      ml.textContent = d.add_ml != null ? fmt(d.add_ml, 3) : d.enabled ? "0 mL" : "—";
      if (d.add_ml != null) ml.textContent += " mL";
      ml.classList.toggle("warn", d.below_resolution);
      const ionParts = Object.entries(d.ions || {})
        .map(([ion, v]) => `<b>${ION_UNITS[ion] || ion}</b> ${fmt(v, 1)}`);
      tr.querySelector(".salt-ions").innerHTML = ionParts.join("，") || "（不添加）";
    });

    // 目标偏差
    const devBox = $("waterDeviation");
    devBox.innerHTML = "";
    for (const ion of ["ca", "mg", "hco3"]) {
      const dv = r.deviation[ion];
      const ok = Math.abs(dv) < 0.05;
      const pill = htmlEl("span", `dev-pill ${ok ? "ok" : "off"}`);
      pill.textContent = `${ION_UNITS[ion]} 偏差 ${dv >= 0 ? "+" : ""}${fmt(dv, 2)} mg/L`;
      devBox.appendChild(pill);
    }

    // 冲突 / 提示
    const ul = $("waterConflicts");
    ul.innerHTML = "";
    if (!r.conflicts.length) {
      ul.appendChild(htmlEl("li", "wc-empty", "✓ 目标可达，所有储备液添加量均在量具分辨率以上。"));
    } else {
      const order = { error: 0, warning: 1, info: 2 };
      [...r.conflicts].sort((a, b) => order[a.level] - order[b.level]).forEach((c) => {
        const li = htmlEl("li", `wc-item ${c.level}`);
        const head = htmlEl("span", "wc-head", conflictTitle(c));
        li.appendChild(head);
        li.appendChild(document.createTextNode(" " + c.message));
        if (c.adjust) li.appendChild(htmlEl("span", "wc-adjust", "💡 " + c.adjust));
        ul.appendChild(li);
      });
    }

    renderWaterPrint(r);
  }

  function setIon(id, val, cap) {
    const node = $(id);
    node.textContent = fmt(val, 1);
    node.parentElement.classList.toggle("over", cap > 0 && val > cap + 0.05);
  }

  function conflictTitle(c) {
    return {
      target_below_source: "⛔ 目标低于原水（需负剂量）",
      anion_cap_exceeded: "⛔ 目标不可达（Cl⁻/SO₄²⁻ 上限冲突）",
      no_salt_available: "⛔ 缺少可用盐",
      negative_input: "⛔ 输入有误",
      stock_conc_missing: "⚠️ 储备液未配置",
      below_resolution: "⚠️ 添加量低于量具分辨率",
      volume_displacement: "ℹ️ 储备液体积提示",
    }[c.code] || (c.level === "error" ? "⛔ 冲突" : "⚠️ 提示");
  }

  /* ---------------- 打印摘要 ---------------- */

  function renderWaterPrint(r) {
    const e = state.editor;
    const doses = r.doses.filter((d) => d.add_ml != null)
      .map((d) => `${d.formula} ${fmt(d.add_ml, 2)}mL`)
      .join("，") || "无";
    $("waterPrint").innerHTML =
      `<div class="wp-line"><b>配方：</b>${e.name || "（未命名）"} · ${fmt(r.volume_ml, 0)}mL ·
       原水 Ca ${fmt(e.source.ca)}/Mg ${fmt(e.source.mg)}/Na ${fmt(e.source.na)}/碱度 ${fmt(e.source.hco3)}</div>` +
      `<div class="wp-line"><b>成品：</b>Ca ${fmt(r.final.ca)} / Mg ${fmt(r.final.mg)} /
       Na ${fmt(r.final.na)} / Cl ${fmt(r.final.cl)} / SO₄ ${fmt(r.final.so4)} /
       碱度 ${fmt(r.final.hco3)} mg/L；总硬度 ${fmt(r.hardness.as_caco3, 0)}
       (CaCO₃) / ${fmt(r.hardness.dh, 1)}°dH</div>` +
      `<div class="wp-line"><b>储备液：</b>${doses}</div>`;
  }

  /* ---------------- 已保存配方选择器 ---------------- */

  function fillSavedSelect() {
    const sel = $("wfSavedSelect");
    const keep = sel.value;
    sel.innerHTML = '<option value="">（载入已保存配方…）</option>';
    for (const rec of state.recipes) {
      const opt = htmlEl("option", null,
        `${rec.name}（${rec.spec.volume_ml}mL）`);
      opt.value = rec.id;
      sel.appendChild(opt);
    }
    if (state.savedId) sel.value = state.savedId;
    else if (keep) sel.value = keep;
  }

  function loadRecipeIntoEditor(rec) {
    const d = defaultEditor();
    const spec = rec.spec || {};
    state.editor = {
      name: rec.name,
      volume_ml: Number(spec.volume_ml) || d.volume_ml,
      source: { ...d.source, ...(spec.source || {}) },
      target: { ...d.target, ...(spec.target || {}) },
      limits: { ...d.limits, ...(spec.limits || {}) },
      stocks: (() => {
        const merged = { ...d.stocks };
        for (const [k, v] of Object.entries(spec.stocks || {})) {
          merged[k] = { ...d.stocks[k], ...v };
        }
        return merged;
      })(),
    };
    state.savedId = rec.id;
    $("wfSavedSelect").value = rec.id;
    fillInputs();
    markDirty(false);
    scheduleCalculate();
  }

  async function saveRecipe() {
    const e = state.editor;
    if (!e.name || !e.name.trim()) {
      toast("请先填写水配方名称");
      $("wfName").focus();
      return;
    }
    if (!state.result) {
      toast("计算尚未完成，请稍候");
      return;
    }
    const payload = {
      name: e.name.trim(),
      spec: collectSpec(),
      result: state.result,
    };
    try {
      let rec;
      if (state.savedId) {
        const data = await api("PUT", `/api/water/recipes/${state.savedId}`, payload);
        rec = data.recipe;
        const i = state.recipes.findIndex((x) => x.id === rec.id);
        if (i >= 0) state.recipes[i] = rec;
        toast("水配方已更新 ✓");
      } else {
        const data = await api("POST", "/api/water/recipes", payload);
        rec = data.recipe;
        state.recipes.unshift(rec);
        state.savedId = rec.id;
        toast("水配方已保存 ✓");
      }
      fillSavedSelect();
      markDirty(false);
      refreshLinkTag();
      return rec;
    } catch (err) {
      toast(err.message);
      return null;
    }
  }

  async function deleteRecipe() {
    if (!state.savedId) {
      toast("当前是未保存的编辑内容，无需删除");
      return;
    }
    const rec = state.recipes.find((x) => x.id === state.savedId);
    if (!confirm(`确定删除水配方「${rec ? rec.name : state.savedId}」？引用它的冲煮记录会解除关联。`)) return;
    try {
      await api("DELETE", `/api/water/recipes/${state.savedId}`);
      state.recipes = state.recipes.filter((x) => x.id !== state.savedId);
      state.savedId = null;
      $("wfSavedSelect").value = "";
      state.editor.name = "";
      $("wfName").value = "";
      fillSavedSelect();
      markDirty(true);
      refreshLinkTag();
      // 冲煮记录侧的关联也要解除
      if (typeof App !== "undefined" && App.unlinkWaterFromCurrent) {
        App.unlinkWaterFromCurrent();
      }
      toast("已删除 ✓");
    } catch (err) {
      toast(err.message);
    }
  }

  /* ---------------- 等比换算 ---------------- */

  async function applyScale() {
    const newVol = parseFloat($("wfScaleVol").value);
    if (!newVol || newVol <= 0) {
      toast("请填写大于 0 的新体积");
      return;
    }
    if (!state.result) {
      toast("计算尚未完成，请稍候");
      return;
    }
    // 优先用当前参数按新体积重新反算（below_resolution、体积占比等冲突随之重算）；
    // 后端在缺 params 时才退化为对旧结果做比例换算
    try {
      const payload = { volume_ml: newVol };
      const spec = collectSpec();
      if (spec) {
        payload.params = spec;
      } else {
        payload.result = state.result;
      }
      const data = await api("POST", "/api/water/scale", payload);
      const factor = newVol / state.result.volume_ml;
      state.result = data.result;
      state.editor.volume_ml = newVol;
      $("wfVolume").value = newVol;
      renderResult();
      markDirty(true);
      $("wfScaleHint").textContent =
        `已换算到 ${newVol} mL：浓度不变，添加量 ×${fmt(factor, 3)}，剂量冲突已按新体积重算`;
      setTimeout(() => { $("wfScaleHint").textContent = ""; }, 4000);
    } catch (err) {
      toast(err.message);
    }
  }

  /* ---------------- 与冲煮记录联动 ---------------- */

  // 切换冲煮时：有关联则载入该配方；没有则保留编辑器，只更新标签
  function refreshLinkTag() {
    const linked = (typeof App !== "undefined" && App.getCurrentBrew
      && App.getCurrentBrew()
      && App.getCurrentBrew().water_recipe_id === state.savedId && state.savedId);
    const tag = $("waterLinkTag");
    const btn = $("wfLinkBtn");
    const delBtn = $("wfDelBtn");
    delBtn.style.visibility = state.savedId ? "visible" : "hidden";
    if (linked) {
      const rec = state.recipes.find((x) => x.id === state.savedId);
      tag.textContent = `已关联：${rec ? rec.name : ""}`;
      btn.textContent = "解除关联";
      btn.classList.add("linked");
    } else if (state.savedId) {
      tag.textContent = "编辑中（未关联本杯）";
      btn.textContent = "关联到本杯";
      btn.classList.remove("linked");
    } else {
      tag.textContent = "未关联冲煮";
      btn.textContent = "关联到本杯";
      btn.classList.remove("linked");
    }
  }

  function onBrewLoaded(brew, allRecipes) {
    if (allRecipes) state.recipes = allRecipes;
    state.brewId = brew ? brew.id : null;
    fillSavedSelect();
    const rid = brew ? brew.water_recipe_id : null;
    if (rid) {
      const rec = state.recipes.find((x) => x.id === rid);
      if (rec) {
        loadRecipeIntoEditor(rec);
        refreshLinkTag();
        return;
      }
    }
    refreshLinkTag();
  }

  // 保存（或更新）当前配方并关联到冲煮；返回 recipe id
  async function linkToCurrentBrew() {
    if (typeof App === "undefined" || !App.currentBrewId || !App.currentBrewId()) {
      toast("请先保存冲煮记录，再关联水配方");
      return null;
    }
    // 已关联 -> 解除
    const brew = App.getCurrentBrew && App.getCurrentBrew();
    if (brew && brew.water_recipe_id === state.savedId && state.savedId) {
      if (!confirm("解除这杯冲煮与当前水配方的关联？配方本身保留。")) return null;
      await App.updateBrewField("water_recipe_id", null);
      refreshLinkTag();
      toast("已解除关联");
      return null;
    }
    const rec = await saveRecipe();
    if (!rec) return null;
    await App.updateBrewField("water_recipe_id", rec.id);
    refreshLinkTag();
    toast("已关联到本杯冲煮 ✓");
    return rec.id;
  }

  function currentRecipeId() {
    return state.savedId;
  }

  function getRecipeById(id) {
    return state.recipes.find((x) => x.id === id) || null;
  }

  function refreshRecipes(recipes) {
    state.recipes = recipes || [];
    fillSavedSelect();
  }

  /* ---------------- 两杯并排对照 ---------------- */

  function renderCompare(brewA, brewB, recipesA, recipesB) {
    const box = $("waterCompare");
    const table = $("waterCompareTable");
    const ra = brewA && brewA.water_recipe_id
      ? (recipesA || state.recipes).find((x) => x.id === brewA.water_recipe_id) : null;
    const rb = brewB && brewB.water_recipe_id
      ? (recipesB || state.recipes).find((x) => x.id === brewB.water_recipe_id) : null;
    if (!ra && !rb) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    table.innerHTML = "";

    const nameA = ra ? ra.name : "（未关联水配方）";
    const nameB = rb ? rb.name : "（未关联水配方）";
    const thead = htmlEl("thead");
    const hr = htmlEl("tr");
    hr.append(htmlEl("th", null, "项目"),
      htmlEl("th", null, `对照杯：${nameB}`),
      htmlEl("th", null, `当前杯：${nameA}`));
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = htmlEl("tbody");
    const sa = ra ? ra.spec : null, sb = rb ? rb.spec : null;
    const xa = ra ? ra.result : null, xb = rb ? rb.result : null;

    const section = (title) => {
      const tr = htmlEl("tr", "wc-section");
      const td = htmlEl("td");
      td.colSpan = 3;
      td.textContent = title;
      tr.appendChild(td);
      tbody.appendChild(tr);
    };
    const row = (label, va, vb, digits = 1, unit = "") => {
      const tr = htmlEl("tr");
      tr.appendChild(htmlEl("td", null, label));
      const tb = htmlEl("td", null, vb == null ? "—" : `${fmt(vb, digits)}${unit}`);
      const ta = htmlEl("td", null, va == null ? "—" : `${fmt(va, digits)}${unit}`);
      if (va != null && vb != null) {
        if (va > vb + 1e-9) ta.classList.add("diff-up");
        else if (va < vb - 1e-9) ta.classList.add("diff-down");
      }
      tr.append(tb, ta);
      tbody.appendChild(tr);
    };

    section("配制");
    row("成品体积 (mL)", sa ? sa.volume_ml : null, sb ? sb.volume_ml : null, 0);

    section("原水 (mg/L)");
    for (const ion of ["ca", "mg", "na", "hco3"]) {
      row(`${ION_UNITS[ion]}（原水）`, sa ? sa.source[ion] : null, sb ? sb.source[ion] : null);
    }

    section("成品离子 (mg/L)");
    for (const ion of ["ca", "mg", "na", "cl", "so4", "hco3"]) {
      row(ION_UNITS[ion], xa ? xa.final[ion] : null, xb ? xb.final[ion] : null);
    }
    row("总硬度 (CaCO₃)", xa ? xa.hardness.as_caco3 : null, xb ? xb.hardness.as_caco3 : null, 0);
    row("总硬度 (°dH)", xa ? xa.hardness.dh : null, xb ? xb.hardness.dh : null, 2);
    row("碱度 (HCO₃⁻)", xa ? xa.final.hco3 : null, xb ? xb.final.hco3 : null);

    section("储备液添加（配方体积）");
    const keys = [...new Set([
      ...((xa && xa.doses || []).filter((d) => d.add_ml != null).map((d) => d.key)),
      ...((xb && xb.doses || []).filter((d) => d.add_ml != null).map((d) => d.key)),
    ])];
    keys.forEach((key) => {
      const da = xa ? xa.doses.find((d) => d.key === key) : null;
      const db = xb ? xb.doses.find((d) => d.key === key) : null;
      const info = saltInfo(key);
      const va = da && da.add_ml != null ? da.add_ml : null;
      const vb = db && db.add_ml != null ? db.add_ml : null;
      const tr = htmlEl("tr");
      tr.appendChild(htmlEl("td", null, info.formula));
      tr.appendChild(htmlEl("td", null, vb == null ? "—" : `${fmt(vb, 2)} mL`));
      const tdA = htmlEl("td", null, va == null ? "—" : `${fmt(va, 2)} mL`);
      if (va != null && vb != null) {
        if (va > vb + 1e-9) tdA.classList.add("diff-up");
        else if (va < vb - 1e-9) tdA.classList.add("diff-down");
      }
      tr.appendChild(tdA);
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
  }

  /* ---------------- 事件绑定 ---------------- */

  function bindUI() {
    if (state.bound) return;
    state.bound = true;

    $("wfVolume").addEventListener("input", () => {
      state.editor.volume_ml = parseFloat($("wfVolume").value) || 0;
      markDirty();
      scheduleCalculate();
    });
    for (const [id, path] of Object.entries(numFields)) {
      $(id).addEventListener("input", () => {
        setDeep(state.editor, path, parseFloat($(id).value) || 0);
        markDirty();
        scheduleCalculate();
      });
    }
    $("wfName").addEventListener("input", () => {
      state.editor.name = $("wfName").value;
      markDirty();
    });
    $("wfSavedSelect").addEventListener("change", (e) => {
      const id = Number(e.target.value);
      if (!id) return;
      const rec = state.recipes.find((x) => x.id === id);
      if (rec) {
        if (state.dirty && !confirm("载入配方将覆盖当前未保存的编辑内容，继续？")) {
          e.target.value = state.savedId || "";
          return;
        }
        loadRecipeIntoEditor(rec);
      }
    });
    $("wfSaveBtn").addEventListener("click", saveRecipe);
    $("wfLinkBtn").addEventListener("click", linkToCurrentBrew);
    $("wfDelBtn").addEventListener("click", deleteRecipe);
    $("wfScaleApply").addEventListener("click", applyScale);
    $("wfScaleVol").addEventListener("keydown", (e) => {
      if (e.key === "Enter") applyScale();
    });
  }

  return {
    init, onBrewLoaded, renderCompare, currentRecipeId, getRecipeById,
    refreshRecipes,
  };
})();
