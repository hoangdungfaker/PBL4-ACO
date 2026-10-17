"""
benchmark.py — PBL4 SAGSINs ACO
================================
Nhiệm vụ:
  Chạy các kịch bản thực nghiệm và đánh giá định lượng thuật toán ACO:
  1. Đồ thị hội tụ (Convergence Curve) qua các vòng lặp.
  2. Đối chiếu toàn diện ACO vs Dijkstra (Cost, Runtime, Hop count, Optimality Gap).
  3. Phân tích độ nhạy tham số (Số lượng kiến n_ants, Tốc độ bay hơi rho).
  4. Xuất bảng dữ liệu CSV và biểu đồ PNG chất lượng cao vào thư mục results/

Tác giả : PBL4 Team
Ngày    : 2026
"""

from __future__ import annotations

import os
import sys
import time
import csv
import itertools
from typing import List, Dict, Tuple, Any

import numpy as np
import networkx as nx
import matplotlib
matplotlib.use("Agg")  # Chạy headless không cần màn hình GUI
import matplotlib.pyplot as plt

# Thêm path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, BASE_DIR)

from satellite_tracker import SatelliteTracker
from topology_manager import TopologyManager
from aco_algorithm import ACOAlgorithm


RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)


def build_test_environment() -> Tuple[nx.Graph, TopologyManager]:
    """Khởi tạo môi trường mạng SAGSINs chuẩn từ TLE Starlink."""
    tle_path = os.path.join(BASE_DIR, "data", "starlink.tle")
    tracker = SatelliteTracker(tle_path)
    topo = TopologyManager(tracker, radius_km=5_000.0)
    topo.build_from_tracker()

    # Thêm các trạm SAGSINs
    topo.add_ground_station("GND_Da_Nang",   16.0544, 108.2022)
    topo.add_ground_station("GND_Ha_Noi",    21.0285, 105.8542)
    topo.add_ground_station("GND_Bien_Dong", 12.0,    114.0)
    topo.add_ground_station("UAV_1",         14.5,    110.0, alt_km=0.5)
    topo.add_ground_station("SHIP_1",        11.0,    112.0)
    topo.add_ground_station("SHIP_2",        9.0,     115.0)

    return topo.graph, topo


def run_convergence_experiment(graph: nx.Graph, source: str = "GND_Ha_Noi", dest: str = "SHIP_1", n_iter: int = 50) -> List[float]:
    """Thực nghiệm 1: Khảo sát quá trình hội tụ qua n_iter vòng lặp."""
    print(f"\n[1/3] Đang chạy thực nghiệm Convergence Curve ({source} -> {dest})...")
    aco = ACOAlgorithm(graph, n_ants=15, n_iterations=n_iter, alpha=1.0, beta=2.0, rho=0.1)
    res = aco.run(source, dest)
    
    # Tính đường chuẩn Dijkstra
    dijkstra_cost = nx.shortest_path_length(graph, source, dest, weight="weight")
    
    # Vẽ biểu đồ
    plt.figure(figsize=(9, 5))
    iters = list(range(1, len(res.all_iterations_best) + 1))
    costs = res.all_iterations_best
    
    plt.plot(iters, costs, marker="o", markersize=4, color="#2563eb", linewidth=2, label="ACO Best Cost (ms)")
    plt.axhline(y=dijkstra_cost, color="#dc2626", linestyle="--", linewidth=1.8, label=f"Dijkstra Optimal ({dijkstra_cost:.2f} ms)")
    
    plt.title(f"Convergence Curve of ACO: {source} -> {dest}", fontsize=13, fontweight="bold")
    plt.xlabel("Iteration", fontsize=11)
    plt.ylabel("Propagation Delay (ms)", fontsize=11)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend(fontsize=11)
    
    # Highlight điểm hội tụ
    conv_iter = res.convergence_iter
    conv_cost = res.best_cost_ms
    plt.annotate(
        f"Converged at iter {conv_iter}\nCost: {conv_cost:.2f} ms",
        xy=(conv_iter, conv_cost),
        xytext=(conv_iter + 5, conv_cost + (max(costs) - conv_cost) * 0.25),
        arrowprops=dict(facecolor="#16a34a", shrink=0.08, width=1.5, headwidth=8),
        fontweight="bold",
        color="#15803d",
        bbox=dict(boxstyle="round,pad=0.4", fc="#dcfce7", ec="#16a34a", lw=1)
    )
    
    chart_path = os.path.join(RESULTS_DIR, "convergence_curve.png")
    plt.tight_layout()
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"  -> Đã lưu biểu đồ: {chart_path}")
    return costs


