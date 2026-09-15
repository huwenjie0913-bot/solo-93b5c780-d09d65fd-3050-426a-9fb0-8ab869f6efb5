"""诊断规则引擎：阶段计算、规则命中、风味归因、对比调整建议。

阶段(stage)定义：相邻两个注水节点之间为一个阶段。
- 水量增加 -> 注水段(pour)，流速 = 水量增量 / 时长
- 水量不变 -> 停顿段(pause)，流速为 0
第 1 个注水段（从 w=0 出发）视为闷蒸段(bloom)。

所有阈值集中在 THRESHOLDS，前端可通过 /api/thresholds 取用并展示。
不依赖第三方库，可独立运行：python3 diagnostics.py 做自检。
"""

import math

# 阈值集中管理（诊断参考值，可按豆子/滤杯调整）
THRESHOLDS = {
    "bloom_min_time": 15,      # 闷蒸注水短于该秒数 -> 闷蒸过短
    "bloom_water_low": 2.2,    # 闷蒸水量 < 粉量 × 该倍数 -> 闷蒸水量不足
    "bloom_water_high": 3.5,   # 闷蒸水量 > 粉量 × 该倍数 -> 闷蒸水量偏大
    "surge_factor": 2.0,       # 流速 > 中位数 × 该系数
    "surge_min_delta": 2.0,    # 且至少高出中位数这么多 g/s
    "slow_factor": 0.6,        # 流速 < 中位数 × 该系数 -> 水流偏小
    "pause_long": 25,          # 非闷蒸停顿超过该秒数 -> 断流
    "temp_low": 89,            # 水温低于该值 -> 偏低
    "temp_high": 96,           # 水温高于该值 -> 偏高
    "temp_off_low": 84,        # 低于该值 -> 可能出现不熟/生涩风味
    "temp_off_high": 97,       # 高于该值 -> 可能出焦苦
    "ratio_low_tol": 1.5,      # 实际粉水比低于目标超过该值 -> 水量不足
    "ratio_high_tol": 2.0,     # 实际粉水比高于目标超过该值 -> 过度稀释
    "brew_long": 240,          # 总时长超过该秒数
    "grind_coarse": 25,        # 研磨刻度大于等于该值（数值越大越粗）视为偏粗
    "water_mismatch": 5,       # 末节点累计水量与登记水量相差超过该克数
}

VARIABLE_LABELS = {
    "grind": "研磨刻度",
    "temp": "水温",
    "dose": "粉量",
    "ratio": "粉水比/总水量",
    "bloom": "闷蒸",
    "flow": "注水流速与节奏",
}

FLAVOR_LABELS = {
    "sour": "偏酸",
    "astringent": "发涩",
    "bitter": "偏苦",
    "weak": "淡薄",
    "muddled": "层次混乱",
    "off": "异味/不熟",
}


# ---------------------------------------------------------------- 阶段计算

def sanitize_nodes(nodes):
    """清洗节点：去重、时间非负且单调递增、水量非负且不回落，保留角色。"""
    clean = []
    for n in (nodes or []):
        try:
            t = max(0, round(float(n.get("t", 0))))
            w = max(0, round(float(n.get("w", 0)), 1))
        except (TypeError, ValueError, AttributeError):
            continue
        if clean and t <= clean[-1]["t"]:
            t = clean[-1]["t"] + 1
        if clean and w < clean[-1]["w"]:
            w = clean[-1]["w"]
        clean.append({"t": t, "w": w})
    if not clean:
        clean = [{"t": 0, "w": 0}]
    if clean[0]["t"] != 0 or clean[0]["w"] != 0:
        clean.insert(0, {"t": 0, "w": 0})
    return clean


def compute_stages(nodes):
    nodes = sanitize_nodes(nodes)
    stages = []
    for i in range(1, len(nodes)):
        t0, w0 = nodes[i - 1]["t"], nodes[i - 1]["w"]
        t1, w1 = nodes[i]["t"], nodes[i]["w"]
        dt, dw = t1 - t0, round(w1 - w0, 1)
        kind = "pour" if dw > 0 else "pause"
        stages.append({
            "index": i - 1,
            "kind": kind,
            "t0": t0, "t1": t1, "duration": dt,
            "w0": w0, "w1": w1, "added": dw,
            "flow": round(dw / dt, 2) if kind == "pour" and dt > 0 else 0,
            "bloom": (i == 1 and w0 == 0 and dw > 0),
        })
    return nodes, stages


