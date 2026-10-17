"""
db_manager.py — PBL4 SAGSINs ACO Persistence Layer
===================================================
Hệ thống lưu trữ cơ sở dữ liệu SQLite bền vững:
  1. schema khởi tạo tự động (data/sagsins.db)
  2. Lưu trữ lịch sử tìm đường (routing_history)
  3. Lưu trữ kết quả đối chiếu ACO vs Dijkstra (comparison_history)
  4. Lưu trữ trạng thái mô phỏng đa luồng (multiflow_history)
  5. Lưu trữ nhật ký sự cố & chuyển giao quỹ đạo (events_log)
  6. Xuất báo cáo dữ liệu định dạng JSON / CSV phục vụ thuyết minh đồ án

Tác giả: PBL4 Team
Năm   : 2026
"""

from __future__ import annotations

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logger = logging.getLogger("db_manager")

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "sagsins.db")


def get_connection() -> sqlite3.Connection:
    """Tạo kết nối tới SQLite database với row_factory tiện lợi."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Khởi tạo bảng cơ sở dữ liệu nếu chưa tồn tại."""
    conn = get_connection()
    try:
        with conn:
            # 1. Bảng lịch sử định tuyến đơn tuyến (ACO Routing)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routing_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    n_ants INTEGER,
                    n_iterations INTEGER,
                    path_hops INTEGER,
                    best_cost_ms REAL,
                    prop_delay_ms REAL,
                    queue_delay_ms REAL,
                    rtt_ms REAL,
                    convergence_iter INTEGER,
                    time_taken_s REAL,
                    path_json TEXT,
                    avoided_congested_json TEXT
                )
            """)

            # 2. Bảng đối chiếu thuật toán (ACO vs Dijkstra)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS comparison_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    n_ants INTEGER,
                    n_iterations INTEGER,
                    aco_cost_ms REAL,
                    aco_hops INTEGER,
                    aco_path_json TEXT,
                    dijkstra_cost_ms REAL,
                    dijkstra_hops INTEGER,
                    dijkstra_path_json TEXT,
                    cost_diff_pct REAL,
                    aco_quality_pct REAL,
                    verdict TEXT
                )
            """)

            # 3. Bảng lịch sử phân luồng giờ cao điểm (Multi-Flow Peak Load)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS multiflow_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_flows INTEGER,
                    flows_json TEXT,
                    summary_text TEXT
                )
            """)

            # 4. Bảng nhật ký sự cố & chuyển giao quỹ đạo (Network Events & Handover)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS network_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,  -- 'HANDOVER', 'NODE_FAILED', 'NODE_RESTORED', 'CONGESTION'
                    node_id TEXT,
                    details_json TEXT
                )
            """)

            # 5. Bảng lưu trữ bền vững tải lưu lượng & tham số tắc nghẽn của từng nút mạng
            conn.execute("""
                CREATE TABLE IF NOT EXISTS node_congestion (
                    node_id TEXT PRIMARY KEY,
                    load REAL NOT NULL,
                    active_conn INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        logger.info(f"Database initialized successfully at '{DB_PATH}'")
    except Exception as e:
        logger.error(f"Lỗi khởi tạo database: {e}")
    finally:
        conn.close()


# ─── API LƯU DỮ LIỆU (WRITE) ────────────────────────────────────────────────

def save_routing_record(
    source: str,
    destination: str,
    n_ants: int,
    n_iterations: int,
    path: List[str],
    cost_ms: float,
    prop_delay_ms: float,
    queue_delay_ms: float,
    rtt_ms: float,
    convergence_iter: int,
    time_taken_s: float,
    avoided_congested: List[str]
) -> int:
    """Lưu một phiên chạy ACO vào CSDL."""
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("""
                INSERT INTO routing_history (
                    source, destination, n_ants, n_iterations,
                    path_hops, best_cost_ms, prop_delay_ms, queue_delay_ms,
                    rtt_ms, convergence_iter, time_taken_s,
                    path_json, avoided_congested_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source, destination, n_ants, n_iterations,
                len(path) - 1 if path else 0,
                cost_ms, prop_delay_ms, queue_delay_ms,
                rtt_ms, convergence_iter, time_taken_s,
                json.dumps(path), json.dumps(avoided_congested)
            ))
            return cursor.lastrowid
    finally:
        conn.close()


