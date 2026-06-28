/*
  No build step, no charting library — plain SVG drawn by hand. For a
  single time-series-per-route line chart with a band and dots, a
  library is overhead; this keeps the frontend a static file you can
  drop on Vercel/any static host with zero dependencies.

  API_BASE is auto-detected: same-origin in production (frontend served
  by the same host as the API, or proxied), overridable via ?api= query
  param for local dev against a backend on a different port.
*/

const params = new URLSearchParams(location.search);
const API_BASE = params.get('api') || '';
const WS_URL = (API_BASE || location.origin).replace(/^http/, 'ws') + '/ws/live';

const ROUTE_COLORS = {
  '1': '#EE352E', '4': '#00933C', '6': '#00933C',
  'L': '#A7A9AC', 'G': '#6CBE45', 'N': '#FCCC0A',
};

let routes = [];
let activeRoute = null;
let history = {};      // route_id -> [{t, headway, anomaly}]
let latestByRoute = {}; // route_id -> latest reading payload
let anomalyLog = [];
let ws = null;

const HISTORY_WINDOW_MIN = 60;
const MAX_POINTS = 200;

init();

async function init() {
  startClock();
  await loadRoutes();
  await Promise.all(routes.map(loadRouteHistory));
  await loadRecentAnomalies();
  renderRail();
  renderVitals();
  if (routes.length) selectRoute(routes[0]);
  connectWebSocket();
}

function startClock() {
  const el = document.getElementById('clock');
  setInterval(() => {
    el.textContent = new Date().toLocaleTimeString('en-US', { hour12: false });
  }, 1000);
}

async function loadRoutes() {
  try {
    const res = await fetch(`${API_BASE}/api/routes`);
    const data = await res.json();
    routes = data.routes;
  } catch (e) {
    console.error('failed to load routes', e);
    routes = [];
  }
}

async function loadRouteHistory(routeId) {
  try {
    const res = await fetch(`${API_BASE}/api/readings?route_id=${routeId}&minutes=${HISTORY_WINDOW_MIN}`);
    const data = await res.json();
    history[routeId] = data.readings.map(r => ({
      t: new Date(r.observed_at).getTime(),
      headway: r.headway_seconds,
      anomaly: false,
    }));
    if (history[routeId].length) {
      const last = data.readings[data.readings.length - 1];
      latestByRoute[routeId] = {
        route_id: routeId,
        headway_seconds: last.headway_seconds,
        active_trains: last.active_trains,
        is_anomaly: false,
      };
    }
  } catch (e) {
    console.error(`failed to load history for ${routeId}`, e);
    history[routeId] = [];
  }
}

async function loadRecentAnomalies() {
  try {
    const res = await fetch(`${API_BASE}/api/anomalies?minutes=1440&limit=50`);
    const data = await res.json();
    anomalyLog = data.anomalies;
    renderLog();
  } catch (e) {
    console.error('failed to load anomalies', e);
  }
}

function connectWebSocket() {
  setConnLabel('connecting…', null);
  ws = new WebSocket(WS_URL);

  ws.onopen = () => setConnLabel('live', true);
  ws.onclose = () => {
    setConnLabel('reconnecting…', false);
    setTimeout(connectWebSocket, 3000);
  };
  ws.onerror = () => setConnLabel('connection error', false);

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'reading') handleReading(msg.payload);
    if (msg.type === 'anomaly') handleAnomaly(msg.payload);
  };
}

function setConnLabel(text, isLive) {
  document.getElementById('conn-label').textContent = text;
  const dot = document.getElementById('conn-dot');
  dot.className = 'status__dot' + (isLive === true ? ' live' : isLive === false ? ' down' : '');
}

