/* global L */

const state = {
  manifest: null,
  data: null,
  thresholdIndex: 1,
  periodId: null,
  season: 'annual',
  boundary: null,
  pointLayer: null,
  boundaryLayer: null,
  cache: new Map(),
};

const seasonMonths = {
  annual: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
  djf: [11, 0, 1],
  mam: [2, 3, 4],
  jja: [5, 6, 7],
  son: [8, 9, 10],
};

const seasonNames = {
  annual: 'annual',
  djf: 'winter',
  mam: 'spring',
  jja: 'summer',
  son: 'fall',
};

const map = L.map('map', {
  center: [30.15, -90.25],
  zoom: 8,
  minZoom: 6,
  maxZoom: 13,
  preferCanvas: true,
  zoomControl: true,
});

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);

const canvasRenderer = L.canvas({ padding: 0.5, tolerance: 5 });

const els = {
  thresholdButtons: document.getElementById('thresholdButtons'),
  periodSelect: document.getElementById('periodSelect'),
  seasonSelect: document.getElementById('seasonSelect'),
  medianMetric: document.getElementById('medianMetric'),
  medianDetail: document.getElementById('medianDetail'),
  maxMetric: document.getElementById('maxMetric'),
  p90Metric: document.getElementById('p90Metric'),
  pointMetric: document.getElementById('pointMetric'),
  interpretationText: document.getElementById('interpretationText'),
  hotspotList: document.getElementById('hotspotList'),
  status: document.getElementById('status'),
  legend: document.getElementById('legend'),
  aboutBtn: document.getElementById('aboutBtn'),
  aboutDialog: document.getElementById('aboutDialog'),
};

els.aboutBtn.addEventListener('click', () => els.aboutDialog.showModal());
els.seasonSelect.addEventListener('change', () => {
  state.season = els.seasonSelect.value;
  render();
});
els.periodSelect.addEventListener('change', async () => {
  state.periodId = els.periodSelect.value;
  await loadPeriod(state.periodId);
  render();
});

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  return sorted[base + 1] !== undefined
    ? sorted[base] + rest * (sorted[base + 1] - sorted[base])
    : sorted[base];
}

function formatRate(v) {
  if (!Number.isFinite(v)) return '—';
  if (v >= 10) return `${v.toFixed(1)} days/yr`;
  if (v >= 1) return `${v.toFixed(2)} days/yr`;
  if (v >= 0.1) return `${v.toFixed(3)} days/yr`;
  if (v > 0) return `${v.toFixed(4)} days/yr`;
  return '0 days/yr';
}

function formatShortRate(v) {
  if (!Number.isFinite(v)) return '—';
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.1) return v.toFixed(3);
  if (v > 0) return v.toFixed(4);
  return '0';
}

function recurrenceText(rate) {
  if (!rate) return 'No exceedance in this period';
  const years = 1 / rate;
  if (years < 1) {
    const days = 365.2425 / (rate * 365.2425);
    return rate >= 1 ? `about ${rate.toFixed(1)} exceedance days each year` : `about one exceedance day every ${days.toFixed(1)} years`;
  }
  if (years < 10) return `about one exceedance day every ${years.toFixed(1)} years`;
  return `about one exceedance day every ${Math.round(years)} years`;
}

function rateForPoint(point) {
  const months = seasonMonths[state.season];
  const counts = point.c[state.thresholdIndex];
  const total = months.reduce((sum, m) => sum + (counts[m] || 0), 0);
  return total / state.data.period.years;
}

function countForPoint(point) {
  const months = seasonMonths[state.season];
  const counts = point.c[state.thresholdIndex];
  return months.reduce((sum, m) => sum + (counts[m] || 0), 0);
}

function colorFor(value, scaleMax) {
  const colors = ['#18334a', '#1f6475', '#2a8c82', '#67ad6c', '#c1c85c', '#f2b24f', '#ec7845', '#d84455', '#9e2f68'];
  if (value <= 0 || scaleMax <= 0) return '#d4dde4';
  const t = Math.min(1, Math.sqrt(value / scaleMax));
  return colors[Math.min(colors.length - 1, Math.floor(t * colors.length))];
}

function dateText(yyyymmdd) {
  if (!yyyymmdd) return '—';
  const s = String(yyyymmdd);
  if (s.length !== 8) return s;
  return `${s.slice(4, 6)}/${s.slice(6, 8)}/${s.slice(0, 4)}`;
}

