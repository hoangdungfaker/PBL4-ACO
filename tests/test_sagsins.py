"""
test_sagsins.py — Unit Tests cho PBL4 SAGSINs ACO
===================================================
Chạy kiểm thử:
  pytest tests/
  pytest tests/ -v
"""

import os
import sys
import pytest
import networkx as nx

# Thêm path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, BASE_DIR)

from satellite_tracker import SatelliteTracker, propagation_delay_ms, distance_ecef_km
from topology_manager import TopologyManager
from aco_algorithm import ACOAlgorithm


TLE_PATH = os.path.join(BASE_DIR, "data", "starlink.tle")


def test_propagation_delay():
    """Kiểm tra công thức tính độ trễ truyền sóng theo vận tốc ánh sáng c."""
    c = 299792.458
    assert propagation_delay_ms(0) == 0.0
    dist_1000km = 1000.0
    expected_ms = (1000.0 / c) * 1000.0
    assert abs(propagation_delay_ms(dist_1000km) - expected_ms) < 1e-4


def test_satellite_tracker_load():
    """Kiểm tra đọc file TLE và lọc vệ tinh hợp lệ."""
    assert os.path.exists(TLE_PATH), f"File TLE không tồn tại tại {TLE_PATH}"
    tracker = SatelliteTracker(TLE_PATH)
    assert tracker.get_count() > 0

    infos = tracker.get_satellite_infos()
    assert len(infos) == tracker.get_count()

    # Kiểm tra ECEF matrix
    ecef = tracker.get_ecef_positions()
    assert ecef.shape == (tracker.get_count(), 3)


def test_topology_builder():
    """Kiểm tra xây dựng đồ thị từ vệ tinh và thêm trạm mặt đất/tàu biển."""
    tracker = SatelliteTracker(TLE_PATH)
    topo = TopologyManager(tracker, radius_km=5000.0)
    G = topo.build_from_tracker()

    assert G.number_of_nodes() > 0
    assert G.number_of_edges() > 0

    # Kiểm tra thêm trạm mặt đất & UAV
    gnd = topo.add_ground_station("GND_Ha_Noi", 21.0285, 105.8542)
    assert gnd.node_id in G.nodes
    assert G.nodes["GND_Ha_Noi"]["node_type"] == "ground"

    uav = topo.add_ground_station("UAV_1", 14.5, 110.0, alt_km=0.5)
    assert uav.node_id in G.nodes
    assert G.nodes["UAV_1"]["node_type"] == "uav"


def test_aco_routing_convergence():
    """Kiểm tra giải thuật ACO tìm đường và hội tụ."""
    tracker = SatelliteTracker(TLE_PATH)
    topo = TopologyManager(tracker, radius_km=6000.0)
    G = topo.build_from_tracker()

    topo.add_ground_station("GND_Ha_Noi", 21.0285, 105.8542)
    topo.add_ground_station("SHIP_1", 11.0, 112.0)

    # Đảm bảo đồ thị liên thông cho test
    if not nx.has_path(G, "GND_Ha_Noi", "SHIP_1"):
        # Nếu bán kính chưa phủ tới, nối tạm một cạnh qua vệ tinh gần nhất
        nodes = list(G.nodes)
        sat_node = [n for n in nodes if "SAT" in n][0]
        G.add_edge("GND_Ha_Noi", sat_node, weight=10.0, pheromone=1.0)
        G.add_edge(sat_node, "SHIP_1", weight=10.0, pheromone=1.0)

    aco = ACOAlgorithm(G, n_ants=10, n_iterations=20)
    res = aco.run("GND_Ha_Noi", "SHIP_1")

    assert res.converged
    assert res.best_cost_ms < float("inf")
    assert len(res.best_path) >= 2
    assert res.best_path[0] == "GND_Ha_Noi"
    assert res.best_path[-1] == "SHIP_1"


def test_api_routes():
    """Kiểm tra các endpoints REST API của FastAPI (gọi trực tiếp không phụ thuộc httpx)."""
    import asyncio
    from web_api import root

    data = asyncio.run(root())
    assert data["status"] == "running"
    assert "name" in data
    assert data["version"] == "1.0.0"


def test_traffic_manager_loads():
    """Kiểm tra đặt tải, sinh lưu lượng và reset tải mạng."""
    tracker = SatelliteTracker(TLE_PATH)
    topo = TopologyManager(tracker, radius_km=5000.0)
    G = topo.build_from_tracker()

    gnd = topo.add_ground_station("GND_Ha_Noi", 21.0285, 105.8542)
    assert topo.set_node_load("GND_Ha_Noi", 0.85)
    loads = topo.get_node_loads()
    assert loads["GND_Ha_Noi"]["load"] == 0.85
    assert loads["GND_Ha_Noi"]["is_congested"] is True

    # Test mô phỏng lưu lượng giờ cao điểm
    peak_loads = topo.simulate_background_traffic(peak_mode=True)
    congested = [nid for nid, st in peak_loads.items() if st["is_congested"]]
    assert len(congested) > 0

    # Test reset tải
    reset_loads = topo.reset_traffic()
    assert all(st["load"] == 0.10 for st in reset_loads.values())