function handleReading(payload) {
  const { route_id } = payload;
  if (!routes.includes(route_id)) {
    routes.push(route_id);
    renderRail();
  }
  if (!history[route_id]) history[route_id] = [];
  history[route_id].push({
    t: new Date(payload.observed_at).getTime(),
    headway: payload.headway_seconds,
    anomaly: payload.is_anomaly,
  });
  if (history[route_id].length > MAX_POINTS) history[route_id].shift();

  latestByRoute[route_id] = payload;
  renderVitals();
  if (route_id === activeRoute) renderChart();
}

function handleAnomaly(payload) {
  anomalyLog.unshift({
    route_id: payload.route_id,
    detected_at: payload.observed_at,
    method: payload.method,
    score: payload.score,
    headway_seconds: payload.headway_seconds,
  });
  anomalyLog = anomalyLog.slice(0, 50);
  renderLog();
  flashVital(payload.route_id);
}

function flashVital(routeId) {
  const card = document.querySelector(`.vital-card[data-route="${routeId}"]`);
  if (!card) return;
  card.classList.add('flash');
  setTimeout(() => card.classList.remove('flash'), 1200);
}

/* ---------- rendering ---------- */

function renderRail() {
  const rail = document.getElementById('route-rail');
  rail.querySelectorAll('.rail__btn').forEach(el => el.remove());
  routes.forEach(routeId => {
    const btn = document.createElement('button');
    btn.className = 'rail__btn' + (routeId === activeRoute ? ' active' : '');
    btn.innerHTML = `<span class="rail__chip" style="background:${ROUTE_COLORS[routeId] || '#444'};color:#0B0E11">${routeId}</span><span>Line ${routeId}</span>`;
    btn.onclick = () => selectRoute(routeId);
    rail.appendChild(btn);
  });
}

function selectRoute(routeId) {
  activeRoute = routeId;
  document.getElementById('chart-title').textContent = `LINE ${routeId} — HEADWAY (last ${HISTORY_WINDOW_MIN}min)`;
  renderRail();
  renderChart();
}

function renderVitals() {
  const strip = document.getElementById('vitals-strip');
  strip.innerHTML = '';
  routes.forEach(routeId => {
    const latest = latestByRoute[routeId];
    const card = document.createElement('div');
    card.className = 'vital-card';
    card.dataset.route = routeId;

    if (!latest) {
      card.innerHTML = `
        <div class="vital-card__route"><span>LINE ${routeId}</span></div>
        <div class="vital-card__value">—</div>
        <div class="vital-card__sub">awaiting first poll</div>`;
    } else {
      const mins = Math.floor(latest.headway_seconds / 60);
      const secs = Math.round(latest.headway_seconds % 60);
      const subClass = latest.is_anomaly ? 'anomalous' : 'healthy';
      const subText = latest.is_anomaly ? '⚠ anomalous gap' : 'within normal range';
      card.innerHTML = `
        <div class="vital-card__route">
          <span>LINE ${routeId}</span>
          <span>${latest.active_trains ?? ''} active</span>
        </div>
        <div class="vital-card__value">${mins}<span class="unit">m</span> ${secs}<span class="unit">s</span></div>
        <div class="vital-card__sub ${subClass}">${subText}</div>`;
    }
    card.onclick = () => selectRoute(routeId);
    strip.appendChild(card);
  });
}

function renderLog() {
  const list = document.getElementById('log-list');
  document.getElementById('log-count').textContent = `${anomalyLog.length} flagged`;
  if (!anomalyLog.length) {
    list.innerHTML = '<div class="log-empty">No anomalies yet. The console will populate as the feed runs.</div>';
    return;
  }
  list.innerHTML = anomalyLog.slice(0, 50).map(a => {
    const time = new Date(a.detected_at).toLocaleTimeString('en-US', { hour12: false });
    const mins = Math.floor(a.headway_seconds / 60);
    const secs = Math.round(a.headway_seconds % 60);
    return `
      <div class="log-row">
        <span class="log-row__time">${time}</span>
        <span class="log-row__route" style="background:${ROUTE_COLORS[a.route_id] || '#444'};color:#0B0E11">${a.route_id}</span>
        <span class="log-row__detail">gap of ${mins}m ${secs}s · ${a.method}</span>
        <span class="log-row__score">z=${a.score.toFixed(2)}</span>
      </div>`;
  }).join('');
}

