"""
web_api.py — PBL4 SAGSINs ACO
================================
Nhiệm vụ:
  Server mạng cung cấp 2 loại endpoint:
  1. REST API  : GET /routing?source=X&dest=Y → trả đường đi ngay
  2. WebSocket : WS /ws → push real-time ACO progress lên frontend

Pattern được dùng (FastAPI official docs):
  - ConnectionManager: quản lý danh sách WebSocket clients
  - BackgroundTasks  : chạy ACO không block HTTP response
  - asyncio + threading: bridge giữa async FastAPI và sync ACO

JSON broadcast format:
  {
    "type"           : "ACO_UPDATE",
    "iteration"      : 42,
    "path"           : ["GND_Ha_Noi", "SAT_9", "SHIP_1"],
    "path_coords"    : [{"lat": 21.0, "lon": 105.8, "alt_km": 0, "node": "GND_Ha_Noi"}, ...],
    "cost_ms"        : 51.36,
    "pheromone_level": 0.73,
    "rtt_ms"         : 102.72,
    "converged"      : false
  }

Chạy server:
  uvicorn backend.web_api:app --reload --port 8000

Tác giả : PBL4 Team
Ngày    : 2026
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from satellite_tracker import SatelliteTracker, distance_ecef_km, propagation_delay_ms
from topology_manager  import TopologyManager
from aco_algorithm     import ACOAlgorithm, AntPath, ACOResult
from thread_manager    import ThreadManager, RoutingTask, RoutingResult
import db_manager as db

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("web_api")
logging.getLogger("satellite_tracker").setLevel(logging.WARNING)
logging.getLogger("topology_manager").setLevel(logging.WARNING)
logging.getLogger("aco_algorithm").setLevel(logging.WARNING)
logging.getLogger("thread_manager").setLevel(logging.WARNING)

# ─── Global state (khởi tạo khi server start) ───────────────────────────────
_tracker  : Optional[SatelliteTracker] = None
_topo     : Optional[TopologyManager]  = None
_graph    : Optional[nx.Graph]         = None
_node_list: List[str]                  = []
_failed_nodes     : set[str]                                  = set()
_saved_node_attrs : Dict[str, Dict[str, Any]]                 = {}
_saved_node_edges : Dict[str, List[Tuple[str, str, Dict]]]    = {}

# ─── Quỹ đạo chuyển động vệ tinh (Dynamic Orbit Simulation) ───────────────────
_sim_start_time         : datetime               = datetime.now(timezone.utc)
_orbit_time_offset      : float                  = 0.0
_orbit_speed_multiplier : float                  = 15.0   # Tốc độ mô phỏng mặc định 15x
_orbit_running          : bool                   = True
_orbit_task             : Optional[asyncio.Task] = None

# ─── Tuyến đường chủ động & Tự động thích nghi quỹ đạo (Dynamic Handover) ────
_active_route_src       : Optional[str]          = None
_active_route_dst       : Optional[str]          = None
_active_route_path      : List[str]              = []
_active_route_cost      : float                  = 0.0
_active_route_prop      : float                  = 0.0
_active_route_queue     : float                  = 0.0

TERRESTRIAL_NODES = ["GND_Da_Nang", "GND_Ha_Noi", "GND_Bien_Dong", "UAV_1", "SHIP_1", "SHIP_2"]


# ─── WebSocket ConnectionManager ─────────────────────────────────────────────

class ConnectionManager:
    """
    Quản lý tất cả WebSocket connections đang active.

    Pattern chuẩn từ FastAPI official docs:
      connect() / disconnect() / broadcast()
    """

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Chấp nhận kết nối mới và thêm vào danh sách."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info(f"WS client connected | total={len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Xóa kết nối khỏi danh sách khi client ngắt."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(f"WS client disconnected | total={len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Gửi JSON message đến tất cả client đang kết nối."""
        if not self.active_connections:
            return
        data = json.dumps(message, ensure_ascii=False)
        dead = []
        async with self._lock:
            connections = list(self.active_connections)
        for ws in connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        # Xóa các connection đã chết
        for ws in dead:
            await self.disconnect(ws)

    async def send_personal(self, websocket: WebSocket, message: Dict) -> None:
        """Gửi message đến 1 client cụ thể."""
        try:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            await self.disconnect(websocket)

    @property
    def n_clients(self) -> int:
        return len(self.active_connections)


# ─── Khởi tạo ConnectionManager ──────────────────────────────────────────────
manager = ConnectionManager()

# ─── Event loop reference (để thread ACO broadcast qua WS) ───────────────────
_main_loop: Optional[asyncio.AbstractEventLoop] = None


# ─── Hàm hỗ trợ ─────────────────────────────────────────────────────────────

def _get_node_coords(node_id: str) -> Dict:
    """Lấy lat/lon/alt của một node từ graph, cache đã lưu hoặc topo."""
    if _graph is not None and node_id in _graph.nodes:
        data = _graph.nodes[node_id]
        return {
            "lat"   : data.get("lat_deg", 0.0),
            "lon"   : data.get("lon_deg", 0.0),
            "alt_km": data.get("alt_km",  0.0),
            "type"  : data.get("node_type", "unknown"),
        }
    if node_id in _saved_node_attrs:
        data = _saved_node_attrs[node_id]
        return {
            "lat"   : data.get("lat_deg", 0.0),
            "lon"   : data.get("lon_deg", 0.0),
            "alt_km": data.get("alt_km",  0.0),
            "type"  : data.get("node_type", "unknown"),
        }
    if _topo and node_id in _topo._nodes:
        nd = _topo._nodes[node_id]
        return {
            "lat"   : nd.lat,
            "lon"   : nd.lon,
            "alt_km": nd.alt_km,
            "type"  : nd.node_type.value if hasattr(nd.node_type, "value") else str(nd.node_type),
        }
    return {"lat": 0.0, "lon": 0.0, "alt_km": 0.0, "type": "unknown"}


def _build_path_coords(path: List[str]) -> List[Dict]:
    """Chuyển path (tên node) → list tọa độ cho Leaflet."""
    result = []
    for node in path:
        coords = _get_node_coords(node)
        coords["node"] = node
        result.append(coords)
    return result


def _broadcast_sync(message: Dict) -> None:
    """
    Gọi broadcast từ sync thread (ACO callback).
    Bridge giữa sync ACO và async WebSocket.
    """
    if _main_loop and not _main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(message),
            _main_loop
        )


# ─── Quỹ đạo vệ tinh động (Dynamic Orbit Simulation Loop) ───────────────────

