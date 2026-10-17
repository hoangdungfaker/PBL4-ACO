/**
 * network_map.js — PBL4 SAGSINs ACO Frontend
 * ==============================================================
 * Hệ thống hiển thị số kép (Digital Twin):
 *   1. Không gian 3D tương tác (Globe.gl + Three.js):
 *      - Vệ tinh bay lơ lửng ngoài vũ trụ theo độ cao quỹ đạo LEO
 *      - UAV ở tầng đối lưu, Trạm mặt đất & Tàu biển trên bề mặt
 *      - Đường liên kết ISL / GSL không gian dạng 3D Arcs phát sáng
 *      - Hiệu ứng xung tín hiệu (Laser / RF pulses) thời gian thực
 *      - Camera bay tự động, xoay Trái Đất 360 độ
 *   2. Bản đồ 2D chi tiết (Leaflet.js):
 *      - Quan sát tọa độ phẳng, đo khoảng cách, phân tích chi tiết
 *
 * Tác giả : PBL4 Team
 * Năm    : 2026
 * ==============================================================
 */

// ── Cấu hình kết nối ──────────────────────────────────────────
const API_BASE = 'http://localhost:8000';
const WS_URL   = 'ws://localhost:8000/ws';

// ── Trạng thái toàn cục (State) ────────────────────────────────
let activeView       = '3d';        // '3d' | '2d'
let ws               = null;
let globe            = null;        // Instance Globe.gl
let map              = null;        // Instance Leaflet.js
let leafletGroup     = null;        // LayerGroup cho đường ACO trên 2D (chống rò rỉ bộ nhớ)
let leafletNodeMarkers = {};
let leafletEdgeLayers  = {};

let topology         = { nodes: [], edges: [] };
let nodeLookup       = {};          // id -> node object
let bestPathCoords     = [];
let dijkstraPathCoords = [];          // Tọa độ đường đi chuẩn Dijkstra (Màu Vàng)
let isRunning        = false;
let currentIter      = 0;
let totalIter        = 50;
let autoRotate       = true;
let selectedNode     = null;
let failedNodes      = new Set();   // Danh sách các node đang bị sự cố (Fault Tolerance)
let nodeLoads        = {};          // id -> { load, active_conn, capacity, is_congested }
let multiFlows       = [];          // Danh sách các luồng dữ liệu giờ cao điểm đa tuyến
let islDisplayMode   = 'dimmed';    // 'dimmed' (mờ tinh tế) | 'hidden' (ẩn hẳn) | 'full' (đầy đủ)
let labelDisplayMode = 'smart';     // 'smart' (gọn) | 'all' (tất cả) | 'none' (tắt)
let orbitRunning     = true;        // Trạng thái chuyển động quỹ đạo SGP4
let orbitSpeed       = 15;          // Hệ số tua thời gian (1x, 5x, 15x, 30x, 60x)
let cachedHtmlElements = [];        // Mảng đối tượng tham chiếu bền vững cho 3D Globe
let cachedHtmlElementMap = new Map(); // id -> cachedHtmlElement
let cloudsMesh       = null;        // Three.js Mesh cho tầng mây khí quyển
let cloudsEnabled    = true;        // Trạng thái bật/tắt mây
let lastRouteArcUpdate   = 0;       // Timestamp giới hạn tần suất vẽ lại Arcs khi có tuyến đường

// ── Bảng màu chuẩn SAGSINs ────────────────────────────────────
const LAYER_COLORS = {
  satellite: '#38bdf8',  // Xanh dương neon (Space)
  ground   : '#4ade80',  // Xanh lá neon (Ground)
  uav      : '#facc15',  // Vàng cam neon (Air)
  ship     : '#2dd4bf',  // Xanh ngọc neon (Sea)
  unknown  : '#94a3b8',
};

const LAYER_ICONS = {
  satellite: '🛰️',
  ground   : '📡',
  uav      : '✈️',
  ship     : '🚢',
  unknown  : '●',
};

// ── Hàm chuyển đổi độ cao 3D ──────────────────────────────────
function getNodeAltitude(node) {
  if (!node) return 0.005;
  const id = String(node.id || node.name || '');
  const type = node.type || (id.startsWith('SAT_') ? 'satellite' : (id.startsWith('UAV_') ? 'uav' : (id.startsWith('SHIP_') ? 'ship' : 'ground')));
  if (type === 'satellite') {
    // Vệ tinh LEO (300 - 550 km) được ánh xạ lên độ cao quỹ đạo 0.14 ~ 0.18 (bay bổng rõ rệt trong không gian)
    const alt = node.alt_km || 500;
    return 0.14 + Math.min(Math.max((alt - 300) / 300, 0), 1) * 0.04;
  }
  if (type === 'uav') return 0.04; // Tầng không trung (Air)
  return 0.005; // Trạm mặt đất và tàu biển (Ground / Sea)
}

// ── Màu sắc Pheromone ─────────────────────────────────────────
function getPheromoneColor(level) {
  if (level > 0.7) return '#ef4444'; // Đỏ rực — đường tối ưu
  if (level > 0.4) return '#eab308'; // Vàng cam — trung bình
  return '#3b82f6';                 // Xanh dương — thấp
}

function hexToRgba(hex, alpha = 1) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ==============================================================
// 0. MÔ HÌNH VẬT THỂ SVG SCI-FI CHO TỪNG TẦNG SAGSINs (CSS2D)
// ==============================================================
function createNodeHTMLElement(node) {
  const isFailed = failedNodes.has(node.id);
  const isSelected = selectedNode && (selectedNode.id === node.id);
  const isRoute = node.isRoute;
  const isSrc = node.isSrc;
  const isDst = node.isDst;

  const loadData = nodeLoads[node.id] || {};
  const currentLoad = (loadData.load !== undefined) ? loadData.load : (node.load !== undefined ? node.load : 0.10);
  const isCongested = (currentLoad >= 0.75);

  const wrapper = document.createElement('div');
  let extraClasses = '';
  if (isFailed) extraClasses += ' node-failed';
  else if (isCongested) extraClasses += ' node-congested';
  else if (isRoute) extraClasses += ' node-on-route';
  else if (isSelected || isSrc || isDst) extraClasses += ' node-selected';
  if (isSrc) extraClasses += ' node-source';
  if (isDst) extraClasses += ' node-dest';

  wrapper.className = `sagsins-node-marker node-${node.type}${extraClasses}`;
  wrapper.id = `node-marker-${node.id}`;

  const inner = document.createElement('div');
  inner.className = 'marker-inner';
  inner.title = `${node.id} (${node.type.toUpperCase()}) — Click để chọn`;

  if (node.type === 'satellite') {
    // 🛰️ VỆ TINH LEO: Thân kim loại + 2 cánh pin mặt trời (Solar Panels) + Anten ISL
    const bodyColor = isFailed ? '#991b1b' : (isRoute ? '#facc15' : (isSelected ? '#38bdf8' : '#e2e8f0'));
    const panelFill = isFailed ? '#450a0a' : '#1e3a8a';
    const panelStroke = isFailed ? '#ef4444' : '#60a5fa';
    const beaconColor = isFailed ? '#ef4444' : (isRoute ? '#ff1e56' : '#38bdf8');

    inner.innerHTML = `
      <svg viewBox="0 0 66 26" class="satellite-svg">
        <!-- Cánh pin mặt trời trái -->
        <g class="solar-panel">
          <rect x="2" y="5" width="20" height="16" rx="1.5" fill="${panelFill}" stroke="${panelStroke}" stroke-width="0.8"/>
          <line x1="8.5" y1="5" x2="8.5" y2="21" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="15.5" y1="5" x2="15.5" y2="21" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="2" y1="13" x2="22" y2="13" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="22" y1="14" x2="26" y2="14" stroke="#cbd5e1" stroke-width="1.5"/>
        </g>
        <!-- Cánh pin mặt trời phải -->
        <g class="solar-panel">
          <rect x="44" y="5" width="20" height="16" rx="1.5" fill="${panelFill}" stroke="${panelStroke}" stroke-width="0.8"/>
          <line x1="50.5" y1="5" x2="50.5" y2="21" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="57.5" y1="5" x2="57.5" y2="21" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="44" y1="13" x2="64" y2="13" stroke="#93c5fd" stroke-width="0.5" opacity="0.85"/>
          <line x1="40" y1="14" x2="44" y2="14" stroke="#cbd5e1" stroke-width="1.5"/>
        </g>
        <!-- Thân vệ tinh Chassis -->
        <rect x="26" y="6" width="14" height="14" rx="2" fill="${bodyColor}" stroke="#94a3b8" stroke-width="0.9"/>
        <!-- Anten truyền thông vệ tinh hướng xuống Trái Đất -->
        <path d="M 29 20 Q 33 24 37 20 Z" fill="#64748b" stroke="#cbd5e1" stroke-width="0.5"/>
        <line x1="33" y1="20" x2="33" y2="25" stroke="${beaconColor}" stroke-width="1.2"/>
        <circle cx="33" cy="25" r="1.2" fill="${beaconColor}"/>
        <!-- Đèn tín hiệu Beacon viễn thông -->
        <circle cx="33" cy="13" r="2.2" class="sat-beacon" fill="${beaconColor}"/>
      </svg>
    `;
  } else if (node.type === 'ground') {
    // 📡 TRẠM MẶT ĐẤT: Tháp đài viễn thông + Chảo Parabol + Sóng vô tuyến
    const strokeColor = isFailed ? '#ef4444' : (isSrc ? '#4ade80' : '#22c55e');
    inner.innerHTML = `
      <svg viewBox="0 0 36 36" class="ground-svg">
        <polygon points="11,32 25,32 21,21 15,21" fill="#1e293b" stroke="${strokeColor}" stroke-width="0.9"/>
        <line x1="18" y1="21" x2="18" y2="12" stroke="${strokeColor}" stroke-width="2"/>
        <path d="M 8,13 A 11,11 0 0,0 28,13" fill="none" stroke="${strokeColor}" stroke-width="2.4" stroke-linecap="round"/>
        <line x1="18" y1="13" x2="18" y2="6" stroke="#bbf7d0" stroke-width="1.5"/>
        <circle cx="18" cy="6" r="2.5" fill="${strokeColor}"/>
        <circle cx="18" cy="6" r="6" fill="none" stroke="${strokeColor}" class="radio-ping"/>
      </svg>
    `;
  } else if (node.type === 'uav') {
    // ✈️ UAV: Khí tài bay không người lái + 4 Rotor xoay tròn
    const strokeColor = isFailed ? '#ef4444' : (isRoute ? '#facc15' : '#eab308');
    inner.innerHTML = `
      <svg viewBox="0 0 34 34" class="uav-svg">
        <line x1="7" y1="7" x2="27" y2="27" stroke="${strokeColor}" stroke-width="1.8"/>
        <line x1="27" y1="7" x2="7" y2="27" stroke="${strokeColor}" stroke-width="1.8"/>
        <circle cx="7" cy="7" r="4.2" fill="rgba(250,204,21,0.22)" stroke="${strokeColor}" stroke-width="0.8" class="rotor-spin"/>
        <circle cx="27" cy="7" r="4.2" fill="rgba(250,204,21,0.22)" stroke="${strokeColor}" stroke-width="0.8" class="rotor-spin"/>
        <circle cx="7" cy="27" r="4.2" fill="rgba(250,204,21,0.22)" stroke="${strokeColor}" stroke-width="0.8" class="rotor-spin"/>
        <circle cx="27" cy="27" r="4.2" fill="rgba(250,204,21,0.22)" stroke="${strokeColor}" stroke-width="0.8" class="rotor-spin"/>
        <ellipse cx="17" cy="17" rx="5" ry="7" fill="#0f172a" stroke="${strokeColor}" stroke-width="1.2"/>
        <circle cx="17" cy="17" r="2.2" fill="${strokeColor}"/>
      </svg>
    `;
  } else {
    // 🚢 TÀU BIỂN: Thân tàu hàng hải viễn dương + Tháp radar
    const strokeColor = isFailed ? '#ef4444' : (isDst ? '#f87171' : '#2dd4bf');
    inner.innerHTML = `
      <svg viewBox="0 0 38 28" class="ship-svg">
        <path d="M 4,20 L 30,20 L 36,13 L 26,13 L 6,13 Z" fill="#0f766e" stroke="${strokeColor}" stroke-width="1.2"/>
        <rect x="12" y="8" width="10" height="5" fill="#134e4a" stroke="${strokeColor}" stroke-width="0.8"/>
        <line x1="17" y1="8" x2="17" y2="3" stroke="${strokeColor}" stroke-width="1.4"/>
        <circle cx="17" cy="3" r="2" fill="${strokeColor}"/>
      </svg>
    `;
  }

  wrapper.appendChild(inner);

  // Thẻ nhãn thông tin trôi nổi (Hover Tag) — Chỉ hiển thị khi di chuột vào node (hỗ trợ tiếng Việt 100% không lỗi font)
  const tag = document.createElement('div');
  tag.className = 'marker-hover-tag';

  let typeName = 'Vệ tinh LEO';
  let typeIcon = '🛰️';
  let tagColor = '#38bdf8';
  if (node.type === 'ground') { typeName = 'Trạm Mặt Đất'; typeIcon = '📡'; tagColor = '#4ade80'; }
  else if (node.type === 'uav') { typeName = 'Khí tài UAV'; typeIcon = '✈️'; tagColor = '#facc15'; }
  else if (node.type === 'ship') { typeName = 'Tàu Hải Quân'; typeIcon = '🚢'; tagColor = '#2dd4bf'; }

  let roleBadge = '';
  if (isFailed) {
    roleBadge = `<span class="tag-badge badge-failed">⚠️ HỎNG</span>`;
  } else if (isCongested) {
    roleBadge = `<span class="tag-badge badge-congested">🔥 NGHẼN: ${(currentLoad * 100).toFixed(0)}%</span>`;
  } else if (isSrc) {
    roleBadge = `<span class="tag-badge badge-src">🚩 NGUỒN</span>`;
  } else if (isDst) {
    roleBadge = `<span class="tag-badge badge-dst">🎯 ĐÍCH</span>`;
  } else if (isRoute) {
    roleBadge = `<span class="tag-badge badge-route">★ TUYẾN ACO</span>`;
  }

  const altText = node.alt_km > 0 ? ` • ${node.alt_km.toFixed(0)} km` : '';
  const loadColor = isCongested ? '#ef4444' : (currentLoad >= 0.5 ? '#f59e0b' : '#22c55e');
  const loadStatusTxt = isCongested ? 'Nghẽn tải (>75%)' : (currentLoad >= 0.5 ? 'Tải vừa' : 'Ổn định');

  tag.innerHTML = `
    ${roleBadge}
    <span class="tag-title" style="color:${tagColor}">${typeIcon} ${node.name || node.id}</span>
    <span class="tag-sub">${typeName}${altText}</span>
    <div class="tag-load-bar" title="Tải mạng: ${(currentLoad * 100).toFixed(0)}%">
      <div class="tag-load-bar-fill" style="width:${Math.round(currentLoad * 100)}%;background:${loadColor}"></div>
    </div>
    <div style="font-size:9px;color:${isCongested ? '#fca5a5' : '#94a3b8'};margin-top:2px">
      📊 Tải: <b>${(currentLoad * 100).toFixed(0)}%</b> • ${loadStatusTxt}
    </div>
  `;
  wrapper.appendChild(tag);

  // Bắt sự kiện click để mở HUD inspector và chọn nguồn/đích
  wrapper.addEventListener('click', (e) => {
    e.stopPropagation();
    handleNodeClick(node);
  });

  return wrapper;
}

