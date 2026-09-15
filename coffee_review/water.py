"""冲煮水配方计算引擎（仅依赖 Python 3 标准库）。

业务场景：原水本身含有 Ca/Mg/Na/碱度，加入浓缩盐储备液把成品水调到目标
离子组成。本模块负责反算每支储备液的添加体积，并给出成品组成、总硬度、
碱度、目标偏差与冲突诊断。

浓度约定：
- Ca/Mg/Na/K/Cl/SO4 均以 mg/L（以离子本身计）
- 碱度以 mg/L HCO3⁻ 计；展示时另折算 mg/L（以 CaCO3 计）与德国度 °dH
- 储备液浓度单位 g/L（每升储备液溶多少克盐），添加体积单位 mL
  （add_ml = salt_mg / stock_g_l，因 mg ÷ (g/L) 数值上恰好等于 mL）

可独立运行自检：python3 water.py
"""

import math

# ---------------------------------------------------------------- 常量

# 离子摩尔质量（g/mol）
M = {
    "ca": 40.078, "mg": 24.305, "na": 22.990, "k": 39.098,
    "cl": 35.453, "so4": 96.06, "hco3": 61.016,
}

# 1 mg/L 以 CaCO3 计 = 17.848 mg/L 的德国度换算
CACO3_MW_HALF = 50.045
DH_FACTOR = 17.848

ION_LABELS = {
    "ca": "Ca²⁺（钙）",
    "mg": "Mg²⁺（镁）",
    "na": "Na⁺（钠）",
    "k": "K⁺（钾）",
    "cl": "Cl⁻（氯离子）",
    "so4": "SO₄²⁻（硫酸根）",
    "hco3": "HCO₃⁻（碱度/碳酸氢根）",
}

GROUP_LABELS = {
    "ca": "钙盐",
    "mg": "镁盐",
    "alk": "碱度盐（碳酸氢盐）",
}

# 可选盐：ions 为每摩尔盐带入的离子摩尔数
SALTS = {
    "cacl2_2h2o": {
        "label": "氯化钙（二水）", "formula": "CaCl₂·2H₂O", "mw": 147.01,
        "ions": {"ca": 1, "cl": 2}, "group": "ca", "order": 1,
    },
    "mgso4_7h2o": {
        "label": "硫酸镁（七水·泻盐）", "formula": "MgSO₄·7H₂O", "mw": 246.47,
        "ions": {"mg": 1, "so4": 1}, "group": "mg", "order": 2,
    },
    "nahco3": {
        "label": "小苏打", "formula": "NaHCO₃", "mw": 84.007,
        "ions": {"na": 1, "hco3": 1}, "group": "alk", "order": 3,
    },
    "caso4_2h2o": {
        "label": "石膏（二水）", "formula": "CaSO₄·2H₂O", "mw": 172.17,
        "ions": {"ca": 1, "so4": 1}, "group": "ca", "order": 4,
    },
    "mgcl2_6h2o": {
        "label": "氯化镁（六水）", "formula": "MgCl₂·6H₂O", "mw": 203.30,
        "ions": {"mg": 1, "cl": 2}, "group": "mg", "order": 5,
    },
    "khco3": {
        "label": "碳酸氢钾", "formula": "KHCO₃", "mw": 100.115,
        "ions": {"k": 1, "hco3": 1}, "group": "alk", "order": 6,
    },
}

DEFAULT_LIMITS = {"cl_max": 100.0, "so4_max": 150.0, "resolution_ml": 0.1}

EPS = 1e-9


# ---------------------------------------------------------------- 输入解析

def _num(container, key):
    """从 dict 取数字；空值按 0；非法值抛 ValueError。"""
    raw = container.get(key, 0) if isinstance(container, dict) else container
    if raw in (None, ""):
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是数字")
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"{key} 必须是有限数字")
    return v


def _nonneg(container, key, label, conflicts):
    v = _num(container, key)
    if v < -EPS:
        conflicts.append({
            "level": "error", "code": "negative_input",
            "message": f"{label}浓度不能为负（输入为 {v:g} mg/L）。",
            "adjust": "请改为 ≥ 0 的数值。",
        })
        return 0.0
    return max(0.0, v)


# ---------------------------------------------------------------- 主计算