function renderChart() {
  const svg = document.getElementById('chart');
  const data = history[activeRoute] || [];
  svg.innerHTML = '';
  if (data.length < 2) {
    svg.innerHTML = `<text x="500" y="180" fill="#6B7785" font-size="13" text-anchor="middle">collecting data…</text>`;
    return;
  }

  const W = 1000, H = 360, PAD = { top: 20, right: 20, bottom: 30, left: 50 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  const tMin = data[0].t, tMax = data[data.length - 1].t;
  const tRange = Math.max(tMax - tMin, 1);
  const values = data.map(d => d.headway);
  const vMax = Math.max(...values) * 1.15;
  const vMin = 0;

  const x = t => PAD.left + ((t - tMin) / tRange) * plotW;
  const y = v => PAD.top + plotH - ((v - vMin) / (vMax - vMin)) * plotH;

  // rolling mean/std band, computed client-side for display only
  // (the real detection happens server-side; this is a visual aid)
  const band = computeBand(values);

  let svgContent = '';

  // expected range band
  let bandPath = `M ${x(data[0].t)} ${y(band[0].upper)} `;
  data.forEach((d, i) => { bandPath += `L ${x(d.t)} ${y(band[i].upper)} `; });
  for (let i = data.length - 1; i >= 0; i--) { bandPath += `L ${x(data[i].t)} ${y(band[i].lower)} `; }
  bandPath += 'Z';
  svgContent += `<path d="${bandPath}" fill="rgba(255,176,0,0.08)" stroke="none" />`;

  // gridlines + y labels
  for (let i = 0; i <= 4; i++) {
    const v = vMin + (vMax - vMin) * (i / 4);
    const yy = y(v);
    svgContent += `<line x1="${PAD.left}" y1="${yy}" x2="${W - PAD.right}" y2="${yy}" stroke="#232A33" stroke-width="1" />`;
    svgContent += `<text x="${PAD.left - 8}" y="${yy + 4}" fill="#6B7785" font-size="10" text-anchor="end">${Math.round(v)}s</text>`;
  }

  // headway line
  let linePath = `M ${x(data[0].t)} ${y(data[0].headway)} `;
  data.forEach(d => { linePath += `L ${x(d.t)} ${y(d.headway)} `; });
  svgContent += `<path d="${linePath}" fill="none" stroke="#FFB000" stroke-width="2" />`;

  // anomaly dots
  data.forEach(d => {
    if (d.anomaly) {
      svgContent += `<circle cx="${x(d.t)}" cy="${y(d.headway)}" r="5" fill="#FF3B30" stroke="#0B0E11" stroke-width="1.5" />`;
    }
  });

  // x-axis time labels (start / mid / end)
  [0, Math.floor(data.length / 2), data.length - 1].forEach(i => {
    const d = data[i];
    const label = new Date(d.t).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    svgContent += `<text x="${x(d.t)}" y="${H - 8}" fill="#6B7785" font-size="10" text-anchor="middle">${label}</text>`;
  });

  svg.innerHTML = svgContent;
}

function computeBand(values, window = 15, k = 2) {
  const band = [];
  for (let i = 0; i < values.length; i++) {
    const start = Math.max(0, i - window);
    const slice = values.slice(start, i + 1);
    const mean = slice.reduce((a, b) => a + b, 0) / slice.length;
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / slice.length;
    const std = Math.sqrt(variance);
    band.push({ upper: mean + k * std, lower: Math.max(0, mean - k * std) });
  }
  return band;
}

window.addEventListener('resize', () => { if (activeRoute) renderChart(); });
