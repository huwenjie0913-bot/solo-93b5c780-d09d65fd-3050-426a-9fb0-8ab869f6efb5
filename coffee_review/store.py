"""SQLite 持久层：冲煮记录的增删改查与方案版本管理。

仅使用 Python 标准库。一个 brew 即一次冲煮（也是方案的一个版本），
同组（group_id）的若干 brew 构成同一方案的不同版本。
"""

import json
import os
import sqlite3
import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coffee.db")

FLAVOR_TAGS = ("sour", "astringent", "bitter", "weak", "muddled", "off")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                brew_date TEXT NOT NULL,
                dose REAL NOT NULL,
                water REAL NOT NULL,
                temp REAL NOT NULL,
                grind REAL NOT NULL,
                target_ratio REAL NOT NULL,
                flavors TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                nodes TEXT NOT NULL,
                is_sample INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        if conn.execute("SELECT COUNT(*) AS c FROM brews").fetchone()["c"] == 0:
            seed_samples(conn)


def _row_to_dict(row):
    d = dict(row)
    d["nodes"] = json.loads(d["nodes"])
    d["flavors"] = json.loads(d["flavors"])
    d["is_sample"] = bool(d["is_sample"])
    return d


LIST_FIELDS = (
    "id", "group_id", "version", "name", "brew_date",
    "dose", "water", "temp", "grind", "target_ratio",
    "flavors", "is_sample", "created_at",
)


def list_brews():
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(LIST_FIELDS)} FROM brews ORDER BY brew_date DESC, id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flavors"] = json.loads(d["flavors"])
            d["is_sample"] = bool(d["is_sample"])
            out.append(d)
        return out