def calculate_recipe(params):
    """根据原水、目标水、储备液与上限反算配方。

    入参 params：
      volume_ml : 成品体积 mL
      source    : {ca, mg, na, hco3} 原水离子浓度 mg/L
      target    : {ca, mg, hco3} 目标离子浓度 mg/L
      stocks    : {salt_key: {enabled: bool, conc_g_l: number}}
      limits    : {cl_max, so4_max, resolution_ml}（上限空/0 表示不限制）
    返回 dict（结构见文件末尾自检输出），任何目标冲突写入 conflicts。
    """
    params = params or {}
    conflicts = []

    volume_ml = _num(params, "volume_ml")
    if volume_ml <= 0:
        raise ValueError("成品体积必须大于 0 mL")

    src_raw = params.get("source") or {}
    tgt_raw = params.get("target") or {}
    src = {k: _nonneg(src_raw, k, ION_LABELS[k], conflicts)
           for k in ("ca", "mg", "na", "hco3")}
    tgt = {k: _nonneg(tgt_raw, k, ION_LABELS[k], conflicts)
           for k in ("ca", "mg", "hco3")}

    limits_raw = params.get("limits") or {}
    resolution_ml = _num(limits_raw, "resolution_ml")
    cl_max = _num(limits_raw, "cl_max")
    so4_max = _num(limits_raw, "so4_max")
    # 0 / 负值留空语义：不设上限
    cl_cap = cl_max if cl_max > 0 else None
    so4_cap = so4_max if so4_max > 0 else None

    # --- 储备液可用性（勾选但浓度为 0 视为未配置好，给警告）---
    stocks_in = params.get("stocks") or {}
    stocks = {}
    for key in SALTS:
        row = stocks_in.get(key) or {}
        enabled = bool(row.get("enabled"))
        conc = _num(row, "conc_g_l")
        if conc < -EPS:
            raise ValueError(f"{SALTS[key]['formula']} 储备液浓度不能为负")
        if enabled and conc <= EPS:
            conflicts.append({
                "level": "warning", "code": "stock_conc_missing",
                "message": f"已选用 {SALTS[key]['formula']}，但储备液浓度为 0。",
                "adjust": f"请填写该储备液的浓度（g/L），或取消勾选 {SALTS[key]['formula']}。",
            })
            enabled = False
        stocks[key] = {"enabled": enabled, "conc": max(0.0, conc)}

    # --- 目标低于原水：加盐无法稀释 ---
    need = {}
    below = []
    for ion in ("ca", "mg", "hco3"):
        delta = tgt[ion] - src[ion]
        if delta < -EPS:
            below.append(ion)
        need[ion] = max(0.0, delta)

    # 稀释建议：取目标/原水最严格的比例
    ratios = [tgt[i] / src[i] for i in ("ca", "mg", "hco3") if src[i] > EPS]
    dilution_hint = ""
    if ratios:
        frac = min(ratios)
        if frac < 1.0 - EPS:
            pure = (1.0 - frac) / frac
            dilution_hint = (
                f"加盐不能降低已有离子：可把目标提高到不低于原水，或先用纯水稀释原水"
                f"（受最严离子约束，原水占比需 ≤ {frac * 100:.0f}%，"
                f"即约 1 份原水兑 {pure:.2g} 份纯水），再按稀释后的水质配制。"
            )
    for ion in below:
        conflicts.append({
            "level": "error", "code": "target_below_source",
            "message": f"目标 {ION_LABELS[ion]} {tgt[ion]:g} mg/L 低于原水 {src[ion]:g} mg/L，"
                       f"需要补充量为负（{tgt[ion] - src[ion]:g} mg/L），无法靠加盐实现。",
            "adjust": dilution_hint or
                      f"请把目标 {ION_LABELS[ion]} 提高到 ≥ {src[ion]:g} mg/L。",
        })

    # 需求摩尔浓度 mol/L
    n_req = {ion: need[ion] / M[ion] / 1000.0 for ion in ("ca", "mg", "hco3")}

    avail = {
        "cacl2": stocks["cacl2_2h2o"]["enabled"],
        "caso4": stocks["caso4_2h2o"]["enabled"],
        "mgso4": stocks["mgso4_7h2o"]["enabled"],
        "mgcl2": stocks["mgcl2_6h2o"]["enabled"],
        "nahco3": stocks["nahco3"]["enabled"],
        "khco3": stocks["khco3"]["enabled"],
    }

    # 各组无可用盐
    group_missing = []
    if n_req["ca"] > EPS and not (avail["cacl2"] or avail["caso4"]):
        group_missing.append(("ca", "Ca²⁺", "CaCl₂·2H₂O 或 CaSO₄·2H₂O"))
    if n_req["mg"] > EPS and not (avail["mgso4"] or avail["mgcl2"]):
        group_missing.append(("mg", "Mg²⁺", "MgSO₄·7H₂O 或 MgCl₂·6H₂O"))
    if n_req["hco3"] > EPS and not (avail["nahco3"] or avail["khco3"]):
        group_missing.append(("alk", "碱度", "NaHCO₃ 或 KHCO₃"))
    for _g, ion_name, salt_names in group_missing:
        conflicts.append({
            "level": "error", "code": "no_salt_available",
            "message": f"目标需要补充 {ion_name}，但未启用任何可用的{GROUP_LABELS[_g]}（{salt_names}）。",
            "adjust": f"请勾选并配置 {salt_names} 中至少一支储备液，或调低对应目标。",
        })

    # --- 钙/镁联立分配（Cl⁻、SO₄²⁻ 上限耦合）---
    # x = CaCl2 摩尔量，v = MgCl2 摩尔量（每 L）
    # 2(x+v) ≤ Cl 上限(摩尔)；(nCa-x)+(nMg-v) ≤ SO4 上限(摩尔)
    split, cap_infeasible = _split_ca_mg(
        n_req["ca"], n_req["mg"], avail, cl_cap, so4_cap,
        allow_ca=not any(g == "ca" for g, *_ in group_missing),
        allow_mg=not any(g == "mg" for g, *_ in group_missing),
    )

    amounts = {key: 0.0 for key in SALTS}
    if split is not None:
        amounts["cacl2_2h2o"] = split["cacl2"]
        amounts["caso4_2h2o"] = split["caso4"]
        amounts["mgso4_7h2o"] = split["mgso4"]
        amounts["mgcl2_6h2o"] = split["mgcl2"]

    # --- 碱度分配：优先 NaHCO3，其次 KHCO3 ---
    if n_req["hco3"] > EPS and (avail["nahco3"] or avail["khco3"]):
        amounts["nahco3" if avail["nahco3"] else "khco3"] = n_req["hco3"]

    # --- 上限冲突诊断（不可达时仍给出"偏好方案"用于界面标红）---
    if cap_infeasible:
        conflicts.append(_cap_conflict(
            n_req["ca"], n_req["mg"], need["ca"], need["mg"],
            avail, cl_cap, so4_cap))
        # 回退方案：按首选盐（CaCl2、MgSO4）投，界面据此把超限离子标红
        amounts["cacl2_2h2o"] = n_req["ca"] if avail["cacl2"] else 0.0
        amounts["caso4_2h2o"] = 0.0 if avail["cacl2"] else n_req["ca"]
        amounts["mgso4_7h2o"] = n_req["mg"] if avail["mgso4"] else 0.0
        amounts["mgcl2_6h2o"] = 0.0 if avail["mgso4"] else n_req["mg"]
        if not (avail["cacl2"] or avail["caso4"]):
            amounts["cacl2_2h2o"] = amounts["caso4_2h2o"] = 0.0
        if not (avail["mgso4"] or avail["mgcl2"]):
            amounts["mgso4_7h2o"] = amounts["mgcl2_6h2o"] = 0.0

    # 组缺盐时，对应分配量强制清零（目标不可达，冲突已登记）
    missing_groups = {g for g, *_ in group_missing}
    if "ca" in missing_groups:
        amounts["cacl2_2h2o"] = amounts["caso4_2h2o"] = 0.0
    if "mg" in missing_groups:
        amounts["mgso4_7h2o"] = amounts["mgcl2_6h2o"] = 0.0
    if "alk" in missing_groups:
        amounts["nahco3"] = amounts["khco3"] = 0.0

    # --- 成品组成与各盐剂量 ---
    final = {"ca": src["ca"], "mg": src["mg"], "na": src["na"],
             "k": 0.0, "cl": 0.0, "so4": 0.0, "hco3": src["hco3"]}
    liters = volume_ml / 1000.0
    doses = []
    total_add_ml = 0.0

    for key in sorted(SALTS, key=lambda k: SALTS[k]["order"]):
        meta = SALTS[key]
        row_cfg = stocks[key]
        mol = amounts[key]

        ions_added = {ion: mol * nu * M[ion] * 1000.0
                      for ion, nu in meta["ions"].items()}
        for ion, conc_add in ions_added.items():
            final[ion] += conc_add

        # mol 为每升成品需要的盐摩尔数：g = mol/L × L × MW(g/mol)，再换算 mg
        salt_mg = mol * meta["mw"] * liters * 1000.0
        add_ml = salt_mg / row_cfg["conc"] if row_cfg["conc"] > EPS and mol > EPS else None
        if add_ml is not None:
            total_add_ml += add_ml

        below_res = False
        suggest_stock_max = None
        if add_ml is not None and resolution_ml > 0 and add_ml < resolution_ml - EPS:
            below_res = True
            suggest_stock_max = salt_mg / resolution_ml
            conflicts.append({
                "level": "warning", "code": "below_resolution",
                "message": f"{meta['formula']} 储备液只需加 {add_ml:.3f} mL，"
                           f"低于量具分辨率 {resolution_ml:g} mL，实际无法准确量取。",
                "adjust": f"可把该储备液稀释到 ≤ {suggest_stock_max:.1f} g/L"
                          f"（添加量即升至约 {resolution_ml:g} mL），"
                          f"或把成品体积加大到 ≥ {volume_ml * resolution_ml / add_ml:.0f} mL；"
                          f"也可直接用 0.001g 天平称取 {salt_mg:.1f} mg 固体盐。",
            })

        doses.append({
            "key": key,
            "label": meta["label"],
            "formula": meta["formula"],
            "group": meta["group"],
            "enabled": row_cfg["enabled"],
            "stock_g_l": round(row_cfg["conc"], 3),
            "salt_mg": round(salt_mg, 1),
            "add_ml": round(add_ml, 3) if add_ml is not None else None,
            "below_resolution": below_res,
            "ions": {k: round(v, 2) for k, v in ions_added.items() if v > EPS},
        })

    # 储备液体积占比过大时提示底水修正
    if volume_ml > 0 and total_add_ml / volume_ml > 0.02:
        conflicts.append({
            "level": "info", "code": "volume_displacement",
            "message": f"储备液合计加入 {total_add_ml:.1f} mL，占成品体积 "
                       f"{total_add_ml / volume_ml * 100:.1f}%。",
            "adjust": "家庭配制通常忽略这点体积位移；若要精确，可先加 "
                      f"{volume_ml - total_add_ml:.0f} mL 原水/纯水再补储备液至总体积。",
        })

    # --- 硬度 / 碱度折算 ---
    gh_caco3 = final["ca"] * CACO3_MW_HALF / M["ca"] \
        + final["mg"] * CACO3_MW_HALF / M["mg"]
    alk_caco3 = final["hco3"] * CACO3_MW_HALF / M["hco3"]

    result = {
        "volume_ml": round(volume_ml, 1),
        "final": {k: round(v, 2) for k, v in final.items()},
        "hardness": {
            "as_caco3": round(gh_caco3, 1),
            "dh": round(gh_caco3 / DH_FACTOR, 2),
        },
        "alkalinity": {
            "as_hco3": round(final["hco3"], 2),
            "as_caco3": round(alk_caco3, 1),
            "dh": round(alk_caco3 / DH_FACTOR, 2),
        },
        "deviation": {
            ion: round(final[ion] - tgt[ion], 2) for ion in ("ca", "mg", "hco3")
        },
        "need": {ion: round(need[ion], 2) for ion in ("ca", "mg", "hco3")},
        "total_add_ml": round(total_add_ml, 3),
        "doses": doses,
        "conflicts": conflicts,
        "feasible": not any(c["level"] == "error" for c in conflicts),
    }
    return result