// ==============================================================
// 1. KHỞI TẠO KHÔNG GIAN 3D (GLOBE.GL)
// ==============================================================
function init3DGlobe() {
  const container = document.getElementById('globe-3d');
  if (!container || typeof Globe === 'undefined') {
    log('Globe.gl chưa sẵn sàng hoặc không tìm thấy container', 'warning');
    return;
  }

  log('Đang khởi tạo Quả cầu 3D không gian...', 'info');

  globe = Globe()(container)
    // Bản đồ bề mặt Trái Đất độ phân giải cao & bản đồ độ gồ ghề
    .globeImageUrl('textures/earth-blue-marble.jpg')
    .bumpImageUrl('textures/earth-topology.png')
    .backgroundImageUrl('textures/night-sky.png')
    .showAtmosphere(true)
    .atmosphereColor('#38bdf8')
    .atmosphereAltitude(0.24)

    // Cấu hình Mô hình SVG 3D Nodes (HTML Elements Layer - Vệ tinh bay tự do, không cột chống)
    .htmlElementsData([])
    .htmlLat(d => d.lat)
    .htmlLng(d => d.lon)
    .htmlAltitude(d => d.altitude)
    .htmlElement(d => createNodeHTMLElement(d))
    .htmlTransitionDuration(1000)

    // Cấu hình Sóng Radar (Rings Layer) cho trạm mặt đất / tàu biển
    .ringsData([])
    .ringLat(d => d.lat)
    .ringLng(d => d.lon)
    .ringAltitude(d => d.altitude)
    .ringColor(d => () => d.color)
    .ringMaxRadius(d => d.maxRadius || 3.5)
    .ringPropagationSpeed(d => d.speed || 1.8)
    .ringRepeatPeriod(d => d.period || 1300)

    // Cấu hình Tia Laser Tuyến Đường Không Gian (Arcs Layer) - Bám sát độ cao vệ tinh, không phồng lên không gian
    .arcsData([])
    .arcStartLat(d => d.fromLat)
    .arcStartLng(d => d.fromLon)
    .arcStartAltitude(d => d.fromAlt)
    .arcEndLat(d => d.toLat)
    .arcEndLng(d => d.toLon)
    .arcEndAltitude(d => d.toAlt)
    .arcAltitude(d => d.arcAltitude !== undefined ? d.arcAltitude : (Math.max(d.fromAlt || 0, d.toAlt || 0) + 0.025))
    .arcColor(d => d.color)
    .arcStroke(d => d.stroke || 0.5)
    .arcDashLength(d => d.dashLength !== undefined ? d.dashLength : 0.4)
    .arcDashGap(d => d.dashGap !== undefined ? d.dashGap : 0.15)
    .arcDashAnimateTime(d => d.animateTime !== undefined ? d.animateTime : 2200)

    // Cấu hình Lưới ISL nền toàn mạng không gian (Paths Layer) - Vẽ dạng FatLine siêu nhẹ 60 FPS, không gây giật lag
    .pathsData([])
    .pathPoints(d => d.points)
    .pathPointLat(p => p[0])
    .pathPointLng(p => p[1])
    .pathPointAlt(p => p[2])
    .pathColor(d => d.color || 'rgba(56, 189, 248, 0.12)')
    .pathStroke(d => d.stroke || 0.8)
    .pathDashLength(d => d.dashLength || 1.0)
    .pathDashGap(d => d.dashGap || 0.0)
    .pathDashAnimateTime(d => d.animateTime || 0);

  // Cấu hình xoay camera tự động
  const controls = globe.controls();
  if (controls) {
    controls.autoRotate = autoRotate;
    controls.autoRotateSpeed = 0.5;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
  }

  // Góc nhìn mặc định: Hướng về Biển Đông & Việt Nam
  globe.pointOfView({ lat: 15.0, lng: 112.0, altitude: 2.2 }, 1000);

  // Tạo lớp mây khí quyển chuyển động (Realistic Atmosphere Clouds)
  try {
    const CLOUDS_IMG_URL = 'textures/clouds.png';
    const CLOUDS_ALT = 0.012; // Mây lơ lửng sát tầng đối lưu, thấp hơn quỹ đạo vệ tinh LEO (0.14 - 0.18)
    const globeRadius = globe.getGlobeRadius ? globe.getGlobeRadius() : 100;
    
    if (typeof THREE !== 'undefined') {
      new THREE.TextureLoader().load(CLOUDS_IMG_URL, cloudsTexture => {
        cloudsTexture.wrapS = THREE.RepeatWrapping;
        const cloudsMat = new THREE.MeshLambertMaterial({
          map: cloudsTexture,
          transparent: true,
          opacity: 0.38,
          depthWrite: false
        });
        const cloudsGeom = new THREE.SphereGeometry(globeRadius * (1 + CLOUDS_ALT), 64, 64);
        cloudsMesh = new THREE.Mesh(cloudsGeom, cloudsMat);
        globe.scene().add(cloudsMesh);

        // Vòng lặp xoay mây độc lập tự nhiên
        (function rotateClouds() {
          if (cloudsMesh && cloudsEnabled) {
            cloudsMesh.rotation.y += 0.00028;
          }
          requestAnimationFrame(rotateClouds);
        })();
      });
    }
  } catch (err) {
    console.warn('Không thể khởi tạo lớp mây 3D:', err);
  }

  log('Không gian 3D sẵn sàng ✓', 'success');
}

// ── HTML Tooltip khi hover trên 3D Globe ────────────────────────
function render3DTooltip(node) {
  const isFailed = failedNodes.has(node.id || node.name);
  const icon = isFailed ? '❌' : (LAYER_ICONS[node.type] || '●');
  const color = isFailed ? '#ef4444' : (LAYER_COLORS[node.type] || '#38bdf8');
  const statusHtml = isFailed
    ? `<div style="color:#f87171;font-weight:700;font-size:10px;background:rgba(239,68,68,0.25);padding:2px 4px;border-radius:4px;margin-bottom:4px;border:1px solid #ef4444">⚠️ NÚT ĐANG BỊ SỰ CỐ (FAILED)</div>`
    : `<div style="color:#4ade80;font-size:9px;margin-bottom:2px">● Trạng thái: Hoạt động</div>`;

  return `
    <div style="font-family:'Inter',sans-serif;padding:4px 6px;line-height:1.4">
      <div style="font-weight:700;color:${color};font-size:12px;margin-bottom:2px">
        ${icon} ${node.name} ${isFailed ? '<span style="color:#ef4444">[HỎNG]</span>' : ''}
      </div>
      ${statusHtml}
      <div style="color:#94a3b8;font-size:10px;text-transform:uppercase">
        Tầng: <b>${node.type}</b>
      </div>
      <div style="color:#cbd5e1;font-size:10px;margin-top:2px">
        Lat: ${node.lat.toFixed(2)}° | Lon: ${node.lon.toFixed(2)}°
      </div>
      <div style="color:#cbd5e1;font-size:10px">
        Alt: <b>${(node.alt_km || 0).toFixed(1)} km</b>
      </div>
      <div style="color:${isFailed ? '#fca5a5' : '#38bdf8'};font-size:9px;margin-top:4px">
        👉 Click để xem chi tiết / ${isFailed ? 'khôi phục' : 'đặt tuyến'}
      </div>
    </div>
  `;
}

function updateNodeVisualStates() {
  const selSrc = document.getElementById('sel-source')?.value || '';
  const selDst = document.getElementById('sel-dest')?.value || '';
  const activeRouteNodeSet = new Set();
  if (bestPathCoords && bestPathCoords.length) {
    bestPathCoords.forEach(p => activeRouteNodeSet.add(p.node || p.id));
  }
  if (dijkstraPathCoords && dijkstraPathCoords.length) {
    dijkstraPathCoords.forEach(p => activeRouteNodeSet.add(p.node || p.id));
  }

  if (topology && topology.nodes) {
    topology.nodes.forEach(n => {
      const el = document.getElementById(`node-marker-${n.id}`);
      if (!el) return;
      const isFailed = failedNodes.has(n.id);
      const isSelected = selectedNode && (selectedNode.id === n.id);
      const isRoute = activeRouteNodeSet.has(n.id);
      const isSrc = (n.id === selSrc);
      const isDst = (n.id === selDst);

      el.classList.toggle('node-failed', isFailed);
      el.classList.toggle('node-on-route', isRoute);
      el.classList.toggle('node-selected', isSelected || isSrc || isDst);
      el.classList.toggle('node-source', isSrc);
      el.classList.toggle('node-dest', isDst);
    });
  }
}

// ── Tạo danh sách Radar Rings (Sóng quét vô tuyến & vòng cảnh báo sự cố/nghẽn tải) ──
function buildActiveRings() {
  const rings = [];
  const nodes = topology && topology.nodes ? topology.nodes : [];

  nodes.forEach(n => {
    // Vòng cảnh báo đỏ rực cho các nút bị hỏng
    if (failedNodes.has(n.id)) {
      rings.push({
        lat      : n.lat,
        lon      : n.lon,
        altitude : getNodeAltitude(n),
        color    : 'rgba(239, 68, 68, 0.95)',
        maxRadius: 4.8,
        speed    : 3.2,
        period   : 750,
      });
      return;
    }

    // Vòng cảnh báo cam rực cho các nút đang nghẽn tải cao (>= 75%)
    const nLoad = (nodeLoads[n.id]?.load !== undefined) ? nodeLoads[n.id].load : (n.load || 0.1);
    if (!failedNodes.has(n.id) && nLoad >= 0.75) {
      rings.push({
        lat      : n.lat,
        lon      : n.lon,
        altitude : getNodeAltitude(n),
        color    : 'rgba(249, 115, 22, 0.95)',
        maxRadius: 4.8,
        speed    : 3.0,
        period   : 850,
      });
    }
  });

  return rings;
}

function update3DGlobeData(nodes, edges, activePath = null, dijkstraPath = null) {
  if (!globe) return;

  // Cập nhật lại cache tọa độ nếu có truyền vào
  if (activePath !== null) bestPathCoords = activePath;
  if (dijkstraPath !== null) dijkstraPathCoords = dijkstraPath;

  const selSrc = document.getElementById('sel-source')?.value || '';
  const selDst = document.getElementById('sel-dest')?.value || '';

  // Tập hợp các node nằm trên tuyến đường đang khảo sát (ACO hoặc Dijkstra)
  const activeRouteNodeSet = new Set();
  const aP = activePath || bestPathCoords;
  const dP = dijkstraPath || dijkstraPathCoords;
  if (aP && aP.length) aP.forEach(p => activeRouteNodeSet.add(p.node || p.id));
  if (dP && dP.length) dP.forEach(p => activeRouteNodeSet.add(p.node || p.id));

  // 1. Quản lý danh sách HTML Elements với tham chiếu bền vững (Persistent Objects)
  if (!cachedHtmlElements || cachedHtmlElements.length !== nodes.length) {
    cachedHtmlElements = nodes.map(n => {
      const isRoute = activeRouteNodeSet.has(n.id);
      const isSrc = (n.id === selSrc);
      const isDst = (n.id === selDst);
      return {
        id      : n.id,
        name    : n.id,
        type    : n.type,
        lat     : n.lat,
        lon     : n.lon,
        alt_km  : n.alt_km || 0,
        altitude: getNodeAltitude(n),
        isRoute : isRoute,
        isSrc   : isSrc,
        isDst   : isDst,
      };
    });
    cachedHtmlElementMap = new Map(cachedHtmlElements.map(e => [e.id, e]));
  } else {
    // Tái sử dụng object cũ, chỉ cập nhật thuộc tính để Three-globe tween mượt mà, không hủy DOM
    nodes.forEach(n => {
      const elem = cachedHtmlElementMap.get(n.id);
      if (elem) {
        elem.lat = n.lat;
        elem.lon = n.lon;
        elem.alt_km = n.alt_km || 0;
        elem.altitude = getNodeAltitude(n);
        elem.isRoute = activeRouteNodeSet.has(n.id);
        elem.isSrc = (n.id === selSrc);
        elem.isDst = (n.id === selDst);
      }
    });
  }

  // Cập nhật trạng thái CSS classes trực tiếp trên các node DOM
  updateNodeVisualStates();

  // 2. Tạo danh sách Radar Rings (Sóng quét vô tuyến & vòng cảnh báo sự cố/nghẽn tải)
  const rings = buildActiveRings();

  // 3. Cập nhật các tia laser tuyến đường (Chỉ gồm 4-12 arcs, cực nhẹ, cập nhật tức thì <0.1ms)
  const activeArcs = getAllActiveRouteArcs();

  globe
    .htmlElementsData(cachedHtmlElements)
    .ringsData(rings)
    .arcsData(activeArcs);
}

// ==============================================================
// 1b. HỆ THỐNG TIA LASER TUYẾN ĐƯỜNG ĐỊNH HƯỚNG & KHÔNG GIẬT LAG
// ==============================================================

/**
 * Xây dựng danh sách tia laser cho một lộ trình tuần tự (Path).
 * Đảm bảo:
 * - Tia laser đi theo đúng chiều từ Nguồn -> Trung gian -> Đích.
 * - Điểm đầu và điểm cuối chạm chính xác 100% vào tâm node vệ tinh/trạm.
 * - Độ cao tia laser bám sát mặt cầu quỹ đạo (arcAltitude thấp, không phồng lên không gian).
 * - Kết hợp 2 lớp: Tia lõi liên tục (Solid Core) + Xung dữ liệu ánh sáng (Directional Pulse).
 */