def _median(values):
    if not values:
        return None
    s = sorted(values)
    m = len(s) // 2
    if len(s) % 2:
        return round(s[m], 2)
    return round((s[m - 1] + s[m]) / 2, 2)


def analyze(brew):
    """对一次冲煮运行全部诊断，返回摘要/阶段/规则/风味归因。"""
    th = THRESHOLDS
    nodes, stages = compute_stages(brew.get("nodes"))
    dose = float(brew.get("dose") or 0)
    water_param = float(brew.get("water") or 0)
    temp = float(brew.get("temp") or 0)
    target_ratio = float(brew.get("target_ratio") or 0)

    pours = [s for s in stages if s["kind"] == "pour"]
    pauses = [s for s in stages if s["kind"] == "pause"]
    flows = [s["flow"] for s in pours]
    total_time = nodes[-1]["t"] if nodes else 0
    total_water = nodes[-1]["w"] if nodes else 0
    actual_ratio = round(total_water / dose, 2) if dose else 0
    target_water = round(dose * target_ratio, 1) if dose and target_ratio else 0
    deviation = round(actual_ratio - target_ratio, 2) if target_ratio else 0
    median = _median(flows)
    mean_flow = round(sum(flows) / len(flows), 2) if flows else 0
    cv = round(math.sqrt(sum((f - mean_flow) ** 2 for f in flows) / len(flows)) / mean_flow, 2) \
        if len(flows) >= 2 and mean_flow else 0

    bloom = next((s for s in stages if s.get("bloom")), None)
    bloom_water = bloom["w1"] if bloom else 0
    bloom_pause = next((s for s in pauses if bloom and s["t0"] >= bloom["t1"]
                        and s["index"] == bloom["index"] + 1), None)

    summary = {
        "total_time": total_time,
        "total_water": round(total_water, 1),
        "water_param": round(water_param, 1),
        "actual_ratio": actual_ratio,
        "target_water": target_water,
        "ratio_deviation": deviation,
        "bloom_water": round(bloom_water, 1),
        "bloom_time": bloom["duration"] if bloom else 0,
        "bloom_pause": bloom_pause["duration"] if bloom_pause else 0,
        "pour_count": len(pours),
        "pause_count": len(pauses),
        "median_flow": median,
        "flow_cv": cv,
    }

    rules = []

    def add_rule(rid, level, label, message, stage_indexes=None):
        rules.append({
            "id": rid,
            "level": level,
            "label": label,
            "message": message,
            "stages": stage_indexes or [],
        })

    # --- 闷蒸 ---
    if bloom:
        if bloom["duration"] < th["bloom_min_time"]:
            add_rule(
                "bloom_short", "warning", "闷蒸不足",
                f"闷蒸注水仅 {bloom['duration']}s，建议持续 {th['bloom_min_time']}–30s 让咖啡粉充分排气。",
                [bloom["index"]])
        if dose and bloom_water < th["bloom_water_low"] * dose:
            add_rule(
                "bloom_water_low", "warning", "闷蒸水量不足",
                f"闷蒸水量 {bloom_water:g}g 低于粉量的 {th['bloom_water_low']:g} 倍"
                f"（约 {th['bloom_water_low'] * dose:g}g），粉层可能未完全浸润。",
                [bloom["index"]])
        elif dose and bloom_water > th["bloom_water_high"] * dose:
            add_rule(
                "bloom_excess", "info", "闷蒸水量偏大",
                f"闷蒸水量 {bloom_water:g}g 超过粉量的 {th['bloom_water_high']:g} 倍，前段萃取可能偏多。",
                [bloom["index"]])
    else:
        add_rule("no_bloom", "warning", "缺少闷蒸",
                 "曲线中没有从 0 开始的闷蒸注水段，建议先做小水量闷蒸。")

    # --- 流速：突增 / 偏小（至少两段注水时才比较中位数）---
    if median and len(pours) >= 2:
        surge_line = max(median * th["surge_factor"], median + th["surge_min_delta"])
        for s in pours:
            if s["flow"] > surge_line:
                add_rule(
                    "surge", "bad", "注水突增",
                    f"第 {s['index'] + 1} 段流速 {s['flow']:g} g/s，"
                    f"远高于中位数 {median:g} g/s，容易冲出通道、萃取不均。",
                    [s["index"]])
            elif s["flow"] < median * th["slow_factor"] and s["added"] >= 10:
                add_rule(
                    "slow_pour", "info", "水流偏小",
                    f"第 {s['index'] + 1} 段流速仅 {s['flow']:g} g/s，"
                    f"明显低于中位数 {median:g} g/s，耗时过长易带入苦涩。",
                    [s["index"]])

    # --- 停顿 / 断流（闷蒸后的第一个停顿豁免）---
    for s in pauses:
        if bloom_pause and s["index"] == bloom_pause["index"]:
            continue
        if s["duration"] > th["pause_long"]:
            add_rule(
                "pause_long", "bad", "断流",
                f"第 {s['index'] + 1} 段停顿 {s['duration']}s（超过 {th['pause_long']}s），"
                f"粉层失水后再萃取容易发涩、层次断裂。",
                [s["index"]])

    # --- 水温 ---
    if temp:
        if temp < th["temp_off_low"]:
            add_rule("temp_off", "bad", "水温过低",
                     f"水温 {temp:g}℃ 过低，容易萃取不足并带生涩味。")
        elif temp < th["temp_low"]:
            add_rule("temp_low", "warning", "水温偏低",
                     f"水温 {temp:g}℃ 低于 {th['temp_low']}℃，酸甜可能失衡、偏向尖酸。")
        elif temp > th["temp_off_high"]:
            add_rule("temp_off", "bad", "水温过高",
                     f"水温 {temp:g}℃ 过高，容易萃出焦苦与涩感。")
        elif temp > th["temp_high"]:
            add_rule("temp_high", "warning", "水温偏高",
                     f"水温 {temp:g}℃ 高于 {th['temp_high']}℃，注意苦涩与过度萃取。")

    # --- 粉水比 ---
    if target_ratio and dose:
        if deviation < -th["ratio_low_tol"]:
            add_rule(
                "ratio_low", "warning", "实际粉水比偏低",
                f"累计注水 {total_water:g}g，实际粉水比 1:{actual_ratio:g}，"
                f"低于目标 1:{target_ratio:g}，少了约 {round(target_water - total_water, 1):g}g 水。")
        elif deviation > th["ratio_high_tol"]:
            add_rule("ratio_high", "warning", "实际粉水比偏高",
                     f"累计注水 {total_water:g}g，实际粉水比 1:{actual_ratio:g}，"
                     f"高于目标 1:{target_ratio:g}，可能过度稀释、口感发淡。")

    # --- 登记水量与曲线末值一致性 ---
    if water_param and abs(water_param - total_water) > th["water_mismatch"]:
        add_rule(
            "water_mismatch", "warning", "水量记录不一致",
            f"登记总水量 {water_param:g}g，但曲线末节点累计为 {total_water:g}g，请核对注水节点。")

    # --- 总时长 ---
    if total_time > th["brew_long"]:
        add_rule("brew_long", "info", "总时长偏长",
                 f"全程 {total_time}s 超过 {th['brew_long']}s，尾段过萃风险升高。")

    flavors = _attribute_flavors(
        brew, stages, rules, summary, bloom, bloom_pause, median)

    return {
        "summary": summary,
        "stages": stages,
        "rules": rules,
        "flavors": flavors,
    }


