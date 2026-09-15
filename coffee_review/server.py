"""浏览器复盘台 HTTP 服务（仅依赖 Python 3 标准库）。

启动：python3 server.py [--port 8000] [--host 127.0.0.1]
- 静态文件：static/ 目录
- JSON API：/api/...
"""

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import store
import diagnostics
import water

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class Handler(BaseHTTPRequestHandler):
    server_version = "CoffeeReview/1.0"

    # ------------------------------------------------------------ 基础工具

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _static(self, rel_path):
        rel_path = rel_path.lstrip("/")
        if not rel_path:
            rel_path = "index.html"
        # 防目录穿越
        target = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not target.startswith(STATIC_DIR + os.sep) or not os.path.isfile(target):
            self._send_error(404, "文件不存在")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_error(404, "文件读取失败")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if rel_path.endswith(".html"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 路由

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/brews":
                self._send_json({"brews": store.list_brews()})
            elif path == "/api/water/meta":
                self._send_json(water.meta())
            elif path == "/api/water/recipes":
                self._send_json({"recipes": store.list_water_recipes()})
            elif path.startswith("/api/water/recipes/"):
                self._handle_water_recipe_get(path)
            elif path == "/api/thresholds":
                self._send_json({"thresholds": diagnostics.THRESHOLDS,
                                 "flavor_labels": diagnostics.FLAVOR_LABELS,
                                 "variable_labels": diagnostics.VARIABLE_LABELS})
            elif path.startswith("/api/brews/"):
                self._handle_brew_get(path)
            elif path.startswith("/api/"):
                self._send_error(404, "未知接口")
            else:
                self._static(path)
        except Exception as exc:  # noqa: BLE001 - 统一兜底
            self._send_error(500, f"服务器错误：{exc}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            data = self._read_json()
            if path == "/api/brews":
                errors = _validate(data)
                if errors:
                    self._send_error(400, "；".join(errors))
                    return
                brew = store.create_brew(data)
                self._send_json({"brew": brew}, 201)
            elif path == "/api/water/calculate":
                params = data.get("params")
                if not isinstance(params, dict):
                    self._send_error(400, "缺少 params（配方参数）对象")
                    return
                self._send_json({"result": water.calculate_recipe(params)})
            elif path == "/api/water/scale":
                result = data.get("result")
                try:
                    new_volume = float(data.get("volume_ml"))
                except (TypeError, ValueError):
                    self._send_error(400, "缺少合法的 volume_ml")
                    return
                if not isinstance(result, dict):
                    self._send_error(400, "缺少 result（已算好的配方结果）对象")
                    return
                self._send_json({"result": water.scale_volume(result, new_volume)})
            elif path == "/api/water/recipes":
                errors = _validate_recipe(data)
                if errors:
                    self._send_error(400, "；".join(errors))
                    return
                recipe = store.create_water_recipe(data)
                self._send_json({"recipe": recipe}, 201)
            elif path == "/api/samples/reset":
                brews = store.reset_samples()
                self._send_json({"brews": brews})
            elif path == "/api/diagnose":
                brew = data.get("brew")
                if not isinstance(brew, dict):
                    self._send_error(400, "缺少 brew 对象")
                    return
                self._send_json({"diagnosis": diagnostics.analyze(brew)})
            elif path == "/api/suggest":
                anchor = data.get("anchor")
                if not isinstance(anchor, dict):
                    self._send_error(400, "缺少 anchor（当前冲煮）对象")
                    return
                baseline = data.get("baseline")
                locked = data.get("locked") or []
                if not isinstance(locked, list):
                    locked = []
                self._send_json(
                    diagnostics.suggest(anchor,
                                        baseline if isinstance(baseline, dict) else None,
                                        locked))
            else:
                self._send_error(404, "未知接口")
        except ValueError as exc:
            self._send_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error(500, f"服务器错误：{exc}")

    def do_PUT(self):
        parsed = urlparse(self.path)
        try:
            data = self._read_json()
            recipe_id = _water_recipe_id_from_path(parsed.path)
            if recipe_id is not None:
                if store.get_water_recipe(recipe_id) is None:
                    self._send_error(404, "水配方不存在")
                    return
                errors = _validate_recipe(data, partial=True)
                if errors:
                    self._send_error(400, "；".join(errors))
                    return
                recipe = store.update_water_recipe(recipe_id, data)
                self._send_json({"recipe": recipe})
                return
            brew_id = _brew_id_from_path(parsed.path)
            if brew_id is None:
                self._send_error(404, "未知接口")
                return
            if store.get_brew(brew_id) is None:
                self._send_error(404, "冲煮记录不存在")
                return
            errors = _validate(data, partial=True)
            if errors:
                self._send_error(400, "；".join(errors))
                return
            brew = store.update_brew(brew_id, data)
            self._send_json({"brew": brew})
        except ValueError as exc:
            self._send_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error(500, f"服务器错误：{exc}")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        recipe_id = _water_recipe_id_from_path(parsed.path)
        if recipe_id is not None:
            if store.delete_water_recipe(recipe_id):
                self._send_json({"ok": True})
            else:
                self._send_error(404, "水配方不存在")
            return
        brew_id = _brew_id_from_path(parsed.path)
        if brew_id is None:
            self._send_error(404, "未知接口")
            return
        if store.delete_brew(brew_id):
            self._send_json({"ok": True})
        else:
            self._send_error(404, "冲煮记录不存在")

    def _handle_brew_get(self, path):
        brew_id = _brew_id_from_path(path)
        if brew_id is None:
            self._send_error(404, "未知接口")
            return
        brew = store.get_brew(brew_id)
        if brew is None:
            self._send_error(404, "冲煮记录不存在")
            return
        qs = parse_qs(urlparse(self.path).query)
        payload = {"brew": brew}
        if qs.get("diag"):
            payload["diagnosis"] = diagnostics.analyze(brew)
        self._send_json(payload)

    def _handle_water_recipe_get(self, path):
        recipe_id = _water_recipe_id_from_path(path)
        if recipe_id is None:
            self._send_error(404, "未知接口")
            return
        recipe = store.get_water_recipe(recipe_id)
        if recipe is None:
            self._send_error(404, "水配方不存在")
            return
        self._send_json({"recipe": recipe})

    def log_message(self, fmt, *args):
        # 简洁日志
        print("%s - %s" % (self.address_string(), fmt % args))


def _brew_id_from_path(path):
    parts = [p for p in path.split("/") if p]
    # /api/brews/<id> 或 /api/brews/<id>/...
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "brews":
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


def _water_recipe_id_from_path(path):
    parts = [p for p in path.split("/") if p]
    # /api/water/recipes/<id>
    if len(parts) >= 4 and parts[:3] == ["api", "water", "recipes"]:
        try:
            return int(parts[3])
        except ValueError:
            return None
    return None


def _validate_recipe(data, partial=False):
    errors = []
    if not partial and not data.get("name"):
        errors.append("缺少水配方名称")
    spec = data.get("spec")
    if spec is not None:
        if not isinstance(spec, dict):
            errors.append("spec 必须是对象")
        else:
            try:
                vol = float(spec.get("volume_ml", 0))
                if vol <= 0:
                    errors.append("成品体积必须大于 0")
            except (TypeError, ValueError):
                errors.append("成品体积必须是数字")
            for section in ("source", "target"):
                if section in spec and not isinstance(spec[section], dict):
                    errors.append(f"{section} 必须是对象")
    if "result" in data and not isinstance(data["result"], dict):
        errors.append("result 必须是对象")
    return errors


def _validate(data, partial=False):
    errors = []
    numeric = ("dose", "water", "temp", "grind", "target_ratio")
    for f in numeric:
        if f in data and data[f] not in (None, ""):
            try:
                v = float(data[f])
                if v < 0:
                    errors.append(f"{f} 不能为负数")
            except (TypeError, ValueError):
                errors.append(f"{f} 必须是数字")
    if "nodes" in data:
        nodes = data["nodes"]
        if not isinstance(nodes, list) or len(nodes) < 2:
            errors.append("至少需要 2 个注水节点")
        else:
            for n in nodes:
                if not isinstance(n, dict) or not isinstance(n.get("t"), (int, float)) \
                        or not isinstance(n.get("w"), (int, float)):
                    errors.append("节点必须包含数字 t（秒）与 w（克）")
                    break
    if "flavors" in data and not isinstance(data["flavors"], list):
        errors.append("flavors 必须是数组")
    if not partial:
        if not data.get("name"):
            errors.append("缺少方案名称")
    return errors


def main():
    ap = argparse.ArgumentParser(description="手冲咖啡复盘台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    store.init_db()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"咖啡复盘台已启动：http://{args.host}:{args.port}/")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