def _split_ca_mg(n_ca, n_mg, avail, cl_cap, so4_cap, allow_ca, allow_mg):
    """在 Cl/SO4 上限下分配钙盐、镁盐。

    优先用 CaCl₂ 补钙（x 取最大），优先用 MgSO₄ 补镁（MgCl₂ 量 v 取最小）。
    返回 (amounts 或 None, infeasible_bool)。
    """
    # x（CaCl2）取值域
    if not allow_ca or n_ca <= EPS:
        xl = xh = 0.0
    elif avail["cacl2"] and avail["caso4"]:
        xl, xh = 0.0, n_ca
    elif avail["cacl2"]:
        xl = xh = n_ca
    elif avail["caso4"]:
        xl = xh = 0.0
    else:
        return None, False  # 无盐可用由上层报错

    # v（MgCl2）取值域
    if not allow_mg or n_mg <= EPS:
        vl = vh = 0.0
    elif avail["mgso4"] and avail["mgcl2"]:
        vl, vh = 0.0, n_mg
    elif avail["mgso4"]:
        vl = vh = 0.0
    elif avail["mgcl2"]:
        vl = vh = n_mg
    else:
        return None, False

    c_half = (cl_cap / M["cl"] / 1000.0) / 2.0 if cl_cap is not None else 1e18
    s_mol = so4_cap / M["so4"] / 1000.0 if so4_cap is not None else 1e18

    total = n_ca + n_mg
    z_low = max(xl + vl, total - s_mol)     # x+v 下界（SO4 约束）
    z_high = min(xh + vh, c_half)           # x+v 上界（Cl 约束）
    if z_low > z_high + 1e-12:
        return None, True

    x = min(xh, c_half - vl)                # CaCl2 尽量多
    v = max(vl, total - s_mol - x)          # MgCl2 尽量少
    # 数值清洁
    x = max(0.0, min(n_ca, x))
    v = max(0.0, min(n_mg, v))
    return {
        "cacl2": x, "caso4": n_ca - x,
        "mgso4": n_mg - v, "mgcl2": v,
    }, False