def run_comparison_experiment(graph: nx.Graph) -> List[Dict[str, Any]]:
    """Thực nghiệm 2: So sánh ACO vs Dijkstra trên nhiều cặp nút mạng."""
    print("\n[2/3] Đang chạy thực nghiệm Đối chiếu ACO vs Dijkstra...")
    test_nodes = ["GND_Ha_Noi", "GND_Da_Nang", "GND_Bien_Dong", "SHIP_1", "SHIP_2", "UAV_1"]
    pairs = list(itertools.combinations(test_nodes, 2))
    
    records = []
    
    for src, dst in pairs:
        if not nx.has_path(graph, src, dst):
            continue
            
        # 1. Dijkstra
        t0 = time.perf_counter()
        d_path = nx.shortest_path(graph, src, dst, weight="weight")
        d_cost = nx.shortest_path_length(graph, src, dst, weight="weight")
        d_time = (time.perf_counter() - t0) * 1000  # ms
        
        # 2. ACO
        t1 = time.perf_counter()
        aco = ACOAlgorithm(graph, n_ants=15, n_iterations=40, alpha=1.0, beta=2.0, rho=0.1)
        res = aco.run(src, dst)
        a_time = (time.perf_counter() - t1) * 1000  # ms
        
        if not res.converged:
            continue
            
        a_cost = res.best_cost_ms
        a_path = res.best_path
        
        ratio = (a_cost / d_cost) * 100 if d_cost > 0 else 100.0
        same = (a_path == d_path)
        verdict = "OPTIMAL" if ratio <= 100.5 else ("NEAR_OPTIMAL" if ratio <= 110.0 else "SUB_OPTIMAL")
        
        records.append({
            "Pair": f"{src} -> {dst}",
            "Source": src,
            "Destination": dst,
            "ACO_Cost_ms": round(a_cost, 2),
            "Dijkstra_Cost_ms": round(d_cost, 2),
            "Optimality_%": round(ratio, 2),
            "Verdict": verdict,
            "Same_Path": same,
            "ACO_Hops": len(a_path) - 1,
            "Dijkstra_Hops": len(d_path) - 1,
            "ACO_Runtime_ms": round(a_time, 2),
            "Dijkstra_Runtime_ms": round(d_time, 4),
            "ACO_Path": " -> ".join(a_path),
            "Dijkstra_Path": " -> ".join(d_path),
        })

    # Lưu CSV
    csv_path = os.path.join(RESULTS_DIR, "benchmark_comparison.csv")
    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    print(f"  -> Đã lưu bảng số liệu: {csv_path}")

    # Vẽ biểu đồ so sánh Cost
    plt.figure(figsize=(11, 5.5))
    indices = np.arange(len(records))
    width = 0.38
    
    pair_labels = [r["Pair"].replace("GND_", "").replace("SHIP_", "S_").replace("UAV_", "U_") for r in records]
    aco_costs = [r["ACO_Cost_ms"] for r in records]
    dijk_costs = [r["Dijkstra_Cost_ms"] for r in records]
    
    plt.bar(indices - width/2, dijk_costs, width, label="Dijkstra (Optimal)", color="#10b981", alpha=0.9)
    plt.bar(indices + width/2, aco_costs, width, label="ACO Result", color="#6366f1", alpha=0.9)
    
    plt.title("Routing Cost Comparison: ACO vs Dijkstra", fontsize=13, fontweight="bold")
    plt.xlabel("Node Pairs", fontsize=11)
    plt.ylabel("Delay (ms)", fontsize=11)
    plt.xticks(indices, pair_labels, rotation=35, ha="right", fontsize=9)
    plt.legend(fontsize=11)
    plt.grid(axis="y", linestyle=":", alpha=0.7)
    
    cost_chart_path = os.path.join(RESULTS_DIR, "aco_vs_dijkstra_cost.png")
    plt.tight_layout()
    plt.savefig(cost_chart_path, dpi=300)
    plt.close()
    print(f"  -> Đã lưu biểu đồ: {cost_chart_path}")

    return records


