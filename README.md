# PBL4 SAGSINs ACO — Adaptive Routing for Space-Air-Ground-Sea Networks

## Mục tiêu

Demo hệ thống **ACO (Ant Colony Optimization)** tìm đường tối ưu trong mạng SAGSINs (Space-Air-Ground-Sea Integrated Networks) với:
- Đa luồng (`threading`, `queue`, `Lock`) — OS concepts
- FastAPI + WebSocket — Network concepts
- Leaflet.js visualization — Frontend

---

## Cấu Trúc

```
PBL4/
├── backend/
│   ├── satellite_tracker.py   ✅ Parse TLE → ECEF positions & lọc vệ tinh hợp lệ
│   ├── topology_manager.py    ✅ KD-Tree → NetworkX graph & liên kết đa tầng SAGSINs
│   ├── aco_algorithm.py       ✅ Ant Colony Optimization định tuyến thích nghi
│   ├── thread_manager.py      ✅ Quản lý bầy kiến đa luồng (OS Producer-Consumer & Lock)
│   ├── benchmark.py           ✅ Bộ thực nghiệm định lượng (Convergence, Dijkstra vs ACO, Sensitivity)
│   └── web_api.py             ✅ FastAPI REST + WebSocket real-time telemetry
├── frontend/
│   ├── index.html             ✅ 3D Digital Twin (Three.js & Globe.gl) + 2D Leaflet.js
│   ├── style.css              ✅ Giao diện Cyberpunk Glassmorphism
│   └── network_map.js         ✅ Mô hình 3D vệ tinh, Smart Labels, điều khiển lưới ISL
├── client_nodes/
│   └── dummy_client.py        ✅ Giả lập trạm khách gửi lệnh REST & nghe stream WS
├── tests/
│   └── test_sagsins.py        ✅ Bộ Unit Test tự động (Pytest)
├── data/
│   └── starlink.tle           ✅ Chòm 60 vệ tinh Starlink
├── results/                   ✅ Biểu đồ PNG (Convergence, Comparison, Sensitivity) & CSV
└── requirements.txt           ✅ Thư viện phụ thuộc
```

---

## Cài Đặt

```bash
pip install -r requirements.txt
```

---

## Hướng Dẫn Vận Hành

### 1. Khởi động Web Server & Giao diện 3D
```bash
python backend/web_api.py
```
👉 Truy cập giao diện Digital Twin: **http://localhost:8000/ui/**

### 2. Chạy Client giả lập (Node Client)
```bash
# Gửi yêu cầu qua REST API:
python client_nodes/dummy_client.py --src GND_Ha_Noi --dst SHIP_1

# Hoặc lắng nghe trực tiếp luồng WebSocket:
python client_nodes/dummy_client.py --src GND_Ha_Noi --dst SHIP_1 --ws
```

### 3. Chạy Kiểm Thử Tự Động (Unit Tests)
```bash
python -m pytest tests/ -v
```

### 4. Chạy Benchmark Thực Nghiệm (Tạo biểu đồ & CSV)
```bash
python backend/benchmark.py
```
Kết quả sẽ xuất ra thư mục `results/`:
- `convergence_curve.png`: Đồ thị hội tụ bầy kiến
- `aco_vs_dijkstra_cost.png`: So sánh chi phí định tuyến ACO vs Dijkstra
- `parameter_sensitivity.png`: Phân tích độ nhạy $n_{\text{ants}}$ và $\rho$
- `benchmark_comparison.csv`: Bảng số liệu chi tiết

---

## Stack Kỹ Thuật

| Layer | Thư viện | Mục đích |
|-------|----------|---------|
| TLE Parse | `skyfield` | `EarthSatellite`, `itrf_xyz()` → ECEF |
| Neighbor Search | `scipy.spatial.KDTree` | `query_ball_point(r=radius_km)` |
| Graph | `networkx` | Lưu node/edge, tính shortest path |
| Đa luồng | `threading`, `queue` | Thread, Queue, Lock — OS concepts |
| API | `fastapi`, `uvicorn` | REST + WebSocket |
| Frontend | Leaflet.js | 2D map, polyline pheromone |

---

## Lưu Ý Kỹ Thuật

- **ECEF frame**: Dùng `sat.at(t).itrf_xyz().km` (KHÔNG dùng `.itrs.position.km` — deprecated)
- **Lat/Lon**: Dùng `wgs84.geographic_position_of(pos)` từ `skyfield.api`
- **Pheromone**: Lưu dạng numpy 1D flatten `pheromone[i*N + j]` — CPU cache-friendly
- **ISL radius**: Mặc định 5000 km (điều chỉnh theo TLE thực tế)
- **Graph connectivity**: Chỉ 10 vệ tinh → có thể không fully connected
  - Giải pháp: Tăng `radius_km` hoặc dùng nhiều vệ tinh hơn

---

## Tác Giả

PBL4 Team — 2026
