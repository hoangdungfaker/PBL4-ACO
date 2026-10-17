"""
topology_manager.py — PBL4 SAGSINs ACO
========================================
Nhiệm vụ:
  1. Nhận ECEF positions (N, 3) từ satellite_tracker
  2. Dùng scipy.spatial.KDTree để tìm neighbor trong bán kính
  3. Build NetworkX Graph với node + edge:
       - edge weight   = propagation delay (ms)
       - edge pheromone = 1.0 / cost (khởi tạo đồng đều)
  4. Hỗ trợ cập nhật topology động khi vệ tinh di chuyển

API scipy được dùng (đã kiểm chứng):
  - scipy.spatial.KDTree(positions)
  - tree.query_ball_point(point, r=radius_km)

Tác giả : PBL4 Team
Ngày    : 2026
"""

import os
import sys
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import networkx as nx
import numpy as np
from scipy.spatial import KDTree

from satellite_tracker import (
    SatelliteInfo,
    SatelliteTracker,
    distance_ecef_km,
    propagation_delay_ms,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger("topology_manager")

# ─── Tham số mặc định ────────────────────────────────────────────────────────
DEFAULT_RADIUS_KM       = 2_500.0   # Bán kính ISL (km) — Starlink ≈ 2000-3000 km
DEFAULT_PHEROMONE_INIT  = 1.0       # Pheromone khởi tạo trên mỗi cạnh
DEFAULT_ALPHA           = 1.0       # Trọng số pheromone trong ACO
DEFAULT_BETA            = 2.0       # Trọng số heuristic (1/delay) trong ACO


# ─── Data class cho node ─────────────────────────────────────────────────────
@dataclass
class NetworkNode:
    """Đại diện cho một node trong đồ thị SAGSINs."""

    node_id   : str           # VD: "SAT_0", "UAV_2", "GND_Da_Nang"
    node_type : str           # "satellite" | "uav" | "ground" | "ship"
    ecef_km   : np.ndarray    # [x, y, z] km
    lat_deg   : float = 0.0
    lon_deg   : float = 0.0
    alt_km    : float = 0.0
    load      : float = 0.1   # Tải mạng hiện tại (0.0: rảnh rỗi -> 1.0: bão hòa/nghẽn)
    active_conn: int  = 5     # Số lượng kết nối/luồng dữ liệu đang xử lý
    capacity  : int   = 100   # Băng thông tải tối đa (số luồng đồng thời)
    meta      : dict  = field(default_factory=dict)

    def __hash__(self):
        return hash(self.node_id)

    def __eq__(self, other):
        return isinstance(other, NetworkNode) and self.node_id == other.node_id


# ─── Class chính ─────────────────────────────────────────────────────────────

class TopologyManager:
    """
    Quản lý topology mạng SAGSINs dựa trên KD-Tree + NetworkX.

    Luồng dữ liệu:
      SatelliteTracker → ecef (N,3) → KDTree → NetworkX Graph
      → Giao cho ACO để tìm đường

    Thread-safety:
      Dùng threading.RLock bảo vệ graph khi update topology động.
    """

    def __init__(
        self,
        tracker: SatelliteTracker,
        radius_km: float = DEFAULT_RADIUS_KM,
        pheromone_init: float = DEFAULT_PHEROMONE_INIT,
    ) -> None:
        """
        Args:
            tracker       : SatelliteTracker đã được khởi tạo
            radius_km     : Bán kính tối đa tạo ISL (km)
            pheromone_init: Giá trị pheromone khởi tạo trên mỗi cạnh
        """
        self.tracker        = tracker
        self.radius_km      = radius_km
        self.pheromone_init = pheromone_init

        self.graph: nx.Graph = nx.Graph()
        self._lock   = threading.RLock()   # Bảo vệ graph khi multi-thread update
        self._nodes  : Dict[str, NetworkNode] = {}
        self._kdtree : Optional[KDTree] = None
        self._ecef   : Optional[np.ndarray] = None

        logger.info(
            f"TopologyManager khởi tạo — radius={radius_km} km, "
            f"pheromone_init={pheromone_init}"
        )

    # ── Xây dựng topology từ vệ tinh ─────────────────────────────────────────

    def build_from_tracker(self, timestamp=None) -> nx.Graph:
        """
        Build toàn bộ graph từ SatelliteTracker.

        1. Lấy SatelliteInfo từ tracker
        2. Tạo NetworkNode cho mỗi vệ tinh
        3. Build KDTree từ ECEF positions
        4. Tìm neighbor + tạo edge với cost = delay (ms)

        Args:
            timestamp: datetime UTC (mặc định: now)

        Returns:
            NetworkX Graph đã build xong
        """
        logger.info("Bắt đầu build topology từ satellite tracker...")
        infos = self.tracker.get_satellite_infos(timestamp)
        ecef  = self.tracker.get_ecef_positions(timestamp)

        nodes: List[NetworkNode] = []
        valid_ecef: List[np.ndarray] = []
        for i, info in enumerate(infos):
            # Lọc bỏ vệ tinh có cao độ bất thường (quỹ đạo phân rã hoặc TLE cũ gây lỗi SGP4)
            if info.alt_km < 100.0 or info.alt_km > 2000.0:
                logger.warning(
                    f"Bỏ qua vệ tinh {info.name} (SAT_{i}) do cao độ bất thường: {info.alt_km:.1f} km"
                )
                continue

            node = NetworkNode(
                node_id   = f"SAT_{i}",
                node_type = "satellite",
                ecef_km   = info.ecef_km,
                lat_deg   = info.lat_deg,
                lon_deg   = info.lon_deg,
                alt_km    = info.alt_km,
                meta      = {"tle_name": info.name, "velocity_km_s": info.velocity_km_s},
            )
            nodes.append(node)
            valid_ecef.append(info.ecef_km)

        return self.build_from_nodes(nodes, np.array(valid_ecef) if valid_ecef else None)

    def build_from_nodes(
        self,
        nodes: List[NetworkNode],
        ecef_positions: Optional[np.ndarray] = None,
    ) -> nx.Graph:
        """
        Build graph từ danh sách NetworkNode.

        Cho phép thêm node thủ công (UAV, Ground station, Ship)
        ngoài các vệ tinh.

        Args:
            nodes         : Danh sách NetworkNode
            ecef_positions: (N, 3) numpy — nếu None sẽ lấy từ nodes

        Returns:
            nx.Graph
        """
        with self._lock:
            # ── 1. Làm sạch graph cũ ──
            self.graph.clear()
            self._nodes.clear()

            # ── 2. Chuẩn bị ECEF matrix ──
            if ecef_positions is not None:
                self._ecef = ecef_positions
            else:
                self._ecef = np.array([n.ecef_km for n in nodes])

            # ── 3. Thêm node vào graph ──
            for i, node in enumerate(nodes):
                self._nodes[node.node_id] = node
                self.graph.add_node(
                    node.node_id,
                    node_type   = node.node_type,
                    ecef_km     = node.ecef_km,
                    lat_deg     = node.lat_deg,
                    lon_deg     = node.lon_deg,
                    alt_km      = node.alt_km,
                    load        = getattr(node, "load", 0.1),
                    active_conn = getattr(node, "active_conn", 5),
                    capacity    = getattr(node, "capacity", 100),
                    meta        = node.meta,
                    pos_2d      = (node.lon_deg, node.lat_deg),   # Cho visualization
                )

            # ── 4. Build KD-Tree ──
            self._kdtree = KDTree(self._ecef)
            logger.info(f"KD-Tree built với {len(nodes)} nodes")

            # ── 5. Tìm neighbors + tạo edges ──
            edge_count = 0
            for i, node in enumerate(nodes):
                # query_ball_point: trả về TẤT CẢ index trong bán kính
                neighbor_indices: List[int] = self._kdtree.query_ball_point(
                    self._ecef[i], r=self.radius_km
                )
                neighbor_indices = list(neighbor_indices)

                # Loại bỏ chính nó
                if i in neighbor_indices:
                    neighbor_indices.remove(i)

                for j in neighbor_indices:
                    if j <= i:
                        continue  # Tránh thêm cạnh 2 lần (đồ thị vô hướng)

                    neighbor_node = nodes[j]
                    dist_km  = distance_ecef_km(self._ecef[i], self._ecef[j])
                    delay_ms = propagation_delay_ms(dist_km)

                    # edge weight = delay (ms) — cost thấp → đường tốt
                    # pheromone khởi tạo đồng đều
                    self.graph.add_edge(
                        node.node_id,
                        neighbor_node.node_id,
                        weight          = delay_ms,          # cost (ms)
                        distance_km     = dist_km,
                        pheromone       = self.pheromone_init,
                        heuristic       = 1.0 / (delay_ms + 1e-9),  # η = 1/cost
                    )
                    edge_count += 1

            logger.info(
                f"✅ Graph built: {self.graph.number_of_nodes()} nodes, "
                f"{edge_count} edges | radius={self.radius_km} km"
            )
            return self.graph

    # ── Thêm node thủ công (UAV, Ground, Ship) ───────────────────────────────

    def add_ground_station(
        self,
        node_id: str,
        lat_deg: float,
        lon_deg: float,
        alt_km: float = 0.0,
    ) -> NetworkNode:
        """
        Thêm trạm mặt đất / UAV / tàu vào topology.

        Args:
            node_id : ID duy nhất (VD: "GND_Da_Nang")
            lat_deg : Vĩ độ
            lon_deg : Kinh độ
            alt_km  : Độ cao (km)

        Returns:
            NetworkNode vừa thêm
        """
        from satellite_tracker import ground_station_ecef
        ecef = ground_station_ecef(lat_deg, lon_deg, alt_km)

        # Xác định node_type dựa vào prefix ID
        if "GND" in node_id.upper() or "GROUND" in node_id.upper():
            ntype = "ground"
        elif "UAV" in node_id.upper():
            ntype = "uav"
        elif "SHIP" in node_id.upper():
            ntype = "ship"
        else:
            ntype = "ground"

        node = NetworkNode(
            node_id=node_id, node_type=ntype,
            ecef_km=ecef, lat_deg=lat_deg, lon_deg=lon_deg, alt_km=alt_km,
            load=0.1, active_conn=5, capacity=100,
        )

        with self._lock:
            self._nodes[node_id] = node
            self.graph.add_node(
                node_id,
                node_type   = ntype,
                ecef_km     = ecef,
                lat_deg     = lat_deg,
                lon_deg     = lon_deg,
                alt_km      = alt_km,
                load        = 0.1,
                active_conn = 5,
                capacity    = 100,
                meta        = {},
                pos_2d      = (lon_deg, lat_deg),
            )

            # Kết nối đến các node khác trong mạng:
            # 1. Kết nối với satellite trong bán kính radius_km
            # 2. UAV kết nối với Ground / Ship / UAV lân cận trong bán kính 600km làm trạm chuyển tiếp (Air relay)
            for other_id, other_node in self._nodes.items():
                if other_id == node_id:
                    continue
                dist_km = distance_ecef_km(ecef, other_node.ecef_km)
                can_connect = False

                if other_node.node_type == "satellite":
                    can_connect = (dist_km <= self.radius_km)
                elif ntype == "uav" or other_node.node_type == "uav":
                    can_connect = (dist_km <= 600.0)

                if can_connect:
                    delay_ms = propagation_delay_ms(dist_km)
                    self.graph.add_edge(
                        node_id, other_id,
                        weight      = delay_ms,
                        distance_km = dist_km,
                        pheromone   = self.pheromone_init,
                        heuristic   = 1.0 / (delay_ms + 1e-9),
                    )

        logger.info(f"➕ Thêm node '{node_id}' ({ntype}) tại ({lat_deg:.2f}, {lon_deg:.2f})")
        return node

    # ── Pheromone flat array (phục vụ ACO) ───────────────────────────────────

    def get_pheromone_matrix(self) -> Tuple[np.ndarray, List[str]]:
        """
        Trả về pheromone dưới dạng numpy 1D flatten (cache-friendly).

        pheromone[i * N + j] = pheromone trên cạnh (node_i, node_j)

        Returns:
            (pheromone_flat, node_list)
            - pheromone_flat: numpy 1D, shape (N*N,)
            - node_list     : list tên node (để map index → tên)
        """
        with self._lock:
            nodes_list = list(self.graph.nodes())
            N = len(nodes_list)
            node_idx = {name: i for i, name in enumerate(nodes_list)}

            pheromone_flat = np.zeros(N * N, dtype=np.float64)

            for u, v, data in self.graph.edges(data=True):
                i = node_idx[u]
                j = node_idx[v]
                p = data.get("pheromone", self.pheromone_init)
                pheromone_flat[i * N + j] = p
                pheromone_flat[j * N + i] = p   # Đồ thị vô hướng

        return pheromone_flat, nodes_list

    def update_pheromone(
        self,
        node_a: str,
        node_b: str,
        delta: float,
        rho: float = 0.1,
    ) -> None:
        """
        Cập nhật pheromone trên cạnh (evaporation + deposit).

        Gọi bởi ACO sau khi một con kiến hoàn thành hành trình.

        Args:
            node_a : Tên node đầu
            node_b : Tên node cuối
            delta  : Q / path_cost (lượng pheromone thêm vào)
            rho    : Hệ số bay hơi (0 < rho < 1)
        """
        with self._lock:
            if not self.graph.has_edge(node_a, node_b):
                return
            data = self.graph[node_a][node_b]
            old  = data["pheromone"]
            data["pheromone"] = (1 - rho) * old + delta
            logger.debug(
                f"Pheromone [{node_a}↔{node_b}]: {old:.4f} → {data['pheromone']:.4f}"
            )

    def evaporate_all(self, rho: float = 0.1) -> None:
        """
        Bay hơi pheromone toàn bộ graph.

        pheromone *= (1 - rho)

        Args:
            rho: Tốc độ bay hơi (0 < rho < 1)
        """
        with self._lock:
            for u, v, data in self.graph.edges(data=True):
                data["pheromone"] *= (1 - rho)

    # ── Quản lý tải lưu lượng & mô phỏng nghẽn mạng (Congestion & Load Balancing) ─

    def set_node_load(
        self,
        node_id: str,
        load: float,
        active_conn: Optional[int] = None,
    ) -> bool:
        """
        Đặt tải lưu lượng cho một nút mạng (0.0 <= load <= 1.0).

        Args:
            node_id    : Tên nút (VD: "SAT_10", "GND_Ha_Noi")
            load       : Giá trị tải (0.0 = rảnh, 1.0 = bão hòa)
            active_conn: Số lượng kết nối (tùy chọn)

        Returns:
            True nếu cập nhật thành công, False nếu node không tồn tại.
        """
        with self._lock:
            if node_id not in self.graph.nodes:
                return False

            clamped_load = max(0.0, min(1.0, float(load)))
            capacity = self.graph.nodes[node_id].get("capacity", 100)
            if active_conn is None:
                active_conn = int(clamped_load * capacity)

            self.graph.nodes[node_id]["load"] = clamped_load
            self.graph.nodes[node_id]["active_conn"] = active_conn

            if node_id in self._nodes:
                self._nodes[node_id].load = clamped_load
                self._nodes[node_id].active_conn = active_conn

            logger.info(
                f"📊 Đã đặt tải nút '{node_id}': {clamped_load * 100:.1f}% "
                f"({active_conn}/{capacity} kết nối)"
            )
            return True

    def get_node_loads(self) -> Dict[str, Dict]:
        """
        Lấy thông tin tải của tất cả các nút.

        Returns:
            Dict: {node_id: {"load": float, "active_conn": int, "is_congested": bool}}
        """
        with self._lock:
            res = {}
            for nid, data in self.graph.nodes(data=True):
                ld = float(data.get("load", 0.1))
                res[nid] = {
                    "load": round(ld, 2),
                    "active_conn": int(data.get("active_conn", 5)),
                    "capacity": int(data.get("capacity", 100)),
                    "is_congested": bool(ld >= 0.75),
                }
            return res

    def simulate_background_traffic(self, peak_mode: bool = False) -> Dict[str, Dict]:
        """
        Sinh ngẫu nhiên lưu lượng nền cho toàn bộ mạng SAGSINs.

        Args:
            peak_mode: Nếu True, chọn ngẫu nhiên 2-4 vệ tinh trọng yếu
                       làm nút thắt cổ chai với tải cao (80% - 98%).

        Returns:
            Dict mapping node_id -> load stats
        """
        import random
        with self._lock:
            all_nodes = list(self.graph.nodes())
            sat_nodes = [nid for nid in all_nodes if "SAT" in nid]

            # Thiết lập tải cơ bản
            for nid in all_nodes:
                ntype = self.graph.nodes[nid].get("node_type", "satellite")
                if ntype in ("ground", "ship"):
                    base_load = random.uniform(0.15, 0.40)
                elif ntype == "uav":
                    base_load = random.uniform(0.10, 0.35)
                else:
                    base_load = random.uniform(0.08, 0.30)
                self.set_node_load(nid, base_load)

            congested_nodes = []
            if peak_mode and sat_nodes:
                # Chọn 2-4 vệ tinh làm điểm nghẽn
                k = min(len(sat_nodes), random.randint(2, 4))
                bottlenecks = random.sample(sat_nodes, k)
                for bn in bottlenecks:
                    high_load = random.uniform(0.82, 0.96)
                    self.set_node_load(bn, high_load)
                    congested_nodes.append(bn)

            logger.info(
                f"🎲 Đã mô phỏng lưu lượng (peak_mode={peak_mode}): "
                f"{len(congested_nodes)} nút nghẽn {congested_nodes}"
            )
            return self.get_node_loads()

    def reset_traffic(self) -> Dict[str, Dict]:
        """Đặt lại tải toàn bộ nút về mức bình thường (10%)."""
        with self._lock:
            for nid in self.graph.nodes():
                self.set_node_load(nid, 0.10, active_conn=5)
            logger.info("🔄 Đã reset tải toàn bộ mạng SAGSINs về 10%")
            return self.get_node_loads()

    # ── Thống kê & visualization ──────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Trả về thống kê nhanh về topology."""
        with self._lock:
            degrees = [d for _, d in self.graph.degree()]
            weights = [d["weight"] for _, _, d in self.graph.edges(data=True)]

        return {
            "n_nodes"          : self.graph.number_of_nodes(),
            "n_edges"          : self.graph.number_of_edges(),
            "avg_degree"       : float(np.mean(degrees)) if degrees else 0,
            "max_degree"       : int(np.max(degrees))    if degrees else 0,
            "avg_delay_ms"     : float(np.mean(weights)) if weights else 0,
            "min_delay_ms"     : float(np.min(weights))  if weights else 0,
            "max_delay_ms"     : float(np.max(weights))  if weights else 0,
            "is_connected"     : nx.is_connected(self.graph),
            "n_components"     : nx.number_connected_components(self.graph),
        }

    def get_node_positions_for_leaflet(self) -> List[Dict]:
        """
        Trả về danh sách node với lat/lon cho Leaflet.js frontend.

        Returns:
            List of dicts: {id, type, lat, lon, alt_km, load, active_conn, is_congested}
        """
        with self._lock:
            result = []
            for nid, data in self.graph.nodes(data=True):
                ld = float(data.get("load", 0.1))
                result.append({
                    "id"          : nid,
                    "type"        : data.get("node_type", "unknown"),
                    "lat"         : data.get("lat_deg", 0.0),
                    "lon"         : data.get("lon_deg", 0.0),
                    "alt_km"      : data.get("alt_km", 0.0),
                    "load"        : round(ld, 2),
                    "active_conn" : int(data.get("active_conn", 5)),
                    "capacity"    : int(data.get("capacity", 100)),
                    "is_congested": bool(ld >= 0.75),
                })
        return result

    def get_edges_for_leaflet(self) -> List[Dict]:
        """
        Trả về danh sách edge cho Leaflet.js (vẽ polyline).

        Returns:
            List of dicts: {from, to, delay_ms, pheromone, distance_km}
        """
        with self._lock:
            result = []
            for u, v, data in self.graph.edges(data=True):
                result.append({
                    "from"       : u,
                    "to"        : v,
                    "delay_ms"  : data.get("weight", 0.0),
                    "pheromone" : data.get("pheromone", 1.0),
                    "dist_km"   : data.get("distance_km", 0.0),
                })
        return result

    def __repr__(self) -> str:
        return (
            f"<TopologyManager nodes={self.graph.number_of_nodes()} "
            f"edges={self.graph.number_of_edges()} "
            f"radius={self.radius_km}km>"
        )


# ─── Quick demo / self-test ──────────────────────────────────────────────────

def _demo() -> None:
    """Demo nhanh — chạy: python backend/topology_manager.py"""
    import sys, os

    tle_path = "data/starlink.tle"
    if not os.path.exists(tle_path):
        print(f"[ERROR] Không tìm thấy '{tle_path}'")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] — %(message)s")

    print("=" * 65)
    print("  SAGSINs ACO — TopologyManager Demo")
    print("=" * 65)

    # 1. Tạo tracker
    tracker = SatelliteTracker(tle_path)

    # 2. Tạo topology manager
    topo = TopologyManager(tracker, radius_km=5_000.0)

    # 3. Build graph từ satellite positions
    G = topo.build_from_tracker()

    # 4. Thêm node mặt đất (Việt Nam + khu vực)
    topo.add_ground_station("GND_Da_Nang",  lat_deg=16.0544, lon_deg=108.2022)
    topo.add_ground_station("GND_Ha_Noi",   lat_deg=21.0285, lon_deg=105.8542)
    topo.add_ground_station("GND_Bien_Dong", lat_deg=12.0,   lon_deg=114.0)
    topo.add_ground_station("UAV_1",        lat_deg=14.5,    lon_deg=110.0, alt_km=0.5)
    topo.add_ground_station("SHIP_1",       lat_deg=11.0,    lon_deg=112.0)

    # 5. Thống kê
    stats = topo.get_stats()
    print(f"\n📊 Thống kê topology:")
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"   {k:<20} = {v:.2f}")
        else:
            print(f"   {k:<20} = {v}")

    # 6. In pheromone matrix shape
    pheromone, node_list = topo.get_pheromone_matrix()
    N = len(node_list)
    print(f"\n🐜 Pheromone flat array:")
    print(f"   N nodes      = {N}")
    print(f"   Shape (1D)   = {pheromone.shape} — pheromone[i*N+j]")
    print(f"   Non-zero     = {np.count_nonzero(pheromone)}")
    print(f"   Sample [0,1] = {pheromone[0*N+1]:.4f}")

    # 7. In danh sách edges
    edges = topo.get_edges_for_leaflet()
    print(f"\n🔗 Edges ({len(edges)} tổng):")
    for e in edges[:8]:
        print(f"   {e['from']:<20} ↔ {e['to']:<20} | {e['delay_ms']:.2f} ms | {e['dist_km']:.1f} km")
    if len(edges) > 8:
        print(f"   ... và {len(edges) - 8} edges khác")

    # 8. Kiểm tra connected
    if stats["is_connected"]:
        # Tìm shortest path nếu đồ thị liên thông
        nodes_list = list(G.nodes())
        if len(nodes_list) >= 2:
            try:
                path = nx.shortest_path(G, nodes_list[0], nodes_list[-1], weight="weight")
                print(f"\n🛤️  Shortest path (by delay):")
                print(f"   {' → '.join(path)}")
            except nx.NetworkXNoPath:
                print("\n⚠️  Không có đường đi giữa 2 node!")
    else:
        print(f"\n⚠️  Graph KHÔNG liên thông ({stats['n_components']} thành phần)")
        print("   Tăng radius_km để kết nối thêm node!")

    print("\n✅ topology_manager.py hoạt động đúng!\n")


if __name__ == "__main__":
    import sys, os
    # Thêm thư mục cha vào path khi chạy trực tiếp
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _demo()
