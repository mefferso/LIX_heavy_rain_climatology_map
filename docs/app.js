/* global L */

const state = {
  manifest: null,
  mode: 'period',
  displayMode: 'exceedance',
  selectionId: null,
  data: null,
  comparison: null,
  thresholdIndex: 1,
  rangeIndex: 0,
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

const rangeSpecs = [
  { label: '1–<2"', lowerIndex: 0, upperIndex: 1 },
  { label: '2–<3"', lowerIndex: 1, upperIndex: 2 },
  { label: '3–<5"', lowerIndex: 2, upperIndex: 3 },
  { label: '5–<8"', lowerIndex: 3, upperIndex: 4 },
  { label: '8–<10"', lowerIndex: 4, upperIndex: 5 },
  { label: '≥10"', lowerIndex: 5, upperIndex: null },
];

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
  displayModeButtons: document.getElementById('displayModeButtons'),
  measureHeading: document.getElementById('measureHeading'),
  thresholdButtons: document.getElementById('thresholdButtons'),
  periodSelect: document.getElementById('periodSelect'),
  seasonSelect: document.getElementById('seasonSelect'),
  medianLabel: document.getElementById('medianLabel'),
  medianMetric: document.getElementById('medianMetric'),
  medianDetail: document.getElementById('medianDetail'),
  maxLabel: document.getElementById('maxLabel'),
  maxMetric: document.getElementById('maxMetric'),
  p90Label: document.getElementById('p90Label'),
  p90Metric: document.getElementById('p90Metric'),
  pointMetric: document.getElementById('pointMetric'),
  interpretationText: document.getElementById('interpretationText'),
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
  state.selectionId = els.periodSelect.value;
  await loadSelection(state.selectionId);
  render();
});
els.displayModeButtons.addEventListener('click', event => {
  const button = event.target.closest('.mode-button');
  if (!button) return;
  state.displayMode = button.dataset.mode;
  document.querySelectorAll('.mode-button').forEach(btn => {
    const active = btn.dataset.mode === state.displayMode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  buildMeasureButtons();
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
  const a = Math.abs(v);
  if (a >= 1) return v.toFixed(2);
  if (a >= 0.1) return v.toFixed(3);
  if (a > 0) return v.toFixed(4);
  return '0';
}

function formatDelta(v, long = true) {
  if (!Number.isFinite(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return long ? `${sign}${formatShortRate(v)} days/yr` : `${sign}${formatShortRate(v)}`;
}

function frequencyText(rate) {
  if (!rate) return 'No matching days in this period';
  const years = 1 / rate;
  if (rate >= 1) return `about ${rate.toFixed(1)} matching days each year`;
  if (years < 10) return `about one matching day every ${years.toFixed(1)} years`;
  return `about one matching day every ${Math.round(years)} years`;
}

function currentMeasure() {
  if (state.displayMode === 'range') {
    return { ...rangeSpecs[state.rangeIndex], kind: 'range' };
  }
  const threshold = state.manifest.thresholds_in[state.thresholdIndex];
  return { label: `≥${threshold}"`, threshold, lowerIndex: state.thresholdIndex, upperIndex: null, kind: 'exceedance' };
}

function monthlyCountsForPoint(point) {
  if (state.displayMode === 'exceedance') return point.c[state.thresholdIndex];
  const spec = rangeSpecs[state.rangeIndex];
  const lower = point.c[spec.lowerIndex];
  if (spec.upperIndex === null) return lower;
  const upper = point.c[spec.upperIndex];
  return lower.map((value, month) => Math.max(0, value - (upper[month] || 0)));
}

function countForPoint(point) {
  const months = seasonMonths[state.season];
  const counts = monthlyCountsForPoint(point);
  return months.reduce((sum, month) => sum + (counts[month] || 0), 0);
}

function rateForPoint(point, data) {
  return countForPoint(point) / data.period.years;
}

function sequentialColor(value, scaleMax) {
  const colors = ['#18334a', '#1f6475', '#2a8c82', '#67ad6c', '#c1c85c', '#f2b24f', '#ec7845', '#d84455', '#9e2f68'];
  if (value <= 0 || scaleMax <= 0) return '#d4dde4';
  const t = Math.min(1, Math.sqrt(value / scaleMax));
  return colors[Math.min(colors.length - 1, Math.floor(t * colors.length))];
}

function comparisonColor(value, scaleMax) {
  if (!Number.isFinite(value) || scaleMax <= 0) return '#e5e7eb';
  const t = Math.min(1, Math.abs(value) / scaleMax);
  if (Math.abs(value) < scaleMax * 0.04) return '#e5e7eb';
  const negative = ['#dbeafe', '#93c5fd', '#60a5fa', '#2563eb', '#1e3a8a'];
  const positive = ['#fff7d6', '#fcd34d', '#fb923c', '#ef4444', '#9f1239'];
  const palette = value < 0 ? negative : positive;
  return palette[Math.min(palette.length - 1, Math.floor(t * palette.length))];
}

function dateText(yyyymmdd) {
  if (!yyyymmdd) return '—';
  const s = String(yyyymmdd);
  if (s.length !== 8) return s;
  return `${s.slice(4, 6)}/${s.slice(6, 8)}/${s.slice(0, 4)}`;
}

function periodTooltipHtml(point, rate) {
  const measure = currentMeasure();
  const count = countForPoint(point);
  const maxIn = point.mx ? point.mx / 25.4 : null;
  const dayLabel = measure.kind === 'range' ? 'range day' : 'exceedance day';
  return `
    <div class="tooltip-title">${measure.label} in 24 hr</div>
    <div><strong>${formatRate(rate)}</strong></div>
    <div class="tooltip-small">
      ${count} ${dayLabel}${count === 1 ? '' : 's'} in ${state.data.period.label}<br>
      ${frequencyText(rate)}<br>
      Grid center: ${point.lat.toFixed(3)}°, ${point.lon.toFixed(3)}°<br>
      Period max: ${maxIn ? `${maxIn.toFixed(2)}"` : '—'} ${point.md ? `on ${dateText(point.md)}` : ''}
    </div>`;
}

function comparisonTooltipHtml(item) {
  const measure = currentMeasure();
  const c = state.comparison;
  let pct = '—';
  if (item.olderRate > 0) pct = `${item.delta >= 0 ? '+' : ''}${((item.delta / item.olderRate) * 100).toFixed(1)}%`;
  return `
    <div class="tooltip-title">${measure.label} in 24 hr · change</div>
    <div><strong>${formatDelta(item.delta)}</strong></div>
    <div class="tooltip-small">
      ${c.newer.label}: ${formatRate(item.newerRate)}<br>
      ${c.older.label}: ${formatRate(item.olderRate)}<br>
      Relative change: ${pct}<br>
      Grid center: ${item.point.lat.toFixed(3)}°, ${item.point.lon.toFixed(3)}°
    </div>`;
}

function renderItems() {
  if (state.mode === 'period') {
    return state.data.points.map(point => ({ point, rate: rateForPoint(point, state.data) }));
  }
  return state.comparison.newerData.points.map((point, i) => {
    const olderPoint = state.comparison.olderData.points[i];
    const newerRate = rateForPoint(point, state.comparison.newerData);
    const olderRate = rateForPoint(olderPoint, state.comparison.olderData);
    return { point, newerRate, olderRate, delta: newerRate - olderRate };
  });
}

function updateStats(items) {
  const measure = currentMeasure();
  const season = seasonNames[state.season];
  if (state.mode === 'period') {
    const rates = items.map(x => x.rate).filter(Number.isFinite).sort((a, b) => a - b);
    const median = quantile(rates, 0.5);
    const p90 = quantile(rates, 0.9);
    const max = rates.length ? rates[rates.length - 1] : 0;
    els.medianLabel.textContent = 'CWA median';
    els.medianMetric.textContent = formatRate(median);
    els.medianDetail.textContent = frequencyText(median);
    els.maxLabel.textContent = 'Wettest grid';
    els.maxMetric.textContent = formatShortRate(max) + '/yr';
    els.p90Label.textContent = '90th percentile';
    els.p90Metric.textContent = formatShortRate(p90) + '/yr';
    els.pointMetric.textContent = state.data.points.length.toLocaleString();

    if (measure.kind === 'range') {
      els.interpretationText.textContent = `Each pixel shows the average number of ${season} days per year with a daily precipitation total in the ${measure.label} range during ${state.data.period.label}. Ranges are mutually exclusive.`;
    } else {
      els.interpretationText.textContent = `Each pixel shows the average number of ${season} days per year with at least ${measure.threshold}" of precipitation during ${state.data.period.label}.`;
    }
  } else {
    const deltas = items.map(x => x.delta).filter(Number.isFinite).sort((a, b) => a - b);
    const median = quantile(deltas, 0.5);
    const p90 = quantile(deltas, 0.9);
    const max = deltas.length ? deltas[deltas.length - 1] : 0;
    els.medianLabel.textContent = 'CWA median change';
    els.medianMetric.textContent = formatDelta(median);
    els.medianDetail.textContent = `${state.comparison.newer.label} minus ${state.comparison.older.label}`;
    els.maxLabel.textContent = 'Largest increase';
    els.maxMetric.textContent = formatDelta(max, false) + '/yr';
    els.p90Label.textContent = '90th percentile change';
    els.p90Metric.textContent = formatDelta(p90, false) + '/yr';
    els.pointMetric.textContent = items.length.toLocaleString();

    const descriptor = measure.kind === 'range' ? `${measure.label} range days` : `${measure.label} exceedance days`;
    els.interpretationText.textContent = `Each pixel shows the change in average ${season} ${descriptor} per year: ${state.comparison.newer.label} minus ${state.comparison.older.label}. Warm colors mean more frequent days; blue means less frequent.`;
  }
}

function updateLegend(items) {
  const measure = currentMeasure();
  if (state.mode === 'period') {
    const rates = items.map(x => x.rate);
    const positive = rates.filter(v => v > 0).sort((a, b) => a - b);
    const scaleMax = positive.length ? Math.max(quantile(positive, 0.95), positive[positive.length - 1] * 0.35) : 1;
    els.legend.innerHTML = `
      <div class="legend-title">${measure.label} · days/yr</div>
      <div class="legend-ramp" style="background:linear-gradient(90deg,#18334a,#1f6475,#2a8c82,#67ad6c,#c1c85c,#f2b24f,#ec7845,#d84455,#9e2f68)"></div>
      <div class="legend-labels"><span>0</span><span>${formatShortRate(scaleMax / 2)}</span><span>≥${formatShortRate(scaleMax)}</span></div>
      <div class="tooltip-small" style="margin-top:6px">Scale capped near the upper tail so local structure remains visible. Hover for exact values.</div>`;
    return scaleMax;
  }

  const abs = items.map(x => Math.abs(x.delta)).filter(v => v > 0).sort((a, b) => a - b);
  const scaleMax = abs.length ? Math.max(quantile(abs, 0.95), abs[abs.length - 1] * 0.35) : 1;
  els.legend.innerHTML = `
    <div class="legend-title">${measure.label} · change in days/yr</div>
    <div class="legend-ramp" style="background:linear-gradient(90deg,#1e3a8a,#2563eb,#93c5fd,#e5e7eb,#fcd34d,#ef4444,#9f1239)"></div>
    <div class="legend-labels"><span>≤−${formatShortRate(scaleMax)}</span><span>0</span><span>≥+${formatShortRate(scaleMax)}</span></div>
    <div class="tooltip-small" style="margin-top:6px">Newer minus older period. Symmetric scale centered on zero; hover for exact rates and percent change.</div>`;
  return scaleMax;
}

function render() {
  if (!state.manifest || (!state.data && !state.comparison)) return;
  if (state.pointLayer) state.pointLayer.remove();

  const items = renderItems();
  const scaleMax = updateLegend(items);
  updateStats(items);

  const layer = L.layerGroup();
  for (const item of items) {
    const value = state.mode === 'period' ? item.rate : item.delta;
    const marker = L.circleMarker([item.point.lat, item.point.lon], {
      renderer: canvasRenderer,
      radius: 4.1,
      stroke: false,
      fill: true,
      fillColor: state.mode === 'period' ? sequentialColor(value, scaleMax) : comparisonColor(value, scaleMax),
      fillOpacity: state.mode === 'period' ? (value > 0 ? 0.88 : 0.28) : 0.88,
      bubblingMouseEvents: true,
    });
    marker.bindTooltip(() => state.mode === 'period' ? periodTooltipHtml(item.point, item.rate) : comparisonTooltipHtml(item), {
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
    const activeIndex = state.displayMode === 'range' ? state.rangeIndex : state.thresholdIndex;
    btn.classList.toggle('active', idx === activeIndex);
    btn.setAttribute('aria-pressed', idx === activeIndex ? 'true' : 'false');
  });
}

async function loadPeriodData(periodId) {
  if (state.cache.has(periodId)) return state.cache.get(periodId);
  const meta = state.manifest.periods.find(p => p.id === periodId);
  if (!meta) throw new Error(`Unknown period: ${periodId}`);
  const response = await fetch(meta.file, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`Could not load ${meta.file}`);
  const data = await response.json();
  state.cache.set(periodId, data);
  return data;
}

async function loadSelection(selectionId) {
  els.status.classList.remove('ready');
  const comparison = state.manifest.comparisons.find(c => c.id === selectionId);
  if (comparison) {
    state.mode = 'comparison';
    els.status.textContent = `Loading ${comparison.label}…`;
    const [newerData, olderData] = await Promise.all([
      loadPeriodData(comparison.newer_period),
      loadPeriodData(comparison.older_period),
    ]);
    state.data = null;
    state.comparison = {
      ...comparison,
      newerData,
      olderData,
      newer: newerData.period,
      older: olderData.period,
    };
  } else {
    state.mode = 'period';
    const meta = state.manifest.periods.find(p => p.id === selectionId);
    if (!meta) throw new Error(`Unknown selection: ${selectionId}`);
    els.status.textContent = `Loading ${meta.label}…`;
    state.data = await loadPeriodData(selectionId);
    state.comparison = null;
  }
  els.status.textContent = state.mode === 'comparison' ? `Loaded ${state.comparison.label}` : `Loaded ${state.data.period.label}`;
  els.status.classList.add('ready');
}

function buildMeasureButtons() {
  els.thresholdButtons.innerHTML = '';
  const isRange = state.displayMode === 'range';
  els.measureHeading.textContent = isRange ? 'Rainfall range' : 'Threshold';
  els.thresholdButtons.setAttribute('aria-label', isRange ? 'Rainfall range' : 'Rainfall threshold');

  const measures = isRange
    ? rangeSpecs.map(spec => spec.label)
    : state.manifest.thresholds_in.map(threshold => `≥${threshold}"`);

  measures.forEach((label, idx) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'threshold-button';
    button.textContent = label;
    const activeIndex = isRange ? state.rangeIndex : state.thresholdIndex;
    button.setAttribute('aria-pressed', idx === activeIndex ? 'true' : 'false');
    button.addEventListener('click', () => {
      if (isRange) state.rangeIndex = idx;
      else state.thresholdIndex = idx;
      render();
    });
    els.thresholdButtons.appendChild(button);
  });
}

function buildControls() {
  buildMeasureButtons();

  els.periodSelect.innerHTML = '';
  const periodsGroup = document.createElement('optgroup');
  periodsGroup.label = 'Climatology periods';
  state.manifest.periods.forEach(period => {
    const option = document.createElement('option');
    option.value = period.id;
    option.textContent = period.label;
    option.title = period.label;
    periodsGroup.appendChild(option);
  });
  els.periodSelect.appendChild(periodsGroup);

  if (state.manifest.comparisons?.length) {
    const comparisonGroup = document.createElement('optgroup');
    comparisonGroup.label = 'Change / comparisons';
    state.manifest.comparisons.forEach(comparison => {
      const option = document.createElement('option');
      option.value = comparison.id;
      option.textContent = comparison.label;
      option.title = comparison.label;
      comparisonGroup.appendChild(option);
    });
    els.periodSelect.appendChild(comparisonGroup);
  }
}

async function loadBoundary() {
  try {
    const response = await fetch(state.manifest.cwa_file, { cache: 'no-cache' });
    if (!response.ok) return;
    state.boundary = await response.json();
    if (state.boundaryLayer) state.boundaryLayer.remove();
    state.boundaryLayer = L.geoJSON(state.boundary, {
      style: { color: '#ffffff', weight: 2.2, opacity: 0.95, fill: false, dashArray: '7 5' },
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
      els.medianDetail.textContent = 'The NOAA data build runs automatically in GitHub Actions.';
      return;
    }

    const twoInchIndex = state.manifest.thresholds_in.indexOf(2);
    state.thresholdIndex = twoInchIndex >= 0 ? twoInchIndex : 0;
    state.selectionId = state.manifest.periods.find(p => p.id === 'era_2005_2025')?.id || state.manifest.periods[0].id;
    buildControls();
    els.periodSelect.value = state.selectionId;
    await Promise.all([loadSelection(state.selectionId), loadBoundary()]);
    render();
  } catch (error) {
    console.error(error);
    els.status.textContent = 'Could not load climatology data.';
    els.medianDetail.textContent = 'Check the GitHub Actions build and docs/data/manifest.json.';
  }
}

init();