function buildRouteArcsForPath(pathCoords, mainColor, coreColor) {
  const arcs = [];
  if (!pathCoords || pathCoords.length < 2) return arcs;

  for (let i = 0; i < pathCoords.length - 1; i++) {
    const u = pathCoords[i];
    const v = pathCoords[i + 1];
    const uId = u.node || u.id;
    const vId = v.node || v.id;
    if (!uId || !vId || failedNodes.has(uId) || failedNodes.has(vId)) continue;

    const uElem = cachedHtmlElementMap ? cachedHtmlElementMap.get(uId) : null;
    const vElem = cachedHtmlElementMap ? cachedHtmlElementMap.get(vId) : null;
    const uObj = uElem || nodeLookup[uId] || u;
    const vObj = vElem || nodeLookup[vId] || v;

    const fromAlt = uElem ? uElem.altitude : getNodeAltitude(uObj);
    const toAlt   = vElem ? vElem.altitude : getNodeAltitude(vObj);
    const maxAlt  = Math.max(fromAlt, toAlt);
    // Độ cong bám sát độ cao quỹ đạo: arcAltitude phải lớn hơn maxAlt để cung uốn nhẹ phía trên
    const arcAlt  = maxAlt + 0.025;

    // Lớp 1: Tia laser liên tục nền (Solid Laser Core) - Đảm bảo đường nối luôn liền mạch, chạm chính xác tâm node
    arcs.push({
      fromLat    : uObj.lat,
      fromLon    : uObj.lon,
      fromAlt    : fromAlt,
      toLat      : vObj.lat,
      toLon      : vObj.lon,
      toAlt      : toAlt,
      arcAltitude: arcAlt,
      color      : [coreColor, coreColor],
      stroke     : 0.40,
      dashLength : 1.0,
      dashGap    : 0.0,
      animateTime: 0,
    });

    // Lớp 2: Luồng xung dữ liệu ánh sáng định hướng (Animated Directional Laser Pulse) - Bay tuần tự từ u -> v
    arcs.push({
      fromLat    : uObj.lat,
      fromLon    : uObj.lon,
      fromAlt    : fromAlt,
      toLat      : vObj.lat,
      toLon      : vObj.lon,
      toAlt      : toAlt,
      arcAltitude: arcAlt,
      color      : ['#ffffff', mainColor],
      stroke     : 0.65,
      dashLength : 0.38,
      dashGap    : 0.15,
      animateTime: 950,
    });
  }

  return arcs;
}

/**
 * Tổng hợp toàn bộ các tia laser của các tuyến đang hiển thị (ACO, Dijkstra, Multi-flows).
 * Tổng số tia chỉ từ 4 đến 12 tia (thay vì 285 cạnh nền) => Render cực nhanh 0.1ms, triệt tiêu 100% giật lag!
 */
function getAllActiveRouteArcs() {
  const allArcs = [];

  // 1. Tuyến đường tối ưu ACO (Đỏ Laser rực rỡ)
  if (bestPathCoords && bestPathCoords.length >= 2) {
    const acoArcs = buildRouteArcsForPath(bestPathCoords, '#ff1e56', 'rgba(255, 30, 86, 0.45)');
    allArcs.push(...acoArcs);
  }

  // 2. Tuyến đường Dijkstra đối chứng (Vàng Neon)
  if (dijkstraPathCoords && dijkstraPathCoords.length >= 2) {
    const dijkArcs = buildRouteArcsForPath(dijkstraPathCoords, '#facc15', 'rgba(250, 204, 21, 0.45)');
    allArcs.push(...dijkArcs);
  }

  // 3. Các luồng Giờ Cao Điểm đa tuyến (Multi-Flows)
  if (multiFlows && multiFlows.length > 0) {
    multiFlows.forEach(fl => {
      const flCoords = fl.path_coords || fl.coords;
      if (flCoords && flCoords.length >= 2) {
        const c = fl.color || '#38bdf8';
        const mfArcs = buildRouteArcsForPath(flCoords, c, hexToRgba(c, 0.45));
        allArcs.push(...mfArcs);
      }
    });
  }

  return allArcs;
}

/**
 * Cập nhật Lưới liên kết ISL nền toàn mạng bằng PathsData (Line2 FatLines siêu tốc).
 * Chỉ chạy 1 lần khi load topology hoặc khi người dùng bấm đổi chế độ Lưới ISL.
 */
function updateBackgroundISLPaths(edges) {
  if (!globe) return;
  if (islDisplayMode === 'hidden' || !edges || edges.length === 0) {
    globe.pathsData([]);
    return;
  }

  const paths = [];
  const isFull = (islDisplayMode === 'full');
  edges.forEach(e => {
    if (failedNodes.has(e.from) || failedNodes.has(e.to)) return;
    const aElem = cachedHtmlElementMap ? cachedHtmlElementMap.get(e.from) : null;
    const bElem = cachedHtmlElementMap ? cachedHtmlElementMap.get(e.to) : null;
    const a = aElem || nodeLookup[e.from];
    const b = bElem || nodeLookup[e.to];
    if (!a || !b) return;

    const norm = Math.min((e.pheromone || 1.0) / 10.0, 1.0);
    const color = isFull
      ? getPheromoneColor(norm)
      : 'rgba(56, 189, 248, 0.14)';

    const aAlt = aElem ? aElem.altitude : getNodeAltitude(a);
    const bAlt = bElem ? bElem.altitude : getNodeAltitude(b);

    paths.push({
      points: [
        [a.lat, a.lon, aAlt],
        [b.lat, b.lon, bAlt]
      ],
      color : color,
      stroke: isFull ? 1.0 : 0.65,
    });
  });

  globe.pathsData(paths);
}

// ── Hàm chuyển đổi chế độ Lưới ISL & Nhãn chữ ─────────────────
function toggleISLDisplay() {
  const btn = document.getElementById('btn-toggle-isl');
  if (islDisplayMode === 'dimmed') {
    islDisplayMode = 'hidden';
    if (btn) btn.innerHTML = '🔗 Lưới ISL: Ẩn';
    log('3D: Đã ẩn lưới liên kết ISL nền', 'info');
  } else if (islDisplayMode === 'hidden') {
    islDisplayMode = 'full';
    if (btn) btn.innerHTML = '🔗 Lưới ISL: Đầy đủ';
    log('3D: Đã bật toàn bộ hiệu ứng lưới ISL', 'info');
  } else {
    islDisplayMode = 'dimmed';
    if (btn) btn.innerHTML = '🔗 Lưới ISL: Mờ';
    log('3D: Đã đặt lưới ISL chế độ mờ tinh tế', 'info');
  }
  updateBackgroundISLPaths(topology.edges);
}

function toggleLabelDisplay() {
  const btn = document.getElementById('btn-toggle-labels');
  if (labelDisplayMode === 'smart') {
    labelDisplayMode = 'all';
    if (btn) btn.innerHTML = '🏷️ Nhãn: Tất cả';
    log('3D: Hiển thị toàn bộ nhãn tên vệ tinh & trạm', 'info');
  } else if (labelDisplayMode === 'all') {
    labelDisplayMode = 'none';
    if (btn) btn.innerHTML = '🏷️ Nhãn: Tắt';
    log('3D: Đã tắt toàn bộ nhãn trôi nổi', 'info');
  } else {
    labelDisplayMode = 'smart';
    if (btn) btn.innerHTML = '🏷️ Nhãn: Gọn';
    log('3D: Hiển thị nhãn thông minh (chỉ trạm & tuyến chọn)', 'info');
  }
  update3DGlobeData(topology.nodes, topology.edges);
}

// ==============================================================
// 2. KHỞI TẠO BẢN ĐỒ 2D (LEAFLET.JS)
// ==============================================================
function init2DMap() {
  map = L.map('map', {
    center            : [15, 112],
    zoom              : 4,
    zoomControl       : true,
    attributionControl: false,
  });

  // 1. Lớp Ảnh vệ tinh thực tế (Đại dương xanh tự nhiên + địa hình lục địa thực tế)
  const satelliteBase = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom    : 18,
    attribution: 'Esri World Imagery',
  });

  // 2. Lớp Bản đồ Đại dương xanh (Ocean Blue Basemap — màu xanh lam biển sâu cho hàng hải)
  const oceanBase = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}', {
    maxZoom    : 16,
    attribution: 'Esri Ocean Basemap',
  });

  // 3. Lớp Bản đồ Địa hình tự nhiên (World Topo Map)
  const topoBase = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
    maxZoom    : 18,
    attribution: 'Esri World Topo',
  });

  // Lớp phủ nhãn tên quốc gia & biên giới địa danh
  const boundariesOverlay = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 18,
    opacity: 0.85,
  });

  // Mặc định chọn Ảnh vệ tinh màu xanh thực tế + Nhãn địa danh
  satelliteBase.addTo(map);
  boundariesOverlay.addTo(map);

  // Bộ chọn lớp bản đồ (Base Map Switcher) ở góc trên bên phải
  const baseMaps = {
    "🛰️ Ảnh vệ tinh thực tế": satelliteBase,
    "🌊 Đại dương xanh (Ocean)": oceanBase,
    "🗺️ Địa hình tự nhiên (Topo)": topoBase,
  };
  const overlayMaps = {
    "🏷️ Tên quốc gia & Biên giới": boundariesOverlay,
  };
  L.control.layers(baseMaps, overlayMaps, { position: 'topright' }).addTo(map);

  L.control.attribution({ prefix: false })
    .addTo(map)
    .setPrefix('PBL4 SAGSINs ACO');

  // LayerGroup quản lý các đường vẽ ACO để dọn dẹp sạch sẽ
  leafletGroup = L.layerGroup().addTo(map);
}

function getNodeIcon2D(type, isFailed = false) {
  if (isFailed) {
    return L.divIcon({
      className : '',
      iconSize  : [32, 32],
      iconAnchor: [16, 16],
      html: `
        <div style="
          width:32px; height:32px;
          background: rgba(185, 28, 28, 0.85);
          border: 2px solid #ef4444;
          border-radius: 50%;
          display: flex; align-items: center; justify-content: center;
          font-size: 16px;
          box-shadow: 0 0 14px #ef4444;
          animation: pulse 1s infinite alternate;
        ">❌</div>`,
    });
  }

  const cfg = {
    satellite: { emoji: '🛰️', color: '#38bdf8', size: 28 },
    ground   : { emoji: '📡', color: '#4ade80', size: 30 },
    uav      : { emoji: '✈️', color: '#facc15', size: 26 },
    ship     : { emoji: '🚢', color: '#2dd4bf', size: 28 },
    unknown  : { emoji: '●',  color: '#94a3b8', size: 22 },
  }[type] || { emoji: '●', color: '#94a3b8', size: 22 };

  return L.divIcon({
    className : '',
    iconSize  : [cfg.size, cfg.size],
    iconAnchor: [cfg.size / 2, cfg.size / 2],
    html: `
      <div style="
        width:${cfg.size}px; height:${cfg.size}px;
        background: rgba(15, 23, 42, 0.7);
        border: 2px solid ${cfg.color};
        border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: ${cfg.size * 0.5}px;
        box-shadow: 0 0 10px ${cfg.color};
      ">${cfg.emoji}</div>`,
  });
}

function draw2DTopology(nodes, edges) {
  // Xóa sạch layers cũ
  Object.values(leafletNodeMarkers).forEach(m => m.remove());
  Object.values(leafletEdgeLayers).forEach(l => l.remove());
  leafletNodeMarkers = {};
  leafletEdgeLayers  = {};

  const coords = {};
  nodes.forEach(n => { coords[n.id] = [n.lat, n.lon]; });

  // Vẽ các cạnh (Edges) - ẩn cạnh liên quan đến nút bị hỏng
  edges.forEach(e => {
    if (failedNodes.has(e.from) || failedNodes.has(e.to)) return;
    const a = coords[e.from];
    const b = coords[e.to];
    if (!a || !b) return;

    const norm  = Math.min((e.pheromone || 1.0) / 10.0, 1.0);
    const color = getPheromoneColor(norm);

    const line = L.polyline([a, b], {
      color    : color,
      weight   : 1 + norm * 3,
      opacity  : 0.35 + norm * 0.4,
      dashArray: '4 6',
    }).addTo(map);

    line.bindTooltip(
      `<b>${e.from} ↔ ${e.to}</b><br>` +
      `Trễ: ${(e.delay_ms || 0).toFixed(1)} ms<br>` +
      `Khoảng cách: ${(e.dist_km || 0).toFixed(0)} km`,
      { sticky: true, className: 'leaflet-tooltip' }
    );

    leafletEdgeLayers[`${e.from}|${e.to}`] = line;
  });

  // Vẽ các nút (Nodes)
  nodes.forEach(n => {
    if (!n.lat && !n.lon) return;
    const isFailed = failedNodes.has(n.id);

    const marker = L.marker([n.lat, n.lon], {
      icon: getNodeIcon2D(n.type, isFailed),
      zIndexOffset: isFailed ? 200 : (n.type === 'satellite' ? 50 : 100),
    }).addTo(map);

    const typeLabel = {
      satellite: '🛰️ Vệ tinh Không gian',
      ground   : '📡 Trạm mặt đất',
      uav      : '✈️ Thiết bị bay UAV',
      ship     : '🚢 Tàu hàng hải',
    }[n.type] || n.type;

    const icon = isFailed ? '❌' : (LAYER_ICONS[n.type] || '●');

    marker.bindPopup(`
      <div style="min-width:200px;font-family:'Inter',sans-serif;padding:2px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:22px">${icon}</span>
          <div>
            <b style="color:${isFailed ? '#ef4444' : '#38bdf8'};font-size:13px">${n.id} ${isFailed ? '[HỎNG]' : ''}</b>
            <div style="font-size:10px;color:#94a3b8">${typeLabel}</div>
          </div>
        </div>
        ${isFailed ? '<div style="background:rgba(239,68,68,0.25);border:1px solid #ef4444;color:#fca5a5;padding:4px 6px;border-radius:4px;font-size:10px;font-weight:700;margin-bottom:6px;text-align:center">⚠️ NÚT ĐANG BỊ SỰ CỐ (FAILED)</div>' : ''}
        <div style="font-size:11px;color:#cbd5e1;line-height:1.5;margin-bottom:10px;background:rgba(0,0,0,0.3);padding:6px;border-radius:6px;border:1px solid rgba(255,255,255,0.06)">
          <div>Tọa độ: <b>${n.lat.toFixed(2)}°, ${n.lon.toFixed(2)}°</b></div>
          <div>Độ cao: <b>${(n.alt_km || 0).toFixed(1)} km</b></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
          <button onclick="window.setNodeAsSource('${n.id}')"
            style="padding:6px 4px;background:rgba(74,222,128,0.2);border:1px solid #4ade80;color:#4ade80;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;transition:all 0.2s">
            📍 Đặt Nguồn
          </button>
          <button onclick="window.setNodeAsDest('${n.id}')"
            style="padding:6px 4px;background:rgba(239,68,68,0.2);border:1px solid #ef4444;color:#ef4444;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;transition:all 0.2s">
            🎯 Đặt Đích
          </button>
        </div>
        <button onclick="window.toggleNodeFailure('${n.id}')"
          style="margin-top:6px;width:100%;padding:6px 4px;background:${isFailed ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'};border:1px solid ${isFailed ? '#22c55e' : '#ef4444'};color:${isFailed ? '#86efac' : '#fca5a5'};border-radius:6px;font-size:11px;font-weight:600;cursor:pointer">
          ${isFailed ? '🟢 Khôi Phục Nút' : '💥 Đánh Sập Nút'}
        </button>
      </div>
    `);

    marker.on('click', () => {
      handleNodeClick(n);
    });

    leafletNodeMarkers[n.id] = marker;
  });

  update2DSelectionMarkers();
}