# ---------------------------------------------------------------- 风味归因

def _attribute_flavors(brew, stages, rules, summary, bloom, bloom_pause, median_flow):
    """把风味问题关联到可疑阶段。返回 {风味key: [{reason, stages:[...]}]}。"""
    th = THRESHOLDS
    dose = float(brew.get("dose") or 0)
    temp = float(brew.get("temp") or 0)
    grind = float(brew.get("grind") or 0)
    total_time = summary["total_time"]
    deviation = summary["ratio_deviation"]

    by_id = {r["id"]: r for r in rules}
    out = {}

    def add(flavor, reason, stage_indexes=None):
        out.setdefault(flavor, []).append({"reason": reason, "stages": stage_indexes or []})

    bloom_idx = [bloom["index"]] if bloom else []
    last_pour = next((s for s in reversed(stages) if s["kind"] == "pour"), None)
    tail_idx = [last_pour["index"]] if last_pour else []

    # 偏酸：多为萃取不足
    if "bloom_water_low" in by_id or "bloom_short" in by_id:
        idxs = sorted({i for k in ("bloom_water_low", "bloom_short")
                       if k in by_id for i in by_id[k]["stages"]})
        add("sour", "闷蒸不足导致排气与前段萃取不充分，易出现尖酸。", idxs)
    if temp and temp < th["temp_low"]:
        add("sour", f"水温 {temp:g}℃ 偏低，酸质融出比例偏高、甜感不足。")
    if grind and grind >= th["grind_coarse"]:
        add("sour", f"研磨刻度 {grind:g}（数值越大越粗）可能偏粗，整体萃取不足。")
    if deviation < -th["ratio_low_tol"]:
        add("sour", "总注水量少于目标粉水比，浓度偏高且萃取不完整，酸感突出。")

    # 发涩：细粉过度萃取 / 断流复吸
    if "surge" in by_id:
        add("astringent", "注水突增冲击粉层、细粉迁移并造成通道，带来干涩感。",
            by_id["surge"]["stages"])
    if "pause_long" in by_id:
        add("astringent", "长时间断流使粉层失水后复吸，再注水时易萃出涩味。",
            by_id["pause_long"]["stages"])
    if "slow_pour" in by_id:
        add("astringent", "尾段水流过小、接触时间过长，容易过萃发涩。",
            by_id["slow_pour"]["stages"])
    if total_time > th["brew_long"]:
        add("astringent", f"全程 {total_time}s 偏长，尾段萃出过多单宁。", tail_idx)
    if temp and temp > th["temp_high"]:
        add("astringent", f"水温 {temp:g}℃ 偏高，细粉中的涩味更易被萃出。")

    # 偏苦
    if temp and temp > th["temp_high"]:
        add("bitter", f"水温 {temp:g}℃ 偏高会加重苦味。")
    if total_time > th["brew_long"]:
        add("bitter", "萃取时间过长，尾段的苦味物质被过度带出。", tail_idx)
    if "slow_pour" in by_id:
        add("bitter", "尾段流速过慢、浸泡过久，苦味堆积。", by_id["slow_pour"]["stages"])

    # 淡薄：水量过多 / 萃取不足 / 通道
    if deviation > th["ratio_high_tol"]:
        add("weak", "实际粉水比高于目标，咖啡液被过度稀释。")
    elif deviation < -th["ratio_low_tol"]:
        add("weak", "注水量不足导致出液量偏少；若同时偏粗，body 也会偏薄。")
    if "surge" in by_id:
        add("weak", "注水过猛形成通道，有效萃取路径变短，口感薄、余韵短。",
            by_id["surge"]["stages"])
    if grind and grind >= th["grind_coarse"] + 1:
        add("weak", f"研磨偏粗（刻度 {grind:g}），可溶物析出不足。")

    # 层次混乱：节奏不均
    if "surge" in by_id:
        add("muddled", "突增的大水流打乱粉层，各段萃取程度不一，风味混杂。",
            by_id["surge"]["stages"])
    if "pause_long" in by_id:
        add("muddled", "断流把萃取节奏切成两段，前段与后段风格断裂。",
            by_id["pause_long"]["stages"])
    if "bloom_short" in by_id or "bloom_water_low" in by_id:
        add("muddled", "闷蒸不充分会让后续萃取起点不均，层次含糊。", bloom_idx)
    if summary["flow_cv"] and summary["flow_cv"] >= 0.6:
        add("muddled",
            f"各注水段流速差异大（变异系数 {summary['flow_cv']:g}），节奏不稳定。")

    # 异味/不熟
    if temp and temp < th["temp_off_low"]:
        add("off", f"水温 {temp:g}℃ 过低，可能带出生涩、不熟的风味。")
    if temp and temp > th["temp_off_high"]:
        add("off", f"水温 {temp:g}℃ 过高，可能出现焦苦、烟熏味。")
    if "no_bloom" in by_id:
        add("off", "缺少闷蒸排气，新鲜气体残留会带来生青感。")

    return out