def _cap_conflict(n_ca, n_mg, need_ca, need_mg, avail, cl_cap, so4_cap):
    """构造 Cl/SO4 上限不可达冲突，给出具体可调目标。"""
    total = n_ca + n_mg
    s_mol = so4_cap / M["so4"] / 1000.0 if so4_cap is not None else None
    c_half = (cl_cap / M["cl"] / 1000.0) / 2.0 if cl_cap is not None else None

    # 被禁用盐逼出来的"不可避免"量
    forced_cl = (n_ca if not avail["caso4"] else 0.0) \
        + (n_mg if not avail["mgso4"] else 0.0)
    forced_so = (n_ca if not avail["cacl2"] else 0.0) \
        + (n_mg if not avail["mgcl2"] else 0.0)

    cl_need_mol = 2.0 * max(forced_cl, total - (s_mol if s_mol is not None else -1e18))
    so_need_mol = max(forced_so, total - (c_half if c_half is not None else -1e18))
    cl_need_mg = cl_need_mol * M["cl"] * 1000.0
    so_need_mg = so_need_mol * M["so4"] * 1000.0

    over = []
    if cl_cap is not None and cl_need_mg > cl_cap + EPS:
        over.append(("cl", cl_need_mg, cl_cap))
    if so4_cap is not None and so_need_mg > so4_cap + EPS:
        over.append(("so4", so_need_mg, so4_cap))

    over_txt = "、".join(
        f"{ION_LABELS[k]}至少约 {v:.0f} mg/L（上限 {cap:g}）" for k, v, cap in over)
    message = (
        f"目标不可达：需补充 Ca²⁺ {need_ca:g} mg/L、Mg²⁺ {need_mg:g} mg/L"
        f"（合计 {total * 1000:.2f} mmol/L 二价阳离子），每 1 mmol Ca/Mg 必伴随"
        f" 2 mmol Cl⁻ 或 1 mmol SO₄²⁻。最优分配下{over_txt}。"
    )

    adjusts = []
    for k, v, _cap in over:
        if k == "cl":
            adjusts.append(f"把 Cl⁻ 上限放宽到 ≥ {math.ceil(v)} mg/L")
        else:
            adjusts.append(f"把 SO₄²⁻ 上限放宽到 ≥ {math.ceil(v)} mg/L")
    # 需削减的二价阳离子量
    bound = (c_half if c_half is not None else 0.0) \
        + (s_mol if s_mol is not None else 0.0)
    shortfall = max(0.0, total - bound)
    if shortfall > EPS:
        adjusts.append(
            f"或降低 Ca/Mg 目标：合计少补至少 {shortfall * 1000:.2f} mmol/L"
            f"（约相当于 Ca²⁺ {shortfall * M['ca'] * 1000:.0f} mg/L"
            f"或 Mg²⁺ {shortfall * M['mg'] * 1000:.0f} mg/L）")
    if not avail["caso4"]:
        adjusts.append("启用 CaSO₄·2H₂O 可把部分钙需求的伴随离子从 Cl⁻ 转为 SO₄²⁻")
    if not avail["mgcl2"]:
        adjusts.append("启用 MgCl₂·6H₂O 可把部分镁需求的伴随离子从 SO₄²⁻ 转为 Cl⁻")
    return {
        "level": "error", "code": "anion_cap_exceeded",
        "message": message,
        "adjust": "；".join(adjusts) + "。",
        "limits": {
            "cl_min_mg_l": round(cl_need_mg, 1),
            "so4_min_mg_l": round(so_need_mg, 1),
        },
    }