function draw2DBestPath(pathCoords, dijkstraCoords = null, autoFit = false) {
  if (!leafletGroup) return;
  leafletGroup.clearLayers(); // Dọn dẹp an toàn không để lại vết

  const aCoords = pathCoords || bestPathCoords;
  const dCoords = dijkstraCoords !== null ? dijkstraCoords : dijkstraPathCoords;

  let bounds = null;

  // 1. Vẽ đường Dijkstra MÀU VÀNG (Nếu có)
  if (dCoords && dCoords.length >= 2) {
    const dijkLatLngs = dCoords
      .filter(p => p.lat !== undefined && p.lon !== undefined)
      .map(p => [p.lat, p.lon]);

    if (dijkLatLngs.length >= 2) {
      // Đường chính vàng nét đứt thanh mảnh
      const dijkLine = L.polyline(dijkLatLngs, {
        color    : '#facc15',
        weight   : 2.5,
        opacity  : 0.95,
        dashArray: '6, 6',
        lineCap  : 'round',
      });
      dijkLine.bindTooltip('📐 Tuyến đường Dijkstra (Tối ưu chuẩn — Màu Vàng)', { sticky: true });
      leafletGroup.addLayer(dijkLine);

      // Thêm mũi tên chỉ hướng vàng ▶ ▶ ▶
      if (typeof L.polylineDecorator === 'function') {
        try {
          const dijkDecorator = L.polylineDecorator(dijkLine, {
            patterns: [
              {
                offset: '25px',
                repeat: '60px',
                symbol: L.Symbol.arrowHead({
                  pixelSize: 8,
                  polygon: false,
                  pathOptions: { stroke: true, color: '#facc15', weight: 2.0, opacity: 0.9 }
                })
              }
            ]
          });
          leafletGroup.addLayer(dijkDecorator);
        } catch (e) {
          console.warn('Lỗi decorator Dijkstra:', e);
        }
      }

      bounds = dijkLine.getBounds();
    }
  }

  // 2. Vẽ đường ACO MÀU ĐỎ (Nếu có)
  if (aCoords && aCoords.length >= 2) {
    const acoLatLngs = aCoords
      .filter(p => p.lat !== undefined && p.lon !== undefined)
      .map(p => [p.lat, p.lon]);

    if (acoLatLngs.length >= 2) {
      // Đường chính đỏ laser thanh mảnh
      const mainLine = L.polyline(acoLatLngs, {
        color  : '#ef4444',
        weight : 2.5,
        opacity: 0.95,
        lineCap: 'round',
      });
      mainLine.bindTooltip('🐜 Tuyến đường ACO (Bầy kiến — Màu Đỏ)', { sticky: true });
      leafletGroup.addLayer(mainLine);

      // Thêm mũi tên chỉ hướng đỏ ▶ ▶ ▶ từ Nguồn đến Đích
      if (typeof L.polylineDecorator === 'function') {
        try {
          const arrowDecorator = L.polylineDecorator(mainLine, {
            patterns: [
              {
                offset: '30px',
                repeat: '60px',
                symbol: L.Symbol.arrowHead({
                  pixelSize: 9,
                  polygon: false,
                  pathOptions: { stroke: true, color: '#ef4444', weight: 2.4, opacity: 0.95 }
                })
              }
            ]
          });
          leafletGroup.addLayer(arrowDecorator);
        } catch (e) {
          console.warn('Lỗi decorator ACO:', e);
        }
      }

      bounds = bounds ? bounds.extend(mainLine.getBounds()) : mainLine.getBounds();
    }
  }

  // 3. Vẽ các luồng Giờ Cao Điểm đa tuyến (Multi-Flows) trên 2D
  if (multiFlows && multiFlows.length > 0) {
    multiFlows.forEach((fl, idx) => {
      const flCoords = fl.path_coords || fl.coords;
      if (flCoords && flCoords.length >= 2) {
        const latlngs = flCoords
          .filter(p => p.lat !== undefined && p.lon !== undefined)
          .map(p => [p.lat, p.lon]);
        if (latlngs.length >= 2) {
          const color = fl.color || '#38bdf8';
          const line = L.polyline(latlngs, {
            color  : color,
            weight : 2.5,
            opacity: 0.95,
            lineCap: 'round',
          });
          line.bindTooltip(`⚡ ${fl.name} (${fl.hops} hops | ${fl.cost_ms}ms)`, { sticky: true });
          leafletGroup.addLayer(line);

          if (typeof L.polylineDecorator === 'function') {
            try {
              const flDecorator = L.polylineDecorator(line, {
                patterns: [
                  {
                    offset: '25px',
                    repeat: '60px',
                    symbol: L.Symbol.arrowHead({
                      pixelSize: 8,
                      polygon: false,
                      pathOptions: { stroke: true, color: color, weight: 2.0, opacity: 0.9 }
                    })
                  }
                ]
              });
              leafletGroup.addLayer(flDecorator);
            } catch (e) {
              console.warn('Lỗi decorator MultiFlow:', e);
            }
          }

          bounds = bounds ? bounds.extend(line.getBounds()) : line.getBounds();
        }
      }
    });
  }

  // Zoom bản đồ vừa vặn với các tuyến đường (chỉ khi có yêu cầu autoFit, tránh rung màn hình khi orbit tick)
  if (autoFit && bounds && map) {
    map.fitBounds(bounds.pad(0.2), { animate: true, duration: 0.8 });
  }
}

// ==============================================================
// 3. ĐIỀU KHIỂN & CHUYỂN ĐỔI CHẾ ĐỘ XEM (3D / 2D)
// ==============================================================
function setViewMode(mode) {
  activeView = mode;
  const btn3D = document.getElementById('btn-mode-3d');
  const btn2D = document.getElementById('btn-mode-2d');
  const pnl3D = document.getElementById('globe-3d');
  const pnl2D = document.getElementById('map');
  const ovlMode = document.getElementById('ovl-mode');

  if (mode === '3d') {
    btn3D.classList.add('active');
    btn2D.classList.remove('active');
    pnl3D.classList.add('active');
    pnl2D.classList.remove('active');
    ovlMode.textContent = '3D Globe';
    document.getElementById('toolbar-3d').style.display = 'flex';

    if (globe) {
      globe.width(pnl3D.clientWidth);
      globe.height(pnl3D.clientHeight);
    }
    log('Đã chuyển sang chế độ Không gian 3D', 'info');
  } else {
    btn2D.classList.add('active');
    btn3D.classList.remove('active');
    pnl2D.classList.add('active');
    pnl3D.classList.remove('active');
    ovlMode.textContent = '2D Map';
    document.getElementById('toolbar-3d').style.display = 'none';

    setTimeout(() => {
      if (map) map.invalidateSize();
    }, 50);
    log('Đã chuyển sang chế độ Bản đồ 2D', 'info');
  }
}

// ── Các phím tắt Camera 3D ─────────────────────────────────────
function focusVietnam() {
  if (globe) {
    const curPov = globe.pointOfView ? globe.pointOfView() : null;
    globe.pointOfView({
      lat: 15.0,
      lng: 112.0,
      altitude: curPov ? curPov.altitude : 2.0
    }, 1000);
  }
}

function focusGlobal() {
  if (globe) {
    globe.pointOfView({ lat: 15.0, lng: 110.0, altitude: 3.2 }, 1200);
  }
}

function focusRoute() {
  if (!bestPathCoords || bestPathCoords.length === 0) {
    log('Chưa có tuyến đường để khóa camera!', 'warning');
    return;
  }
  const lats = bestPathCoords.map(p => p.lat).filter(l => l !== undefined);
  const lons = bestPathCoords.map(p => p.lon).filter(l => l !== undefined);
  if (lats.length === 0) return;

  const centerLat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const centerLon = (Math.min(...lons) + Math.max(...lons)) / 2;

  if (activeView === '3d' && globe) {
    // Bảo toàn độ cao zoom (altitude) hiện tại của người dùng, KHÔNG tự ý phóng to hay thu nhỏ!
    const curPov = globe.pointOfView ? globe.pointOfView() : null;
    globe.pointOfView({
      lat: centerLat,
      lng: centerLon,
      altitude: curPov ? curPov.altitude : 2.2
    }, 1000);
    log('3D: Đã định tâm camera vào lộ trình (bảo toàn mức zoom)', 'info');
  } else if (activeView === '2d' && map) {
    const aCoords = bestPathCoords.filter(p => p.lat !== undefined && p.lon !== undefined);
    if (aCoords.length >= 2) {
      const bounds = L.latLngBounds(aCoords.map(p => [p.lat, p.lon]));
      map.fitBounds(bounds.pad(0.2), { animate: true, duration: 0.8 });
    }
  }
}

function toggleAutoRotate() {
  autoRotate = !autoRotate;
  const btn = document.getElementById('btn-toggle-rotate');
  if (globe && globe.controls()) {
    globe.controls().autoRotate = autoRotate;
  }
  if (autoRotate) {
    btn.classList.add('active');
    btn.textContent = '🔄 Tự xoay: Bật';
  } else {
    btn.classList.remove('active');
    btn.textContent = '⏸️ Tự xoay: Tắt';
  }
}

function toggleClouds() {
  cloudsEnabled = !cloudsEnabled;
  const btn = document.getElementById('btn-toggle-clouds');
  if (cloudsMesh) {
    cloudsMesh.visible = cloudsEnabled;
  }
  if (btn) {
    if (cloudsEnabled) {
      btn.classList.add('active');
      btn.textContent = '☁️ Mây: Bật';
    } else {
      btn.classList.remove('active');
      btn.textContent = '☁️ Mây: Tắt';
    }
  }
  log(cloudsEnabled ? '☁️ Đã bật tầng mây khí quyển' : '☁️ Đã tắt tầng mây khí quyển', 'info');
}

// ── Điều khiển chuyển động Quỹ đạo vệ tinh SGP4 ─────────────
const ORBIT_SPEED_STEPS = [1, 5, 15, 30, 60];

function toggleOrbitMotion() {
  orbitRunning = !orbitRunning;
  updateOrbitControlsUI();
  sendOrbitControl(orbitRunning, orbitSpeed);
  log(orbitRunning ? '▶️ Tiếp tục chuyển động quỹ đạo vệ tinh' : '⏸️ Đã tạm dừng chuyển động quỹ đạo vệ tinh', 'info');
}

function cycleOrbitSpeed() {
  const currentIdx = ORBIT_SPEED_STEPS.indexOf(orbitSpeed);
  const nextIdx = (currentIdx + 1) % ORBIT_SPEED_STEPS.length;
  orbitSpeed = ORBIT_SPEED_STEPS[nextIdx];
  updateOrbitControlsUI();
  sendOrbitControl(orbitRunning, orbitSpeed);
  log(`⚡ Tốc độ mô phỏng quỹ đạo: ${orbitSpeed}x`, 'info');
}

function updateOrbitControlsUI() {
  const btnToggle  = document.getElementById('btn-toggle-orbit');
  const btnSpeed   = document.getElementById('btn-orbit-speed');
  const clockBadge = document.getElementById('orbit-sim-clock');

  if (btnToggle) {
    if (orbitRunning) {
      btnToggle.classList.add('active');
      btnToggle.textContent = '⏸️ Quỹ đạo: Chạy';
    } else {
      btnToggle.classList.remove('active');
      btnToggle.textContent = '▶️ Quỹ đạo: Tạm dừng';
    }
  }

  if (btnSpeed) {
    btnSpeed.textContent = `⚡ ${orbitSpeed}x`;
    if (orbitSpeed > 1) {
      btnSpeed.classList.add('active');
    } else {
      btnSpeed.classList.remove('active');
    }
  }

  if (clockBadge) {
    if (orbitRunning) {
      clockBadge.classList.remove('paused');
    } else {
      clockBadge.classList.add('paused');
    }
  }
}

function sendOrbitControl(running, speed) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      action : 'orbit_control',
      running: running,
      speed  : speed,
    }));
  } else {
    fetch(`${API_BASE}/topology/orbit/control?running=${running}&speed=${speed}`, {
      method: 'POST'
    }).catch(err => console.error('Lỗi gửi điều khiển quỹ đạo:', err));
  }
}