def get_brew(brew_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM brews WHERE id = ?", (brew_id,)).fetchone()
        return _row_to_dict(row) if row else None


def _max_group(conn):
    row = conn.execute("SELECT COALESCE(MAX(group_id), 0) AS m FROM brews").fetchone()
    return row["m"]


def _max_version(conn, group_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM brews WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    return row["m"]


def create_brew(data):
    """新建一次冲煮。group_id 缺省取新组；version 自动递增。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        group_id = data.get("group_id")
        # 只有引用已存在的方案组时才在该组内递增版本，否则另起新组
        if group_id and conn.execute(
            "SELECT 1 FROM brews WHERE group_id = ? LIMIT 1", (group_id,)
        ).fetchone():
            version = _max_version(conn, group_id) + 1
        else:
            group_id = _max_group(conn) + 1
            version = 1
        cur = conn.execute(
            """INSERT INTO brews
               (group_id, version, name, brew_date, dose, water, temp, grind,
                target_ratio, flavors, notes, nodes, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(group_id),
                int(version),
                data.get("name") or "未命名方案",
                data.get("brew_date") or datetime.date.today().isoformat(),
                float(data.get("dose", 15)),
                float(data.get("water", 0)),
                float(data.get("temp", 92)),
                float(data.get("grind", 20)),
                float(data.get("target_ratio", 15)),
                json.dumps(_clean_flavors(data.get("flavors", [])), ensure_ascii=False),
                data.get("notes", "") or "",
                json.dumps(data.get("nodes", []), ensure_ascii=False),
                1 if data.get("is_sample") else 0,
                now,
            ),
        )
        conn.commit()
        return get_brew(cur.lastrowid)


def update_brew(brew_id, data):
    brew = get_brew(brew_id)
    if brew is None:
        return None
    fields = ("name", "brew_date", "dose", "water", "temp", "grind", "target_ratio", "notes")
    sets, params = [], []
    for f in fields:
        if f in data:
            val = data[f]
            if f in ("dose", "water", "temp", "grind", "target_ratio"):
                val = float(val)
            sets.append(f"{f} = ?")
            params.append(val)
    if "flavors" in data:
        sets.append("flavors = ?")
        params.append(json.dumps(_clean_flavors(data["flavors"]), ensure_ascii=False))
    if "nodes" in data:
        sets.append("nodes = ?")
        params.append(json.dumps(data["nodes"], ensure_ascii=False))
    if not sets:
        return brew
    params.append(brew_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE brews SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    return get_brew(brew_id)


def delete_brew(brew_id):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM brews WHERE id = ?", (brew_id,))
        conn.commit()
        return cur.rowcount > 0


def _clean_flavors(flavors):
    if not isinstance(flavors, list):
        return []
    return [f for f in flavors if f in FLAVOR_TAGS]


def reset_samples():
    """删除现有示例并重新播种，返回示例列表。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM brews WHERE is_sample = 1")
        seed_samples(conn)
        conn.commit()
    return [b for b in list_brews() if b["is_sample"]]


# ---------------------------------------------------------------- 示例数据

def _sample_common(date):
    return {"brew_date": date, "is_sample": True}


def seed_samples(conn):
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=2)).isoformat()
    d2 = (today - datetime.timedelta(days=1)).isoformat()

    # 示例 A：节奏平稳的一杯，仅有末段水流偏小的轻度提示。
    a_nodes = [
        {"t": 0, "w": 0},
        {"t": 20, "w": 36},    # 闷蒸 36g / 20s
        {"t": 32, "w": 36},    # 闷蒸停顿 12s
        {"t": 67, "w": 125},   # 第二段 89g / 35s ≈ 2.5 g/s
        {"t": 77, "w": 125},   # 停顿 10s
        {"t": 102, "w": 185},  # 第三段 60g / 25s = 2.4 g/s
        {"t": 107, "w": 185},  # 停顿 5s
        {"t": 142, "w": 225},  # 尾段 40g / 35s ≈ 1.1 g/s（略慢，轻度过萃）
    ]
    sample_a = {
        **_sample_common(d1),
        "name": "耶加雪菲 V1（示例·平稳）",
        "dose": 15, "water": 225, "temp": 92, "grind": 22, "target_ratio": 15,
        "flavors": ["astringent"],
        "notes": (
            "现场备注：整体干净，柑橘和蜂蜜明显，尾段略有干涩感。"
            "尾段注水流速偏慢（约1.4g/s），怀疑过萃，下一次研磨调粗半格试试。"
        ),
        "nodes": a_nodes,
    }

    # 示例 B：闷蒸不足 + 注水突增 + 断流，层次混乱、偏酸。
    b_nodes = [
        {"t": 0, "w": 0},
        {"t": 8, "w": 20},      # 闷蒸只有 20g / 8s
        {"t": 16, "w": 20},     # 停顿也很短
        {"t": 26, "w": 105},    # 10 秒灌 85g = 8.5 g/s（突增）
        {"t": 60, "w": 105},    # 断流 34s
        {"t": 90, "w": 165},    # 60g / 30s = 2.0 g/s
        {"t": 103, "w": 196},   # 31g / 13s ≈ 2.4 g/s
    ]
    sample_b = {
        **_sample_common(d2),
        "name": "耶加雪菲 V2（示例·问题杯）",
        "dose": 15, "water": 196, "temp": 88, "grind": 25, "target_ratio": 15,
        "flavors": ["sour", "muddled"],
        "notes": (
            "现场备注：开头手抖水注大了，中间等滤杯等太久。喝起来尖酸、风味混在一起，"
            "body 偏薄。水温也比平时低 2℃。"
        ),
        "nodes": b_nodes,
    }

    group_id = _max_group(conn) + 1
    for version, sample in enumerate((sample_a, sample_b), start=1):
        conn.execute(
            """INSERT INTO brews
               (group_id, version, name, brew_date, dose, water, temp, grind,
                target_ratio, flavors, notes, nodes, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                group_id,
                version,
                sample["name"],
                sample["brew_date"],
                sample["dose"],
                sample["water"],
                sample["temp"],
                sample["grind"],
                sample["target_ratio"],
                json.dumps(sample["flavors"], ensure_ascii=False),
                sample["notes"],
                json.dumps(sample["nodes"], ensure_ascii=False),
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