def test_congestion_aware_aco_rerouting():
    """Kiểm tra ACO tự động bẻ lái né tránh nút nghẽn tải nặng."""
    # Xây dựng đồ thị kim cương 4 nút:
    # Source: S -> A -> D  (Path 1: delay 10ms + 10ms = 20ms, nhưng A bị nghẽn tải 95%)
    #         S -> B -> D  (Path 2: delay 12ms + 12ms = 24ms, nhưng B rảnh rỗi tải 5%)
    G = nx.Graph()
    G.add_node("SRC", node_type="ground", load=0.1)
    G.add_node("SAT_A", node_type="satellite", load=0.98) # Nút nghẽn cực nặng
    G.add_node("SAT_B", node_type="satellite", load=0.05) # Nút thông thoáng
    G.add_node("DST", node_type="ship", load=0.1)

    # Cạnh qua A
    G.add_edge("SRC", "SAT_A", weight=10.0, pheromone=1.0)
    G.add_edge("SAT_A", "DST", weight=10.0, pheromone=1.0)

    # Cạnh qua B
    G.add_edge("SRC", "SAT_B", weight=12.0, pheromone=1.0)
    G.add_edge("SAT_B", "DST", weight=12.0, pheromone=1.0)

    # Chạy ACO với trọng số nghẽn cao
    aco = ACOAlgorithm(G, n_ants=20, n_iterations=30, congestion_weight=5.0)
    res = aco.run("SRC", "DST")

    assert res.converged
    # Nhờ có phạt nghẽn, kiến sẽ chọn tuyến thông thoáng qua SAT_B
    assert "SAT_B" in res.best_path
    assert "SAT_A" not in res.best_path
    assert "SAT_A" in res.avoided_bottlenecks


def test_dynamic_orbit_handover():
    """Kiểm tra cơ chế tự thích nghi và chuyển giao vệ tinh (Satellite Handover) khi khoảng cách thay đổi."""
    from datetime import datetime, timedelta, timezone

    tracker = SatelliteTracker(TLE_PATH)
    topo = TopologyManager(tracker, radius_km=5000.0)
    topo.build_from_tracker()
    topo.add_ground_station("GND_Da_Nang", 16.0544, 108.2022)
    topo.add_ground_station("SHIP_2", 9.0, 115.0)

    G = topo.graph
    src, dst = "GND_Da_Nang", "SHIP_2"
    assert nx.has_path(G, src, dst)

    p1 = nx.shortest_path(G, src, dst, weight="weight")
    assert len(p1) >= 3  # Ít nhất qua 1 nút vệ tinh trung gian

    # Giả lập thời gian tiến về phía trước 600 giây (vệ tinh cũ bay đi, vệ tinh mới bay đến)
    t_future = datetime.now(timezone.utc) + timedelta(seconds=600)
    infos = tracker.get_satellite_infos(t_future)
    terr_ecef_src = G.nodes[src]["ecef_km"]
    terr_ecef_dst = G.nodes[dst]["ecef_km"]

    # Cập nhật liên kết theo khoảng cách mới
    for i, info in enumerate(infos):
        if info.alt_km < 100.0 or info.alt_km > 2000.0:
            continue
        sat_id = f"SAT_{i}"
        if sat_id not in G.nodes:
            continue
        G.nodes[sat_id]["ecef_km"] = info.ecef_km

        d_src = distance_ecef_km(terr_ecef_src, info.ecef_km)
        if d_src <= 5000.0:
            delay = propagation_delay_ms(d_src)
            G.add_edge(src, sat_id, weight=delay, distance_km=d_src)
        elif G.has_edge(src, sat_id):
            G.remove_edge(src, sat_id)

        d_dst = distance_ecef_km(terr_ecef_dst, info.ecef_km)
        if d_dst <= 5000.0:
            delay = propagation_delay_ms(d_dst)
            G.add_edge(dst, sat_id, weight=delay, distance_km=d_dst)
        elif G.has_edge(dst, sat_id):
            G.remove_edge(dst, sat_id)

    assert nx.has_path(G, src, dst)
    p2 = nx.shortest_path(G, src, dst, weight="weight")
    assert len(p2) >= 3
    # Xác nhận đường đi mới hợp lệ và được tính toán động
    assert p2[0] == src
    assert p2[-1] == dst