function handleOrbitTick(msg) {
  const clockText = document.getElementById('sim-clock-text');
  if (clockText && msg.sim_time) {
    clockText.textContent = `⏱️ ${msg.sim_time} (${msg.speed || orbitSpeed}x)`;
  }

  if (Array.isArray(msg.satellites) && msg.satellites.length > 0) {
    const satPosMap = new Map();
    msg.satellites.forEach(s => {
      satPosMap.set(s.id, s);
      const node = nodeLookup[s.id];
      if (node) {
        node.lat = s.lat;
        node.lon = s.lon;
        node.alt_km = s.alt_km;
      }
    });

    // Cập nhật tọa độ các nút vệ tinh trên đường ACO/Dijkstra nếu đang hiển thị
    if (bestPathCoords && bestPathCoords.length) {
      bestPathCoords.forEach(p => {
        const id = p.node || p.id;
        const sat = satPosMap.get(id);
        if (sat) {
          p.lat = sat.lat;
          p.lon = sat.lon;
          if (p.alt_km !== undefined) p.alt_km = sat.alt_km;
        }
      });
    }

    if (dijkstraPathCoords && dijkstraPathCoords.length) {
      dijkstraPathCoords.forEach(p => {
        const id = p.node || p.id;
        const sat = satPosMap.get(id);
        if (sat) {
          p.lat = sat.lat;
          p.lon = sat.lon;
          if (p.alt_km !== undefined) p.alt_km = sat.alt_km;
        }
      });
    }

    // Cập nhật 3D Globe siêu mượt: TWEEN 60 FPS vị trí vệ tinh (KHÔNG REBUILD 285 ARCS HAY DOM!)
    if (activeView === '3d' && globe) {
      update3DOrbitPositions(msg.satellites);
    }

    // Cập nhật 2D Map nếu đang ở chế độ 2D
    if (activeView === '2d' && map) {
      update2DSatellitePositions(msg.satellites);
    }

    // Cập nhật tọa độ trực tiếp trong HUD nếu nút đang mở là vệ tinh
    if (selectedNode && satPosMap.has(selectedNode.id || selectedNode.name)) {
      const sat = satPosMap.get(selectedNode.id || selectedNode.name);
      selectedNode.lat = sat.lat;
      selectedNode.lon = sat.lon;
      selectedNode.alt_km = sat.alt_km;
      const latEl = document.getElementById('hud-lat');
      const lonEl = document.getElementById('hud-lon');
      const altEl = document.getElementById('hud-alt');
      if (latEl) latEl.textContent = `${sat.lat.toFixed(2)}°`;
      if (lonEl) lonEl.textContent = `${sat.lon.toFixed(2)}°`;
      if (altEl) altEl.textContent = `${sat.alt_km.toFixed(1)} km`;
    }
  }
}

// ── Cập nhật chuyển động vệ tinh 3D không giật lag (60 FPS Tween) ──
function update3DOrbitPositions(satellites) {
  if (!globe || !cachedHtmlElements || cachedHtmlElements.length === 0) return;

  satellites.forEach(s => {
    const elem = cachedHtmlElementMap.get(s.id);
    if (elem) {
      elem.lat = s.lat;
      elem.lon = s.lon;
      elem.alt_km = s.alt_km;
      elem.altitude = getNodeAltitude(elem);
    }
  });

  // 1. Cập nhật vị trí HTML Marker vệ tinh (Three-Globe tự động tween 60 FPS mượt mà)
  globe.htmlElementsData(cachedHtmlElements);

  // 2. Cập nhật tia laser bám sát các nút vệ tinh đang bay theo thời gian thực (chỉ 4-12 arcs, siêu nhẹ <0.1ms, 60 FPS)
  if ((bestPathCoords && bestPathCoords.length >= 2) || (dijkstraPathCoords && dijkstraPathCoords.length >= 2) || (multiFlows && multiFlows.length > 0)) {
    globe.arcsData(getAllActiveRouteArcs());
  }

  // 3. Cập nhật toàn bộ Lưới liên kết ISL nền toàn mạng bám sát từng vệ tinh đang bay
  if (islDisplayMode !== 'hidden' && topology && topology.edges) {
    updateBackgroundISLPaths(topology.edges);
  }

  // 4. Cập nhật vị trí sóng radar nếu có vệ tinh bị sự cố hoặc nghẽn tải đang bay
  if (failedNodes.size > 0 || Object.keys(nodeLoads).length > 0) {
    globe.ringsData(buildActiveRings());
  }
}

function update2DSatellitePositions(satellites) {
  if (!map || !satellites || activeView !== '2d') return;
  satellites.forEach(s => {
    const marker = leafletNodeMarkers[s.id];
    if (marker) {
      marker.setLatLng([s.lat, s.lon]);
    }
  });

  // Cập nhật tuyến đường 2D nếu có (chỉ vài điểm, không tính toán lại 285 cạnh nền)
  if (bestPathCoords && bestPathCoords.length >= 2) {
    draw2DBestPath(bestPathCoords, dijkstraPathCoords);
  }
}

// ==============================================================
// 4. FLOATING NODE HUD INSPECTOR
// ==============================================================
function handleNodeClick(node) {
  selectedNode = node;
  const hud = document.getElementById('node-hud');
  if (!hud) return;

  const isFailed = failedNodes.has(node.id || node.name);
  const icon = isFailed ? '❌' : (LAYER_ICONS[node.type] || '🛰️');
  document.getElementById('hud-icon').textContent = icon;
  document.getElementById('hud-name').textContent = (node.id || node.name) + (isFailed ? ' [HỎNG]' : '');
  document.getElementById('hud-type').textContent = isFailed ? 'SỰ CỐ / VÔ HIỆU HÓA' : `${node.type.toUpperCase()} NODE`;
  document.getElementById('hud-lat').textContent  = `${(node.lat || 0).toFixed(2)}°`;
  document.getElementById('hud-lon').textContent  = `${(node.lon || 0).toFixed(2)}°`;
  document.getElementById('hud-alt').textContent  = `${(node.alt_km || 0).toFixed(1)} km`;

  // Đếm bậc liên kết
  let degree = 0;
  if (topology.edges) {
    degree = topology.edges.filter(e => 
      !failedNodes.has(e.from) && !failedNodes.has(e.to) &&
      (e.from === node.id || e.to === node.id)
    ).length;
  }
  document.getElementById('hud-degree').textContent = isFailed ? '0 (Bị ngắt)' : `${degree} liên kết`;

  // Cập nhật giá trị tải lưu lượng và thanh trượt trong HUD
  const loadData = nodeLoads[node.id] || {};
  const currentLoad = (loadData.load !== undefined) ? loadData.load : (node.load !== undefined ? node.load : 0.10);
  const isCongested = (currentLoad >= 0.75);

  const loadValEl = document.getElementById('hud-load-val');
  const loadStatusEl = document.getElementById('hud-load-status');
  const loadSliderEl = document.getElementById('hud-load-slider');
  if (loadValEl) loadValEl.textContent = `${Math.round(currentLoad * 100)}%`;
  if (loadStatusEl) {
    loadStatusEl.textContent = isCongested ? '🔥 Nghẽn Tải' : (currentLoad >= 0.5 ? 'Tải Vừa' : 'Ổn Định');
    loadStatusEl.style.color = isCongested ? '#f87171' : (currentLoad >= 0.5 ? '#fbbf24' : '#4ade80');
  }
  if (loadSliderEl) loadSliderEl.value = Math.round(currentLoad * 100);

  // Cập nhật nút Đánh Sập / Khôi Phục trong HUD
  const btnHudFail = document.getElementById('hud-toggle-fail');
  if (btnHudFail) {
    if (isFailed) {
      btnHudFail.textContent = '🟢 Khôi Phục Nút (Restore)';
      btnHudFail.style.background = 'linear-gradient(135deg, #16a34a, #15803d)';
      btnHudFail.style.borderColor = '#22c55e';
      btnHudFail.style.color = '#fff';
    } else {
      btnHudFail.textContent = '💥 Đánh Sập Nút (Fail)';
      btnHudFail.style.background = 'linear-gradient(135deg, #dc2626, #991b1b)';
      btnHudFail.style.borderColor = '#ef4444';
      btnHudFail.style.color = '#fff';
    }
  }

  hud.style.display = 'block';
  updateNodeVisualStates();
}

function closeNodeHUD() {
  const hud = document.getElementById('node-hud');
  if (hud) hud.style.display = 'none';
  selectedNode = null;
  updateNodeVisualStates();
}

function update2DSelectionMarkers() {
  const srcId = document.getElementById('sel-source')?.value;
  const dstId = document.getElementById('sel-dest')?.value;

  Object.entries(leafletNodeMarkers).forEach(([id, marker]) => {
    const el = marker.getElement()?.querySelector('div');
    if (!el) return;

    if (id === srcId) {
      el.style.boxShadow = '0 0 16px #4ade80, 0 0 28px rgba(74,222,128,0.7)';
      el.style.borderColor = '#4ade80';
      el.style.transform = 'scale(1.25)';
      el.style.zIndex = '999';
    } else if (id === dstId) {
      el.style.boxShadow = '0 0 16px #ef4444, 0 0 28px rgba(239,68,68,0.7)';
      el.style.borderColor = '#ef4444';
      el.style.transform = 'scale(1.25)';
      el.style.zIndex = '999';
    } else {
      el.style.removeProperty('box-shadow');
      el.style.removeProperty('border-color');
      el.style.removeProperty('transform');
      el.style.removeProperty('z-index');
    }
  });
}

function setHUDAsSource() {
  if (!selectedNode) return;
  const sel = document.getElementById('sel-source');
  sel.value = selectedNode.id || selectedNode.name;
  log(`Đã chọn [${sel.value}] làm Node NGUỒN 📍`, 'ws');
  updateNodeVisualStates();
  update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
  update2DSelectionMarkers();
}

function setHUDAsDest() {
  if (!selectedNode) return;
  const sel = document.getElementById('sel-dest');
  sel.value = selectedNode.id || selectedNode.name;
  log(`Đã chọn [${sel.value}] làm Node ĐÍCH 🎯`, 'ws');
  updateNodeVisualStates();
  update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
  update2DSelectionMarkers();
}

// Xuất các hàm ra phạm vi toàn cục window để gọi từ popup của Leaflet
window.handleNodeClick = handleNodeClick;
window.setNodeAsSource = function(nodeId) {
  const node = nodeLookup[nodeId] || { id: nodeId, name: nodeId, type: 'unknown' };
  selectedNode = node;
  setHUDAsSource();
  if (map) map.closePopup();
};
window.setNodeAsDest = function(nodeId) {
  const node = nodeLookup[nodeId] || { id: nodeId, name: nodeId, type: 'unknown' };
  selectedNode = node;
  setHUDAsDest();
  if (map) map.closePopup();
};

// ── Hàm điều khiển Mô Phỏng Sự Cố Mạng (Fault Tolerance) ───────
async function toggleNodeFailure(nodeId) {
  if (!nodeId) return;
  try {
    const res = await fetch(`${API_BASE}/topology/node/${encodeURIComponent(nodeId)}/toggle`, {
      method: 'POST'
    });
    const data = await res.json();
    if (data.status === 'success') {
      if (data.action === 'failed') {
        failedNodes.add(nodeId);
        log(`⚠️ Đã đánh sập nút [${nodeId}]! Mạng đang tái cấu trúc...`, 'warning');
      } else {
        failedNodes.delete(nodeId);
        log(`🟢 Đã khôi phục nút [${nodeId}] thành công ✓`, 'success');
      }
      renderFailedNodesUI();
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      updateBackgroundISLPaths(topology.edges);
      draw2DTopology(topology.nodes, topology.edges);
      if (selectedNode && (selectedNode.id === nodeId || selectedNode.name === nodeId)) {
        handleNodeClick(selectedNode);
      }

      if (!ws || ws.readyState !== WebSocket.OPEN) {
        handleTopologyStateChanged(nodeId, data.action);
      }
    }
  } catch (err) {
    log(`Lỗi khi chuyển trạng thái nút: ${err}`, 'error');
  }
}
window.toggleNodeFailure = toggleNodeFailure;

async function restoreAllNodes() {
  try {
    const res = await fetch(`${API_BASE}/topology/restore_all`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'success') {
      failedNodes.clear();
      log('🟢 Đã khôi phục tất cả các nút về trạng thái bình thường ✓', 'success');
      renderFailedNodesUI();
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      updateBackgroundISLPaths(topology.edges);
      draw2DTopology(topology.nodes, topology.edges);
      if (selectedNode) handleNodeClick(selectedNode);

      if (!ws || ws.readyState !== WebSocket.OPEN) {
        handleTopologyStateChanged(null, 'all_restored');
      }
    }
  } catch (err) {
    log(`Lỗi khi khôi phục các nút: ${err}`, 'error');
  }
}
window.restoreAllNodes = restoreAllNodes;

// ── Cơ chế Tự Phục Hồi Mạng (Self-Healing Failover) ───────────
// Khi một nút trung gian trên lộ trình bị đánh sập hoặc khôi phục, tự động reload lại đường đi
function handleTopologyStateChanged(nodeId, action) {
  const selSrc = document.getElementById('sel-source')?.value;
  const selDst = document.getElementById('sel-dest')?.value;

  const hasAcoRoute = bestPathCoords && bestPathCoords.length >= 2;
  const hasDijkstraRoute = dijkstraPathCoords && dijkstraPathCoords.length >= 2;
  const hasMultiFlow = multiFlows && multiFlows.length > 0;

  // Kiểm tra xem nút bị tác động có nằm trên lộ trình đang hiển thị không
  const onAcoPath = hasAcoRoute && bestPathCoords.some(p => (p.node || p.id) === nodeId);
  const onDijkstraPath = hasDijkstraRoute && dijkstraPathCoords.some(p => (p.node || p.id) === nodeId);
  const onMultiFlow = hasMultiFlow && multiFlows.some(fl => {
    const coords = fl.path_coords || fl.coords;
    return coords && coords.some(p => (p.node || p.id) === nodeId);
  });
  const isOnActiveRoute = onAcoPath || onDijkstraPath;

  const cmpBox = document.getElementById('compare-result');
  const isCompareMode = (cmpBox && cmpBox.style.display !== 'none');

  if (action === 'failed') {
    // 1. Nếu node bị sập chính là Node Nguồn hoặc Node Đích
    if (nodeId === selSrc || nodeId === selDst) {
      log(`💥 SỰ CỐ ĐẦU CUỐI: ${nodeId === selSrc ? 'Node Nguồn' : 'Node Đích'} [${nodeId}] đã bị sập! Mất kết nối toàn tuyến.`, 'error');
      clearPath();
      document.getElementById('path-display').innerHTML =
        `<div style="color:#ef4444;font-size:12px;font-weight:600;padding:6px;background:rgba(239,68,68,0.1);border-radius:6px;border:1px solid #ef4444">` +
        `❌ MẤT KẾT NỐI: ${nodeId === selSrc ? 'Node Nguồn' : 'Node Đích'} [${nodeId}] hiện đang bị sự cố!</div>`;
      return;
    }

    // 2. Nếu node bị sập là NÚT TRUNG GIAN trên tuyến đang kết nối
    if (isOnActiveRoute && selSrc && selDst) {
      log(`⚡ PHÁT HIỆN SỰ CỐ: Nút trung gian [${nodeId}] trên lộ trình đã bị đánh sập!`, 'warning');
      log(`🔄 Kích hoạt cơ chế Tự Phục Hồi Mạng (Self-Healing Failover): Đang tự động tìm lộ trình bẻ lái thay thế...`, 'info');

      // Tạm xóa lộ trình cũ đi qua nút chết để vẽ lại tuyến mới
      bestPathCoords = [];
      dijkstraPathCoords = [];
      update3DGlobeData(topology.nodes, topology.edges, [], []);

      if (isCompareMode) {
        runCompare();
      } else {
        sendRoutingRequest();
      }
    } else if (onMultiFlow) {
      log(`⚡ Nút trung gian [${nodeId}] trên các luồng giờ cao điểm đã sập! Đang tự động tái định tuyến luồng...`, 'warning');
      runMultiFlowSimulation();
    }
  } else if (action === 'restored' || action === 'all_restored') {
    // Khi một hoặc nhiều nút được khôi phục
    if (selSrc && selDst && (hasAcoRoute || hasDijkstraRoute)) {
      log(`🟢 Nút [${nodeId || 'toàn bộ'}] đã phục hồi. Đang kiểm tra tái tối ưu hóa lộ trình...`, 'info');
      if (isCompareMode) {
        runCompare();
      } else {
        sendRoutingRequest();
      }
    } else if (hasMultiFlow) {
      log(`🟢 Nút [${nodeId || 'toàn bộ'}] đã phục hồi. Tái cân bằng các luồng giờ cao điểm...`, 'info');
      runMultiFlowSimulation();
    }
  }
}

