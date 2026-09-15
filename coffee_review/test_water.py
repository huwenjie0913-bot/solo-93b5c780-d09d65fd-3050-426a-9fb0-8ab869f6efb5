"""冲煮水配方引擎回归测试（仅 Python 标准库）。

运行：python3 -m unittest test_water -v
覆盖两个已复核的正常使用缺陷：
1. 等比换算到极小体积时，below_resolution 与体积相关冲突必须按新剂量重算；
2. 单边阴离子上限（另一边填 0=不限制）时，冲突文案给出有限、可用、
   与实际约束一致的建议，不得出现 1e18 之类的哨兵数字。
"""

import copy
import json
import math
import unittest

import water

CLASSIC_SPEC = {
    "volume_ml": 1000,
    "source": {"ca": 10, "mg": 2, "na": 5, "hco3": 20},
    "target": {"ca": 55, "mg": 10, "hco3": 45},
    "stocks": {
        "cacl2_2h2o": {"enabled": True, "conc_g_l": 50},
        "mgso4_7h2o": {"enabled": True, "conc_g_l": 50},
        "nahco3": {"enabled": True, "conc_g_l": 25},
    },
    "limits": {"cl_max": 100, "so4_max": 150, "resolution_ml": 0.1},
}


def dose(result, key):
    return next(d for d in result["doses"] if d["key"] == key)


class ScaleVolumeTests(unittest.TestCase):
    """缺陷 1：专用等比换算必须按新剂量重算分辨率/冲突。"""

    def test_scaling_to_10ml_marks_all_three_below_resolution(self):
        r1000 = water.calculate_recipe(CLASSIC_SPEC)
        # 基线：1L 下三项均高于 0.1 mL，无 below_resolution
        self.assertTrue(r1000["feasible"])
        self.assertFalse(
            [c for c in r1000["conflicts"] if c["code"] == "below_resolution"])
        base = {k: dose(r1000, k)["add_ml"]
                for k in ("cacl2_2h2o", "mgso4_7h2o", "nahco3")}
        for v in base.values():
            self.assertGreaterEqual(v, 0.1)

        # 缩到 10 mL（权威路径：带 params 重算）
        r10 = water.scale_volume(r1000, 10, spec=CLASSIC_SPEC)
        expected = {k: round(v / 100, 3) for k, v in base.items()}
        self.assertEqual(r10["volume_ml"], 10)
        for key, want in expected.items():
            self.assertAlmostEqual(dose(r10, key)["add_ml"], want, places=3)

        # 三项（0.033 / 0.016 / 0.014 mL）都必须触发 0.1 mL 分辨率提示
        warnings = [c for c in r10["conflicts"] if c["code"] == "below_resolution"]
        self.assertEqual(len(warnings), 3)
        for key in ("cacl2_2h2o", "mgso4_7h2o", "nahco3"):
            self.assertTrue(dose(r10, key)["below_resolution"], key)
        mentioned = " ".join(c["message"] for c in warnings)
        self.assertIn("0.033", mentioned)
        self.assertIn("0.016", mentioned)
        self.assertIn("0.014", mentioned)
        self.assertIn("0.1", mentioned)
        # 浓度相关结果不随体积变化
        self.assertEqual(r10["final"], r1000["final"])
        self.assertEqual(r10["deviation"], r1000["deviation"])
        self.assertTrue(r10["feasible"])

    def test_scaling_snapshot_path_rebuilds_conflicts(self):
        """无 params 的旧快照路径同样要重算 below_resolution，而不是沿用旧冲突。"""
        r1000 = water.calculate_recipe(CLASSIC_SPEC)
        r10 = water._rescale_snapshot(r1000, 10)
        warnings = [c for c in r10["conflicts"] if c["code"] == "below_resolution"]
        self.assertEqual(len(warnings), 3)
        for key in ("cacl2_2h2o", "mgso4_7h2o", "nahco3"):
            self.assertTrue(dose(r10, key)["below_resolution"])
        # 浓度型错误（若有）应保留；below_resolution 不应来自 1L 旧结果
        self.assertTrue(all(c["code"] == "below_resolution" or
                            c["code"] in water._VOLUME_INVARIANT_CODES
                            for c in r10["conflicts"]))

    def test_scaling_up_keeps_no_resolution_warnings(self):
        r1000 = water.calculate_recipe(CLASSIC_SPEC)
        r5000 = water.scale_volume(r1000, 5000, spec=CLASSIC_SPEC)
        self.assertFalse(
            [c for c in r5000["conflicts"] if c["code"] == "below_resolution"])
        for key in ("cacl2_2h2o", "mgso4_7h2o", "nahco3"):
            self.assertFalse(dose(r5000, key)["below_resolution"])

    def test_scaling_preserves_concentration_conflict(self):
        """原本就因 Cl/SO4 上限不可达的配方，缩小体积后浓度型错误仍在。"""
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 20, "so4_max": 40, "resolution_ml": 0.1}
        r1000 = water.calculate_recipe(spec)
        self.assertFalse(r1000["feasible"])
        r10 = water.scale_volume(r1000, 10, spec=spec)
        codes = [c["code"] for c in r10["conflicts"]]
        self.assertIn("anion_cap_exceeded", codes)
        # 同时体积相关的分辨率警告也要新增
        self.assertEqual(codes.count("below_resolution"), 3)


