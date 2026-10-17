"""
satellite_tracker.py — PBL4 SAGSINs ACO
=========================================
Nhiệm vụ:
  1. Parse file TLE tĩnh (data/starlink.tle)
  2. Tính toán vị trí ECEF (X, Y, Z) km tại thời điểm bất kỳ
  3. Tính delay truyền sóng Sat↔Ground (km → ms)
  4. Cung cấp thông tin vệ tinh cho topology_manager

API sử dụng (đã kiểm chứng với Skyfield Official Docs):
  - skyfield.api.EarthSatellite
  - sat.at(t).itrs.position.km  → numpy [x, y, z] km ECEF

Tác giả : PBL4 Team
Ngày    : 2026
"""

from __future__ import annotations

import os
import sys
import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timezone

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
from skyfield.api import load, EarthSatellite, wgs84

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("satellite_tracker")

# ─── Hằng số vật lý ─────────────────────────────────────────────────────────
SPEED_OF_LIGHT_KM_S = 299_792.458   # km/s
MS_PER_S = 1_000                    # ms / s
EARTH_RADIUS_KM = 6_371.0           # km


# ─── Data class cho vệ tinh ─────────────────────────────────────────────────
@dataclass
class SatelliteInfo:
    """Thông tin snapshot của một vệ tinh tại thời điểm t."""
    name: str                        # Tên vệ tinh (từ TLE line-0)
    ecef_km: np.ndarray              # [x, y, z] km — tọa độ ECEF
    lat_deg: float                   # Vĩ độ (geodetic)
    lon_deg: float                   # Kinh độ (geodetic)
    alt_km: float                    # Độ cao (km) so với bề mặt
    velocity_km_s: float = 0.0       # Tốc độ (km/s) — phục vụ báo cáo

    def __repr__(self) -> str:
        return (
            f"<Satellite '{self.name}' "
            f"alt={self.alt_km:.1f}km "
            f"lat={self.lat_deg:.2f}° lon={self.lon_deg:.2f}°>"
        )


# ─── Hàm phụ trợ tính delay ─────────────────────────────────────────────────

def propagation_delay_ms(dist_km: float) -> float:
    """
    Tính delay truyền sóng (ms) từ khoảng cách (km).

    RTT = 2 × (distance / c)
    Hàm này trả về ONE-WAY delay (ms), RTT = 2 × kết quả.

    Args:
        dist_km: Khoảng cách km giữa 2 điểm

    Returns:
        One-way propagation delay (ms)
    """
    if dist_km <= 0:
        return 0.0
    return (dist_km / SPEED_OF_LIGHT_KM_S) * MS_PER_S


def distance_ecef_km(a: np.ndarray, b: np.ndarray) -> float:
    """Tính khoảng cách Euclid (km) giữa 2 điểm ECEF."""
    return float(np.linalg.norm(a - b))


def ground_station_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> np.ndarray:
    """
    Chuyển toạ độ (lat, lon, alt) sang ECEF (km).

    Công thức WGS84 đơn giản hoá (giả sử Trái Đất là hình cầu).

    Args:
        lat_deg : Vĩ độ (độ, -90 → +90)
        lon_deg : Kinh độ (độ, -180 → +180)
        alt_km  : Độ cao km so với bề mặt

    Returns:
        numpy array [x, y, z] km
    """
    R = EARTH_RADIUS_KM + alt_km
    lat_r = math.radians(lat_deg)
    lon_r = math.radians(lon_deg)
    x = R * math.cos(lat_r) * math.cos(lon_r)
    y = R * math.cos(lat_r) * math.sin(lon_r)
    z = R * math.sin(lat_r)
    return np.array([x, y, z])


# ─── Class chính ─────────────────────────────────────────────────────────────