function renderFailedNodesUI() {
  const container = document.getElementById('failed-nodes-container');
  const listEl = document.getElementById('failed-nodes-list');
  const selFail = document.getElementById('sel-fail-node');

  // Cập nhật dropdown chọn nút giả lập sự cố
  if (selFail && topology.nodes) {
    const curVal = selFail.value;
    selFail.innerHTML = '<option value="">-- Chọn nút cần tắt --</option>';
    topology.nodes.forEach(n => {
      const isF = failedNodes.has(n.id);
      const opt = document.createElement('option');
      opt.value = n.id;
      opt.textContent = isF ? `❌ ${n.id} (Đang hỏng)` : `${LAYER_ICONS[n.type] || ''} ${n.id}`;
      selFail.appendChild(opt);
    });
    selFail.value = curVal;
  }

  if (!container || !listEl) return;

  if (failedNodes.size === 0) {
    container.style.display = 'none';
    listEl.innerHTML = '';
  } else {
    container.style.display = 'block';
    listEl.innerHTML = '';
    failedNodes.forEach(nid => {
      const tag = document.createElement('span');
      tag.style.cssText = 'background:#7f1d1d;color:#fecaca;padding:2px 7px;border-radius:4px;font-size:10px;display:inline-flex;align-items:center;gap:4px;border:1px solid #ef4444;cursor:pointer;font-weight:600';
      tag.innerHTML = `❌ ${nid} <small style="color:#f87171;font-weight:bold">✕</small>`;
      tag.title = 'Click để khôi phục nút này';
      tag.onclick = () => toggleNodeFailure(nid);
      listEl.appendChild(tag);
    });
  }

  // Cập nhật lại nút trong HUD nếu đang mở
  if (selectedNode) {
    const isF = failedNodes.has(selectedNode.id || selectedNode.name);
    const btnHudFail = document.getElementById('hud-toggle-fail');
    if (btnHudFail) {
      if (isF) {
        btnHudFail.textContent = '🟢 Khôi Phục Nút (Restore)';
        btnHudFail.style.background = 'linear-gradient(135deg, #16a34a, #15803d)';
        btnHudFail.style.borderColor = '#22c55e';
      } else {
        btnHudFail.textContent = '💥 Đánh Sập Nút (Fail)';
        btnHudFail.style.background = 'linear-gradient(135deg, #dc2626, #991b1b)';
        btnHudFail.style.borderColor = '#ef4444';
      }
    }
  }
}

// ==============================================================
// 5. HIỂN THỊ DỮ LIỆU TOPOLOGY & TUYẾN ĐƯỜNG
// ==============================================================
function applyTopology(nodes, edges) {
  topology = { nodes, edges };
  nodeLookup = {};
  nodes.forEach(n => {
    nodeLookup[n.id] = n;
    if (n.load !== undefined) {
      if (!nodeLoads[n.id]) nodeLoads[n.id] = {};
      nodeLoads[n.id].load = n.load;
      nodeLoads[n.id].active_conn = n.active_conn || 5;
      nodeLoads[n.id].capacity = n.capacity || 100;
      nodeLoads[n.id].is_congested = n.is_congested || (n.load >= 0.75);
    }
  });

  // Cập nhật select dropdown
  updateNodeSelects(nodes);
  renderFailedNodesUI();

  // Vẽ lên cả 3D Globe và 2D Leaflet
  update3DGlobeData(nodes, edges);
  updateBackgroundISLPaths(edges);
  draw2DTopology(nodes, edges);

  // Cập nhật badges & stats
  document.getElementById('stat-nodes').textContent = nodes.length;
  document.getElementById('stat-edges').textContent = edges.length;
  document.getElementById('ovl-sats').textContent   = nodes.length;
  document.getElementById('ovl-edges').textContent  = edges.length;

  log(`Topology cập nhật: ${nodes.length} nodes, ${edges.length} edges`, 'success');
}

function displayBestPath(pathCoords, costMs) {
  bestPathCoords = pathCoords;

  // Cập nhật tuyến đường lên cả 3D và 2D
  update3DGlobeData(topology.nodes, topology.edges, pathCoords);
  draw2DBestPath(pathCoords);

  // Render danh sách node trong sidebar
  renderPathSidebar(pathCoords, costMs);

  // Cập nhật thống kê
  document.getElementById('stat-cost').textContent = `${costMs.toFixed(1)} ms`;
  document.getElementById('stat-rtt').textContent  = `${(costMs * 2).toFixed(1)} ms`;
  document.getElementById('stat-hops').textContent = pathCoords.length - 1;

  // Hiển thị huy hiệu tự động thích nghi quỹ đạo
  const autoBadge = document.getElementById('badge-auto-route');
  if (autoBadge) {
    autoBadge.style.display = 'inline-flex';
  }
}

function renderPathSidebar(pathCoords, costMs) {
  const container = document.getElementById('path-display');
  container.innerHTML = '';

  pathCoords.forEach((p, i) => {
    const nodeObj = nodeLookup[p.node] || p;
    const type    = nodeObj.type || 'unknown';
    const icon    = LAYER_ICONS[type] || '●';

    const row = document.createElement('div');
    row.className = 'path-node';
    row.innerHTML = `
      <div class="path-node-icon ${type}">${icon}</div>
      <div>
        <div style="font-weight:600;font-size:12px;color:#f8fafc">${p.node}</div>
        <div style="font-size:10px;color:#94a3b8">
          Alt: ${(nodeObj.alt_km || 0).toFixed(0)}km | (${(p.lat || 0).toFixed(1)}°, ${(p.lon || 0).toFixed(1)}°)
        </div>
      </div>
    `;
    container.appendChild(row);

    if (i < pathCoords.length - 1) {
      const arrow = document.createElement('div');
      arrow.className = 'path-arrow';
      arrow.innerHTML = '↓';
      container.appendChild(arrow);
    }
  });
}

function updateNodeSelects(nodes) {
  const selSource = document.getElementById('sel-source');
  const selDest   = document.getElementById('sel-dest');
  [selSource, selDest].forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = '<option value="">-- Chọn node --</option>';
    nodes.forEach(n => {
      const opt = document.createElement('option');
      opt.value = n.id;
      opt.textContent = `${n.id} (${n.type})`;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  });
}

// ==============================================================
// 6. GIAO TIẾP WEBSOCKET & REST API
// ==============================================================
function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  log('Đang kết nối WebSocket đến server...', 'info');
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    updateWsStatus(true);
    log('WebSocket kết nối thành công ✓', 'success');
    ws.send(JSON.stringify({ action: 'ping' }));
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (e) {
      log(`Lỗi phân tích WebSocket JSON: ${e}`, 'error');
    }
  };

  ws.onerror = () => {
    updateWsStatus(false);
  };

  ws.onclose = () => {
    updateWsStatus(false);
    log('WebSocket bị ngắt. Đang thử kết nối lại sau 3s...', 'warning');
    setTimeout(connectWebSocket, 3000);
  };
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case 'CONNECTED':
      if (msg.failed_nodes) {
        failedNodes = new Set(msg.failed_nodes);
      }
      if (msg.orbit_speed !== undefined) {
        orbitSpeed = Number(msg.orbit_speed);
        updateOrbitControlsUI();
      }
      if (msg.orbit_running !== undefined) {
        orbitRunning = Boolean(msg.orbit_running);
        updateOrbitControlsUI();
      }
      if (msg.nodes && msg.edges) {
        applyTopology(msg.nodes, msg.edges);
      }
      renderFailedNodesUI();
      break;

    case 'NODE_FAILED':
      failedNodes.add(msg.node_id);
      log(`⚠️ SỰ CỐ: Nút [${msg.node_id}] đã bị vô hiệu hóa!`, 'error');
      renderFailedNodesUI();
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      updateBackgroundISLPaths(topology.edges);
      draw2DTopology(topology.nodes, topology.edges);
      if (selectedNode && (selectedNode.id === msg.node_id || selectedNode.name === msg.node_id)) {
        handleNodeClick(selectedNode);
      }
      handleTopologyStateChanged(msg.node_id, 'failed');
      break;

    case 'NODE_RESTORED':
      failedNodes.delete(msg.node_id);
      log(`🟢 KHÔI PHỤC: Nút [${msg.node_id}] đã phục hồi ✓`, 'success');
      renderFailedNodesUI();
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      updateBackgroundISLPaths(topology.edges);
      draw2DTopology(topology.nodes, topology.edges);
      if (selectedNode && (selectedNode.id === msg.node_id || selectedNode.name === msg.node_id)) {
        handleNodeClick(selectedNode);
      }
      handleTopologyStateChanged(msg.node_id, 'restored');
      break;

    case 'ALL_NODES_RESTORED':
      failedNodes.clear();
      log(`🟢 Tất cả các nút mạng đã được khôi phục ✓`, 'success');
      renderFailedNodesUI();
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      updateBackgroundISLPaths(topology.edges);
      draw2DTopology(topology.nodes, topology.edges);
      if (selectedNode) {
        handleNodeClick(selectedNode);
      }
      handleTopologyStateChanged(null, 'all_restored');
      break;

    case 'NODE_LOAD_CHANGED':
      if (!nodeLoads[msg.node_id]) nodeLoads[msg.node_id] = {};
      nodeLoads[msg.node_id].load = msg.load;
      nodeLoads[msg.node_id].active_conn = msg.active_conn;
      nodeLoads[msg.node_id].is_congested = msg.is_congested;
      log(`📊 Tải nút [${msg.node_id}]: ${(msg.load * 100).toFixed(0)}% ${msg.is_congested ? '🔥 (NGHẼN TẢI)' : ''}`, 'info');
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      if (selectedNode && (selectedNode.id === msg.node_id || selectedNode.name === msg.node_id)) {
        handleNodeClick(selectedNode);
      }
      break;

    case 'TRAFFIC_UPDATED':
      if (msg.loads) nodeLoads = msg.loads;
      log(`🎲 Đã cập nhật lưu lượng nền toàn mạng: ${msg.congested_count || 0} nút nghẽn`, 'warning');
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      if (selectedNode) handleNodeClick(selectedNode);
      break;

    case 'TRAFFIC_RESET':
      if (msg.loads) nodeLoads = msg.loads;
      else {
        Object.keys(nodeLoads).forEach(k => {
          nodeLoads[k].load = 0.10;
          nodeLoads[k].is_congested = false;
        });
      }
      log(`🔄 Đã đặt lại tải lưu lượng toàn mạng về 10%`, 'success');
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      if (selectedNode) handleNodeClick(selectedNode);
      break;

    case 'MULTIFLOW_DONE':
      multiFlows = msg.flows || [];
      if (msg.loads) nodeLoads = msg.loads;
      renderMultiFlowBoard(multiFlows);
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      draw2DBestPath(bestPathCoords, dijkstraPathCoords);
      log(`⚡ Hoàn tất mô phỏng ${multiFlows.length} luồng giờ cao điểm! ACO tự động phân tán tải qua các vệ tinh khác nhau.`, 'success');
      break;

    case 'PONG':
      log('Server phản hồi Ping (Heartbeat OK)', 'info');
      break;

    case 'ACO_STARTED':
      isRunning = true;
      currentIter = 0;
      setRunningState(true);
      log(`Bầy kiến xuất phát: ${msg.source} → ${msg.destination}`, 'ws');
      break;

    case 'ACO_UPDATE':
      currentIter = msg.iteration;
      updateProgress(currentIter, totalIter);
      document.getElementById('iter-display').textContent = currentIter;

      if (msg.path_coords && msg.path_coords.length >= 2) {
        displayBestPath(msg.path_coords, msg.cost_ms);
      }

      if (currentIter % 5 === 0) {
        log(`[Vòng ${currentIter}] Trễ tốt nhất: ${msg.cost_ms.toFixed(1)}ms | ${msg.path?.join('→')}`, 'info');
      }
      break;

    case 'ACO_DONE':
      isRunning = false;
      setRunningState(false);
      updateProgress(totalIter, totalIter);

      if (msg.converged && msg.path_coords && msg.path_coords.length >= 2) {
        displayBestPath(msg.path_coords, msg.cost_ms);
        log(`✅ Đã tìm thấy đường tối ưu mới! Chi phí: ${msg.cost_ms} ms | RTT: ${msg.rtt_ms} ms`, 'success');
      } else {
        log(`❌ Không thể tìm được đường đi giữa ${msg.source} và ${msg.destination} (Mạng bị chia cắt do sự cố nút trung gian)!`, 'error');
        clearPath();
        document.getElementById('path-display').innerHTML =
          `<div style="color:#ef4444;font-size:12px;font-weight:600;padding:6px;background:rgba(239,68,68,0.1);border-radius:6px;border:1px solid #ef4444">` +
          `❌ MẠNG BỊ CHIA CẮT: Không có đường đi từ [${msg.source}] đến [${msg.destination}] do các nút trung gian bị đánh sập!</div>`;
      }
      break;

    case 'COMPARE_DONE':
      // Vẽ cả đường ACO (Đỏ) và đường Dijkstra (Vàng) lên bản đồ khi kết quả so sánh về qua WS
      bestPathCoords     = msg.aco_path_coords || [];
      dijkstraPathCoords = msg.dijkstra_coords || [];
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords, dijkstraPathCoords);
      draw2DBestPath(bestPathCoords, dijkstraPathCoords);
      break;

    case 'BATCH_RESULT':
      log(`[Luồng ${msg.thread}] Xong Task #${msg.task_id}: ${msg.source}→${msg.destination} (${msg.cost_ms}ms)`, 'info');
      break;

    case 'BATCH_DONE':
      log(`✅ Hoàn thành Batch: ${msg.completed} tasks đa luồng | Trễ TB: ${msg.avg_cost}ms`, 'success');
      setRunningState(false);
      break;

    case 'ORBIT_CONFIG':
      if (msg.running !== undefined) orbitRunning = Boolean(msg.running);
      if (msg.speed !== undefined) orbitSpeed = Number(msg.speed);
      updateOrbitControlsUI();
      break;

    case 'ORBIT_TICK':
      handleOrbitTick(msg);
      break;

    case 'ROUTE_ADAPTED':
      handleRouteAdapted(msg);
      break;

    case 'ROUTE_CLEARED':
      bestPathCoords     = [];
      dijkstraPathCoords = [];
      update3DGlobeData(topology.nodes, topology.edges, [], []);
      if (activeView === '2d') draw2DBestPath([], []);
      document.getElementById('path-display').innerHTML =
        '<div style="color:#475569;font-size:12px">Chưa có đường đi nào</div>';
      document.getElementById('stat-cost').textContent = '--';
      document.getElementById('stat-rtt').textContent  = '--';
      document.getElementById('stat-hops').textContent = '--';
      const clrBadge = document.getElementById('badge-auto-route');
      if (clrBadge) clrBadge.style.display = 'none';
      break;

    case 'ERROR':
      log(`❌ ${msg.message}`, 'error');
      setRunningState(false);
      break;
  }
}