function tooltipHtml(point, rate) {
  const threshold = state.manifest.thresholds_in[state.thresholdIndex];
  const count = countForPoint(point);
  const maxIn = point.mx ? point.mx / 25.4 : null;
  return `
    <div class="tooltip-title">≥${threshold}" in 24 hr</div>
    <div><strong>${formatRate(rate)}</strong></div>
    <div class="tooltip-small">
      ${count} exceedance day${count === 1 ? '' : 's'} in ${state.data.period.label}<br>
      ${recurrenceText(rate)}<br>
      Grid center: ${point.lat.toFixed(3)}°, ${point.lon.toFixed(3)}°<br>
      Period max: ${maxIn ? `${maxIn.toFixed(2)}"` : '—'} ${point.md ? `on ${dateText(point.md)}` : ''}
    </div>`;
}

function haversineMiles(a, b) {
  const r = 3958.7613;
  const toRad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * toRad;
  const dLon = (b.lon - a.lon) * toRad;
  const lat1 = a.lat * toRad;
  const lat2 = b.lat * toRad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}

function findHotspots(pointsWithRates, limit = 8, separationMiles = 15) {
  const sorted = [...pointsWithRates].filter(x => x.rate > 0).sort((a, b) => b.rate - a.rate);
  const chosen = [];
  for (const candidate of sorted) {
    if (chosen.every(existing => haversineMiles(existing.point, candidate.point) >= separationMiles)) {
      chosen.push(candidate);
      if (chosen.length >= limit) break;
    }
  }
  return chosen;
}

function updateHotspots(pointsWithRates) {
  const hotspots = findHotspots(pointsWithRates);
  els.hotspotList.innerHTML = '';
  if (!hotspots.length) {
    els.hotspotList.innerHTML = '<p class="muted">No exceedances found for this threshold and period.</p>';
    return;
  }
  hotspots.forEach((item, i) => {
    const button = document.createElement('button');
    button.className = 'hotspot-item';
    button.type = 'button';
    button.innerHTML = `
      <span class="hotspot-rank">${i + 1}</span>
      <span>
        <strong>${item.point.lat.toFixed(3)}°, ${item.point.lon.toFixed(3)}°</strong>
        <div class="hotspot-coord">${recurrenceText(item.rate)}</div>
      </span>
      <span class="hotspot-value">${formatShortRate(item.rate)}/yr</span>`;
    button.addEventListener('click', () => {
      map.flyTo([item.point.lat, item.point.lon], 10, { duration: 0.7 });
    });
    els.hotspotList.appendChild(button);
  });
}

function updateStats(rates) {
  const sorted = rates.filter(Number.isFinite).sort((a, b) => a - b);
  const median = quantile(sorted, 0.5);
  const p90 = quantile(sorted, 0.9);
  const max = sorted.length ? sorted[sorted.length - 1] : 0;
  els.medianMetric.textContent = formatRate(median);
  els.medianDetail.textContent = recurrenceText(median);
  els.maxMetric.textContent = formatShortRate(max) + '/yr';
  els.p90Metric.textContent = formatShortRate(p90) + '/yr';
  els.pointMetric.textContent = state.data.points.length.toLocaleString();

  const threshold = state.manifest.thresholds_in[state.thresholdIndex];
  const season = seasonNames[state.season];
  els.interpretationText.textContent = `Each pixel shows the average number of ${season} days per year with at least ${threshold}" of precipitation in the fixed daily 24-hour accumulation. The hot-spot list suppresses maxima within 15 miles of a higher-ranked point.`;
}

function updateLegend(rates) {
  const positive = rates.filter(v => v > 0).sort((a, b) => a - b);
  const scaleMax = positive.length ? Math.max(quantile(positive, 0.95), positive[positive.length - 1] * 0.35) : 1;
  const threshold = state.manifest.thresholds_in[state.thresholdIndex];
  els.legend.innerHTML = `
    <div class="legend-title">≥${threshold}" · exceedance days/yr</div>
    <div class="legend-ramp" style="background:linear-gradient(90deg,#18334a,#1f6475,#2a8c82,#67ad6c,#c1c85c,#f2b24f,#ec7845,#d84455,#9e2f68)"></div>
    <div class="legend-labels"><span>0</span><span>${formatShortRate(scaleMax / 2)}</span><span>≥${formatShortRate(scaleMax)}</span></div>
    <div class="tooltip-small" style="margin-top:6px">Scale capped near the upper tail so local structure remains visible. Hover for exact values.</div>`;
  return scaleMax;
}

