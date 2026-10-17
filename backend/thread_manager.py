"""
thread_manager.py — PBL4 SAGSINs ACO
======================================
Nhiệm vụ:
  Quản lý bầy kiến đa luồng — thể hiện OS concepts:
  - threading.Thread  : Tạo và quản lý luồng thủ công
  - queue.Queue       : Hàng đợi task an toàn giữa các thread
  - threading.Lock    : Bảo vệ shared resource (pheromone)
  - Race condition    : Demo + giải thích khi KHÔNG dùng Lock

Pattern được chọn (OS best practice):
  Producer → queue.Queue → Consumer Threads
  KHÔNG dùng ThreadPoolExecutor để thể hiện rõ OS concepts

Luồng hoạt động:
  Producer : Đưa N task (source, dest) vào Queue
  Consumer : K threads liên tục rút task ra, chạy ACO, update pheromone
  Lock     : Chỉ lock khi WRITE pheromone (không lock khi tính toán)

Log output mẫu:
  [Thread-2] Nhận task: GND_Ha_Noi → SHIP_1
  [Thread-2] ACO xong: 51.36ms | path: GND_Ha_Noi → SAT_9 → SHIP_1
  [LOCK acquired - Thread-2] Cập nhật pheromone...
  [LOCK released - Thread-2]

Tác giả : PBL4 Team
Ngày    : 2026
"""

import os
import sys
import logging
import queue
import threading
import time
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import networkx as nx

from aco_algorithm import ACOAlgorithm, ACOResult, AntPath

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger("thread_manager")

# ─── Sentinel báo thread dừng ────────────────────────────────────────────────
_STOP_SIGNAL = None   # Đưa None vào queue = báo consumer thread dừng lại


# ─── Data class cho task ─────────────────────────────────────────────────────

@dataclass
class RoutingTask:
    """Một yêu cầu định tuyến được đưa vào Queue."""
    task_id    : int
    source     : str
    destination: str
    priority   : int = 0    # Ưu tiên cao hơn = số nhỏ hơn

    def __repr__(self) -> str:
        return f"<Task #{self.task_id}: {self.source} → {self.destination}>"


@dataclass
class RoutingResult:
    """Kết quả sau khi một thread xử lý xong task."""
    task       : RoutingTask
    result     : Optional[ACOResult]
    thread_name: str
    duration_s : float
    success    : bool

    def __repr__(self) -> str:
        if self.success and self.result:
            return (
                f"<Result Task#{self.task.task_id} | "
                f"{self.thread_name} | "
                f"{self.result.best_cost_ms:.2f}ms | "
                f"{'→'.join(self.result.best_path)}>"
            )
        return f"<Result Task#{self.task.task_id} | FAILED>"


# ─── Class chính ─────────────────────────────────────────────────────────────