// ── Xử lý khi quỹ đạo vệ tinh thay đổi khoảng cách và chuyển giao (Satellite Handover) ──
function handleRouteAdapted(msg) {
  if (!msg.path || msg.path.length < 2) return;

  const costMs = Number(msg.cost_ms) || 0;
  const rttMs = Number(msg.rtt_ms) || (costMs * 2);

  // Cập nhật số liệu hiển thị thời gian thực
  const statCost = document.getElementById('stat-cost');
  const statRtt = document.getElementById('stat-rtt');
  const statHops = document.getElementById('stat-hops');
  if (statCost) statCost.textContent = `${costMs.toFixed(1)} ms`;
  if (statRtt) statRtt.textContent = `${rttMs.toFixed(1)} ms`;
  if (statHops) statHops.textContent = msg.path.length - 1;

  const autoBadge = document.getElementById('badge-auto-route');
  if (autoBadge) {
    autoBadge.style.display = 'inline-flex';
  }

  if (msg.path_changed) {
    // 1. Chuyển giao vệ tinh chủ động (Proactive Satellite Handover!)
    const oldPathStr = (msg.old_path && msg.old_path.length) ? msg.old_path.join(' → ') : '';
    const newPathStr = msg.path.join(' → ');

    log(`🛰️ CHỦ ĐỘNG CHUYỂN GIAO (PROACTIVE HANDOVER): Tuyến đã đổi trước khi mất sóng → [${newPathStr}] | Trễ: ${costMs.toFixed(1)} ms`, 'success');

    bestPathCoords = msg.path_coords || [];
    renderPathSidebar(bestPathCoords, costMs);
    updateNodeVisualStates();

    // Buộc 3D Globe và 2D Map vẽ lại đường kết nối ngay lập tức
    lastRouteArcUpdate = Date.now();
    update3DGlobeData(topology.nodes, topology.edges, bestPathCoords, dijkstraPathCoords);
    if (activeView === '2d') {
      draw2DBestPath(bestPathCoords, dijkstraPathCoords);
    }
  } else {
    // 2. Cùng danh sách vệ tinh, khoảng cách vật lý thay đổi liên tục theo bước bay
    if (msg.path_coords && msg.path_coords.length) {
      bestPathCoords = msg.path_coords;
    }
  }

  // 3. Hiển thị cảnh báo nếu vệ tinh trên tuyến đang lặn dần về đường chân trời
  if (msg.handover_alert) {
    const alert = msg.handover_alert;
    const timeSec = alert.time_to_horizon_sec !== null ? `${alert.time_to_horizon_sec}s` : 'sắp lặn';
    log(`⚠️ DỰ BÁO QUỸ ĐẠO: Vệ tinh ${alert.satellite} đang lặn về chân trời (${alert.elevation}°). Còn ${timeSec} trước khi mất sóng — Sẵn sàng chuyển giao!`, 'warning');
  }
}

// ── Gửi yêu cầu tìm đường ─────────────────────────────────────
async function sendRoutingRequest() {
  const src = document.getElementById('sel-source').value;
  const dst = document.getElementById('sel-dest').value;

  if (!src || !dst) {
    log('Vui lòng chọn cả Node Nguồn và Node Đích!', 'warning');
    return;
  }
  if (src === dst) {
    log('Node Nguồn và Đích phải khác nhau!', 'warning');
    return;
  }

  totalIter   = parseInt(document.getElementById('inp-iter').value) || 50;
  currentIter = 0;
  document.getElementById('iter-total').textContent = totalIter;

  document.getElementById('path-display').innerHTML =
    '<div style="color:#38bdf8;font-size:12px;display:flex;align-items:center;gap:6px">' +
    '<span class="spinning">⏳</span> Đang thả bầy kiến tìm đường thích nghi...</div>';

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'route', source: src, destination: dst }));
    setRunningState(true);
  } else {
    // Fallback qua REST API nếu WebSocket chưa kết nối
    try {
      log(`Fallback gọi REST API: ${src} → ${dst}`, 'warning');
      setRunningState(true);
      const res = await fetch(
        `${API_BASE}/routing?source=${src}&destination=${dst}&n_iter=${totalIter}&broadcast=false`
      );
      const data = await res.json();
      if (data.best_path) {
        displayBestPath(data.path_coords, data.cost_ms);
        log(`✅ REST tìm xong: ${data.best_path.join('→')} | ${data.cost_ms}ms`, 'success');
      }
    } catch (e) {
      log(`Lỗi API: ${e}`, 'error');
    } finally {
      setRunningState(false);
    }
  }
}

// ── Tải Topology từ REST API ──────────────────────────────────
async function loadTopologyFromREST() {
  try {
    const res  = await fetch(`${API_BASE}/topology`);
    const data = await res.json();
    if (data.failed_nodes) {
      failedNodes = new Set(data.failed_nodes);
    }
    applyTopology(data.nodes, data.edges);
    log(`Tải Topology REST thành công: ${data.n_nodes} nodes, ${data.n_edges} edges`, 'success');
  } catch (e) {
    log(`Không thể tải Topology từ REST: ${e}`, 'error');
  }
}

// ── Đối chiếu ACO vs Dijkstra ─────────────────────────────────
async function runCompare() {
  const src = document.getElementById('sel-source').value;
  const dst = document.getElementById('sel-dest').value;

  if (!src || !dst) { log('⚠️ Chọn Node Nguồn và Đích trước khi so sánh!', 'warning'); return; }
  if (src === dst)  { log('⚠️ Node Nguồn và Đích phải khác nhau!', 'warning'); return; }

  const btn  = document.getElementById('btn-compare');
  const spin = document.getElementById('cmp-spin');
  btn.disabled = true;
  spin.style.display = 'inline';
  document.getElementById('compare-result').style.display = 'none';
  log(`🔬 So sánh ACO vs Dijkstra: ${src} → ${dst}...`, 'info');

  try {
    const nIter = parseInt(document.getElementById('inp-iter').value) || 50;
    const res   = await fetch(
      `${API_BASE}/routing/compare?source=${encodeURIComponent(src)}&destination=${encodeURIComponent(dst)}&n_iter=${nIter}`
    );
    if (!res.ok) { const err = await res.json(); log(`❌ Lỗi: ${err.detail}`, 'error'); return; }
    const data = await res.json();
    renderCompareResult(data);
  } catch (e) {
    log(`❌ Lỗi kết nối: ${e}`, 'error');
  } finally {
    btn.disabled = false;
    spin.style.display = 'none';
  }
}

function renderCompareResult(data) {
  const cmp  = data.comparison;
  const aco  = data.aco;
  const dijk = data.dijkstra;

  document.getElementById('compare-result').style.display = 'block';

  // ── Verdict Badge ──
  const verdictEl  = document.getElementById('cmp-verdict');
  const verdictMap = {
    OPTIMAL           : { text: '✅ TỐI ƯU HOÀN HẢO — ACO = Dijkstra', bg: '#052e16', border: '#16a34a', color: '#4ade80' },
    NEAR_OPTIMAL      : { text: '🟡 GẦN TỐI ƯU (chênh ≤10%)',          bg: '#1c1700', border: '#ca8a04', color: '#facc15' },
    SUBOPTIMAL        : { text: '🔴 CHƯA TỐI ƯU — tăng n_ants/n_iter', bg: '#2d0000', border: '#dc2626', color: '#f87171' },
    CONGESTION_AVOIDED: { text: '🛡️ ACO BẺ LÁI THÀNH CÔNG — Né nút nghẽn (Dijkstra bị tắc)', bg: '#052e16', border: '#22c55e', color: '#4ade80' },
    NO_PATH           : { text: '❌ KHÔNG TÌM ĐƯỢC ĐƯỜNG',              bg: '#1e293b', border: '#475569', color: '#94a3b8' },
  };
  const v = verdictMap[cmp.verdict] || verdictMap['NO_PATH'];
  verdictEl.textContent       = v.text;
  verdictEl.style.background  = v.bg;
  verdictEl.style.borderColor = v.border;
  verdictEl.style.color       = v.color;

  // ── ACO numbers ──
  document.getElementById('cmp-aco-cost').textContent = aco.found ? `${aco.cost_ms.toFixed(2)} ms` : 'KHÔNG TÌM ĐƯỢC';
  document.getElementById('cmp-aco-hops').textContent = aco.found ? `${aco.n_hops} hop`             : '--';
  document.getElementById('cmp-aco-iter').textContent = aco.found ? `Iter ${aco.convergence_iter}/${aco.n_iterations}` : '--';
  document.getElementById('cmp-aco-path').textContent = aco.found ? aco.path.join(' → ')             : '--';

  // ── Dijkstra numbers ──
  document.getElementById('cmp-dijk-cost').textContent = dijk.found ? `${dijk.cost_ms.toFixed(2)} ms` : 'KHÔNG TÌM ĐƯỢC';
  document.getElementById('cmp-dijk-hops').textContent = dijk.found ? `${dijk.n_hops} hop`             : '--';
  document.getElementById('cmp-diff').textContent      = cmp.cost_diff_ms !== null ? `+${cmp.cost_diff_ms.toFixed(3)} ms` : '--';
  document.getElementById('cmp-dijk-path').textContent = dijk.found ? dijk.path.join(' → ')             : '--';

  // ── Quality bar ──
  const bar = document.getElementById('cmp-quality-bar');
  const txt = document.getElementById('cmp-quality-txt');
  if (cmp.aco_quality_pct !== null) {
    const displayPct = Math.min(100, Math.round(100 / (cmp.aco_quality_pct / 100)));
    bar.style.width      = `${displayPct}%`;
    bar.style.background = cmp.aco_quality_pct <= 100.5 ? '#22c55e' : cmp.aco_quality_pct <= 110 ? '#eab308' : '#ef4444';
    txt.textContent      = `${cmp.aco_quality_pct.toFixed(1)}% so với tối ưu`;
    txt.style.color      = bar.style.background;
  } else {
    bar.style.width = '0%';
    txt.textContent = 'Không có dữ liệu';
  }

  // ── Vẽ cả đường ACO (Đỏ) và đường Dijkstra (Vàng) lên bản đồ ──
  bestPathCoords     = (aco.found && aco.path_coords) ? aco.path_coords : [];
  dijkstraPathCoords = (dijk.found && dijk.path_coords) ? dijk.path_coords : [];

  update3DGlobeData(topology.nodes, topology.edges, bestPathCoords, dijkstraPathCoords);
  draw2DBestPath(bestPathCoords, dijkstraPathCoords);

  log(`⚖️ Kết quả: ACO=${aco.cost_ms?.toFixed(2)}ms (Đỏ) | Dijkstra=${dijk.cost_ms?.toFixed(2)}ms (Vàng) | ${cmp.verdict}`, 'success');
}


// ==============================================================
function setRunningState(running) {
  isRunning = running;
  const btn  = document.getElementById('btn-route');
  const spin = document.getElementById('btn-spin');
  btn.disabled       = running;
  spin.style.display = running ? 'inline' : 'none';
  if (running) btn.classList.add('running-indicator');
  else         btn.classList.remove('running-indicator');
}

function updateProgress(current, total) {
  const pct = Math.min((current / total) * 100, 100);
  document.getElementById('progress-fill').style.width = `${pct}%`;
  document.getElementById('iter-display').textContent  = current;
}

function updateWsStatus(online) {
  const dot  = document.getElementById('ws-dot');
  const text = document.getElementById('ws-text');
  if (online) {
    dot.className  = 'status-dot';
    text.textContent = 'Server online';
  } else {
    dot.className  = 'status-dot offline';
    text.textContent = 'Server offline';
  }
}

function log(msg, type = 'info') {
  const console_ = document.getElementById('log-console');
  if (!console_) return;
  const entry    = document.createElement('div');
  const time     = new Date().toLocaleTimeString('vi-VN', { hour12: false });
  entry.className = `log-entry ${type}`;
  entry.textContent = `[${time}] ${msg}`;
  console_.appendChild(entry);
  console_.scrollTop = console_.scrollHeight;

  while (console_.children.length > 100) {
    console_.removeChild(console_.firstChild);
  }
}

function clearPath() {
  bestPathCoords     = [];
  dijkstraPathCoords = [];
  multiFlows         = [];
  if (leafletGroup) leafletGroup.clearLayers();
  if (globe) {
    globe.arcsData([]);
  }

  const selSource = document.getElementById('sel-source');
  const selDest   = document.getElementById('sel-dest');
  if (selSource) selSource.value = '';
  if (selDest) selDest.value = '';

  updateNodeVisualStates();
  if (activeView === '2d') {
    update2DSelectionMarkers();
  }

  const mfBoard = document.getElementById('multiflow-board');
  if (mfBoard) mfBoard.style.display = 'none';

  const autoBadge = document.getElementById('badge-auto-route');
  if (autoBadge) autoBadge.style.display = 'none';

  document.getElementById('path-display').innerHTML =
    '<div style="color:#475569;font-size:12px">Chưa có đường đi nào</div>';

  document.getElementById('stat-cost').textContent = '--';
  document.getElementById('stat-rtt').textContent  = '--';
  document.getElementById('stat-hops').textContent = '--';
  updateProgress(0, totalIter);
  document.getElementById('iter-display').textContent = '0';
  closeNodeHUD();
  log('Đã xóa tuyến đường hiện tại', 'info');

  // Gửi thông báo đến backend để dừng tự động thích nghi tuyến này
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'clear_route' }));
  } else {
    fetch(`${API_BASE}/routing/clear`, { method: 'POST' }).catch(() => {});
  }
}