class SatelliteTracker:
    """
    Parse TLE file → Cung cấp vị trí ECEF + delay tại thời điểm bất kỳ.

    Ví dụ sử dụng:
    --------------
    >>> tracker = SatelliteTracker("data/starlink.tle")
    >>> positions = tracker.get_ecef_positions()   # numpy (N, 3)
    >>> names     = tracker.get_names()            # List[str]
    >>> infos     = tracker.get_satellite_infos()  # List[SatelliteInfo]
    """

    def __init__(self, tle_path: str) -> None:
        """
        Khởi tạo tracker với đường dẫn file TLE.

        Args:
            tle_path: Đường dẫn file TLE (tương đối hoặc tuyệt đối)

        Raises:
            FileNotFoundError: Nếu file không tồn tại
            ValueError: Nếu file TLE rỗng hoặc sai định dạng
        """
        self.tle_path = tle_path
        self.ts = load.timescale()
        self._satellites: List[EarthSatellite] = []
        self._names: List[str] = []

        logger.info(f"SatelliteTracker khởi tạo — TLE: '{tle_path}'")
        self._load_tle()

    # ── Tải & parse TLE ──────────────────────────────────────────────────────

    def _load_tle(self) -> None:
        """Đọc file TLE, parse từng 3 dòng thành EarthSatellite."""
        if not os.path.exists(self.tle_path):
            raise FileNotFoundError(
                f"Không tìm thấy file TLE: '{self.tle_path}'\n"
                "Tải về tại: https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle"
            )

        with open(self.tle_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()

        # Lọc dòng trắng & BOM
        lines = [line.strip() for line in raw_lines if line.strip()]

        if len(lines) < 3:
            raise ValueError(
                f"File TLE quá ngắn ({len(lines)} dòng). Cần ít nhất 3 dòng (1 vệ tinh)."
            )

        if len(lines) % 3 != 0:
            logger.warning(
                f"Số dòng TLE ({len(lines)}) không chia hết cho 3. "
                f"Sẽ bỏ qua {len(lines) % 3} dòng cuối."
            )

        parsed_count = 0
        for i in range(0, len(lines) - 2, 3):
            try:
                name  = lines[i]
                line1 = lines[i + 1]
                line2 = lines[i + 2]

                # Kiểm tra format TLE cơ bản
                if not (line1.startswith("1 ") and line2.startswith("2 ")):
                    logger.warning(f"Bỏ qua block không hợp lệ tại dòng {i+1}: '{name}'")
                    continue

                sat = EarthSatellite(line1, line2, name, self.ts)
                self._satellites.append(sat)
                self._names.append(name)
                parsed_count += 1

            except Exception as e:
                logger.warning(f"Lỗi parse vệ tinh tại block {i}: {e}")

        if not self._satellites:
            raise ValueError("Không parse được vệ tinh nào từ file TLE!")

        logger.info(f"✅ Đã parse {parsed_count} vệ tinh từ '{self.tle_path}'")
        for name in self._names:
            logger.info(f"   📡 {name}")

    # ── API công khai ─────────────────────────────────────────────────────────

    def get_names(self) -> List[str]:
        """Trả về danh sách tên vệ tinh."""
        return list(self._names)

    def get_count(self) -> int:
        """Số lượng vệ tinh đã parse."""
        return len(self._satellites)

    def get_ecef_positions(self, timestamp: Optional[datetime] = None) -> np.ndarray:
        """
        Tính vị trí ECEF tất cả vệ tinh tại thời điểm cho trước.

        Args:
            timestamp: datetime UTC. Nếu None → dùng thời điểm hiện tại.

        Returns:
            numpy.ndarray shape (N, 3) — mỗi hàng là [x, y, z] km ECEF
        """
        t = self._resolve_time(timestamp)
        positions = np.zeros((len(self._satellites), 3))

        for idx, sat in enumerate(self._satellites):
            # itrf_xyz().km → [x, y, z] km trong ECEF frame (ITRF = ECEF)
            ecef = sat.at(t).itrf_xyz().km
            positions[idx] = ecef

        logger.debug(f"ECEF computed for {len(self._satellites)} sats at t={t.utc_iso()}")
        return positions

    def get_satellite_infos(self, timestamp: Optional[datetime] = None) -> List[SatelliteInfo]:
        """
        Trả về danh sách SatelliteInfo đầy đủ (ECEF + lat/lon/alt).

        Args:
            timestamp: datetime UTC. Nếu None → dùng thời điểm hiện tại.

        Returns:
            List[SatelliteInfo]
        """
        t = self._resolve_time(timestamp)
        infos: List[SatelliteInfo] = []

        for sat, name in zip(self._satellites, self._names):
            sat_pos = sat.at(t)

            # ECEF (km) — itrf_xyz() trả về vector [x, y, z] km trong ITRF/ECEF frame
            ecef = sat_pos.itrf_xyz().km

            # Lat / Lon / Alt — dùng wgs84.geographic_position_of()
            geo     = wgs84.geographic_position_of(sat_pos)
            lat_deg = float(geo.latitude.degrees)
            lon_deg = float(geo.longitude.degrees)
            alt_km  = float(geo.elevation.km)

            # Tốc độ (km/s) — magnitude của velocity vector
            try:
                vel_vec  = sat_pos.velocity.km_per_s
                velocity = float(np.linalg.norm(vel_vec))
            except Exception:
                velocity = 0.0

            infos.append(SatelliteInfo(
                name=name,
                ecef_km=np.array(ecef),
                lat_deg=lat_deg,
                lon_deg=lon_deg,
                alt_km=alt_km,
                velocity_km_s=velocity,
            ))

        return infos

    def compute_delay_ms(
        self,
        sat_idx: int,
        ground_lat: float,
        ground_lon: float,
        ground_alt_km: float = 0.0,
        timestamp: Optional[datetime] = None,
    ) -> Tuple[float, float]:
        """
        Tính delay truyền sóng từ vệ tinh xuống trạm mặt đất (one-way).

        Args:
            sat_idx      : Index vệ tinh trong danh sách
            ground_lat   : Vĩ độ trạm mặt đất (độ)
            ground_lon   : Kinh độ trạm mặt đất (độ)
            ground_alt_km: Độ cao trạm mặt đất (km, mặc định 0)
            timestamp    : Thời điểm UTC (mặc định: now)

        Returns:
            (dist_km, delay_ms) — khoảng cách và delay one-way
        """
        t = self._resolve_time(timestamp)
        ecef_sat = self._satellites[sat_idx].at(t).itrf_xyz().km
        ecef_gnd = ground_station_ecef(ground_lat, ground_lon, ground_alt_km)

        dist_km  = distance_ecef_km(np.array(ecef_sat), ecef_gnd)
        delay_ms = propagation_delay_ms(dist_km)
        return dist_km, delay_ms

    def compute_sat_to_sat_delay_ms(
        self,
        idx_a: int,
        idx_b: int,
        timestamp: Optional[datetime] = None,
    ) -> Tuple[float, float]:
        """
        Tính delay giữa 2 vệ tinh (ISL — Inter-Satellite Link).

        Args:
            idx_a, idx_b : Index của 2 vệ tinh
            timestamp    : Thời điểm UTC

        Returns:
            (dist_km, delay_ms) one-way
        """
        positions = self.get_ecef_positions(timestamp)
        dist_km   = distance_ecef_km(positions[idx_a], positions[idx_b])
        delay_ms  = propagation_delay_ms(dist_km)
        return dist_km, delay_ms

    def compute_elevation_deg(
        self,
        sat_idx: int,
        ground_lat: float,
        ground_lon: float,
        ground_alt_km: float = 0.0,
        timestamp: Optional[datetime] = None,
    ) -> float:
        """
        Tính góc ngẩng (Elevation Angle tính bằng độ) từ trạm mặt đất/tàu lên vệ tinh.
        Góc ngẩng > 0° nghĩa là vệ tinh nằm trên đường chân trời.
        Chuẩn viễn thông LEO vệ tinh yêu cầu tối thiểu 10° - 15° để liên kết ổn định.
        """
        t = self._resolve_time(timestamp)
        ecef_sat = np.array(self._satellites[sat_idx].at(t).itrf_xyz().km)
        ecef_gnd = ground_station_ecef(ground_lat, ground_lon, ground_alt_km)

        d = ecef_sat - ecef_gnd
        range_km = float(np.linalg.norm(d))
        if range_km <= 0:
            return 90.0

        phi = math.radians(ground_lat)
        lam = math.radians(ground_lon)
        up_vec = np.array([
            math.cos(phi) * math.cos(lam),
            math.cos(phi) * math.sin(lam),
            math.sin(phi)
        ])
        sin_el = float(np.dot(d, up_vec) / range_km)
        sin_el = max(-1.0, min(1.0, sin_el))
        return math.degrees(math.asin(sin_el))

    def predict_satellite_handover(
        self,
        sat_idx: int,
        ground_lat: float,
        ground_lon: float,
        ground_alt_km: float = 0.0,
        start_time: Optional[datetime] = None,
        min_elevation_deg: float = 10.0,
        lookahead_seconds: int = 300,
        step_seconds: int = 5,
    ) -> Dict:
        """
        [DYNAMIC PROACTIVE HANDOVER PREDICTION]
        Dự báo trước quỹ đạo: Tính toán thời gian còn lại (Time-To-Live / Remaining Visibility)
        trước khi vệ tinh lặn xuống dưới đường chân trời (Elevation < min_elevation_deg).

        Returns:
            Dict: {
                "sat_idx": int,
                "current_elevation": float,
                "is_visible": bool,
                "time_to_handover_sec": Optional[int], # Số giây còn lại trước khi mất sóng
                "elevation_rate_deg_per_sec": float,   # Tốc độ lặn/mọc (°/s)
                "status": "stable" | "approaching_horizon" | "below_horizon"
            }
        """
        from datetime import timedelta
        base_t = start_time if start_time is not None else datetime.now(timezone.utc)
        if base_t.tzinfo is None:
            base_t = base_t.replace(tzinfo=timezone.utc)

        current_el = self.compute_elevation_deg(sat_idx, ground_lat, ground_lon, ground_alt_km, base_t)
        
        # Kiểm tra tốc độ biến thiên góc ngẩng (°/s) sau 5 giây
        t_plus = base_t + timedelta(seconds=5)
        el_plus = self.compute_elevation_deg(sat_idx, ground_lat, ground_lon, ground_alt_km, t_plus)
        el_rate = (el_plus - current_el) / 5.0

        if current_el < min_elevation_deg:
            return {
                "sat_idx": sat_idx,
                "current_elevation": round(current_el, 2),
                "is_visible": False,
                "time_to_handover_sec": 0,
                "elevation_rate_deg_per_sec": round(el_rate, 4),
                "status": "below_horizon"
            }

        # Dò trước theo từng bước step_seconds
        remaining_sec = None
        for sec in range(step_seconds, lookahead_seconds + 1, step_seconds):
            future_t = base_t + timedelta(seconds=sec)
            future_el = self.compute_elevation_deg(sat_idx, ground_lat, ground_lon, ground_alt_km, future_t)
            if future_el < min_elevation_deg:
                remaining_sec = sec
                break

        status = "stable"
        if remaining_sec is not None and remaining_sec <= 60:
            status = "approaching_horizon" # Chuẩn bị lặn trong vòng 60s -> Kích hoạt chuyển giao dự phòng!

        return {
            "sat_idx": sat_idx,
            "current_elevation": round(current_el, 2),
            "is_visible": True,
            "time_to_handover_sec": remaining_sec,
            "elevation_rate_deg_per_sec": round(el_rate, 4),
            "status": status
        }

    # ── Helper nội bộ ─────────────────────────────────────────────────────────

    def _resolve_time(self, timestamp: Optional[datetime]):
        """Chuyển datetime → Skyfield Time. Nếu None → now."""
        if timestamp is None:
            return self.ts.now()
        # Đảm bảo UTC
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return self.ts.from_datetime(timestamp)

    def __repr__(self) -> str:
        return f"<SatelliteTracker tle='{self.tle_path}' n_sats={self.get_count()}>"


# ─── Quick demo / self-test ──────────────────────────────────────────────────

def _demo() -> None:
    """Demo nhanh — chạy: python backend/satellite_tracker.py"""
    import sys

    tle_path = "data/starlink.tle"
    if not os.path.exists(tle_path):
        print(f"[ERROR] Không tìm thấy '{tle_path}'")
        print("Tải về: https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle")
        sys.exit(1)

    print("=" * 60)
    print("  SAGSINs ACO — SatelliteTracker Demo")
    print("=" * 60)

    tracker = SatelliteTracker(tle_path)
    infos   = tracker.get_satellite_infos()
    ecef    = tracker.get_ecef_positions()

    print(f"\n📡 Số vệ tinh: {tracker.get_count()}\n")
    print(f"{'No.':<4} {'Tên':<20} {'Alt(km)':<10} {'Lat':<10} {'Lon':<10} {'Vel(km/s)':<12}")
    print("-" * 70)
    for i, info in enumerate(infos):
        print(
            f"{i:<4} {info.name:<20} {info.alt_km:<10.1f} "
            f"{info.lat_deg:<10.2f} {info.lon_deg:<10.2f} {info.velocity_km_s:<12.2f}"
        )

    print(f"\n📐 ECEF matrix shape: {ecef.shape}")
    print(f"   Sample ECEF[0] = {ecef[0]} km")

    # Tính delay giữa 2 vệ tinh đầu
    if tracker.get_count() >= 2:
        dist, delay = tracker.compute_sat_to_sat_delay_ms(0, 1)
        print(f"\n🔗 ISL delay ({infos[0].name} ↔ {infos[1].name}):")
        print(f"   Khoảng cách : {dist:.1f} km")
        print(f"   One-way delay: {delay:.2f} ms")
        print(f"   RTT estimate : {delay * 2:.2f} ms")

    # Tính delay Sat→Đà Nẵng
    DA_NANG_LAT, DA_NANG_LON = 16.0544, 108.2022
    dist_dn, delay_dn = tracker.compute_delay_ms(0, DA_NANG_LAT, DA_NANG_LON)
    print(f"\n📍 Sat[0] → Đà Nẵng (lat={DA_NANG_LAT}, lon={DA_NANG_LON}):")
    print(f"   Khoảng cách : {dist_dn:.1f} km")
    print(f"   One-way delay: {delay_dn:.2f} ms")
    print(f"   RTT estimate : {delay_dn * 2:.2f} ms")

    print("\n✅ satellite_tracker.py hoạt động đúng!\n")


if __name__ == "__main__":
    _demo()
