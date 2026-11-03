(() => {
  const POLL_INTERVAL_MS = 15_000;
  const SAMPLE_LIMIT = 420;
  const PERIODS = { '24h': 24 * 60 * 60_000, '7d': 7 * 24 * 60 * 60_000, '30d': 30 * 24 * 60 * 60_000 };
  const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim'];
  const elements = {
    status: document.getElementById('collector-status'),
    notice: document.getElementById('stats-notice'),
    delay: document.getElementById('metric-delay'),
    late: document.getElementById('metric-late'),
    average: document.getElementById('metric-average'),
    coverage: document.getElementById('metric-coverage'),
    coverageDetail: document.getElementById('metric-coverage-detail'),
    delayChart: document.getElementById('delay-chart'),
    lateChart: document.getElementById('late-chart'),
    hourChart: document.getElementById('hour-chart'),
    weekdayChart: document.getElementById('weekday-chart'),
    hourLateChart: document.getElementById('hour-late-chart'),
    weekdayLateChart: document.getElementById('weekday-late-chart'),
    dailyRanking: document.getElementById('daily-ranking'),
    lineRanking: document.getElementById('line-ranking'),
    footnote: document.getElementById('sample-footnote'),
  };
  let selectedPeriod = '30d';
  let pollInProgress = false;
  let historyRefreshInProgress = false;
  let centralMode = null;
  let historySamples = [];

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }

  function setStatus(text, isError = false) {
    elements.status.innerHTML = `<span class="${isError ? 'status-error' : ''}"></span>${text}`;
    elements.status.classList.toggle('error', isError);
  }

  function formatDelay(seconds) {
    if (!Number.isFinite(Number(seconds))) return '—';
    const minutes = Math.max(0, Math.round(Number(seconds) / 60));
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return remainder ? `${hours} h ${String(remainder).padStart(2, '0')}` : `${hours} h`;
  }

  function formatTimestamp(timestamp, period = selectedPeriod) {
    const date = new Date(timestamp);
    return period === '24h'
      ? date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })
      : date.toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
  }

  function formatFullDate(timestamp) {
    return new Date(timestamp).toLocaleDateString('fr-FR', {
      weekday: 'long', day: 'numeric', month: 'long',
    });
  }

  function downsample(points, limit = SAMPLE_LIMIT) {
    if (points.length <= limit) return points;
    const step = points.length / limit;
    const result = [];
    for (let bucket = 0; bucket < limit; bucket += 1) {
      const start = Math.floor(bucket * step);
      const end = Math.max(start + 1, Math.floor((bucket + 1) * step));
      const group = points.slice(start, end);
      const valid = group.filter((point) => Number.isFinite(point.value));
      result.push({
        timestamp: group[Math.floor(group.length / 2)]?.timestamp ?? points[start].timestamp,
        value: valid.length ? valid.reduce((sum, point) => sum + point.value, 0) / valid.length : null,
      });
    }
    return result;
  }

  function renderLineChart(container, sourcePoints, color, formatter, label) {
    const points = downsample(sourcePoints);
    const valid = points.filter((point) => Number.isFinite(point.value));
    if (!valid.length) {
      container.innerHTML = '<div class="chart-empty">Pas encore assez de relevés pour tracer cette courbe.</div>';
      return;
    }

    const width = 760;
    const height = 245;
    const margin = { top: 14, right: 14, bottom: 34, left: 52 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const maxValue = Math.max(1, ...valid.map((point) => point.value));
    const scaleMax = maxValue * 1.12;
    const x = (index) => margin.left + (points.length <= 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth);
    const y = (value) => margin.top + plotHeight - (value / scaleMax) * plotHeight;
    let linePath = '';
    let segmentOpen = false;
    const circles = [];
    points.forEach((point, index) => {
      if (!Number.isFinite(point.value)) {
        segmentOpen = false;
        return;
      }
      linePath += `${segmentOpen ? 'L' : 'M'}${x(index).toFixed(2)},${y(point.value).toFixed(2)} `;
      segmentOpen = true;
      if (points.length <= 100 && (index === 0 || index === points.length - 1 || index % 4 === 0)) {
        circles.push(`<circle cx="${x(index).toFixed(2)}" cy="${y(point.value).toFixed(2)}" r="3" fill="${color}"><title>${formatTimestamp(point.timestamp)} · ${formatter(point.value)}</title></circle>`);
      }
    });
    const grid = [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
      const value = scaleMax * fraction;
      const posY = y(value);
      return `<g><line x1="${margin.left}" y1="${posY}" x2="${width - margin.right}" y2="${posY}" class="chart-grid-line"/><text x="${margin.left - 9}" y="${posY + 3}" class="chart-axis-label" text-anchor="end">${formatter(value)}</text></g>`;
    }).join('');
    const xTicks = [0, Math.floor((points.length - 1) / 2), points.length - 1]
      .filter((index, position, all) => all.indexOf(index) === position && index >= 0)
      .map((index) => `<text x="${x(index)}" y="${height - 9}" class="chart-axis-label" text-anchor="${index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}">${formatTimestamp(points[index].timestamp)}</text>`)
      .join('');
    const areaPath = valid.length === points.length && linePath.trim()
      ? `${linePath.trim()} L${x(points.length - 1)},${margin.top + plotHeight} L${x(0)},${margin.top + plotHeight} Z`
      : '';
    container.innerHTML = `<svg class="line-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${label}"><defs><linearGradient id="fill-${container.id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".18"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${grid}${areaPath ? `<path d="${areaPath}" fill="url(#fill-${container.id})"/>` : ''}<path d="${linePath}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>${circles.join('')}${xTicks}</svg>`;
  }

  function renderBarChart(container, groups, color, formatter, label) {
    const valid = groups.filter((group) => Number.isFinite(group.value));
    if (!valid.length) {
      container.innerHTML = '<div class="chart-empty">Pas encore assez de relevés pour comparer cette période.</div>';
      return;
    }
    const width = 760;
    const height = 230;
    const margin = { top: 14, right: 10, bottom: 40, left: 52 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const maxValue = Math.max(1, ...valid.map((group) => group.value)) * 1.12;
    const slotWidth = plotWidth / groups.length;
    const barWidth = Math.max(5, Math.min(26, slotWidth * 0.56));
    const grid = [0, 0.5, 1].map((fraction) => {
      const value = maxValue * fraction;
      const posY = margin.top + plotHeight - fraction * plotHeight;
      return `<g><line x1="${margin.left}" y1="${posY}" x2="${width - margin.right}" y2="${posY}" class="chart-grid-line"/><text x="${margin.left - 9}" y="${posY + 3}" class="chart-axis-label" text-anchor="end">${formatter(value)}</text></g>`;
    }).join('');
    const bars = groups.map((group, index) => {
      const value = Number.isFinite(group.value) ? group.value : 0;
      const barHeight = value / maxValue * plotHeight;
      const bx = margin.left + slotWidth * index + (slotWidth - barWidth) / 2;
      const by = margin.top + plotHeight - barHeight;
      return `<g><rect x="${bx}" y="${by}" width="${barWidth}" height="${barHeight}" rx="4" fill="${color}" opacity="${Number.isFinite(group.value) ? '.86' : '.12'}"><title>${group.label} · ${formatter(value)}</title></rect><text x="${margin.left + slotWidth * index + slotWidth / 2}" y="${height - 12}" class="chart-axis-label" text-anchor="middle">${group.shortLabel || group.label}</text></g>`;
    }).join('');
    container.innerHTML = `<svg class="bar-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${label}">${grid}${bars}</svg>`;
  }

  function makeDailyRanking(samples) {
    const days = new Map();
    for (const sample of samples) {
      if (!sample.knownVehicles) continue;
      const date = new Date(sample.timestamp);
      const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
      let day = days.get(key);
      if (!day) {
        day = { timestamp: new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime(), sumDelay: 0, sumLate: 0, peakDelay: 0, peakLate: 0, samples: 0 };
        days.set(key, day);
      }
      day.sumDelay += sample.totalDelaySeconds;
      day.sumLate += sample.lateVehicles;
      day.peakDelay = Math.max(day.peakDelay, sample.totalDelaySeconds);
      day.peakLate = Math.max(day.peakLate, sample.lateVehicles);
      day.samples += 1;
    }
    return [...days.values()].map((day) => ({
      ...day,
      averageDelay: day.sumDelay / day.samples,
      averageLate: day.sumLate / day.samples,
    })).sort((a, b) => b.averageDelay - a.averageDelay).slice(0, 10);
  }

  function makeLineRanking(samples) {
    const lines = new Map();
    for (const sample of samples) {
      if (!Array.isArray(sample.lineData)) continue;
      for (const [id, label, mode, color, known, late, delaySeconds] of sample.lineData) {
        if (!known) continue;
        let item = lines.get(id);
        if (!item) {
          item = { id, label, mode, color, delayTotal: 0, lateTotal: 0, sampleCount: 0, peakDelay: 0, peakLate: 0 };
          lines.set(id, item);
        }
        item.delayTotal += delaySeconds;
        item.lateTotal += late;
        item.sampleCount += 1;
        item.peakDelay = Math.max(item.peakDelay, delaySeconds);
        item.peakLate = Math.max(item.peakLate, late);
      }
    }
    return [...lines.values()].map((line) => ({
      ...line,
      averageDelay: line.delayTotal / line.sampleCount,
      averageLate: line.lateTotal / line.sampleCount,
    })).sort((a, b) => b.averageDelay - a.averageDelay);
  }

  function renderDailyRanking(samples) {
    const days = makeDailyRanking(samples);
    if (!days.length) {
      elements.dailyRanking.innerHTML = '<tr><td colspan="4" class="table-empty">En attente de relevés.</td></tr>';
      return;
    }
    elements.dailyRanking.innerHTML = days.map((day, index) => `<tr><td><span class="rank-number">${index + 1}</span>${formatFullDate(day.timestamp)}</td><td>${formatDelay(day.averageDelay)}</td><td>${formatDelay(day.peakDelay)}</td><td>${Math.round(day.peakLate)}</td></tr>`).join('');
  }

  function renderLineRanking(samples) {
    const lines = makeLineRanking(samples);
    if (!lines.length) {
      elements.lineRanking.innerHTML = '<div class="chart-empty">Les comparaisons par ligne apparaîtront après quelques relevés.</div>';
      return;
    }
    const maxDelay = Math.max(60, ...lines.map((line) => line.averageDelay));
    elements.lineRanking.innerHTML = lines.map((line) => {
      const width = Math.max(2, line.averageDelay / maxDelay * 100);
      const modeLabel = line.mode === 'tramway' ? 'Tram' : line.mode === 'bus' ? 'Bus' : 'Réseau';
      const averageLate = line.averageLate.toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
      const vehicleWord = line.averageLate > 1 ? 'véhicules' : 'véhicule';
      return `<article class="line-stat-row"><div class="line-stat-head"><span class="line-stat-badge" style="--line-color:${line.color}">${escapeHtml(line.label)}</span><span class="line-stat-mode">${modeLabel}</span><strong>${formatDelay(line.averageDelay)}</strong></div><div class="line-stat-track"><span style="width:${width}%;--line-color:${line.color}"></span></div><div class="line-stat-foot"><span>${averageLate} ${vehicleWord} en retard en moyenne</span><span>Pic ${line.peakLate} · ${line.sampleCount} relevés</span></div></article>`;
    }).join('');
  }

  function renderMetrics(samples) {
    const latest = samples.at(-1);
    if (!latest) {
      elements.delay.textContent = '—';
      elements.late.textContent = '—';
      elements.average.textContent = '—';
      elements.coverage.textContent = '—';
      elements.coverageDetail.textContent = 'En attente du premier relevé';
      return;
    }
    const coverage = latest.totalVehicles ? Math.round(latest.knownVehicles / latest.totalVehicles * 100) : 0;
    elements.coverage.textContent = `${coverage} %`;
    elements.coverageDetail.textContent = `${latest.knownVehicles} retard(s) connu(s) sur ${latest.totalVehicles} véhicules`;
    if (!latest.knownVehicles) {
      elements.delay.textContent = '—';
      elements.late.textContent = '—';
      elements.average.textContent = '—';
      return;
    }
    elements.delay.textContent = formatDelay(latest.totalDelaySeconds);
    elements.late.textContent = String(latest.lateVehicles);
    elements.average.textContent = formatDelay(latest.totalDelaySeconds / latest.knownVehicles);
  }

  function renderCharts(samples) {
    const periodStart = Date.now() - PERIODS[selectedPeriod];
    const periodSamples = samples.filter((sample) => sample.timestamp >= periodStart);
    renderMetrics(periodSamples);
    renderLineChart(
      elements.delayChart,
      periodSamples.map((sample) => ({ timestamp: sample.timestamp, value: sample.knownVehicles ? sample.totalDelaySeconds / 60 : null })),
      '#19785e', (value) => `${Math.round(value)}`, 'Retard cumulé en minutes au fil du temps',
    );
    renderLineChart(
      elements.lateChart,
      periodSamples.map((sample) => ({ timestamp: sample.timestamp, value: sample.knownVehicles ? sample.lateVehicles : null })),
      '#d77732', (value) => `${Math.round(value)}`, 'Nombre de véhicules retardés au fil du temps',
    );

    const hourGroups = Array.from({ length: 24 }, (_, hour) => ({ hour, label: `${String(hour).padStart(2, '0')} h`, shortLabel: hour % 3 === 0 ? `${String(hour).padStart(2, '0')}h` : '', total: 0, late: 0, count: 0 }));
    const weekdayGroups = WEEKDAYS.map((label, day) => ({ day, label, shortLabel: label, total: 0, late: 0, count: 0 }));
    for (const sample of periodSamples) {
      if (!sample.knownVehicles) continue;
      const date = new Date(sample.timestamp);
      const hour = hourGroups[date.getHours()];
      hour.total += sample.totalDelaySeconds;
      hour.late += sample.lateVehicles;
      hour.count += 1;
    }
    const weekdayStart = Date.now() - PERIODS['30d'];
    for (const sample of samples.filter((item) => item.timestamp >= weekdayStart)) {
      if (!sample.knownVehicles) continue;
      const date = new Date(sample.timestamp);
      const weekday = weekdayGroups[(date.getDay() + 6) % 7];
      weekday.total += sample.totalDelaySeconds;
      weekday.late += sample.lateVehicles;
      weekday.count += 1;
    }
    const hourAverages = hourGroups.map((group) => ({ ...group, value: group.count ? group.total / group.count / 60 : null }));
    const weekdayAverages = weekdayGroups.map((group) => ({ ...group, value: group.count ? group.total / group.count / 60 : null }));
    const hourLateAverages = hourGroups.map((group) => ({ ...group, value: group.count ? group.late / group.count : null }));
    const weekdayLateAverages = weekdayGroups.map((group) => ({ ...group, value: group.count ? group.late / group.count : null }));
    renderBarChart(elements.hourChart, hourAverages, '#6a9f89', (value) => `${Math.round(value)}`, 'Retard moyen observé par heure de la journée');
    renderBarChart(elements.weekdayChart, weekdayAverages, '#5a87b4', (value) => `${Math.round(value)}`, 'Retard moyen observé par jour de la semaine');
    renderBarChart(elements.hourLateChart, hourLateAverages, '#d77732', (value) => `${Math.round(value)}`, 'Nombre moyen de véhicules en retard par heure');
    renderBarChart(elements.weekdayLateChart, weekdayLateAverages, '#bd5f55', (value) => `${Math.round(value)}`, 'Nombre moyen de véhicules en retard par jour de la semaine');
    renderDailyRanking(periodSamples);
    renderLineRanking(periodSamples);
    const lineSnapshots = periodSamples.filter((sample) => Array.isArray(sample.lineData)).length;
    elements.footnote.textContent = `${periodSamples.length.toLocaleString('fr-FR')} relevés d’une minute · ${lineSnapshots.toLocaleString('fr-FR')} relevés détaillés par ligne · les périodes où le site était fermé ne sont pas estimées.`;
  }

  async function refreshHistory() {
    if (historyRefreshInProgress) return;
    historyRefreshInProgress = true;
    try {
      const lastTimestamp = centralMode === true ? historySamples.at(-1)?.timestamp : null;
      const query = lastTimestamp ? `?after=${encodeURIComponent(lastTimestamp)}` : '';
      const response = await fetch(`/api/stats${query}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Historique partagé indisponible.');

      if (data.persistent) {
        if (centralMode !== true) historySamples = [];
        centralMode = true;
        const cutoff = Date.now() - (data.retention_days || 45) * 24 * 60 * 60_000;
        historySamples = historySamples
          .filter((sample) => sample.timestamp >= cutoff)
          .concat(Array.isArray(data.samples) ? data.samples : [])
          .sort((a, b) => a.timestamp - b.timestamp);
        elements.notice.textContent = `Historique partagé entre tous les appareils · conservé ${data.retention_days || 45} jours dans la base centrale.`;
        elements.notice.classList.remove('warning');
        setStatus('Historique partagé · collecte 1 min');
        renderCharts(historySamples);
      } else {
        centralMode = false;
        elements.notice.textContent = 'Historique local à ce navigateur · conservé 45 jours pendant que le site est ouvert. Configure une base centrale pour le partager entre tes appareils.';
        elements.notice.classList.remove('warning');
        historySamples = await window.TamStats.readAll(Date.now() - PERIODS['30d']);
        renderCharts(historySamples);
      }
    } catch (error) {
      if (centralMode === true) {
        elements.notice.textContent = 'La base centrale ne répond pas pour le moment. Les derniers relevés déjà chargés restent visibles.';
        elements.notice.classList.add('warning');
        setStatus('Historique partagé indisponible', true);
        renderCharts(historySamples);
      } else {
        try {
          centralMode = false;
          historySamples = await window.TamStats.readAll(Date.now() - PERIODS['30d']);
          elements.notice.textContent = `${error.message || 'Base centrale indisponible.'} Affichage de l’historique local à ce navigateur.`;
          elements.notice.classList.add('warning');
          renderCharts(historySamples);
        } catch (localError) {
          elements.notice.textContent = `${localError.message || 'Stockage local inaccessible.'} Les statistiques ne peuvent pas être conservées dans ce navigateur.`;
          elements.notice.classList.add('warning');
          setStatus('Historique inaccessible', true);
        }
      }
    } finally {
      historyRefreshInProgress = false;
    }
  }

  async function pollVehicles() {
    if (pollInProgress) return;
    pollInProgress = true;
    try {
      const response = await fetch('/api/vehicles', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Flux TAM indisponible.');
      const vehicles = data.vehicles || [];
      if (centralMode === true) {
        setStatus('Historique partagé · collecte 1 min');
      } else {
        const saved = await window.TamStats.record(vehicles);
        setStatus(saved ? 'Collecte locale · 1 min' : 'Collecte non enregistrée', !saved);
      }
    } catch (error) {
      setStatus('Flux TAM temporairement indisponible', true);
    } finally {
      pollInProgress = false;
    }
  }

  document.querySelectorAll('[data-period]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedPeriod = button.dataset.period;
      document.querySelectorAll('[data-period]').forEach((item) => {
        const active = item === button;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      refreshHistory();
    });
  });

  refreshHistory();
  pollVehicles();
  window.setInterval(pollVehicles, POLL_INTERVAL_MS);
  window.setInterval(refreshHistory, 60_000);
})();