def run_sensitivity_experiment(graph: nx.Graph, source: str = "GND_Ha_Noi", dest: str = "SHIP_1") -> None:
    """Thực nghiệm 3: Khảo sát độ nhạy tham số (Số lượng kiến n_ants và Tốc độ bay hơi rho)."""
    print("\n[3/3] Đang chạy thực nghiệm Độ nhạy tham số (Sensitivity Analysis)...")
    
    # 1. Khảo sát theo số lượng kiến n_ants
    ant_counts = [5, 10, 15, 20, 30]
    avg_costs_ants = []
    avg_times_ants = []
    
    for n in ant_counts:
        costs = []
        times = []
        for _ in range(3):  # Chạy lặp 3 lần lấy trung bình
            t0 = time.perf_counter()
            aco = ACOAlgorithm(graph, n_ants=n, n_iterations=30, alpha=1.0, beta=2.0, rho=0.1)
            res = aco.run(source, dest)
            times.append((time.perf_counter() - t0) * 1000)
            if res.converged:
                costs.append(res.best_cost_ms)
        avg_costs_ants.append(np.mean(costs) if costs else 0)
        avg_times_ants.append(np.mean(times))
        
    # 2. Khảo sát theo tốc độ bay hơi rho
    rhos = [0.05, 0.1, 0.2, 0.4, 0.6]
    avg_costs_rho = []
    conv_iters_rho = []
    
    for r in rhos:
        costs = []
        iters = []
        for _ in range(3):
            aco = ACOAlgorithm(graph, n_ants=15, n_iterations=35, alpha=1.0, beta=2.0, rho=r)
            res = aco.run(source, dest)
            if res.converged:
                costs.append(res.best_cost_ms)
                iters.append(res.convergence_iter)
        avg_costs_rho.append(np.mean(costs) if costs else 0)
        conv_iters_rho.append(np.mean(iters) if iters else 0)

    # Vẽ biểu đồ 2 đồ thị con
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    
    # Ax1: n_ants vs Runtime & Cost
    ax1.plot(ant_counts, avg_times_ants, marker="s", color="#d97706", linewidth=2, label="Runtime (ms)")
    ax1.set_xlabel("Number of Ants (n_ants)", fontsize=10)
    ax1.set_ylabel("Execution Time (ms)", color="#d97706", fontsize=10)
    ax1.tick_params(axis="y", labelcolor="#d97706")
    ax1.grid(True, linestyle=":", alpha=0.6)
    
    ax1_twin = ax1.twinx()
    ax1_twin.plot(ant_counts, avg_costs_ants, marker="o", color="#2563eb", linewidth=2, linestyle="--", label="Cost (ms)")
    ax1_twin.set_ylabel("Path Delay (ms)", color="#2563eb", fontsize=10)
    ax1_twin.tick_params(axis="y", labelcolor="#2563eb")
    ax1.set_title("Impact of Ant Population (n_ants)", fontweight="bold")
    
    # Ax2: rho vs Convergence Iteration
    ax2.plot(rhos, conv_iters_rho, marker="^", color="#7c3aed", linewidth=2, label="Conv. Iteration")
    ax2.set_xlabel("Evaporation Rate (rho)", fontsize=10)
    ax2.set_ylabel("Iterations to Converge", color="#7c3aed", fontsize=10)
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.set_title("Impact of Pheromone Evaporation (rho)", fontweight="bold")
    
    param_chart_path = os.path.join(RESULTS_DIR, "parameter_sensitivity.png")
    plt.tight_layout()
    plt.savefig(param_chart_path, dpi=300)
    plt.close()
    print(f"  -> Đã lưu biểu đồ: {param_chart_path}")


def main():
    print("=================================================================")
    print("      PBL4 SAGSINs ACO - BẮT ĐẦU CHẠY BENCHMARK THỰC NGHIỆM      ")
    print("=================================================================")
    t_start = time.time()
    
    graph, topo = build_test_environment()
    print(f"Mạng SAGSINs khởi tạo thành công: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")
    
    # 1. Đường cong hội tụ
    run_convergence_experiment(graph, source="GND_Ha_Noi", dest="SHIP_1", n_iter=50)
    
    # 2. Đối chiếu toàn diện ACO vs Dijkstra
    records = run_comparison_experiment(graph)
    
    # 3. Phân tích tham số
    run_sensitivity_experiment(graph, source="GND_Ha_Noi", dest="SHIP_1")
    
    total_time = time.time() - t_start
    print(f"\n✅ HOÀN TẤT TOÀN BỘ THỰC NGHIỆM trong {total_time:.2f} giây!")
    print(f"Toàn bộ kết quả (CSV + 3 ảnh biểu đồ PNG) đã được lưu vào: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