function render() {
  if (!state.data || !state.manifest) return;
  if (state.pointLayer) state.pointLayer.remove();

  const pointsWithRates = state.data.points.map(point => ({ point, rate: rateForPoint(point) }));
  const rates = pointsWithRates.map(x => x.rate);
  const scaleMax = updateLegend(rates);
  updateStats(rates);
  updateHotspots(pointsWithRates);

  const layer = L.layerGroup();
  for (const { point, rate } of pointsWithRates) {
    const marker = L.circleMarker([point.lat, point.lon], {
      renderer: canvasRenderer,
      radius: 4.1,
      stroke: false,
      fill: true,
      fillColor: colorFor(rate, scaleMax),
      fillOpacity: rate > 0 ? 0.88 : 0.28,
      bubblingMouseEvents: true,
    });
    marker.bindTooltip(() => tooltipHtml(point, rate), {
      className: 'grid-tooltip',
      direction: 'top',
      opacity: 0.98,
      sticky: true,
    });
    marker.addTo(layer);
  }
  layer.addTo(map);
  state.pointLayer = layer;

  document.querySelectorAll('.threshold-button').forEach((btn, idx) => {
    btn.classList.toggle('active', idx === state.thresholdIndex);
    btn.setAttribute('aria-pressed', idx === state.thresholdIndex ? 'true' : 'false');
  });
}

async function loadPeriod(periodId) {
  const meta = state.manifest.periods.find(p => p.id === periodId);
  if (!meta) throw new Error(`Unknown period: ${periodId}`);
  els.status.classList.remove('ready');
  els.status.textContent = `Loading ${meta.label}…`;
  if (!state.cache.has(periodId)) {
    const response = await fetch(meta.file, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Could not load ${meta.file}`);
    state.cache.set(periodId, await response.json());
  }
  state.data = state.cache.get(periodId);
  els.status.textContent = `Loaded ${meta.label}`;
  els.status.classList.add('ready');
}

function buildControls() {
  els.thresholdButtons.innerHTML = '';
  state.manifest.thresholds_in.forEach((threshold, idx) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'threshold-button';
    button.textContent = `≥${threshold}"`;
    button.setAttribute('aria-pressed', idx === state.thresholdIndex ? 'true' : 'false');
    button.addEventListener('click', () => {
      state.thresholdIndex = idx;
      render();
    });
    els.thresholdButtons.appendChild(button);
  });

  els.periodSelect.innerHTML = '';
  state.manifest.periods.forEach(period => {
    const option = document.createElement('option');
    option.value = period.id;
    option.textContent = period.label;
    els.periodSelect.appendChild(option);
  });
}

async function loadBoundary() {
  try {
    const response = await fetch(state.manifest.cwa_file, { cache: 'no-cache' });
    if (!response.ok) return;
    state.boundary = await response.json();
    if (state.boundaryLayer) state.boundaryLayer.remove();
    state.boundaryLayer = L.geoJSON(state.boundary, {
      style: {
        color: '#ffffff',
        weight: 2.2,
        opacity: 0.95,
        fill: false,
        dashArray: '7 5',
      },
      interactive: false,
    }).addTo(map);
    map.fitBounds(state.boundaryLayer.getBounds().pad(0.04));
  } catch (error) {
    console.warn('CWA boundary unavailable', error);
  }
}

async function init() {
  try {
    const response = await fetch('data/manifest.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error('manifest unavailable');
    state.manifest = await response.json();

    if (state.manifest.status !== 'ready') {
      els.status.textContent = 'Climatology build has not finished yet.';
      els.medianDetail.textContent = 'The first NOAA data build runs automatically in GitHub Actions.';
      return;
    }

    const twoInchIndex = state.manifest.thresholds_in.indexOf(2);
    state.thresholdIndex = twoInchIndex >= 0 ? twoInchIndex : 0;
    state.periodId = state.manifest.periods[0].id;
    buildControls();
    await Promise.all([loadPeriod(state.periodId), loadBoundary()]);
    render();
  } catch (error) {
    console.error(error);
    els.status.textContent = 'Could not load climatology data.';
    els.medianDetail.textContent = 'Check the GitHub Actions build and docs/data/manifest.json.';
  }
}

init();