async def orbit_simulation_loop():
    """Vòng lặp tính toán SGP4, cập nhật khoảng cách liên kết và phát hiện chuyển giao vệ tinh (Handover) thời gian thực."""
    global _orbit_time_offset, _orbit_speed_multiplier, _orbit_running
    global _active_route_src, _active_route_dst, _active_route_path, _active_route_cost
    global _active_route_prop, _active_route_queue
    logger.info("Orbit simulation background loop started.")
    while True:
        try:
            if manager.n_clients > 0 and _orbit_running and _tracker is not None and _graph is not None:
                _orbit_time_offset += 1.0 * _orbit_speed_multiplier
                target_time = _sim_start_time + timedelta(seconds=_orbit_time_offset)

                infos = _tracker.get_satellite_infos(target_time)
                sat_updates = []
                sat_map = {}
                for i, info in enumerate(infos):
                    if info.alt_km < 100.0 or info.alt_km > 2000.0:
                        continue
                    nid = f"SAT_{i}"
                    sat_map[nid] = info
                    if nid in _graph.nodes:
                        _graph.nodes[nid]["lat_deg"] = info.lat_deg
                        _graph.nodes[nid]["lon_deg"] = info.lon_deg
                        _graph.nodes[nid]["alt_km"]  = info.alt_km
                        _graph.nodes[nid]["ecef_km"] = info.ecef_km
                    sat_updates.append({
                        "id"    : nid,
                        "type"  : "satellite",
                        "lat"   : round(info.lat_deg, 4),
                        "lon"   : round(info.lon_deg, 4),
                        "alt_km": round(info.alt_km, 2),
                        "vel"   : round(info.velocity_km_s, 2),
                    })

                # ── 1. Cập nhật động liên kết GSL (Ground-to-Satellite Links) ──
                for terr_id in TERRESTRIAL_NODES:
                    if terr_id not in _graph.nodes or terr_id in _failed_nodes:
                        continue
                    terr_ecef = _graph.nodes[terr_id].get("ecef_km")
                    if terr_ecef is None:
                        continue

                    for sat_id, sat_info in sat_map.items():
                        if sat_id not in _graph.nodes or sat_id in _failed_nodes:
                            continue
                        dist_km = distance_ecef_km(terr_ecef, sat_info.ecef_km)
                        max_r = 5000.0
                        if dist_km <= max_r:
                            delay = propagation_delay_ms(dist_km)
                            _graph.add_edge(
                                terr_id, sat_id,
                                weight=delay,
                                distance_km=dist_km,
                                pheromone=1.0,
                                heuristic=1.0 / (delay + 1e-9)
                            )
                        else:
                            if _graph.has_edge(terr_id, sat_id):
                                _graph.remove_edge(terr_id, sat_id)

                    # Đảm bảo trạm mặt đất/tàu luôn có liên kết vệ tinh dự phòng nếu nằm ngoài bán kính
                    if _graph.degree(terr_id) < 2:
                        dists = []
                        for sat_id, sat_info in sat_map.items():
                            if sat_id in _graph.nodes and sat_id not in _failed_nodes:
                                d = distance_ecef_km(terr_ecef, sat_info.ecef_km)
                                dists.append((d, sat_id))
                        dists.sort()
                        for d, sat_id in dists[:2]:
                            delay = propagation_delay_ms(d)
                            _graph.add_edge(
                                terr_id, sat_id,
                                weight=delay,
                                distance_km=d,
                                pheromone=1.0,
                                heuristic=1.0 / (delay + 1e-9)
                            )

                # ── 2. Cập nhật động khoảng cách các liên kết liên vệ tinh (ISL) ──
                for u, v, ddata in list(_graph.edges(data=True)):
                    if u in TERRESTRIAL_NODES or v in TERRESTRIAL_NODES:
                        continue
                    if u in _graph.nodes and v in _graph.nodes:
                        pos_u = _graph.nodes[u].get("ecef_km")
                        pos_v = _graph.nodes[v].get("ecef_km")
                        if pos_u is not None and pos_v is not None:
                            d = distance_ecef_km(pos_u, pos_v)
                            delay = propagation_delay_ms(d)
                            ddata["distance_km"] = d
                            ddata["weight"] = delay
                            ddata["heuristic"] = 1.0 / (delay + 1e-9)

                # ── 3. Broadcast ORBIT_TICK cho frontend ──
                await manager.broadcast({
                    "type"      : "ORBIT_TICK",
                    "sim_time"  : target_time.strftime("%H:%M:%S UTC"),
                    "speed"     : _orbit_speed_multiplier,
                    "running"   : _orbit_running,
                    "satellites": sat_updates
                })

                # ── 4. Tự động thích nghi & Chuyển giao vệ tinh (Satellite Handover / Dynamic Tracking) ──
                if _active_route_src and _active_route_dst:
                    if (_active_route_src in _graph.nodes and _active_route_dst in _graph.nodes and
                        _active_route_src not in _failed_nodes and _active_route_dst not in _failed_nodes):

                        def _calc_edge_cost(u, v, data):
                            p_del = data.get("weight", 0.0)
                            ld = min(0.99, max(0.0, float(_graph.nodes[v].get("load", 0.10))))
                            q_del = (ld / (1.01 - ld)) ** 1.8 * 5.0
                            # [DYNAMIC PROACTIVE HANDOVER PENALTY]
                            # Nếu một vệ tinh trên liên kết GSL đang lặn dần về đường chân trời (< 15°),
                            # tăng dần penalty để thuật toán chủ động chuyển giao trước khi đứt kết nối
                            penalty = 0.0
                            for node_check, terr_check in [(u, v), (v, u)]:
                                if node_check.startswith("SAT_") and terr_check in TERRESTRIAL_NODES:
                                    try:
                                        sat_num = int(node_check.split("_")[1])
                                        gnd_data = _graph.nodes[terr_check]
                                        el = _tracker.compute_elevation_deg(
                                            sat_num, gnd_data.get("lat_deg", 0.0), gnd_data.get("lon_deg", 0.0),
                                            timestamp=target_time
                                        )
                                        if el < 15.0:
                                            # Càng sát đường chân trời càng tăng penalty (tránh đứt kết nối đột ngột)
                                            penalty += max(0.0, (15.0 - el) * 15.0)
                                    except Exception:
                                        pass
                            return p_del + q_del + penalty

                        try:
                            new_path = nx.shortest_path(_graph, _active_route_src, _active_route_dst, weight=_calc_edge_cost)

                            # Tính chi phí trễ chi tiết
                            new_cost = 0.0
                            new_prop = 0.0
                            new_queue = 0.0
                            for k in range(len(new_path) - 1):
                                u_node = new_path[k]
                                v_node = new_path[k + 1]
                                edge_d = _graph[u_node][v_node]
                                p_del = edge_d.get("weight", 0.0)
                                q_del = 0.0
                                if k + 1 < len(new_path) - 1:
                                    ld = min(0.99, max(0.0, float(_graph.nodes[v_node].get("load", 0.10))))
                                    q_del = (ld / (1.01 - ld)) ** 1.8 * 5.0
                                new_prop += p_del
                                new_queue += q_del
                                new_cost += (p_del + q_del)

                            path_changed = (new_path != _active_route_path)
                            cost_diff = abs(new_cost - _active_route_cost)

                            # Phát hiện vệ tinh trên tuyến đường hiện tại có đang sắp lặn hay không
                            handover_alert = None
                            for p_node in new_path:
                                if p_node.startswith("SAT_"):
                                    try:
                                        sat_num = int(p_node.split("_")[1])
                                        # So với node nguồn nếu là trạm mặt đất/tàu
                                        src_data = _graph.nodes.get(_active_route_src, {})
                                        if src_data:
                                            pred = _tracker.predict_satellite_handover(
                                                sat_num,
                                                src_data.get("lat_deg", 0.0),
                                                src_data.get("lon_deg", 0.0),
                                                start_time=target_time,
                                                min_elevation_deg=10.0,
                                                lookahead_seconds=180
                                            )
                                            if pred.get("status") == "approaching_horizon":
                                                handover_alert = {
                                                    "satellite": p_node,
                                                    "elevation": pred.get("current_elevation"),
                                                    "time_to_horizon_sec": pred.get("time_to_handover_sec"),
                                                    "action": "proactive_handover_ready"
                                                }
                                                break
                                    except Exception:
                                        pass

                            if path_changed or cost_diff >= 0.05:
                                old_p = list(_active_route_path)
                                _active_route_path = new_path
                                _active_route_cost = new_cost
                                _active_route_prop = new_prop
                                _active_route_queue = new_queue

                                await manager.broadcast({
                                    "type"          : "ROUTE_ADAPTED",
                                    "source"        : _active_route_src,
                                    "destination"   : _active_route_dst,
                                    "path"          : new_path,
                                    "path_coords"   : _build_path_coords(new_path),
                                    "cost_ms"       : round(new_cost, 2),
                                    "prop_delay_ms" : round(new_prop, 2),
                                    "queue_delay_ms": round(new_queue, 2),
                                    "rtt_ms"        : round(new_cost * 2, 2),
                                    "path_changed"  : path_changed,
                                    "reason"        : "proactive_handover" if path_changed else "distance_tracking",
                                    "handover_alert": handover_alert,
                                    "old_path"      : old_p,
                                    "sim_time"      : target_time.strftime("%H:%M:%S UTC"),
                                })

                                if path_changed:
                                    db.log_network_event(
                                        event_type="PROACTIVE_HANDOVER",
                                        details={
                                            "source": _active_route_src,
                                            "destination": _active_route_dst,
                                            "old_path": old_p,
                                            "new_path": new_path,
                                            "cost_ms": round(new_cost, 2),
                                            "alert": handover_alert,
                                            "sim_time": target_time.strftime("%H:%M:%S UTC"),
                                        }
                                    )
                        except (nx.NetworkXNoPath, nx.NodeNotFound):
                            pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Lỗi trong orbit_simulation_loop: {e}")
        await asyncio.sleep(1.0)