class ThreadManager:
    """
    Quản lý bầy kiến đa luồng cho SAGSINs ACO.

    OS Concepts được demo:
    ┌─────────────────────────────────────────────┐
    │  threading.Thread  — tạo/quản lý thread     │
    │  queue.Queue       — task queue FIFO         │
    │  threading.Lock    — mutual exclusion        │
    │  threading.Event   — dừng thread gracefully  │
    │  Race condition    — shared pheromone        │
    └─────────────────────────────────────────────┘

    Ví dụ sử dụng:
    --------------
    >>> tm = ThreadManager(graph, n_threads=4)
    >>> tm.submit_tasks([("GND_Ha_Noi", "SHIP_1"), ...])
    >>> results = tm.run_and_wait()
    """

    def __init__(
        self,
        graph      : nx.Graph,
        n_threads  : int   = 4,
        aco_ants   : int   = 10,
        aco_iters  : int   = 40,
        shared_pheromone: bool = True,
        on_result  : Optional[Callable[[RoutingResult], None]] = None,
    ) -> None:
        """
        Args:
            graph           : NetworkX Graph từ TopologyManager
            n_threads       : Số consumer thread (worker)
            aco_ants        : Số kiến mỗi ACO iteration
            aco_iters       : Số iteration ACO mỗi task
            shared_pheromone: True = dùng chung pheromone giữa các thread
                              False = mỗi thread có pheromone riêng
            on_result       : Callback khi có kết quả (cho WebSocket)
        """
        self.graph            = graph
        self.n_threads        = n_threads
        self.aco_ants         = aco_ants
        self.aco_iters        = aco_iters
        self.shared_pheromone = shared_pheromone
        self.on_result        = on_result

        # ── Queue: Producer đẩy task vào, Consumer rút ra ──
        self.task_queue: queue.Queue[Optional[RoutingTask]] = queue.Queue()

        # ── Lock: Bảo vệ pheromone khi nhiều thread cùng write ──
        self.pheromone_lock = threading.Lock()

        # ── Event: Ra hiệu dừng tất cả thread ──
        self._stop_event = threading.Event()

        # ── Shared pheromone (numpy 1D flatten) ──
        node_list = list(graph.nodes())
        N = len(node_list)
        self.shared_pheromone_arr = np.ones(N * N, dtype=np.float64)
        self.node_list = node_list
        self.N = N

        # ── Tracking kết quả ──
        self.results: List[RoutingResult] = []
        self.results_lock = threading.Lock()

        # ── Stats ──
        self._tasks_submitted  = 0
        self._tasks_completed  = 0
        self._tasks_failed     = 0
        self._threads: List[threading.Thread] = []

        logger.info(
            f"ThreadManager init: {n_threads} threads | "
            f"shared_pheromone={shared_pheromone} | "
            f"ACO(ants={aco_ants}, iter={aco_iters})"
        )

    # ── Producer: Đưa task vào Queue ─────────────────────────────────────────

    def submit_task(self, source: str, destination: str, priority: int = 0) -> int:
        """
        Thêm 1 task định tuyến vào Queue.

        Args:
            source     : Node nguồn
            destination: Node đích
            priority   : Ưu tiên (chưa dùng trong Queue đơn giản)

        Returns:
            task_id
        """
        self._tasks_submitted += 1
        task = RoutingTask(
            task_id    = self._tasks_submitted,
            source     = source,
            destination= destination,
            priority   = priority,
        )
        self.task_queue.put(task)
        logger.info(f"[Producer] Đưa vào queue: {task}")
        return task.task_id

    def submit_tasks(self, pairs: List[Tuple[str, str]]) -> None:
        """
        Đưa nhiều task vào Queue cùng lúc.

        Args:
            pairs: List of (source, destination)
        """
        for src, dst in pairs:
            self.submit_task(src, dst)
        logger.info(f"[Producer] Tổng {len(pairs)} tasks trong queue")

    # ── Consumer: Thread worker ───────────────────────────────────────────────

    def _worker(self, thread_id: int) -> None:
        """
        Hàm chạy trong mỗi thread consumer.

        Vòng lặp:
          1. Rút task từ Queue (blocking — thread ngủ nếu queue rỗng)
          2. Nhận None = Stop signal → thoát
          3. Chạy ACO cho task
          4. Acquire Lock → cập nhật pheromone → Release Lock
          5. Lặp lại

        Đây là nơi thể hiện OS concepts rõ nhất.
        """
        thread_name = threading.current_thread().name
        logger.info(f"[{thread_name}] Bắt đầu chạy...")

        # Tạo ACO instance riêng cho thread này
        aco = ACOAlgorithm(
            graph       = self.graph,
            n_ants      = self.aco_ants,
            n_iterations= self.aco_iters,
        )

        while not self._stop_event.is_set():
            try:
                # ── Rút task từ queue ──
                # timeout=1: Nếu queue rỗng, chờ tối đa 1s rồi kiểm tra stop_event
                task = self.task_queue.get(timeout=1.0)
            except queue.Empty:
                continue   # Queue rỗng → kiểm tra stop_event rồi lặp lại

            # ── Stop signal ──
            if task is _STOP_SIGNAL:
                logger.info(f"[{thread_name}] Nhận stop signal — thoát.")
                self.task_queue.task_done()
                break

            # ── Xử lý task ──
            logger.info(f"[{thread_name}] Nhận task #{task.task_id}: "
                        f"{task.source} → {task.destination}")
            t_start = time.perf_counter()

            # Nếu dùng shared pheromone → load vào ACO trước khi chạy
            if self.shared_pheromone:
                with self.pheromone_lock:
                    aco.pheromone = self.shared_pheromone_arr.copy()

            try:
                result = aco.run(task.source, task.destination)
                success = result.converged
            except Exception as e:
                logger.error(f"[{thread_name}] Lỗi ACO task #{task.task_id}: {e}")
                result  = None
                success = False

            duration = time.perf_counter() - t_start

            if success and result:
                logger.info(
                    f"[{thread_name}] ACO xong task #{task.task_id}: "
                    f"{result.best_cost_ms:.2f}ms | "
                    f"{'→'.join(result.best_path)}"
                )
                # ── Critical Section: Cập nhật shared pheromone ──
                # Chỉ lock khi WRITE — không lock khi tính toán ACO
                if self.shared_pheromone:
                    logger.info(f"[LOCK acquired - {thread_name}] "
                                f"Cập nhật pheromone task #{task.task_id}...")
                    with self.pheromone_lock:
                        # Batch update: cộng dồn pheromone từ thread này
                        delta = aco.Q / result.best_cost_ms
                        path  = result.best_path
                        for k in range(len(path) - 1):
                            i = aco.node_idx.get(path[k],   -1)
                            j = aco.node_idx.get(path[k+1], -1)
                            if i >= 0 and j >= 0:
                                N = aco.N
                                self.shared_pheromone_arr[i * N + j] += delta
                                self.shared_pheromone_arr[j * N + i] += delta
                        # Evaporation toàn bộ
                        self.shared_pheromone_arr *= 0.9
                        self.shared_pheromone_arr = np.clip(
                            self.shared_pheromone_arr, 0.01, 10.0
                        )
                    logger.info(f"[LOCK released - {thread_name}]")
            else:
                logger.warning(f"[{thread_name}] Task #{task.task_id} "
                               f"KHÔNG tìm được đường!")

            # ── Lưu kết quả ──
            routing_result = RoutingResult(
                task        = task,
                result      = result,
                thread_name = thread_name,
                duration_s  = duration,
                success     = success,
            )
            with self.results_lock:
                self.results.append(routing_result)
                if success:
                    self._tasks_completed += 1
                else:
                    self._tasks_failed += 1

            # Callback (cho WebSocket broadcast)
            if self.on_result:
                try:
                    self.on_result(routing_result)
                except Exception:
                    pass

            self.task_queue.task_done()

        logger.info(f"[{thread_name}] Kết thúc.")

    # ── Khởi động / dừng ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Tạo và khởi động N consumer threads."""
        self._stop_event.clear()
        self._threads.clear()

        for i in range(self.n_threads):
            t = threading.Thread(
                target = self._worker,
                args   = (i,),
                name   = f"AntWorker-{i}",
                daemon = True,   # Thread tự chết khi main thread kết thúc
            )
            t.start()
            self._threads.append(t)

        logger.info(f"[ThreadManager] Đã khởi động {self.n_threads} threads")

    def stop(self) -> None:
        """Dừng tất cả thread gracefully bằng cách gửi stop signal."""
        logger.info("[ThreadManager] Gửi stop signal...")
        self._stop_event.set()

        # Gửi N stop signals vào queue (1 cho mỗi thread)
        for _ in range(self.n_threads):
            self.task_queue.put(_STOP_SIGNAL)

        for t in self._threads:
            t.join(timeout=5.0)

        logger.info("[ThreadManager] Tất cả thread đã dừng.")

    def run_and_wait(self) -> List[RoutingResult]:
        """
        Khởi động threads, chờ tất cả task xong, dừng threads.

        Returns:
            Danh sách tất cả RoutingResult
        """
        self.start()

        # Chờ Queue rỗng hoàn toàn (tất cả task_done() được gọi)
        self.task_queue.join()

        self.stop()
        return self.results

    # ── Demo Race Condition (không dùng Lock) ────────────────────────────────

    @staticmethod
    def demo_race_condition() -> None:
        """
        Demo race condition khi KHÔNG dùng Lock.

        Nhiều thread cùng đọc-sửa-ghi 1 biến → kết quả sai.
        Đây là vấn đề mà threading.Lock giải quyết.
        """
        print("\n" + "="*60)
        print("  DEMO RACE CONDITION (không dùng Lock)")
        print("="*60)

        shared_counter = [0]   # Dùng list để truyền by reference
        N_THREADS  = 5
        N_OPS      = 1000
        expected   = N_THREADS * N_OPS

        def unsafe_increment():
            for _ in range(N_OPS):
                # ĐÂY LÀ RACE CONDITION:
                # Thread A đọc counter=100
                # Thread B đọc counter=100
                # Thread A ghi counter=101
                # Thread B ghi counter=101  ← mất 1 lần tăng!
                val = shared_counter[0]
                time.sleep(0)   # yield CPU để tạo race condition
                shared_counter[0] = val + 1

        threads = [threading.Thread(target=unsafe_increment)
                   for _ in range(N_THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()

        print(f"  Kỳ vọng : {expected}")
        print(f"  Thực tế : {shared_counter[0]}")
        print(f"  Mất đi  : {expected - shared_counter[0]} lần tăng!")
        print(f"  → Race condition xảy ra!\n")

        # Bây giờ dùng Lock
        print("  DEMO VỚI LOCK:")
        safe_counter  = [0]
        lock          = threading.Lock()

        def safe_increment():
            for _ in range(N_OPS):
                with lock:   # Chỉ 1 thread được vào tại 1 thời điểm
                    val = safe_counter[0]
                    time.sleep(0)
                    safe_counter[0] = val + 1

        threads = [threading.Thread(target=safe_increment)
                   for _ in range(N_THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()

        print(f"  Kỳ vọng : {expected}")
        print(f"  Thực tế : {safe_counter[0]}")
        print(f"  Mất đi  : {expected - safe_counter[0]} lần tăng!")
        print(f"  → Lock giải quyết race condition!\n")

    # ── Thống kê ─────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Thống kê kết quả sau khi chạy xong."""
        with self.results_lock:
            durations = [r.duration_s for r in self.results if r.success]
            costs     = [r.result.best_cost_ms
                         for r in self.results
                         if r.success and r.result]
        return {
            "submitted"        : self._tasks_submitted,
            "completed"        : self._tasks_completed,
            "failed"           : self._tasks_failed,
            "avg_duration_s"   : float(np.mean(durations)) if durations else 0,
            "avg_cost_ms"      : float(np.mean(costs))     if costs     else 0,
            "min_cost_ms"      : float(np.min(costs))      if costs     else 0,
            "max_cost_ms"      : float(np.max(costs))      if costs     else 0,
        }

    def print_all_results(self) -> None:
        """In bảng kết quả tất cả task."""
        print(f"\n{'='*75}")
        print(f"  KẾT QUẢ TẤT CẢ TASKS")
        print(f"{'='*75}")
        print(f"{'ID':<5} {'Thread':<16} {'Source→Dest':<35} {'Cost':>8} {'OK':>4}")
        print("-"*75)
        with self.results_lock:
            for r in sorted(self.results, key=lambda x: x.task.task_id):
                cost_str = f"{r.result.best_cost_ms:.1f}ms" if r.success and r.result else "FAIL"
                src_dst  = f"{r.task.source}→{r.task.destination}"
                ok_str   = "✓" if r.success else "✗"
                print(f"{r.task.task_id:<5} {r.thread_name:<16} {src_dst:<35} {cost_str:>8} {ok_str:>4}")
        print("-"*75)
        stats = self.get_stats()
        print(f"  Hoàn thành: {stats['completed']}/{stats['submitted']} | "
              f"Avg cost: {stats['avg_cost_ms']:.1f}ms | "
              f"Avg time: {stats['avg_duration_s']*1000:.0f}ms/task")


# ─── Quick demo / self-test ──────────────────────────────────────────────────

def _demo() -> None:
    """Demo nhanh — chạy: python -X utf8 backend/thread_manager.py"""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s %(message)s",
        datefmt= "%H:%M:%S",
    )
    # Tắt log rác từ module con
    logging.getLogger("satellite_tracker").setLevel(logging.WARNING)
    logging.getLogger("topology_manager").setLevel(logging.WARNING)
    logging.getLogger("aco_algorithm").setLevel(logging.WARNING)

    from satellite_tracker import SatelliteTracker
    from topology_manager  import TopologyManager

    tle_path = "data/starlink.tle"
    if not os.path.exists(tle_path):
        print(f"[ERROR] Khong tim thay '{tle_path}'")
        sys.exit(1)

    # ─── 1. Build graph ───────────────────────────────────────────────────────
    tracker = SatelliteTracker(tle_path)
    topo    = TopologyManager(tracker, radius_km=10_000.0)
    topo.build_from_tracker()
    topo.add_ground_station("GND_Da_Nang",   16.0544, 108.2022)
    topo.add_ground_station("GND_Ha_Noi",    21.0285, 105.8542)
    topo.add_ground_station("GND_Bien_Dong", 12.0,    114.0)
    topo.add_ground_station("UAV_1",         14.5,    110.0, alt_km=0.5)
    topo.add_ground_station("SHIP_1",        11.0,    112.0)
    topo.add_ground_station("SHIP_2",        9.0,     115.0)

    G = topo.graph
    # Đảm bảo connected
    if not nx.is_connected(G):
        comps = list(nx.connected_components(G))
        main  = next(iter(max(comps, key=len)))
        for comp in comps:
            node = next(iter(comp))
            if node != main:
                G.add_edge(main, node, weight=50.0,
                           distance_km=15000.0, pheromone=1.0, heuristic=0.02)

    print("=" * 65)
    print("  SAGSINs ACO — ThreadManager Demo")
    print("=" * 65)
    print(f"\nGraph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    # ─── 2. Demo Race Condition ───────────────────────────────────────────────
    ThreadManager.demo_race_condition()

    # ─── 3. Tạo danh sách tasks ───────────────────────────────────────────────
    nodes = list(G.nodes())
    task_pairs = [
        ("GND_Ha_Noi",    "SHIP_1"),
        ("GND_Ha_Noi",    "SHIP_2"),
        ("GND_Da_Nang",   "SHIP_1"),
        ("GND_Da_Nang",   "GND_Bien_Dong"),
        ("GND_Bien_Dong", "SHIP_2"),
        ("UAV_1",         "GND_Ha_Noi"),
        ("UAV_1",         "SHIP_1"),
        ("SHIP_1",        "SHIP_2"),
    ]

    print(f"\n📋 {len(task_pairs)} tasks sẽ được xử lý bởi 3 threads:\n")
    for i, (s, d) in enumerate(task_pairs, 1):
        print(f"  Task #{i}: {s} → {d}")

    # ─── 4. Chạy ThreadManager ────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  BẮT ĐẦU CHẠY ĐA LUỒNG (3 threads)")
    print(f"{'='*65}\n")

    t_global_start = time.perf_counter()

    tm = ThreadManager(
        graph            = G,
        n_threads        = 3,
        aco_ants         = 10,
        aco_iters        = 30,
        shared_pheromone = True,
    )
    tm.submit_tasks(task_pairs)
    results = tm.run_and_wait()

    t_global = time.perf_counter() - t_global_start

    # ─── 5. In kết quả ────────────────────────────────────────────────────────
    tm.print_all_results()

    stats = tm.get_stats()
    print(f"\n📊 Tổng kết:")
    print(f"   Tasks: {stats['completed']}/{stats['submitted']} thành công")
    print(f"   Avg routing cost  : {stats['avg_cost_ms']:.1f} ms")
    print(f"   Tổng thời gian    : {t_global:.2f}s (tuần tự ước tính: {stats['avg_duration_s']*len(task_pairs):.2f}s)")

    speedup = (stats['avg_duration_s'] * len(task_pairs)) / t_global if t_global > 0 else 1
    print(f"   Speedup đa luồng  : ~{speedup:.1f}x")

    print(f"\nThreadManager hoat dong dung!\n")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from topology_manager import TopologyManager
    import networkx as nx
    _demo()