class SingleSidedCapTests(unittest.TestCase):
    """缺陷 2：单边阴离子上限（另一侧 0=不限制）。"""

    def test_cl_cap_with_uncapped_so4(self):
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 10, "so4_max": 0, "resolution_ml": 0.1}
        r = water.calculate_recipe(spec)

        # 必须继续指出 Cl 实际约 79.61 mg/L 的冲突
        self.assertFalse(r["feasible"])
        cap_conflicts = [c for c in r["conflicts"] if c["code"] == "anion_cap_exceeded"]
        self.assertEqual(len(cap_conflicts), 1)
        c = cap_conflicts[0]
        self.assertIn("79.6", c["message"])
        self.assertIn("SO₄²⁻ 未设上限", c["message"])
        # SO4 未设限：实际成品 SO4 不参与冲突（偏好方案里 Mg 仍走 MgSO4）
        self.assertNotIn("SO₄²⁻至少", c["message"])

        # 调整建议必须有限、可用
        text = c["adjust"]
        self.assertIn("Cl⁻ 上限放宽到 ≥ 80 mg/L", text)
        self.assertIn("启用 CaSO₄·2H₂O", text)
        self.assertNotIn("e+", text.lower())
        for token in text.replace("，", "；").split("；"):
            for word in token.replace("mg/L", "").split():
                try:
                    num = float(word)
                except ValueError:
                    continue
                self.assertTrue(math.isfinite(num), token)
                self.assertLess(abs(num), 1e9, token)
        # limits 字段只包含被设置侧的有限最小值
        self.assertEqual(set(c["limits"].keys()), {"cl_min_mg_l"})
        self.assertAlmostEqual(c["limits"]["cl_min_mg_l"], 79.6, places=0)

    def test_actual_cl_value_matches_classic_recipe(self):
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 10, "so4_max": 0, "resolution_ml": 0.1}
        r = water.calculate_recipe(spec)
        # 回退偏好方案（CaCl2 + MgSO4）下成品 Cl 仍为 79.61 mg/L
        self.assertAlmostEqual(r["final"]["cl"], 79.61, places=1)

    def test_so4_cap_with_uncapped_cl(self):
        """对称情形：只设 SO4 上限时建议同样有限。"""
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 0, "so4_max": 10, "resolution_ml": 0.1}
        r = water.calculate_recipe(spec)
        self.assertFalse(r["feasible"])
        c = next(c for c in r["conflicts"] if c["code"] == "anion_cap_exceeded")
        self.assertIn("Cl⁻ 未设上限", c["message"])
        self.assertIn("启用 MgCl₂·6H₂O", c["adjust"])
        self.assertNotIn("e+", c["adjust"].lower())
        self.assertEqual(set(c["limits"].keys()), {"so4_min_mg_l"})

    def test_both_caps_set_still_reports_finite_values(self):
        """两侧都设限（回归保护）：建议仍是有限数值。"""
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 20, "so4_max": 40, "resolution_ml": 0.1}
        r = water.calculate_recipe(spec)
        c = next(c for c in r["conflicts"] if c["code"] == "anion_cap_exceeded")
        self.assertIn("80", c["adjust"])
        self.assertNotIn("e+", c["adjust"].lower())
        for v in c["limits"].values():
            self.assertTrue(math.isfinite(v))
            self.assertLess(abs(v), 1e6)

    def test_feasible_when_cap_is_high_enough(self):
        """Cl 79.6 < 100 且 SO4 31.6 < 150：不应有上限冲突。"""
        r = water.calculate_recipe(CLASSIC_SPEC)
        self.assertFalse(
            [c for c in r["conflicts"] if c["code"] == "anion_cap_exceeded"])

    def test_result_is_json_serializable(self):
        spec = copy.deepcopy(CLASSIC_SPEC)
        spec["limits"] = {"cl_max": 10, "so4_max": 0, "resolution_ml": 0.1}
        r = water.calculate_recipe(spec)
        json.dumps(r, ensure_ascii=False)  # 不抛异常即可
        scaled = water.scale_volume(r, 10, spec=spec)
        json.dumps(scaled, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