# ─── Lifespan: Khởi tạo graph khi server start ───────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Chạy khi server khởi động và tắt."""
    global _tracker, _topo, _graph, _node_list, _main_loop, _orbit_task

    _main_loop = asyncio.get_event_loop()

    logger.info("Server starting — initialize database & build topology...")
    db.init_db()
    tle_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "starlink.tle"
    )

    try:
        _tracker = SatelliteTracker(tle_path)
        _topo    = TopologyManager(_tracker, radius_km=5_000.0)
        _topo.build_from_tracker()

        # Thêm node SAGSINs
        _topo.add_ground_station("GND_Da_Nang",   16.0544, 108.2022)
        _topo.add_ground_station("GND_Ha_Noi",    21.0285, 105.8542)
        _topo.add_ground_station("GND_Bien_Dong", 12.0,    114.0)
        _topo.add_ground_station("UAV_1",         14.5,    110.0, alt_km=0.5)
        _topo.add_ground_station("SHIP_1",        11.0,    112.0)
        _topo.add_ground_station("SHIP_2",        9.0,     115.0)

        _graph = _topo.graph

        # Đảm bảo connected
        if not nx.is_connected(_graph):
            comps = list(nx.connected_components(_graph))
            main_node = next(iter(max(comps, key=len)))
            for comp in comps:
                node = next(iter(comp))
                if node != main_node:
                    _graph.add_edge(main_node, node,
                                    weight=50.0, distance_km=15000.0,
                                    pheromone=1.0, heuristic=0.02)

        _node_list = list(_graph.nodes())
        logger.info(
            f"Topology ready: {_graph.number_of_nodes()} nodes, "
            f"{_graph.number_of_edges()} edges | "
            f"connected={nx.is_connected(_graph)}"
        )

        # Phục hồi các cấu hình tải lưu lượng / tắc nghẽn đã lưu bền vững trong CSDL SQLite
        saved_loads = db.get_all_node_loads()
        for nid, ldata in saved_loads.items():
            if nid in _graph.nodes:
                _topo.set_node_load(nid, ldata["load"], ldata.get("active_conn"))
                _graph.nodes[nid]["load"] = ldata["load"]
                if "active_conn" in ldata and ldata["active_conn"] is not None:
                    _graph.nodes[nid]["active_conn"] = ldata["active_conn"]
        if saved_loads:
            logger.info(f"Đã phục hồi trạng thái tải cho {len(saved_loads)} nút từ SQLite DB")

        # Khởi chạy luồng cập nhật chuyển động quỹ đạo vệ tinh SGP4
        _orbit_task = asyncio.create_task(orbit_simulation_loop())

    except Exception as e:
        logger.error(f"Lỗi build topology: {e}")

    yield   # Server chạy ở đây

    logger.info("Server shutting down...")
    if _orbit_task is not None:
        _orbit_task.cancel()


# ─── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "SAGSINs ACO Routing API",
    description = "PBL4 — Adaptive routing for Space-Air-Ground-Sea networks using ACO",
    version     = "1.0.0",
    lifespan    = lifespan,
)

# CORS — cho phép frontend gọi API
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ─── REST Endpoints ───────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
async def root():
    """Thông tin server."""
    return {
        "name"   : "SAGSINs ACO Routing API",
        "version": "1.0.0",
        "status" : "running",
        "nodes"  : len(_node_list),
        "ws_url" : "ws://localhost:8000/ws",
    }