# ---------------------------------------------------------------- 调整建议

def suggest(anchor, baseline=None, locked=None):
    """以 anchor 为当前杯、baseline 为对照杯，锁定变量后生成下一次调整建议。"""
    locked = set(locked or [])
    diag = analyze(anchor)
    base_diag = analyze(baseline) if baseline else None
    rules_by_id = {r["id"]: r for r in diag["rules"]}
    flavors = anchor.get("flavors") or []

    def locked_note(var):
        return f"（已锁定「{VARIABLE_LABELS[var]}」，本次不动）"

    def diff_text(var, anchor_val, base_val, unit="", reverse=False):
        """对照差异描述。reverse=True 表示数值越小越细（研磨）。"""
        if baseline is None or anchor_val == base_val:
            return None
        delta = round(anchor_val - base_val, 2)
        if reverse:
            word = "偏粗" if delta > 0 else "偏细"
        else:
            word = "偏高" if delta > 0 else "偏低"
        return f"对照 {baseline.get('name', '基线')}：{base_val:g}{unit} → {anchor_val:g}{unit}（{word} {abs(delta):g}）"

    suggestions = []

    def add(var, title, detail, stages=None):
        if var in locked:
            return
        suggestions.append({
            "variable": var,
            "title": title,
            "detail": detail,
            "stages": stages or [],
        })

    s = diag["summary"]
    dose = float(anchor.get("dose") or 0)
    grind = float(anchor.get("grind") or 0)
    temp = float(anchor.get("temp") or 0)

    # 研磨：偏酸/淡薄调细，发涩/偏苦调粗
    if "grind" not in locked and grind:
        base_grind = float(baseline.get("grind") or 0) if baseline else 0
        d = diff_text("grind", grind, base_grind, reverse=True) if baseline else None
        if any(f in flavors for f in ("sour", "weak")):
            target = base_grind if baseline and base_grind and base_grind < grind else grind - 1
            detail = f"当前刻度 {grind:g}（数值越大越粗），先调细 1 格到约 {target:g}，" \
                     f"观察酸质是否收敛、甜感是否提升。"
            if d:
                detail = d + "。建议先回到基线附近：" + detail
            add("grind", "研磨调细 1 格", detail)
        elif any(f in flavors for f in ("astringent", "bitter")):
            target = base_grind if baseline and base_grind and base_grind > grind else grind + 1
            detail = f"当前刻度 {grind:g}，先调粗 1 格到约 {target:g}，缩短有效萃取，减轻干涩/苦尾。"
            if d:
                detail = d + "。建议先回到基线附近：" + detail
            add("grind", "研磨调粗 1 格", detail)

    # 水温
    if "temp" not in locked and temp:
        base_temp = float(baseline.get("temp") or 0) if baseline else 0
        d = diff_text("temp", temp, base_temp, "℃") if baseline else None
        cold_hit = "temp_low" in rules_by_id or (
            "temp_off" in rules_by_id and temp < 90)
        hot_hit = "temp_high" in rules_by_id or (
            "temp_off" in rules_by_id and temp > 90)
        if any(f in flavors for f in ("sour", "weak", "off")) or cold_hit:
            target = base_temp if baseline and base_temp and base_temp > temp else min(94, temp + 2)
            detail = f"当前 {temp:g}℃，下次提高到 {target:g}℃（先 +2℃），帮助甜感与层次展开。"
            if d:
                detail = d + "。" + detail
            add("temp", "水温提高 2℃", detail)
        elif any(f in flavors for f in ("astringent", "bitter")) or hot_hit:
            target = base_temp if baseline and base_temp and base_temp < temp else max(88, temp - 2)
            detail = f"当前 {temp:g}℃，下次降到 {target:g}℃（先 -2℃），压制苦涩与涩感。"
            if d:
                detail = d + "。" + detail
            add("temp", "水温降低 2℃", detail)

    # 闷蒸
    bloom_rule_hit = any(k in rules_by_id for k in ("bloom_short", "bloom_water_low"))
    if "bloom" not in locked and (bloom_rule_hit or "sour" in flavors or "muddled" in flavors):
        target_w = round(2.5 * dose, 0) if dose else 35
        parts = []
        if "bloom_water_low" in rules_by_id:
            parts.append(f"闷蒸水量加到约 {target_w:g}g（粉量的 2.5 倍）")
        if "bloom_short" in rules_by_id:
            parts.append(f"注水拉长到 {THRESHOLDS['bloom_min_time'] + 10}s 左右")
        if not parts:
            parts.append(f"保持闷蒸水量约 {target_w:g}g、时长 25–30s")
        parts.append("注水后静置 25–35s 再开始第二段")
        add("bloom", "补足闷蒸", "；".join(parts) + "。", [0])

    # 粉水比 / 总水量
    if "ratio" not in locked:
        if "ratio_low" in rules_by_id:
            add("ratio", "补足总水量",
                f"目标粉水比 1:{anchor.get('target_ratio'):g} 需要约 {s['target_water']:g}g，"
                f"本次只注了 {s['total_water']:g}g；尾段补到目标水量，注意保持小水流。")
        elif "ratio_high" in rules_by_id and "weak" in flavors:
            add("ratio", "收窄粉水比",
                f"实际 1:{s['actual_ratio']:g} 高于目标，下次按目标水量注水或增加粉量，避免稀释。")
        elif "weak" in flavors and not any(f in flavors for f in ("sour",)):
            add("ratio", "保持粉水比、提高萃取",
                "粉水比不变，优先通过调细研磨/提高水温来增加萃取，而不是加大水量。")

    # 流速与节奏：来自规则命中，最具体，直接指向阶段
    flow_items = []
    for r in diag["rules"]:
        if r["id"] == "surge":
            st = next((x for x in diag["stages"] if x["index"] == r["stages"][0]), None)
            if st:
                target_rate = base_diag["summary"]["median_flow"] if base_diag else s["median_flow"] or 2.5
                seconds = max(round(st["added"] / target_rate), st["duration"] + 5)
                flow_items.append(
                    f"第 {st['index'] + 1} 段 {st['added']:g}g 用了 {st['duration']}s"
                    f"（{st['flow']:g} g/s）：压低壶嘴、绕圈放慢，把该段拉长到约 {seconds}s"
                    f"（≈{target_rate:g} g/s）。")
        elif r["id"] == "pause_long":
            st = next((x for x in diag["stages"] if x["index"] == r["stages"][0]), None)
            if st:
                flow_items.append(
                    f"第 {st['index'] + 1} 段停顿 {st['duration']}s 已构成断流："
                    f"看到液面下降即续水，非闷蒸停顿控制在 5–10s。")
        elif r["id"] == "slow_pour":
            st = next((x for x in diag["stages"] if x["index"] == r["stages"][0]), None)
            if st:
                flow_items.append(
                    f"第 {st['index'] + 1} 段流速仅 {st['flow']:g} g/s：适当加大水流、减少绕圈，"
                    f"避免尾段浸泡过久。")
    if flow_items and "flow" not in locked:
        add("flow", "稳定注水节奏", " ".join(flow_items),
            [i for r in diag["rules"] if r["id"] in ("surge", "pause_long", "slow_pour")
             for i in r["stages"]])

    # 未勾选风味但规则有命中时，仍给出最紧要的两条
    if not flavors and not suggestions and flow_items:
        add("flow", "先修正注水节奏", " ".join(flow_items[:2]))

    note_parts = ["每次只变动一个变量，其余参数与注水节奏尽量保持一致，便于对照。"]
    skipped = [VARIABLE_LABELS[k] for k in locked if k in VARIABLE_LABELS]
    if skipped:
        note_parts.append("已锁定：" + "、".join(skipped) + "，建议中已跳过。")
    if not suggestions:
        note_parts.append("当前未发现需要调整的明显问题，可保持参数再做一杯确认。")

    return {
        "suggestions": suggestions,
        "locked": skipped,
        "note": " ".join(note_parts),
    }


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    sample = {
        "dose": 15, "water": 196, "temp": 88, "grind": 25, "target_ratio": 15,
        "flavors": ["sour", "muddled"],
        "nodes": [
            {"t": 0, "w": 0}, {"t": 8, "w": 20}, {"t": 16, "w": 20},
            {"t": 26, "w": 105}, {"t": 60, "w": 105},
            {"t": 90, "w": 165}, {"t": 103, "w": 196},
        ],
    }
    import json
    r = analyze(sample)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(json.dumps(suggest(sample, None, ["grind"]), ensure_ascii=False, indent=2))
