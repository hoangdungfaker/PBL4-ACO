"""
dummy_client.py — PBL4 SAGSINs ACO
====================================
Nhiệm vụ:
  Mô phỏng một Node khách (Client Node) trong mạng SAGSINs:
  - Gửi yêu cầu định tuyến qua REST API (GET /routing hoặc POST /routing/batch)
  - Kết nối WebSocket (ws://localhost:8000/ws) để nhận dữ liệu streaming telemetry thời gian thực
  - In ra tiến trình tìm đường của bầy kiến qua từng vòng lặp (iteration)

Cách chạy:
  python client_nodes/dummy_client.py
  python client_nodes/dummy_client.py --src GND_Ha_Noi --dst SHIP_1 --ws

Tác giả : PBL4 Team
Năm    : 2026
"""

import sys
import os
import json
import time
import argparse
import urllib.request
import urllib.error

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import websockets
    import asyncio
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


API_BASE = "http://localhost:8000"
WS_URL   = "ws://localhost:8000/ws"


def check_server_health(base_url: str = API_BASE) -> bool:
    """Kiểm tra server FastAPI có đang hoạt động không."""
    try:
        req = urllib.request.Request(f"{base_url}/")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                print(f"✅ Server kết nối thành công: {data.get('name')} (v{data.get('version')})")
                print(f"   Trạng thái: {data.get('status')} | Số node: {data.get('nodes')}")
                return True
    except Exception as e:
        print(f"❌ Không thể kết nối tới server tại {base_url}: {e}")
        print("   Vui lòng khởi động server: uvicorn backend.web_api:app --reload")
        return False
    return False


def request_rest_routing(source: str, destination: str, n_ants: int = 15, n_iter: int = 50, base_url: str = API_BASE):
    """Gửi yêu cầu định tuyến đồng bộ qua REST API."""
    url = f"{base_url}/routing?source={source}&destination={destination}&n_ants={n_ants}&n_iter={n_iter}&broadcast=true"
    print(f"\n📡 [REST Client] Gửi yêu cầu định tuyến: {source} → {destination} (ants={n_ants}, iters={n_iter})...")
    t0 = time.perf_counter()

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            elapsed = (time.perf_counter() - t0) * 1000
            data = json.loads(resp.read().decode("utf-8"))
            print(f"✅ [REST Response {resp.status}] Nhận kết quả sau {elapsed:.1f}ms:")
            print(f"   🛤️  Tuyến đường tối ưu : {' → '.join(data.get('best_path', []))}")
            print(f"   ⏱️  Độ trễ truyền sóng : {data.get('cost_ms'):.2f} ms")
            print(f"   🔄  RTT ước tính       : {data.get('rtt_ms'):.2f} ms")
            print(f"   🦘  Số bước nhảy (Hops): {data.get('n_hops')}")
            print(f"   🎯  Hội tụ tại vòng    : {data.get('convergence_iter')}/{data.get('n_iterations')}")
            return data
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"❌ [Lỗi HTTP {e.code}]: {err_msg}")
    except Exception as e:
        print(f"❌ Lỗi gửi request: {e}")
    return None


async def listen_websocket_stream(source: str, destination: str, ws_url: str = WS_URL):
    """Lắng nghe telemetry bầy kiến thời gian thực qua WebSocket."""
    if not HAS_WEBSOCKETS:
        print("⚠️  Chưa cài đặt thư viện 'websockets'. Chạy: pip install websockets")
        return

    print(f"\n🔌 [WS Client] Đang kết nối tới WebSocket: {ws_url}...")
    try:
        async with websockets.connect(ws_url) as ws:
            print("✅ [WS Client] Đã kết nối thành công! Đang lắng nghe luồng dữ liệu...")

            # Gửi lệnh yêu cầu tìm đường qua WS
            route_cmd = {
                "action": "route",
                "source": source,
                "destination": destination
            }
            await ws.send(json.dumps(route_cmd))
            print(f"📤 [WS Client] Đã gửi lệnh tìm đường: {source} → {destination}\n")

            while True:
                msg_raw = await ws.recv()
                msg = json.loads(msg_raw)
                mtype = msg.get("type")

                if mtype == "CONNECTED":
                    print(f"   [Connected] Server sẵn sàng. Tổng node={len(msg.get('nodes', []))}, cạnh={len(msg.get('edges', []))}")
                elif mtype == "ACO_STARTED":
                    print(f"   🚀 [ACO_STARTED] Server đã bắt đầu giải thuật cho cặp: {msg.get('source')} → {msg.get('destination')}")
                elif mtype == "ACO_UPDATE":
                    it = msg.get("iteration")
                    cost = msg.get("cost_ms")
                    path = " → ".join(msg.get("path", []))
                    print(f"   🐜 [Iter {it:2d}] Tuyến tạm thời ({cost:.2f}ms): {path}")
                elif mtype == "ACO_DONE":
                    print("\n" + "=" * 65)
                    print(f"   🎉 [ACO_DONE] HOÀN TẤT ĐỊNH TUYẾN!")
                    print(f"   Tuyến tốt nhất: {' → '.join(msg.get('path', []))}")
                    print(f"   Độ trễ (Cost) : {msg.get('cost_ms')} ms (RTT: {msg.get('rtt_ms')} ms)")
                    print(f"   Hội tụ tại    : Iteration {msg.get('convergence_iter')}")
                    print("=" * 65 + "\n")
                    break
                elif mtype == "NODE_FAILED":
                    print(f"   💥 [CẢNH BÁO MẠNG] Nút {msg.get('node_id')} vừa bị đánh sập!")
                elif mtype == "NODE_RESTORED":
                    print(f"   🔄 [PHỤC HỒI MẠNG] Nút {msg.get('node_id')} đã hoạt động trở lại.")
                elif mtype == "ERROR":
                    print(f"   ❌ [Lỗi từ Server]: {msg.get('message')}")
                    break

    except Exception as e:
        print(f"❌ [WS Error]: {e}")


def main():
    parser = argparse.ArgumentParser(description="PBL4 SAGSINs ACO Dummy Client")
    parser.add_argument("--src", type=str, default="GND_Ha_Noi", help="Nút nguồn (Source)")
    parser.add_argument("--dst", type=str, default="SHIP_1", help="Nút đích (Destination)")
    parser.add_argument("--ants", type=int, default=15, help="Số kiến mỗi iteration")
    parser.add_argument("--iters", type=int, default=40, help="Số iteration")
    parser.add_argument("--ws", action="store_true", help="Chạy chế độ WebSocket streaming")
    args = parser.parse_args()

    print("=" * 65)
    print("      SAGSINs ACO — CLIENT NODE SIMULATOR (PBL4)      ")
    print("=" * 65)

    if not check_server_health():
        sys.exit(1)

    if args.ws:
        if HAS_WEBSOCKETS:
            asyncio.run(listen_websocket_stream(args.src, args.dst))
        else:
            print("Vui lòng cài đặt: pip install websockets")
            request_rest_routing(args.src, args.dst, args.ants, args.iters)
    else:
        request_rest_routing(args.src, args.dst, args.ants, args.iters)


if __name__ == "__main__":
    main()