@app.get("/topology", tags=["Topology"])
async def get_topology():
    """
    Trả về toàn bộ topology: nodes + edges cho Leaflet.js vẽ bản đồ.

    Frontend dùng endpoint này để vẽ bản đồ ban đầu.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    nodes = _topo.get_node_positions_for_leaflet() if _topo else []
    for n in nodes:
        n["is_failed"] = (n["id"] in _failed_nodes)

    raw_edges = _topo.get_edges_for_leaflet() if _topo else []
    edges = [e for e in raw_edges if e["from"] not in _failed_nodes and e["to"] not in _failed_nodes]

    return {
        "nodes"       : nodes,
        "edges"       : edges,
        "n_nodes"     : len(nodes),
        "n_edges"     : len(edges),
        "failed_nodes": list(_failed_nodes),
        "connected"   : nx.is_connected(_graph) if len(_graph) > 0 else False,
    }


@app.get("/nodes", tags=["Topology"])
async def get_nodes():
    """Danh sách tất cả node và tọa độ."""
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    nodes = _topo.get_node_positions_for_leaflet() if _topo else []
    for n in nodes:
        n["is_failed"] = (n["id"] in _failed_nodes)
    return {"nodes": nodes, "failed_nodes": list(_failed_nodes)}


@app.post("/topology/orbit/control", tags=["Topology"])
async def control_orbit_simulation(
    running: Optional[bool] = Query(default=None, description="Bật/Tắt chuyển động quỹ đạo"),
    speed  : Optional[float] = Query(default=None, ge=1.0, le=120.0, description="Hệ số tốc độ mô phỏng (1x -> 120x)"),
):
    """Điều khiển tốc độ và trạng thái chuyển động quỹ đạo vệ tinh thời gian thực."""
    global _orbit_running, _orbit_speed_multiplier
    if running is not None:
        _orbit_running = bool(running)
    if speed is not None:
        _orbit_speed_multiplier = float(speed)

    await manager.broadcast({
        "type"   : "ORBIT_CONFIG",
        "running": _orbit_running,
        "speed"  : _orbit_speed_multiplier,
    })

    return {
        "status" : "success",
        "running": _orbit_running,
        "speed"  : _orbit_speed_multiplier,
    }


@app.get("/topology/orbit/status", tags=["Topology"])
async def get_orbit_status():
    """Xem trạng thái chuyển động quỹ đạo vệ tinh hiện tại."""
    target_time = _sim_start_time + timedelta(seconds=_orbit_time_offset)
    return {
        "running"      : _orbit_running,
        "speed"        : _orbit_speed_multiplier,
        "sim_time"     : target_time.strftime("%H:%M:%S UTC"),
        "time_offset_s": round(_orbit_time_offset, 1),
    }


# ─── Endpoint Mô Phỏng Sự Cố Mạng (Fault Tolerance) ───────────────────────────

@app.post("/topology/node/{node_id}/fail", tags=["Fault Tolerance"])
async def fail_node(node_id: str):
    """Giả lập sự cố: Vô hiệu hóa một nút vệ tinh/trạm trên mạng."""
    global _graph, _failed_nodes, _saved_node_attrs, _saved_node_edges
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    if node_id not in _graph.nodes and node_id not in _failed_nodes:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy node {node_id}")

    if node_id in _failed_nodes:
        return {"status": "already_failed", "node": node_id, "failed_nodes": list(_failed_nodes)}

    # Lưu thuộc tính và các cạnh kề của node
    _saved_node_attrs[node_id] = dict(_graph.nodes[node_id])
    incident_edges = []
    for u, v, data in list(_graph.edges(node_id, data=True)):
        incident_edges.append((u, v, dict(data)))
    _saved_node_edges[node_id] = incident_edges

    # Gỡ node khỏi đồ thị định tuyến
    _graph.remove_node(node_id)
    _failed_nodes.add(node_id)

    logger.warning(f"FAULT: Node '{node_id}' đã bị vô hiệu hóa! Còn {_graph.number_of_nodes()} nodes, {_graph.number_of_edges()} edges")

    # Broadcast sự kiện qua WebSocket
    await manager.broadcast({
        "type"        : "NODE_FAILED",
        "node_id"     : node_id,
        "failed_nodes": list(_failed_nodes),
        "n_nodes"     : _graph.number_of_nodes(),
        "n_edges"     : _graph.number_of_edges(),
    })

    db.log_network_event("NODE_FAILED", node_id, {"remaining_nodes": _graph.number_of_nodes()})

    return {
        "status"      : "success",
        "action"      : "failed",
        "node"        : node_id,
        "active_nodes": _graph.number_of_nodes(),
        "active_edges": _graph.number_of_edges(),
        "failed_nodes": list(_failed_nodes),
    }


@app.post("/topology/node/{node_id}/restore", tags=["Fault Tolerance"])
async def restore_node(node_id: str):
    """Khôi phục lại một nút đã bị sự cố/vô hiệu hóa."""
    global _graph, _failed_nodes, _saved_node_attrs, _saved_node_edges
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    if node_id not in _failed_nodes:
        return {"status": "not_failed", "node": node_id, "failed_nodes": list(_failed_nodes)}

    # Khôi phục node vào đồ thị
    attrs = _saved_node_attrs.pop(node_id, {})
    _graph.add_node(node_id, **attrs)

    # Khôi phục các cạnh kề (chỉ nối với các node hiện đang active)
    saved_edges = _saved_node_edges.pop(node_id, [])
    for u, v, data in saved_edges:
        if u in _graph.nodes and v in _graph.nodes and u not in _failed_nodes and v not in _failed_nodes:
            _graph.add_edge(u, v, **data)

    _failed_nodes.discard(node_id)
    logger.info(f"RESTORE: Node '{node_id}' đã phục hồi. Đồ thị: {_graph.number_of_nodes()} nodes, {_graph.number_of_edges()} edges")

    # Broadcast sự kiện qua WebSocket
    await manager.broadcast({
        "type"        : "NODE_RESTORED",
        "node_id"     : node_id,
        "failed_nodes": list(_failed_nodes),
        "n_nodes"     : _graph.number_of_nodes(),
        "n_edges"     : _graph.number_of_edges(),
    })

    db.log_network_event("NODE_RESTORED", node_id, {"active_nodes": _graph.number_of_nodes()})

    return {
        "status"      : "success",
        "action"      : "restored",
        "node"        : node_id,
        "active_nodes": _graph.number_of_nodes(),
        "active_edges": _graph.number_of_edges(),
        "failed_nodes": list(_failed_nodes),
    }


@app.post("/topology/node/{node_id}/toggle", tags=["Fault Tolerance"])
async def toggle_node(node_id: str):
    """Chuyển đổi trạng thái giữa Hỏng (Fail) và Bình thường (Restore) của nút."""
    if node_id in _failed_nodes:
        return await restore_node(node_id)
    else:
        return await fail_node(node_id)


@app.post("/topology/restore_all", tags=["Fault Tolerance"])
async def restore_all_nodes():
    """Khôi phục toàn bộ các nút bị hỏng về trạng thái hoạt động ban đầu."""
    global _failed_nodes
    restored = list(_failed_nodes)
    for nid in restored:
        attrs = _saved_node_attrs.pop(nid, {})
        _graph.add_node(nid, **attrs)
        saved_edges = _saved_node_edges.pop(nid, [])
        for u, v, data in saved_edges:
            if u in _graph.nodes and v in _graph.nodes:
                _graph.add_edge(u, v, **data)

    _failed_nodes.clear()
    logger.info(f"RESTORE ALL: Đã khôi phục tất cả {len(restored)} nodes.")

    await manager.broadcast({
        "type"        : "ALL_NODES_RESTORED",
        "restored"    : restored,
        "failed_nodes": [],
        "n_nodes"     : _graph.number_of_nodes(),
        "n_edges"     : _graph.number_of_edges(),
    })

    return {
        "status"      : "success",
        "restored"    : restored,
        "active_nodes": _graph.number_of_nodes(),
        "active_edges": _graph.number_of_edges(),
        "failed_nodes": [],
    }


@app.get("/topology/failed_nodes", tags=["Fault Tolerance"])
async def get_failed_nodes():
    """Lấy danh sách các nút đang bị sự cố."""
    return {"failed_nodes": list(_failed_nodes)}


# ─── Endpoints Quản lý Tải & Nghẽn Mạng (Congestion & Load Balancing) ────────

@app.get("/traffic/status", tags=["Traffic & Congestion"])
async def get_traffic_status():
    """Lấy thông tin tải lưu lượng của toàn bộ các nút trong mạng."""
    if _topo is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    loads = _topo.get_node_loads()
    congested = [nid for nid, stat in loads.items() if stat["is_congested"]]
    return {
        "status"         : "success",
        "congested_count": len(congested),
        "congested_nodes": congested,
        "nodes"          : loads,
    }


@app.post("/traffic/node/{node_id}/load", tags=["Traffic & Congestion"])
async def set_node_traffic_load(
    node_id: str,
    load   : float = Query(..., ge=0.0, le=1.0, description="Hệ số tải (0.0 -> 1.0)"),
):
    """
    Đặt mức độ tải lưu lượng thủ công cho một nút (VD: 0.90 để tạo nghẽn).
    Kiểm tra xem thuật toán ACO có tự động bẻ lái né tránh nút này hay không!
    """
    if _topo is None or _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    if node_id not in _graph.nodes:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' không tồn tại trong đồ thị")

    ok = _topo.set_node_load(node_id, load)
    if not ok:
        raise HTTPException(status_code=400, detail="Không thể cập nhật tải nút")

    # Đồng bộ sang _graph node attribute
    clamped_load = max(0.0, min(1.0, float(load)))
    capacity = _graph.nodes[node_id].get("capacity", 100)
    _graph.nodes[node_id]["load"] = clamped_load
    _graph.nodes[node_id]["active_conn"] = int(clamped_load * capacity)

    # Lưu bền vững vào SQLite DB
    db.save_node_load(node_id, clamped_load, int(clamped_load * capacity))
    db.log_network_event("CONGESTION", node_id, {"load": clamped_load, "is_congested": clamped_load >= 0.75})

    # Broadcast sự kiện qua WebSocket
    await manager.broadcast({
        "type"        : "NODE_LOAD_CHANGED",
        "node_id"     : node_id,
        "load"        : round(clamped_load, 2),
        "active_conn" : int(clamped_load * capacity),
        "is_congested": clamped_load >= 0.75,
    })

    return {
        "status"      : "success",
        "node"        : node_id,
        "load"        : round(clamped_load, 2),
        "active_conn" : int(clamped_load * capacity),
        "is_congested": clamped_load >= 0.75,
    }


@app.post("/traffic/simulate", tags=["Traffic & Congestion"])
async def simulate_traffic(
    peak_mode: bool = Query(default=True, description="Chế độ giờ cao điểm (tạo 2-4 nút nghẽn đỏ)"),
):
    """Mô phỏng sinh lưu lượng nền hoặc tạo tắc nghẽn giờ cao điểm ngẫu nhiên."""
    if _topo is None or _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    loads = _topo.simulate_background_traffic(peak_mode=peak_mode)

    # Đồng bộ sang _graph
    for nid, stat in loads.items():
        if nid in _graph.nodes:
            _graph.nodes[nid]["load"] = stat["load"]
            _graph.nodes[nid]["active_conn"] = stat["active_conn"]

    congested = [nid for nid, stat in loads.items() if stat["is_congested"]]

    await manager.broadcast({
        "type"           : "TRAFFIC_UPDATED",
        "peak_mode"      : peak_mode,
        "congested_count": len(congested),
        "congested_nodes": congested,
        "loads"          : loads,
    })

    return {
        "status"         : "success",
        "peak_mode"      : peak_mode,
        "congested_count": len(congested),
        "congested_nodes": congested,
        "loads"          : loads,
    }


@app.post("/traffic/reset", tags=["Traffic & Congestion"])
async def reset_traffic():
    """Đặt lại toàn bộ tải mạng về trạng thái bình thường (10%)."""
    if _topo is None or _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    loads = _topo.reset_traffic()
    for nid, stat in loads.items():
        if nid in _graph.nodes:
            _graph.nodes[nid]["load"] = stat["load"]
            _graph.nodes[nid]["active_conn"] = stat["active_conn"]

    db.reset_node_loads()

    await manager.broadcast({
        "type" : "TRAFFIC_RESET",
        "loads": loads,
    })

    return {
        "status": "success",
        "message": "Đã reset tải mạng về trạng thái rảnh rỗi (10%)",
        "loads" : loads,
    }


@app.post("/routing/multiflow", tags=["Routing"])
async def run_multi_flow_routing(
    n_ants: int = Query(default=12, ge=1, le=50),
    n_iter: int = Query(default=35, ge=1, le=100),
):
    """
    **Mô phỏng 5 luồng định tuyến đồng thời (Peak Hours Multi-Flow Routing)**:
    Giả lập giờ cao điểm với nhiều yêu cầu gửi dữ liệu cùng lúc.
    Khi các luồng đầu đi qua vệ tinh, tải của vệ tinh đó tăng lên.
    Các luồng sau của bầy kiến ACO sẽ tự động phân tán tải (load balancing) sang các nhánh khác!
    """
    if _graph is None or _topo is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    # Danh sách 5 cặp nguồn - đích chiến lược trong mạng SAGSINs
    flow_presets = [
        {"name": "Luồng 1: Hà Nội → Tàu HQ-01",     "src": "GND_Ha_Noi",   "dst": "SHIP_1",       "color": "#ef4444"},
        {"name": "Luồng 2: Đà Nẵng → Tàu HQ-02",    "src": "GND_Da_Nang",  "dst": "SHIP_2",       "color": "#10b981"},
        {"name": "Luồng 3: Hà Nội → Đảo Biển Đông", "src": "GND_Ha_Noi",   "dst": "GND_Bien_Dong", "color": "#f59e0b"},
        {"name": "Luồng 4: UAV Tuần tra → HQ-01",   "src": "UAV_1",        "dst": "SHIP_1",       "color": "#06b6d4"},
        {"name": "Luồng 5: Đà Nẵng → UAV Tuần tra", "src": "GND_Da_Nang",  "dst": "UAV_1",        "color": "#a855f7"},
    ]

    active_nodes = set(_graph.nodes())
    valid_flows = []
    for fp in flow_presets:
        if fp["src"] in active_nodes and fp["dst"] in active_nodes:
            valid_flows.append(fp)

    # Nếu thiếu do tên khác, tự động fallback lấy từ ground/uav/ship có sẵn
    if len(valid_flows) < 2:
        gnds = [n for n in active_nodes if "GND" in n]
        ships = [n for n in active_nodes if "SHIP" in n]
        uavs = [n for n in active_nodes if "UAV" in n]
        if gnds and ships:
            valid_flows.append({"name": "Luồng 1", "src": gnds[0], "dst": ships[0], "color": "#ef4444"})
        if len(gnds) > 1 and len(ships) > 1:
            valid_flows.append({"name": "Luồng 2", "src": gnds[1], "dst": ships[1], "color": "#10b981"})
        elif len(gnds) > 1 and uavs:
            valid_flows.append({"name": "Luồng 2", "src": gnds[1], "dst": uavs[0], "color": "#10b981"})

    flows_result = []

    # Chạy lần lượt từng luồng và tăng tải dần trên đường đi của luồng đó (Dynamic Load Balancing)
    for i, fl in enumerate(valid_flows):
        src = fl["src"]
        dst = fl["dst"]

        aco = ACOAlgorithm(_graph, n_ants=n_ants, n_iterations=n_iter, congestion_weight=4.0)
        res = aco.run(src, dst)

        if res.converged:
            # Tăng tải của các nút trung gian trên đường đi để các luồng sau tránh nút này (Load balancing)
            for hop in res.best_path[1:-1]:
                curr_load = _graph.nodes[hop].get("load", 0.1)
                new_load = min(0.95, curr_load + 0.22)
                _topo.set_node_load(hop, new_load)
                _graph.nodes[hop]["load"] = new_load
                cap = _graph.nodes[hop].get("capacity", 100)
                _graph.nodes[hop]["active_conn"] = int(new_load * cap)

            flows_result.append({
                "id"                 : i + 1,
                "name"               : fl["name"],
                "source"             : src,
                "destination"        : dst,
                "color"              : fl["color"],
                "path"               : res.best_path,
                "path_coords"        : _build_path_coords(res.best_path),
                "cost_ms"            : round(res.best_cost_ms, 2),
                "prop_delay_ms"      : res.prop_delay_ms,
                "queue_delay_ms"     : res.queue_delay_ms,
                "hops"               : len(res.best_path) - 1,
                "avoided_bottlenecks": res.avoided_bottlenecks,
            })

    # Broadcast kết quả toàn bộ đa luồng lên Dashboard
    await manager.broadcast({
        "type"       : "MULTIFLOW_DONE",
        "flows"      : flows_result,
        "total_flows": len(flows_result),
        "loads"      : _topo.get_node_loads(),
    })

    # Lưu kết quả phân luồng vào SQLite
    db.save_multiflow_record(
        total_flows=len(flows_result),
        flows=flows_result,
        summary=f"Mô phỏng thành công {len(flows_result)} luồng dữ liệu giờ cao điểm"
    )

    return {
        "status"     : "success",
        "total_flows": len(flows_result),
        "flows"      : flows_result,
        "loads"      : _topo.get_node_loads(),
    }


@app.get("/routing", tags=["Routing"])
async def get_routing(
    source     : str = Query(..., description="Tên node nguồn, VD: GND_Ha_Noi"),
    destination: str = Query(..., description="Tên node đích, VD: SHIP_1"),
    n_ants     : int = Query(default=15, ge=1, le=50),
    n_iter     : int = Query(default=50, ge=1, le=200),
    congestion_weight: float = Query(default=3.5, ge=0.5, le=20.0, description="Hệ số phạt tắc nghẽn né nút nghẽn"),
    broadcast  : bool = Query(default=True, description="Broadcast progress qua WebSocket"),
):
    """
    **REST API**: Tìm đường tối ưu từ source → destination.

    Chạy ACO đồng bộ và trả về kết quả ngay.

    - `source`     : Tên node nguồn (phải có trong topology)
    - `destination`: Tên node đích
    - `n_ants`     : Số kiến (1-50)
    - `n_iter`     : Số iteration (1-200)
    - `congestion_weight`: Hệ số phạt né nút nghẽn (0.5 - 20.0)
    - `broadcast`  : Gửi tiến trình qua WebSocket không?
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    if source in _failed_nodes:
        raise HTTPException(
            status_code=400,
            detail=f"Nút nguồn '{source}' hiện đang bị sự cố/vô hiệu hóa! Vui lòng chọn nút khác hoặc khôi phục nút này."
        )
    if destination in _failed_nodes:
        raise HTTPException(
            status_code=400,
            detail=f"Nút đích '{destination}' hiện đang bị sự cố/vô hiệu hóa! Vui lòng chọn nút khác hoặc khôi phục nút này."
        )

    if source not in _graph.nodes:
        raise HTTPException(
            status_code=404,
            detail=f"Node '{source}' không tồn tại. Các node: {_node_list}"
        )
    if destination not in _graph.nodes:
        raise HTTPException(
            status_code=404,
            detail=f"Node '{destination}' không tồn tại. Các node: {_node_list}"
        )

    logger.info(f"REST /routing: {source} → {destination} | congestion_weight={congestion_weight}")

    # ── Callback broadcast qua WebSocket ──
    def aco_callback(iteration: int, best: AntPath, pheromone: np.ndarray, nodes: List[str]):
        if not broadcast or manager.n_clients == 0:
            return
        msg = {
            "type"           : "ACO_UPDATE",
            "iteration"      : iteration,
            "path"           : best.path,
            "path_coords"    : _build_path_coords(best.path),
            "cost_ms"        : round(best.cost_ms, 2),
            "prop_delay_ms"  : round(best.prop_delay_ms, 2),
            "queue_delay_ms" : round(best.queue_delay_ms, 2),
            "pheromone_level": round(float(pheromone.max()), 3),
            "rtt_ms"         : round(best.cost_ms * 2, 2),
            "converged"      : False,
            "source"         : source,
            "destination"    : destination,
        }
        _broadcast_sync(msg)

    # ── Chạy ACO ──
    aco    = ACOAlgorithm(_graph, n_ants=n_ants, n_iterations=n_iter, congestion_weight=congestion_weight, callback=aco_callback)
    result = aco.run(source, destination)

    if not result.converged:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm được đường từ '{source}' đến '{destination}'"
        )

    # ── Broadcast kết quả cuối ──
    if broadcast:
        _broadcast_sync({
            "type"               : "ACO_DONE",
            "path"               : result.best_path,
            "path_coords"        : _build_path_coords(result.best_path),
            "cost_ms"            : round(result.best_cost_ms, 2),
            "prop_delay_ms"      : result.prop_delay_ms,
            "queue_delay_ms"     : result.queue_delay_ms,
            "avoided_bottlenecks": result.avoided_bottlenecks,
            "rtt_ms"             : round(result.rtt_ms, 2),
            "converged"          : True,
            "convergence_iter"   : result.convergence_iter,
            "source"             : source,
            "destination"        : destination,
        })

    # Cập nhật tuyến đường chủ động để hệ thống tự động thích nghi khi vệ tinh chuyển động
    global _active_route_src, _active_route_dst, _active_route_path, _active_route_cost, _active_route_prop, _active_route_queue
    _active_route_src = source
    _active_route_dst = destination
    _active_route_path = result.best_path
    _active_route_cost = result.best_cost_ms
    _active_route_prop = result.prop_delay_ms
    _active_route_queue = result.queue_delay_ms

    # Lưu kết quả định tuyến vào SQLite database
    db.save_routing_record(
        source=source,
        destination=destination,
        n_ants=n_ants,
        n_iterations=n_iter,
        path=result.best_path,
        cost_ms=result.best_cost_ms,
        prop_delay_ms=result.prop_delay_ms,
        queue_delay_ms=result.queue_delay_ms,
        rtt_ms=result.rtt_ms,
        convergence_iter=result.convergence_iter,
        time_taken_s=result.total_time_s,
        avoided_congested=result.avoided_bottlenecks
    )

    return {
        "source"             : source,
        "destination"        : destination,
        "best_path"          : result.best_path,
        "path_coords"        : _build_path_coords(result.best_path),
        "cost_ms"            : round(result.best_cost_ms, 2),
        "prop_delay_ms"      : result.prop_delay_ms,
        "queue_delay_ms"     : result.queue_delay_ms,
        "avoided_bottlenecks": result.avoided_bottlenecks,
        "rtt_ms"             : round(result.rtt_ms, 2),
        "n_hops"             : len(result.best_path) - 1,
        "convergence_iter"   : result.convergence_iter,
        "n_iterations"       : result.n_iterations,
        "time_s"             : round(result.total_time_s, 3),
    }


