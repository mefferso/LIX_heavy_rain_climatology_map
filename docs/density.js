/* global L, map, state */
(() => {
  const density = { enabled: false, layer: null, cache: new Map(), token: 0 };

  function addControl() {
    const anchor = document.getElementById('mapStyleButtons')?.closest('section');
    if (!anchor || document.getElementById('densityToggle')) return;
    const section = document.createElement('section');
    section.className = 'control-row';
    section.innerHTML = '<label>Data support</label><button id="densityToggle" type="button" class="mode-button" aria-pressed="false">Observation support</button>';
    anchor.insertAdjacentElement('afterend', section);
    document.getElementById('densityToggle').addEventListener('click', () => {
      density.enabled = !density.enabled;
      const button = document.getElementById('densityToggle');
      button.classList.toggle('active', density.enabled);
      button.setAttribute('aria-pressed', density.enabled ? 'true' : 'false');
      refreshDensity();
    });
  }

  function months() {
    if (state.season === 'djf') return [11, 0, 1];
    if (state.season === 'mam') return [2, 3, 4];
    if (state.season === 'jja') return [5, 6, 7];
    if (state.season === 'son') return [8, 9, 10];
    return [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
  }

  function pointSupport(point) {
    let sum = 0;
    let days = 0;
    for (const month of months()) {
      sum += point.os?.[month] || 0;
      days += point.on?.[month] || 0;
    }
    return days ? sum / days : NaN;
  }

  async function loadPeriod(id) {
    if (density.cache.has(id)) return density.cache.get(id);
    const file = state.manifest?.observation_density?.files?.[id];
    if (!file) throw new Error(`No observation-support data for ${id}`);
    const response = await fetch(file, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Could not load ${file}`);
    const data = await response.json();
    density.cache.set(id, data);
    return data;
  }

  function quantile(sorted, q) {
    if (!sorted.length) return NaN;
    const pos = (sorted.length - 1) * q;
    const base = Math.floor(pos);
    const rest = pos - base;
    return sorted[base + 1] === undefined ? sorted[base] : sorted[base] + rest * (sorted[base + 1] - sorted[base]);
  }

  function supportColor(value, low, high) {
    const palette = ['#ef4444', '#f97316', '#facc15', '#84cc16', '#22c55e', '#06b6d4', '#3b82f6'];
    if (!Number.isFinite(value)) return '#9ca3af';
    const t = high > low ? Math.max(0, Math.min(1, (value - low) / (high - low))) : 0.5;
    return palette[Math.min(palette.length - 1, Math.floor(t * palette.length))];
  }

  function fmt(value) {
    if (!Number.isFinite(value)) return '—';
    return value >= 10 ? value.toFixed(1) : value.toFixed(2);
  }

  function clearDensity() {
    if (density.layer) density.layer.remove();
    density.layer = null;
    document.getElementById('densityLegendBlock')?.remove();
  }

  function addLegend(low, high, comparison) {
    const legend = document.getElementById('legend');
    if (!legend) return;
    const block = document.createElement('div');
    block.id = 'densityLegendBlock';
    block.style.cssText = 'margin-top:10px;padding-top:9px;border-top:1px solid rgba(255,255,255,.16)';
    block.innerHTML = `<div class="legend-title">Observation support · precip obs/day</div><div class="legend-ramp" style="background:linear-gradient(90deg,#ef4444,#f97316,#facc15,#84cc16,#22c55e,#06b6d4,#3b82f6)"></div><div class="legend-labels"><span>sparser ${fmt(low)}</span><span>denser ${fmt(high)}</span></div><div class="tooltip-small" style="margin-top:6px">${comparison ? 'Comparison rings use the lower-support period at each grid point. ' : ''}Colors span the visible 5th–95th percentile and are not formal confidence categories.</div>`;
    legend.appendChild(block);
  }

  async function buildItems() {
    if (state.mode === 'comparison') {
      const [newer, older] = await Promise.all([loadPeriod(state.comparison.newer_period), loadPeriod(state.comparison.older_period)]);
      const oldMap = new Map(older.points.map(p => [`${p.lat.toFixed(5)},${p.lon.toFixed(5)}`, p]));
      return newer.points.map(p => {
        const oldPoint = oldMap.get(`${p.lat.toFixed(5)},${p.lon.toFixed(5)}`);
        if (!oldPoint) return null;
        const newerValue = pointSupport(p);
        const olderValue = pointSupport(oldPoint);
        return { lat: p.lat, lon: p.lon, value: Math.min(newerValue, olderValue), detail: `${newer.period.label}: ${fmt(newerValue)}<br>${older.period.label}: ${fmt(olderValue)}<br>Ring uses lower-support period` };
      }).filter(Boolean);
    }
    const data = await loadPeriod(state.data.period.id);
    return data.points.map(p => {
      const value = pointSupport(p);
      return { lat: p.lat, lon: p.lon, value, detail: `${data.period.label}: ${fmt(value)} mean nearby precip observations/day` };
    });
  }

  async function refreshDensity() {
    clearDensity();
    if (!density.enabled || !state.manifest?.observation_density?.available) return;
    const token = ++density.token;
    try {
      const items = await buildItems();
      if (token !== density.token || !density.enabled) return;
      const values = items.map(x => x.value).filter(Number.isFinite).sort((a, b) => a - b);
      const low = quantile(values, 0.05);
      const high = quantile(values, 0.95);
      const layer = L.layerGroup();
      for (const item of items) {
        const ring = L.circleMarker([item.lat, item.lon], { radius: 6.6, fill: false, color: supportColor(item.value, low, high), opacity: 0.92, weight: 2.4 });
        ring.bindTooltip(`<div class="tooltip-title">Observation support</div><div><strong>${fmt(item.value)} nearby precip obs/day</strong></div><div class="tooltip-small">${item.detail}<br>Grid center: ${item.lat.toFixed(3)}°, ${item.lon.toFixed(3)}°</div>`, { className: 'grid-tooltip', direction: 'top', opacity: 0.98, sticky: true });
        ring.addTo(layer);
      }
      layer.addTo(map);
      density.layer = layer;
      if (state.boundaryLayer?.bringToFront) state.boundaryLayer.bringToFront();
      addLegend(low, high, state.mode === 'comparison');
    } catch (error) {
      console.warn('Observation-support layer unavailable', error);
      clearDensity();
    }
  }

  addControl();
  document.getElementById('periodSelect')?.addEventListener('change', () => setTimeout(refreshDensity, 0));
  document.getElementById('seasonSelect')?.addEventListener('change', () => setTimeout(refreshDensity, 0));
  document.getElementById('displayModeButtons')?.addEventListener('click', () => setTimeout(refreshDensity, 0));
  document.getElementById('mapStyleButtons')?.addEventListener('click', () => setTimeout(refreshDensity, 0));
})();