# ---------------------------------------------------------------- 等比换算

def scale_volume(result, new_volume_ml):
    """把已算好的配方结果等比换算到新体积（浓度组成不变，仅添加体积/称取量缩放）。"""
    if new_volume_ml <= 0:
        raise ValueError("新体积必须大于 0 mL")
    factor = new_volume_ml / result["volume_ml"]
    scaled = json_safe(result)
    scaled["volume_ml"] = round(new_volume_ml, 1)
    scaled["total_add_ml"] = round(result["total_add_ml"] * factor, 3)
    for d in scaled["doses"]:
        d["salt_mg"] = round(d["salt_mg"] * factor, 1)
        if d["add_ml"] is not None:
            d["add_ml"] = round(d["add_ml"] * factor, 3)
    return scaled


def json_safe(obj):
    import json
    return json.loads(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------- 前端元数据

def meta():
    return {
        "salts": [
            {
                "key": key,
                "label": SALTS[key]["label"],
                "formula": SALTS[key]["formula"],
                "mw": SALTS[key]["mw"],
                "group": SALTS[key]["group"],
                "ions": SALTS[key]["ions"],
                "order": SALTS[key]["order"],
            }
            for key in sorted(SALTS, key=lambda k: SALTS[k]["order"])
        ],
        "ion_labels": ION_LABELS,
        "group_labels": GROUP_LABELS,
        "defaults": {"limits": DEFAULT_LIMITS},
    }


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    import json

    demo = {
        "volume_ml": 1000,
        "source": {"ca": 10, "mg": 2, "na": 5, "hco3": 20},
        "target": {"ca": 55, "mg": 10, "hco3": 45},
        "stocks": {
            "cacl2_2h2o": {"enabled": True, "conc_g_l": 10},
            "mgso4_7h2o": {"enabled": True, "conc_g_l": 10},
            "nahco3": {"enabled": True, "conc_g_l": 10},
        },
        "limits": {"cl_max": 100, "so4_max": 150, "resolution_ml": 0.1},
    }
    print("== 经典三支盐 1L ==")
    print(json.dumps(calculate_recipe(demo), ensure_ascii=False, indent=2))

    print("\n== 等比换算到 250mL ==")
    r = calculate_recipe(demo)
    print(json.dumps(scale_volume(r, 250)["doses"], ensure_ascii=False, indent=2))

    bad = dict(demo)
    bad["limits"] = {"cl_max": 30, "so4_max": 30, "resolution_ml": 0.1}
    print("\n== Cl/SO4 上限过紧 ==")
    print(json.dumps(calculate_recipe(bad)["conflicts"], ensure_ascii=False, indent=2))

    low = dict(demo)
    low["target"] = {"ca": 5, "mg": 10, "hco3": 45}
    print("\n== 目标低于原水 ==")
    print(json.dumps(calculate_recipe(low)["conflicts"], ensure_ascii=False, indent=2))