@app.post("/routing/clear", tags=["Routing"])
async def clear_active_route():
    """Xóa tuyến đường đang theo dõi thích nghi quỹ đạo."""
    global _active_route_src, _active_route_dst, _active_route_path, _active_route_cost
    global _active_route_prop, _active_route_queue
    _active_route_src = None
    _active_route_dst = None
    _active_route_path = []
    _active_route_cost = 0.0
    _active_route_prop = 0.0
    _active_route_queue = 0.0
    await manager.broadcast({
        "type": "ROUTE_CLEARED",
        "message": "Đã xóa tuyến đường chủ động."
    })
    return {"status": "success", "message": "Đã xóa tuyến đường chủ động"}


@app.get("/routing/active", tags=["Routing"])
async def get_active_route():
    """Xem tuyến đường đang được tự động thích nghi quỹ đạo."""
    return {
        "active"     : bool(_active_route_src and _active_route_dst),
        "source"     : _active_route_src,
        "destination": _active_route_dst,
        "path"       : _active_route_path,
        "cost_ms"    : round(_active_route_cost, 2),
    }


# ─── Endpoint so sánh ACO vs Dijkstra (dùng cho thuyết trình) ────────────────

@app.get("/routing/compare", tags=["Routing"])
async def compare_aco_vs_dijkstra(
    source     : str = Query(..., description="Node nguồn, VD: GND_Ha_Noi"),
    destination: str = Query(..., description="Node đích, VD: SHIP_1"),
    n_ants     : int = Query(default=15, ge=1, le=50),
    n_iter     : int = Query(default=50, ge=1, le=200),
    congestion_weight: float = Query(default=3.5, ge=0.5, le=20.0, description="Hệ số phạt tắc nghẽn né nút nghẽn"),
):
    """
    **So sánh ACO vs Dijkstra** — Dùng cho thuyết trình để chứng minh ACO đúng.

    Trả về:
    - Kết quả ACO  : best_path, cost (ms)
    - Kết quả Dijkstra (ground-truth): shortest_path, cost (ms)
    - `aco_quality` : % cost ACO so với Dijkstra (100% = tối ưu hoàn hảo, <110% = rất tốt)
    - `verdict`     : "OPTIMAL" / "NEAR_OPTIMAL" / "SUBOPTIMAL"
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    if source in _failed_nodes:
        raise HTTPException(status_code=400, detail=f"Nút nguồn '{source}' hiện đang bị sự cố/vô hiệu hóa!")
    if destination in _failed_nodes:
        raise HTTPException(status_code=400, detail=f"Nút đích '{destination}' hiện đang bị sự cố/vô hiệu hóa!")

    if source not in _graph.nodes:
        raise HTTPException(status_code=404, detail=f"Node '{source}' không tồn tại")
    if destination not in _graph.nodes:
        raise HTTPException(status_code=404, detail=f"Node '{destination}' không tồn tại")

    logger.info(f"COMPARE: {source} → {destination} | congestion_weight={congestion_weight}")

    # ── 1. Chạy Dijkstra (ground-truth khoảng cách vật lý) ───────────────────
    try:
        dijkstra_path = nx.dijkstra_path(_graph, source, destination, weight="weight")
        dijkstra_prop = nx.dijkstra_path_length(_graph, source, destination, weight="weight")
        dijkstra_ok   = True
    except nx.NetworkXNoPath:
        dijkstra_path = []
        dijkstra_prop = float("inf")
        dijkstra_ok   = False

    # Tính độ trễ hàng đợi thực tế mà Dijkstra phải chịu nếu đi qua nút nghẽn
    dijkstra_queue = 0.0
    dijkstra_has_congestion = False
    if dijkstra_ok and dijkstra_path:
        for nid in dijkstra_path[1:-1]:
            ld = min(0.99, max(0.0, float(_graph.nodes[nid].get("load", 0.10))))
            dijkstra_queue += (ld / (1.01 - ld)) ** 1.8 * 5.0
            if ld >= 0.75:
                dijkstra_has_congestion = True
    dijkstra_cost = dijkstra_prop + dijkstra_queue

    # ── 2. Chạy ACO ──────────────────────────────────────────────────────────
    aco    = ACOAlgorithm(_graph, n_ants=n_ants, n_iterations=n_iter, congestion_weight=congestion_weight)
    result = aco.run(source, destination)
    aco_ok = result.converged

    aco_has_congestion = any(
        float(_graph.nodes[nid].get("load", 0.10)) >= 0.75
        for nid in result.best_path[1:-1]
    ) if aco_ok and result.best_path else False

    # ── 3. Tính chất lượng ACO so với Dijkstra ───────────────────────────────
    aco_quality_pct = None
    verdict = "UNKNOWN"

    if aco_ok and dijkstra_ok and dijkstra_cost > 0:
        if dijkstra_has_congestion and not aco_has_congestion:
            verdict = "CONGESTION_AVOIDED"
            aco_quality_pct = round(result.best_cost_ms / dijkstra_cost * 100, 2)
        else:
            aco_quality_pct = round(result.best_cost_ms / dijkstra_cost * 100, 2)
            if aco_quality_pct <= 100.5:
                verdict = "OPTIMAL"          # ACO tìm được đường tối ưu hoàn hảo
            elif aco_quality_pct <= 110.0:
                verdict = "NEAR_OPTIMAL"     # Chênh ≤ 10% — rất tốt
            else:
                verdict = "SUBOPTIMAL"       # Cần tăng n_ants hoặc n_iter
    elif aco_ok and not dijkstra_ok:
        verdict = "ACO_ONLY"             # ACO tìm được nhưng Dijkstra không?? (lỗi graph)
    elif not aco_ok:
        verdict = "NO_PATH"

    # ── 4. Broadcast kết quả so sánh qua WebSocket ───────────────────────────
    _broadcast_sync({
        "type"           : "COMPARE_DONE",
        "source"         : source,
        "destination"    : destination,
        "aco_path"       : result.best_path,
        "aco_path_coords": _build_path_coords(result.best_path),
        "dijkstra_path"  : dijkstra_path,
        "dijkstra_coords": _build_path_coords(dijkstra_path),
        "verdict"        : verdict,
        "aco_quality_pct": aco_quality_pct,
    })

    cost_diff_val = (result.best_cost_ms - dijkstra_cost) if (aco_ok and dijkstra_ok) else 0.0
    cost_diff_pct_val = (cost_diff_val / dijkstra_cost * 100.0) if (aco_ok and dijkstra_ok and dijkstra_cost > 0) else 0.0

    # Lưu kết quả đối chiếu vào SQLite database
    db.save_comparison_record(
        source=source,
        destination=destination,
        n_ants=n_ants,
        n_iterations=n_iter,
        aco_cost_ms=result.best_cost_ms if aco_ok else 0.0,
        aco_hops=len(result.best_path) - 1 if aco_ok else 0,
        aco_path=result.best_path if aco_ok else [],
        dijkstra_cost_ms=dijkstra_cost if dijkstra_ok else 0.0,
        dijkstra_hops=len(dijkstra_path) - 1 if dijkstra_ok else 0,
        dijkstra_path=dijkstra_path if dijkstra_ok else [],
        cost_diff_pct=round(cost_diff_pct_val, 2),
        aco_quality_pct=round(aco_quality_pct, 2) if aco_quality_pct is not None else 100.0,
        verdict=verdict
    )

    return {
        # ── Thông tin chung ──
        "source"          : source,
        "destination"     : destination,

        # ── Kết quả ACO ──
        "aco": {
            "found"          : aco_ok,
            "path"           : result.best_path,
            "path_coords"    : _build_path_coords(result.best_path),
            "cost_ms"        : round(result.best_cost_ms, 3),
            "prop_delay_ms"  : round(result.prop_delay_ms, 3),
            "queue_delay_ms" : round(result.queue_delay_ms, 3),
            "rtt_ms"         : round(result.rtt_ms, 3),
            "n_hops"         : len(result.best_path) - 1 if aco_ok else 0,
            "avoided"        : result.avoided_bottlenecks,
            "convergence_iter": result.convergence_iter,
            "n_iterations"   : result.n_iterations,
            "time_s"         : round(result.total_time_s, 3),
        },

        # ── Kết quả Dijkstra (ground-truth) ──
        "dijkstra": {
            "found"         : dijkstra_ok,
            "path"          : dijkstra_path,
            "path_coords"   : _build_path_coords(dijkstra_path),
            "cost_ms"       : round(dijkstra_cost, 3) if dijkstra_ok else None,
            "prop_delay_ms" : round(dijkstra_prop, 3) if dijkstra_ok else None,
            "queue_delay_ms": round(dijkstra_queue, 3) if dijkstra_ok else None,
            "rtt_ms"        : round(dijkstra_cost * 2, 3) if dijkstra_ok else None,
            "n_hops"        : len(dijkstra_path) - 1 if dijkstra_ok else 0,
            "has_congestion": dijkstra_has_congestion,
        },

        # ── Kết quả so sánh ──
        "comparison": {
            "aco_quality_pct": aco_quality_pct,      # 100% = tối ưu, thấp hơn = tốt hơn
            "cost_diff_ms"   : round(result.best_cost_ms - dijkstra_cost, 3)
                               if (aco_ok and dijkstra_ok) else None,
            "same_path"      : result.best_path == dijkstra_path if (aco_ok and dijkstra_ok) else False,
            "verdict"        : verdict,
            "verdict_vi"     : {
                "OPTIMAL"           : "✅ ACO tìm được đường TỐI ƯU hoàn hảo (= Dijkstra)",
                "NEAR_OPTIMAL"      : "🟡 ACO gần tối ưu (chênh ≤ 10% so với Dijkstra)",
                "SUBOPTIMAL"        : "🔴 ACO chưa tối ưu — tăng n_ants hoặc n_iter",
                "CONGESTION_AVOIDED": "🛡️ ACO BẺ LÁI THÀNH CÔNG — Tự động né nút nghẽn (Dijkstra bị tắc)",
                "NO_PATH"           : "❌ Không tìm được đường đi",
                "UNKNOWN"           : "❓ Không xác định được",
                "ACO_ONLY"          : "⚠️ Chỉ ACO tìm được (kiểm tra lại graph)",
            }.get(verdict, ""),
        },
    }


@app.post("/routing/batch", tags=["Routing"])
async def batch_routing(
    pairs: List[Dict[str, str]],
    background_tasks: BackgroundTasks,
    n_threads: int = Query(default=3, ge=1, le=8),
):
    """
    **Batch routing**: Gửi nhiều yêu cầu định tuyến, xử lý đa luồng.

    Body:
    ```json
    [
      {"source": "GND_Ha_Noi",  "destination": "SHIP_1"},
      {"source": "GND_Da_Nang", "destination": "SHIP_2"}
    ]
    ```
    Xử lý ngay với ThreadManager, broadcast kết quả qua WebSocket.
    """
    if _graph is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")

    task_pairs = []
    for p in pairs:
        src = p.get("source", "")
        dst = p.get("destination", "")
        if src in _graph.nodes and dst in _graph.nodes:
            task_pairs.append((src, dst))

    if not task_pairs:
        raise HTTPException(status_code=400, detail="Không có task hợp lệ")

    def run_batch():
        def on_result(r: RoutingResult):
            if r.success and r.result:
                _broadcast_sync({
                    "type"       : "BATCH_RESULT",
                    "task_id"    : r.task.task_id,
                    "thread"     : r.thread_name,
                    "source"     : r.task.source,
                    "destination": r.task.destination,
                    "path"       : r.result.best_path,
                    "path_coords": _build_path_coords(r.result.best_path),
                    "cost_ms"    : round(r.result.best_cost_ms, 2),
                    "rtt_ms"     : round(r.result.rtt_ms, 2),
                })

        tm = ThreadManager(_graph, n_threads=n_threads,
                           aco_ants=10, aco_iters=30, on_result=on_result)
        tm.submit_tasks(task_pairs)
        tm.run_and_wait()

        stats = tm.get_stats()
        _broadcast_sync({
            "type"     : "BATCH_DONE",
            "completed": stats["completed"],
            "failed"   : stats["failed"],
            "avg_cost" : round(stats["avg_cost_ms"], 2),
        })

    background_tasks.add_task(run_batch)

    return {
        "status"      : "accepted",
        "n_tasks"     : len(task_pairs),
        "n_threads"   : n_threads,
        "message"     : "Kết quả sẽ được broadcast qua WebSocket /ws",
    }


@app.get("/stats", tags=["Info"])
async def get_stats():
    """Thống kê topology hiện tại."""
    if _topo is None:
        raise HTTPException(status_code=503, detail="Topology chưa sẵn sàng")
    stats = _topo.get_stats()
    stats["ws_clients"] = manager.n_clients
    return stats


# ─── WebSocket Endpoint ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    **WebSocket**: Real-time ACO progress và kết quả routing.

    Client kết nối tại: ws://localhost:8000/ws

    Nhận messages:
      {"action": "ping"}
      {"action": "route", "source": "GND_Ha_Noi", "destination": "SHIP_1"}

    Gửi messages:
      ACO_UPDATE, ACO_DONE, BATCH_RESULT, BATCH_DONE, PONG, ERROR
    """
    await manager.connect(websocket)

    # Gửi thông tin topology ngay khi kết nối
    if _graph:
        nodes = _topo.get_node_positions_for_leaflet() if _topo else []
        for n in nodes:
            n["is_failed"] = (n["id"] in _failed_nodes)
        raw_edges = _topo.get_edges_for_leaflet() if _topo else []
        edges = [e for e in raw_edges if e["from"] not in _failed_nodes and e["to"] not in _failed_nodes]
        await manager.send_personal(websocket, {
            "type"        : "CONNECTED",
            "nodes"        : nodes,
            "edges"        : edges,
            "failed_nodes" : list(_failed_nodes),
            "orbit_speed"  : _orbit_speed_multiplier,
            "orbit_running": _orbit_running,
            "n_clients"    : manager.n_clients,
        })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_personal(websocket,
                    {"type": "ERROR", "message": "Invalid JSON"})
                continue

            action = msg.get("action", "")

            # ── Ping ──
            if action == "ping":
                await manager.send_personal(websocket, {
                    "type": "PONG", "timestamp": time.time()
                })

            # ── Route request qua WebSocket ──
            elif action == "route":
                src = msg.get("source", "")
                dst = msg.get("destination", "")

                if not src or not dst:
                    await manager.send_personal(websocket,
                        {"type": "ERROR", "message": "Thiếu source hoặc destination"})
                    continue

                if _graph is None or src not in _graph.nodes or dst not in _graph.nodes:
                    await manager.send_personal(websocket,
                        {"type": "ERROR",
                         "message": f"Node không tồn tại: {src} hoặc {dst}"})
                    continue

                n_ants_req = int(msg.get("n_ants", 15))
                n_iter_req = int(msg.get("n_iterations", 50))
                cong_w_req = float(msg.get("congestion_weight", 3.5))

                # Chạy ACO trong thread riêng (không block event loop)
                def run_aco_ws():
                    def cb(it, best, ph, nl):
                        _broadcast_sync({
                            "type"        : "ACO_UPDATE",
                            "iteration"   : it,
                            "path"        : best.path,
                            "path_coords" : _build_path_coords(best.path),
                            "cost_ms"     : round(best.cost_ms, 2),
                            "rtt_ms"      : round(best.cost_ms * 2, 2),
                            "converged"   : False,
                            "source"      : src,
                            "destination" : dst,
                        })

                    aco = ACOAlgorithm(_graph, n_ants=n_ants_req, n_iterations=n_iter_req, congestion_weight=cong_w_req, callback=cb)
                    result = aco.run(src, dst)
                    if result.converged:
                        global _active_route_src, _active_route_dst, _active_route_path, _active_route_cost, _active_route_prop, _active_route_queue
                        _active_route_src = src
                        _active_route_dst = dst
                        _active_route_path = result.best_path
                        _active_route_cost = result.best_cost_ms
                        _active_route_prop = result.prop_delay_ms
                        _active_route_queue = result.queue_delay_ms

                        db.save_routing_record(
                            source=src,
                            destination=dst,
                            n_ants=n_ants_req,
                            n_iterations=n_iter_req,
                            path=result.best_path,
                            cost_ms=result.best_cost_ms,
                            prop_delay_ms=result.prop_delay_ms,
                            queue_delay_ms=result.queue_delay_ms,
                            rtt_ms=result.rtt_ms,
                            convergence_iter=result.convergence_iter,
                            time_taken_s=result.total_time_s,
                            avoided_congested=result.avoided_bottlenecks
                        )

                    _broadcast_sync({
                        "type"            : "ACO_DONE",
                        "path"            : result.best_path,
                        "path_coords"     : _build_path_coords(result.best_path),
                        "cost_ms"         : round(result.best_cost_ms, 2) if result.converged else 0.0,
                        "rtt_ms"          : round(result.rtt_ms, 2) if result.converged else 0.0,
                        "converged"       : result.converged,
                        "convergence_iter": result.convergence_iter,
                        "source"          : src,
                        "destination"     : dst,
                    })

                threading.Thread(target=run_aco_ws, daemon=True).start()
                await manager.send_personal(websocket, {
                    "type"   : "ACO_STARTED",
                    "source" : src,
                    "destination": dst,
                })

            elif action == "clear_route":
                global _active_route_src, _active_route_dst, _active_route_path, _active_route_cost
                _active_route_src = None
                _active_route_dst = None
                _active_route_path = []
                _active_route_cost = 0.0
                await manager.broadcast({
                    "type": "ROUTE_CLEARED",
                    "message": "Đã xóa tuyến đường chủ động."
                })

            elif action == "toggle_node":
                nid = msg.get("node_id", "")
                if nid:
                    await toggle_node(nid)

            elif action == "restore_all":
                await restore_all_nodes()

            elif action == "simulate_traffic":
                peak = msg.get("peak_mode", True)
                await simulate_traffic(peak_mode=peak)

            elif action == "reset_traffic":
                await reset_traffic()

            elif action == "set_node_load":
                nid = msg.get("node_id", "")
                ld = float(msg.get("load", 0.1))
                if nid:
                    await set_node_traffic_load(node_id=nid, load=ld)

            elif action == "multiflow":
                ants = int(msg.get("n_ants", 12))
                iters = int(msg.get("n_iter", 35))
                await run_multi_flow_routing(n_ants=ants, n_iter=iters)

            elif action == "orbit_control":
                r_val = msg.get("running")
                s_val = msg.get("speed")
                await control_orbit_simulation(running=r_val, speed=s_val)

            else:
                await manager.send_personal(websocket,
                    {"type": "ERROR", "message": f"Action không hỗ trợ: '{action}'"})

    except WebSocketDisconnect:
        await manager.disconnect(websocket)