// ==============================================================
// 7. QUẢN LÝ TẢI LƯU LƯỢNG & ĐA TUYẾN GIỜ CAO ĐIỂM (LOAD BALANCING)
// ==============================================================
function renderMultiFlowBoard(flows) {
  const board = document.getElementById('multiflow-board');
  const list = document.getElementById('multiflow-list');
  const countEl = document.getElementById('multiflow-count');
  if (!board || !list) return;

  if (!flows || flows.length === 0) {
    board.style.display = 'none';
    return;
  }

  board.style.display = 'block';
  if (countEl) countEl.textContent = `${flows.length}/${flows.length} Đã xong`;
  list.innerHTML = '';

  flows.forEach(fl => {
    const card = document.createElement('div');
    card.className = 'flow-card';
    card.style.borderLeftColor = fl.color || '#38bdf8';

    const pathPreview = fl.path ? fl.path.join(' → ') : '';
    const avoidedCount = fl.avoided_bottlenecks ? fl.avoided_bottlenecks.length : 0;
    const avoidedText = (avoidedCount > 0)
      ? `<span style="color:#f59e0b" title="Đã bẻ lái tránh các điểm nghẽn: ${fl.avoided_bottlenecks.join(', ')}">🛡️ Né ${avoidedCount} điểm nghẽn</span>`
      : `<span style="color:#10b981">✓ Tuyến thông suốt</span>`;

    card.innerHTML = `
      <div class="flow-card-header">
        <span style="color:${fl.color}">${fl.name}</span>
        <span style="color:#f8fafc;font-weight:700">${fl.cost_ms} ms</span>
      </div>
      <div class="flow-card-meta">
        <span>${fl.hops} hops | Prop: ${fl.prop_delay_ms !== undefined ? fl.prop_delay_ms.toFixed(1) : '--'}ms | Q: ${fl.queue_delay_ms !== undefined ? fl.queue_delay_ms.toFixed(1) : '0'}ms</span>
        ${avoidedText}
      </div>
      <div style="font-size:9px;color:#94a3b8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${pathPreview}">
        ${pathPreview}
      </div>
    `;
    list.appendChild(card);
  });
}

async function runMultiFlowSimulation() {
  const btn = document.getElementById('btn-multiflow');
  const spin = document.getElementById('multiflow-spin');
  if (btn) btn.disabled = true;
  if (spin) spin.style.display = 'inline';

  log('⚡ Bắt đầu mô phỏng 5 luồng định tuyến giờ cao điểm...', 'warning');

  try {
    const res = await fetch(`${API_BASE}/routing/multiflow?n_ants=12&n_iter=35`, {
      method: 'POST'
    });
    const data = await res.json();
    if (data.flows) {
      multiFlows = data.flows;
      if (data.loads) nodeLoads = data.loads;
      renderMultiFlowBoard(multiFlows);
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      draw2DBestPath(bestPathCoords, dijkstraPathCoords);
      log(`✅ Đã mô phỏng thành công ${multiFlows.length} luồng đồng thời! Các luồng tự động tách nhánh tránh nhau.`, 'success');
    }
  } catch (err) {
    log(`Lỗi khi mô phỏng đa luồng: ${err}`, 'error');
  } finally {
    if (btn) btn.disabled = false;
    if (spin) spin.style.display = 'none';
  }
}

async function simulateTrafficRandom() {
  log('🎲 Đang bơm tải ngẫu nhiên vào mạng viễn thông...', 'info');
  try {
    const res = await fetch(`${API_BASE}/traffic/simulate?peak_mode=true`, {
      method: 'POST'
    });
    const data = await res.json();
    if (data.loads) {
      nodeLoads = data.loads;
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      if (selectedNode) handleNodeClick(selectedNode);
      log(`🔥 Đã bơm tải thành công: ${data.congested_count || 0} nút nghẽn (vòng màu cam)`, 'warning');
    }
  } catch (err) {
    log(`Lỗi bơm tải: ${err}`, 'error');
  }
}

async function resetTrafficAll() {
  log('🔄 Đang khôi phục tải toàn bộ nút về 10%...', 'info');
  try {
    const res = await fetch(`${API_BASE}/traffic/reset`, {
      method: 'POST'
    });
    const data = await res.json();
    if (data.loads) {
      nodeLoads = data.loads;
      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      if (selectedNode) handleNodeClick(selectedNode);
      log('🟢 Đã khôi phục toàn bộ tải mạng về trạng thái rảnh rỗi (10%)', 'success');
    }
  } catch (err) {
    log(`Lỗi reset tải: ${err}`, 'error');
  }
}

async function applySelectedNodeLoad() {
  if (!selectedNode) return;
  const slider = document.getElementById('hud-load-slider');
  if (!slider) return;
  const loadVal = parseFloat(slider.value) / 100.0;
  const nodeId = selectedNode.id || selectedNode.name;

  log(`📊 Đang đặt tải nút [${nodeId}] = ${(loadVal * 100).toFixed(0)}%...`, 'info');
  try {
    const res = await fetch(`${API_BASE}/traffic/node/${encodeURIComponent(nodeId)}/load?load=${loadVal}`, {
      method: 'POST'
    });
    const data = await res.json();
    if (data.status === 'success') {
      if (!nodeLoads[nodeId]) nodeLoads[nodeId] = {};
      nodeLoads[nodeId].load = data.load;
      nodeLoads[nodeId].is_congested = data.is_congested;
      nodeLoads[nodeId].active_conn = data.active_conn;

      update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
      handleNodeClick(selectedNode);
      log(`✓ Đã áp dụng tải nút [${nodeId}]: ${(data.load * 100).toFixed(0)}% ${data.is_congested ? '🔥 (NGHẼN TẢI)' : ''}`, 'success');

      // Nếu nút này nằm trên lộ trình hiện tại và đang có điểm nguồn/đích -> tự động chạy lại routing để thấy ACO bẻ lái!
      const selSrc = document.getElementById('sel-source')?.value;
      const selDst = document.getElementById('sel-dest')?.value;
      if (selSrc && selDst && bestPathCoords.some(p => (p.node || p.id) === nodeId)) {
        log(`⚡ Nút [${nodeId}] nằm trên đường đi! Đang tự động tìm đường tránh nghẽn...`, 'warning');
        const cmpBox = document.getElementById('compare-result');
        if (cmpBox && cmpBox.style.display !== 'none') {
          runCompare();
        } else {
          sendRoutingRequest();
        }
      }
    }
  } catch (err) {
    log(`Lỗi đặt tải nút: ${err}`, 'error');
  }
}

// ==============================================================
// 8. KHỞI CHẠY KHI TRANG TẢI (MAIN ENTRY POINT)
// ==============================================================
document.addEventListener('DOMContentLoaded', () => {
  // 1. Khởi tạo 2 viewports
  init3DGlobe();
  init2DMap();

  // 2. Kết nối WebSocket server
  connectWebSocket();

  // 3. Tải topology dự phòng sau 1.5s nếu WS chưa sẵn sàng
  setTimeout(() => {
    if (!topology.nodes || topology.nodes.length === 0) {
      loadTopologyFromREST();
    }
  }, 1500);

  // 4. Gắn sự kiện các nút chuyển chế độ 3D / 2D
  document.getElementById('btn-mode-3d').addEventListener('click', () => setViewMode('3d'));
  document.getElementById('btn-mode-2d').addEventListener('click', () => setViewMode('2d'));

  // 5. Gắn sự kiện thanh công cụ 3D Camera
  document.getElementById('btn-view-vn').addEventListener('click', focusVietnam);
  document.getElementById('btn-view-global').addEventListener('click', focusGlobal);
  document.getElementById('btn-view-route').addEventListener('click', focusRoute);
  document.getElementById('btn-toggle-rotate').addEventListener('click', toggleAutoRotate);
  document.getElementById('btn-toggle-isl')?.addEventListener('click', toggleISLDisplay);
  document.getElementById('btn-toggle-clouds')?.addEventListener('click', toggleClouds);
  document.getElementById('btn-toggle-labels')?.addEventListener('click', toggleLabelDisplay);
  document.getElementById('btn-toggle-orbit')?.addEventListener('click', toggleOrbitMotion);
  document.getElementById('btn-orbit-speed')?.addEventListener('click', cycleOrbitSpeed);

  // 6. Gắn sự kiện nút HUD Inspector
  document.getElementById('hud-close').addEventListener('click', closeNodeHUD);
  document.getElementById('hud-set-source').addEventListener('click', setHUDAsSource);
  document.getElementById('hud-set-dest').addEventListener('click', setHUDAsDest);
  document.getElementById('hud-toggle-fail')?.addEventListener('click', () => {
    if (selectedNode) toggleNodeFailure(selectedNode.id || selectedNode.name);
  });

  // 6b. Gắn sự kiện thanh trượt tải trong HUD
  const hudSlider = document.getElementById('hud-load-slider');
  if (hudSlider) {
    hudSlider.addEventListener('input', (e) => {
      const val = e.target.value;
      const valEl = document.getElementById('hud-load-val');
      const statusEl = document.getElementById('hud-load-status');
      if (valEl) valEl.textContent = `${val}%`;
      if (statusEl) {
        const isCong = parseInt(val) >= 75;
        statusEl.textContent = isCong ? '🔥 Nghẽn Tải' : (parseInt(val) >= 50 ? 'Tải Vừa' : 'Ổn Định');
        statusEl.style.color = isCong ? '#f87171' : (parseInt(val) >= 50 ? '#fbbf24' : '#4ade80');
      }
    });
    // Tự động áp dụng mức tải ngay khi kéo thả xong thanh trượt
    hudSlider.addEventListener('change', () => {
      applySelectedNodeLoad();
    });
  }
  document.getElementById('hud-apply-load')?.addEventListener('click', applySelectedNodeLoad);

  // 7. Gắn sự kiện nút chức năng chính
  document.getElementById('btn-route').addEventListener('click', sendRoutingRequest);
  document.getElementById('btn-clear').addEventListener('click', clearPath);
  document.getElementById('btn-reload').addEventListener('click', loadTopologyFromREST);
  document.getElementById('btn-compare').addEventListener('click', runCompare);

  // 7b. Gắn sự kiện Mô Phỏng Sự Cố Mạng (Fault Tolerance)
  document.getElementById('btn-toggle-fail')?.addEventListener('click', () => {
    const sel = document.getElementById('sel-fail-node');
    if (sel && sel.value) {
      toggleNodeFailure(sel.value);
    } else {
      log('Vui lòng chọn nút từ danh sách để đánh sập!', 'warning');
    }
  });
  document.getElementById('btn-restore-all')?.addEventListener('click', restoreAllNodes);

  // 7c. Gắn sự kiện Phân Tải & Giờ Cao Điểm (Load Balancing & Congestion)
  document.getElementById('btn-multiflow')?.addEventListener('click', runMultiFlowSimulation);
  document.getElementById('btn-simulate-traffic')?.addEventListener('click', simulateTrafficRandom);
  document.getElementById('btn-reset-traffic')?.addEventListener('click', resetTrafficAll);

  // 8. Tự động lắng nghe thay đổi dropdown source / dest để vẽ vòng xung kích & highlight
  document.getElementById('sel-source').addEventListener('change', () => {
    update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
    update2DSelectionMarkers();
  });
  document.getElementById('sel-dest').addEventListener('change', () => {
    update3DGlobeData(topology.nodes, topology.edges, bestPathCoords);
    update2DSelectionMarkers();
  });

  // 9. Lắng nghe phím Enter
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !isRunning) sendRoutingRequest();
  });

  // 10. Sidebar: Thu gọn / Mở rộng
  const sidebarEl = document.getElementById('sidebar');
  const toggleBtn  = document.getElementById('btn-sidebar-toggle');
  const toggleIcon = document.getElementById('sidebar-toggle-icon');

  if (toggleBtn && sidebarEl) {
    toggleBtn.addEventListener('click', () => {
      const isCollapsed = sidebarEl.classList.toggle('collapsed');
      toggleIcon.textContent = isCollapsed ? '▶' : '◀';
    });
  }

  // 11. Collapsible sidebar sections
  document.querySelectorAll('.sb-section-collapsible').forEach(header => {
    const targetId = header.dataset.target;
    const body = targetId ? document.getElementById(targetId) : null;
    if (!body) return;

    // Các section nâng cao mặc định đóng để không bị scroll quá dài
    const defaultClosed = ['collapse-compare', 'collapse-load', 'collapse-db', 'collapse-legend'];
    if (defaultClosed.includes(targetId)) {
      body.classList.add('collapsed');
      header.classList.add('collapsed');
    }

    header.addEventListener('click', () => {
      const isNowCollapsed = body.classList.toggle('collapsed');
      header.classList.toggle('collapsed', isNowCollapsed);
      if (!isNowCollapsed && targetId === 'collapse-db') {
        fetchDatabaseStats();
      }
    });
  });

  // 12. Cập nhật thống kê Database SQLite
  async function fetchDatabaseStats() {
    try {
      const res = await fetch(`${API_BASE}/db/stats`);
      if (res.ok) {
        const data = await res.json();
        const routesEl = document.getElementById('db-total-routes');
        const sizeEl = document.getElementById('db-size');
        if (routesEl) routesEl.textContent = data.total_routing_sessions || 0;
        if (sizeEl) sizeEl.textContent = `${data.db_size_kb || 0} KB`;
      }
    } catch (e) {
      // Bỏ qua nếu chưa tải xong
    }
  }
  fetchDatabaseStats();

  // 13. Tiến trình ACO: hiện card-progress khi đang chạy, ẩn khi xong
  const cardProgress = document.getElementById('card-progress');
  const origSetRunning = typeof setRunningState === 'function' ? setRunningState : null;
  // Đã được xử lý nội tuyến trong setRunningState — hook thêm vào DOM
  const progressObserver = new MutationObserver(() => {
    const fill = document.getElementById('progress-fill');
    if (!cardProgress || !fill) return;
    const pct = parseFloat(fill.style.width) || 0;
    cardProgress.style.display = (pct > 0 && pct < 100) ? 'block' : 'none';
  });
  const progressFill = document.getElementById('progress-fill');
  if (progressFill && cardProgress) {
    progressObserver.observe(progressFill, { attributes: true, attributeFilter: ['style'] });
  }

  log('SAGSINs Digital Twin Dashboard khởi chạy thành công 🛰️', 'success');
});