def save_comparison_record(
    source: str,
    destination: str,
    n_ants: int,
    n_iterations: int,
    aco_cost_ms: float,
    aco_hops: int,
    aco_path: List[str],
    dijkstra_cost_ms: float,
    dijkstra_hops: int,
    dijkstra_path: List[str],
    cost_diff_pct: float,
    aco_quality_pct: float,
    verdict: str
) -> int:
    """Lưu một phiên đối chiếu ACO vs Dijkstra vào CSDL."""
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("""
                INSERT INTO comparison_history (
                    source, destination, n_ants, n_iterations,
                    aco_cost_ms, aco_hops, aco_path_json,
                    dijkstra_cost_ms, dijkstra_hops, dijkstra_path_json,
                    cost_diff_pct, aco_quality_pct, verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source, destination, n_ants, n_iterations,
                aco_cost_ms, aco_hops, json.dumps(aco_path),
                dijkstra_cost_ms, dijkstra_hops, json.dumps(dijkstra_path),
                cost_diff_pct, aco_quality_pct, verdict
            ))
            return cursor.lastrowid
    finally:
        conn.close()


def save_multiflow_record(total_flows: int, flows: List[Dict], summary: str = "") -> int:
    """Lưu kết quả chạy giờ cao điểm 5 luồng."""
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("""
                INSERT INTO multiflow_history (total_flows, flows_json, summary_text)
                VALUES (?, ?, ?)
            """, (total_flows, json.dumps(flows), summary))
            return cursor.lastrowid
    finally:
        conn.close()


def log_network_event(event_type: str, node_id: Optional[str] = None, details: Optional[Dict] = None) -> int:
    """Ghi nhận sự cố, chuyển giao vệ tinh hoặc nghẽn mạng vào bảng nhật ký."""
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute("""
                INSERT INTO network_events (event_type, node_id, details_json)
                VALUES (?, ?, ?)
            """, (event_type, node_id, json.dumps(details or {})))
            return cursor.lastrowid
    finally:
        conn.close()


def save_node_load(node_id: str, load: float, active_conn: Optional[int] = None) -> None:
    """Lưu trữ bền vững giá trị tải lưu lượng của một nút mạng vào SQLite."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO node_congestion (node_id, load, active_conn, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(node_id) DO UPDATE SET
                    load = excluded.load,
                    active_conn = excluded.active_conn,
                    updated_at = CURRENT_TIMESTAMP
            """, (node_id, float(load), active_conn))
    except Exception as e:
        logger.error(f"Lỗi lưu tải nút '{node_id}' vào SQLite: {e}")
    finally:
        conn.close()


def get_all_node_loads() -> Dict[str, Dict[str, Any]]:
    """Truy xuất toàn bộ tải lưu lượng các nút đã lưu từ SQLite."""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT node_id, load, active_conn FROM node_congestion")
        rows = cursor.fetchall()
        result = {}
        for r in rows:
            result[r["node_id"]] = {
                "load": float(r["load"]),
                "active_conn": r["active_conn"],
                "is_congested": float(r["load"]) >= 0.75,
            }
        return result
    except Exception as e:
        logger.error(f"Lỗi đọc node_congestion từ SQLite: {e}")
        return {}
    finally:
        conn.close()


def reset_node_loads() -> None:
    """Xóa trắng cấu hình tải lưu lượng tùy chỉnh để đưa về mặc định."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM node_congestion")
    except Exception as e:
        logger.error(f"Lỗi reset node_congestion trong SQLite: {e}")
    finally:
        conn.close()


# ─── API TRUY VẤN DỮ LIỆU (READ) ───────────────────────────────────────────

def get_recent_routing_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Lấy danh sách các lần tìm đường ACO gần nhất."""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT * FROM routing_history
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["path"] = json.loads(d.pop("path_json") or "[]")
            d["avoided_congested"] = json.loads(d.pop("avoided_congested_json") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def get_recent_comparisons(limit: int = 50) -> List[Dict[str, Any]]:
    """Lấy danh sách các lần so sánh ACO vs Dijkstra gần nhất."""
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT * FROM comparison_history
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["aco_path"] = json.loads(d.pop("aco_path_json") or "[]")
            d["dijkstra_path"] = json.loads(d.pop("dijkstra_path_json") or "[]")
            result.append(d)
        return result
    finally:
        conn.close()


def get_recent_events(limit: int = 100, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lấy lịch sử sự cố và chuyển giao quỹ đạo."""
    conn = get_connection()
    try:
        if event_type:
            cursor = conn.execute("""
                SELECT * FROM network_events
                WHERE event_type = ?
                ORDER BY id DESC LIMIT ?
            """, (event_type, limit))
        else:
            cursor = conn.execute("""
                SELECT * FROM network_events
                ORDER BY id DESC LIMIT ?
            """, (limit,))
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d.pop("details_json") or "{}")
            result.append(d)
        return result
    finally:
        conn.close()


def get_database_stats() -> Dict[str, Any]:
    """Tổng hợp số liệu thống kê trong cơ sở dữ liệu."""
    conn = get_connection()
    try:
        n_routing = conn.execute("SELECT COUNT(*) FROM routing_history").fetchone()[0]
        n_compare = conn.execute("SELECT COUNT(*) FROM comparison_history").fetchone()[0]
        n_events  = conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0]
        avg_cost  = conn.execute("SELECT AVG(best_cost_ms) FROM routing_history").fetchone()[0] or 0.0
        avg_qual  = conn.execute("SELECT AVG(aco_quality_pct) FROM comparison_history").fetchone()[0] or 100.0

        return {
            "total_routing_sessions": n_routing,
            "total_comparisons"     : n_compare,
            "total_network_events"  : n_events,
            "avg_routing_cost_ms"   : round(avg_cost, 2),
            "avg_aco_quality_pct"   : round(avg_qual, 2),
            "db_path"               : DB_PATH,
            "db_size_kb"            : round(os.path.getsize(DB_PATH) / 1024.0, 2) if os.path.exists(DB_PATH) else 0.0
        }
    finally:
        conn.close()