# ─── Endpoints Cơ sở dữ liệu Bền vững (SQLite Database & Analytics) ─────────

@app.get("/db/stats", tags=["Database & Analytics"])
async def get_db_stats():
    """Lấy tổng quan số lượng phiên lưu trữ trong cơ sở dữ liệu SQLite."""
    return db.get_database_stats()


@app.get("/db/routing_history", tags=["Database & Analytics"])
async def get_db_routing_history(limit: int = Query(default=20, ge=1, le=200)):
    """Lấy lịch sử các lần chạy định tuyến ACO từ CSDL."""
    return {
        "count": limit,
        "history": db.get_recent_routing_history(limit)
    }


@app.get("/db/comparison_history", tags=["Database & Analytics"])
async def get_db_comparison_history(limit: int = Query(default=20, ge=1, le=200)):
    """Lấy lịch sử đối chiếu thuật toán ACO vs Dijkstra từ CSDL."""
    return {
        "count": limit,
        "history": db.get_recent_comparisons(limit)
    }


@app.get("/db/events", tags=["Database & Analytics"])
async def get_db_events(
    limit: int = Query(default=50, ge=1, le=200),
    event_type: Optional[str] = Query(default=None, description="Lọc theo loại: HANDOVER, NODE_FAILED, NODE_RESTORED")
):
    """Lấy nhật ký sự cố mạng và chuyển giao quỹ đạo từ CSDL."""
    return {
        "count": limit,
        "events": db.get_recent_events(limit, event_type)
    }


@app.get("/db/export/csv", tags=["Database & Analytics"])
async def export_routing_csv():
    """Xuất toàn bộ bảng lịch sử định tuyến ra file CSV để làm tài liệu báo cáo đồ án."""
    import io
    import csv
    from fastapi.responses import Response

    records = db.get_recent_routing_history(limit=500)
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "Thoi_Gian", "Nguon", "Dich", "So_Kien", "Iterations",
        "Hops", "Tong_Tre_ms", "Tre_Truyen_Song_ms", "Tre_Hang_Doi_ms",
        "RTT_ms", "Iteration_Hoi_Tu", "Thoi_Gian_Tinh_s", "Tuyen_Duong"
    ])

    for r in records:
        writer.writerow([
            r.get("id"),
            r.get("created_at"),
            r.get("source"),
            r.get("destination"),
            r.get("n_ants"),
            r.get("n_iterations"),
            r.get("path_hops"),
            r.get("best_cost_ms"),
            r.get("prop_delay_ms"),
            r.get("queue_delay_ms"),
            r.get("rtt_ms"),
            r.get("convergence_iter"),
            r.get("time_taken_s"),
            " -> ".join(r.get("path", []))
        ])

    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sagsins_routing_history.csv"}
    )


# ─── Chạy trực tiếp ──────────────────────────────────────────────────────────

# Serve frontend - PHẢI đặt SAU tất cả API routes
_frontend_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend"
)
if os.path.isdir(_frontend_dir):
    app.mount("/ui", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
    logger.info(f"Frontend: http://localhost:8000/ui/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "web_api:app",
        host    = "0.0.0.0",
        port    = 8000,
        reload  = False,
        app_dir = os.path.dirname(os.path.abspath(__file__)),
    )
