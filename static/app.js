(() => {
  const REFRESH_MS = 15_000;
  const STATUS_LABELS = {
    0: 'À l’approche d’un arrêt',
    1: 'À l’arrêt',
    2: 'En circulation',
  };
  const FALLBACK_COLOR = '#19785e';
  const ROUTE_FILTER_STORAGE_KEY = 'tam-live:selected-route-ids:v1';
  const FAVORITE_STOPS_STORAGE_KEY = 'tam-live:favorite-stops:v1';

  const map = L.map('map', { zoomControl: false }).setView([43.611, 3.877], 12);
  map.createPane('stopsPane');
  map.getPane('stopsPane').style.zIndex = '450';
  L.control.zoom({ position: 'bottomright' }).addTo(map);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);

  const busCasingLayer = L.featureGroup().addTo(map);
  const busLinesLayer = L.featureGroup().addTo(map);
  const bustramCasingLayer = L.featureGroup().addTo(map);
  const bustramLinesLayer = L.featureGroup().addTo(map);
  const casingLayer = L.featureGroup().addTo(map);
  const tramLinesLayer = L.featureGroup().addTo(map);
  const stopsLayer = L.layerGroup().addTo(map);
  const vehiclesLayer = L.layerGroup().addTo(map);
  const userLocationLayer = L.layerGroup().addTo(map);
  const lineList = document.getElementById('line-list');
  const vehicleCount = document.getElementById('vehicle-count');
  const updatedAt = document.getElementById('updated-at');
  const connectionLabel = document.getElementById('connection-label');
  const liveDot = document.querySelector('.live-dot');
  const notice = document.getElementById('notice');
  const stopPanel = document.getElementById('stop-panel');
  const stopPanelName = document.getElementById('stop-panel-name');
  const stopPanelMode = document.getElementById('stop-panel-mode');
  const stopDepartures = document.getElementById('stop-departures');
  const favoriteButton = document.getElementById('toggle-stop-favorite');
  const favoriteList = document.getElementById('favorite-list');
  const favoriteCount = document.getElementById('favorite-count');
  const favoritesSection = document.getElementById('favorites-section');
  const searchInput = document.getElementById('network-search');
  const searchResults = document.getElementById('search-results');
  const myLocationButton = document.getElementById('my-location');
  const tripPanel = document.getElementById('trip-panel');
  const tripLineBadge = document.getElementById('trip-line-badge');
  const tripPanelDirection = document.getElementById('trip-panel-direction');
  const tripPanelVehicle = document.getElementById('trip-panel-vehicle');
  const tripPanelStatus = document.getElementById('trip-panel-status');
  const tripStops = document.getElementById('trip-stops');

  let lineFeatures = [];
  let busLineFeatures = [];
  let bustramLineFeatures = [];
  let tramShapes = [];
  let routeById = new Map();
  let vehicles = [];
  let stops = [];
  let selectedStop = null;
  let selectedScheduleTab = 'realtime';
  let scheduleRequestId = 0;
  let scheduleRefreshTimer = null;
  let tripRequestId = 0;
  const vehicleMarkers = new Map();
  const stopMarkers = new Map();
  const previousVehiclePositions = new Map();
  const vehicleMotion = new Map();
  const animationIds = new WeakMap();
  let selectedRouteIds = loadSelectedRouteIds();
  let favoriteStopKeys = loadFavoriteStopKeys();
  let pollInProgress = false;
  let noticeTimer = null;
  let stopsRetryDelay = 15_000;

  function loadSelectedRouteIds() {
    try {
      const saved = window.localStorage.getItem(ROUTE_FILTER_STORAGE_KEY);
      if (!saved || saved === 'all') return null;
      const routeIds = JSON.parse(saved);
      return Array.isArray(routeIds) ? new Set(routeIds.map(String)) : null;
    } catch {
      return null;
    }
  }

  function saveSelectedRouteIds() {
    try {
      window.localStorage.setItem(
        ROUTE_FILTER_STORAGE_KEY,
        selectedRouteIds === null ? 'all' : JSON.stringify([...selectedRouteIds]),
      );
    } catch {
      // Le stockage peut être désactivé par le navigateur; le filtre reste utilisable pendant la session.
    }
  }

  function loadFavoriteStopKeys() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(FAVORITE_STOPS_STORAGE_KEY) || '[]');
      return Array.isArray(saved) ? new Set(saved.map(String)) : new Set();
    } catch {
      return new Set();
    }
  }

  function saveFavoriteStopKeys() {
    try {
      window.localStorage.setItem(FAVORITE_STOPS_STORAGE_KEY, JSON.stringify([...favoriteStopKeys]));
    } catch {
      // Les favoris restent utilisables pendant la session si le stockage du navigateur est désactivé.
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[char]);
  }

  function validColor(value) {
    return /^#[0-9a-f]{6}$/i.test(String(value || '')) ? value : '';
  }

  function showNotice(message, duration = 7000, kind = 'general') {
    notice.textContent = message;
    notice.dataset.kind = kind;
    notice.hidden = false;
    clearTimeout(noticeTimer);
    if (duration > 0) noticeTimer = setTimeout(() => { notice.hidden = true; }, duration);
  }

  function setConnection(online, label) {
    connectionLabel.textContent = label;
    liveDot.classList.toggle('online', online);
    liveDot.classList.toggle('offline', !online);
  }

  function routeForVehicle(vehicle) {
    const known = routeById.get(String(vehicle.route_id || '')) || {};
    return {
      ...known,
      ...vehicle,
      route_short_name: vehicle.route_short_name || known.short_name || '',
      route_long_name: vehicle.route_long_name || known.long_name || '',
      route_mode: vehicle.route_mode || known.mode || 'autre',
      route_color: vehicle.route_color || known.color || '',
      route_text_color: vehicle.route_text_color || known.text_color || '',
    };
  }

  function routeNumber(route) {
    const match = String(route.route_short_name || route.route_long_name || '').match(/\d+/);
    return match ? Number(match[0]) : null;
  }

  function routeLabel(route) {
    const shortName = String(route.route_short_name || '').trim();
    if (route.route_mode === 'tramway') {
      if (/^T/i.test(shortName)) return shortName.toUpperCase();
      const number = routeNumber(route);
      return number ? `T${number}` : (shortName || 'T?');
    }
    return shortName || '?';
  }

  function routeColor(route) {
    if (route.route_mode === 'tramway') {
      const number = routeNumber(route);
      const feature = lineFeatures.find((item) => Number(item.properties?.num_exploitation) === number);
      const geoJsonColor = validColor(feature?.properties?.code_couleur);
      if (geoJsonColor) return geoJsonColor;
    }
    return validColor(route.route_color) || FALLBACK_COLOR;
  }

  function prepareTramShapes() {
    tramShapes = [];
    for (const feature of lineFeatures) {
      const geometry = feature.geometry;
      const properties = feature.properties || {};
      const number = Number(properties.num_exploitation);
      if (!Number.isFinite(number)) continue;
      if (properties.mode && !String(properties.mode).toLowerCase().includes('tram')) continue;
      const paths = geometry.type === 'LineString'
        ? [geometry.coordinates]
        : geometry.type === 'MultiLineString' ? geometry.coordinates : [];
      for (const coordinates of paths) {
        const points = coordinates
          .filter((coordinate) => Array.isArray(coordinate) && coordinate.length >= 2)
          .map(([longitude, latitude]) => L.latLng(Number(latitude), Number(longitude)))
          .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lng));
        if (points.length < 2) continue;
        const cumulative = [0];
        for (let i = 1; i < points.length; i += 1) {
          cumulative.push(cumulative[i - 1] + points[i - 1].distanceTo(points[i]));
        }
        tramShapes.push({
          number,
          name: properties.nom_ligne || '',
          direction: properties.sens || '',
          points,
          cumulative,
          length: cumulative[cumulative.length - 1],
        });
      }
    }
  }

  function drawTramLines() {
    casingLayer.clearLayers();
    tramLinesLayer.clearLayers();
    for (const shape of tramShapes) {
      L.polyline(shape.points, {
        color: '#ffffff', weight: 9, opacity: 0.96, lineCap: 'round', lineJoin: 'round',
        interactive: false, smoothFactor: 1,
      }).addTo(casingLayer);
    }
    for (const shape of tramShapes) {
      const feature = lineFeatures.find((item) => Number(item.properties?.num_exploitation) === shape.number);
      L.polyline(shape.points, {
        color: validColor(feature?.properties?.code_couleur) || FALLBACK_COLOR,
        weight: 5.5, opacity: 1, lineCap: 'round', lineJoin: 'round',
        interactive: false, smoothFactor: 1,
      }).addTo(tramLinesLayer);
    }
    const bounds = tramLinesLayer.getBounds();
    if (!bounds.isValid()) throw new Error('Le fichier reçu ne contient aucun tracé de tramway exploitable.');
    keepTransitLineOrder();
    map.fitBounds(bounds, { padding: [38, 38] });
    renderMap();
  }

  function keepTransitLineOrder() {
    for (const group of [busCasingLayer, busLinesLayer, bustramCasingLayer, bustramLinesLayer, casingLayer, tramLinesLayer]) {
      group.eachLayer((layer) => layer.bringToFront());
    }
  }

  function drawBusLines() {
    busCasingLayer.clearLayers();
    busLinesLayer.clearLayers();
    for (const feature of busLineFeatures) {
      const geometry = feature.geometry;
      const properties = feature.properties || {};
      if (properties.mode && !String(properties.mode).toLowerCase().includes('bus')) continue;
      const paths = geometry?.type === 'LineString'
        ? [geometry.coordinates]
        : geometry?.type === 'MultiLineString' ? geometry.coordinates : [];
      for (const coordinates of paths) {
        const points = (coordinates || [])
          .filter((coordinate) => Array.isArray(coordinate) && coordinate.length >= 2)
          .map(([longitude, latitude]) => L.latLng(Number(latitude), Number(longitude)))
          .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lng));
        if (points.length < 2) continue;
        L.polyline(points, {
          color: '#ffffff', weight: 5.2, opacity: 0.86, lineCap: 'round', lineJoin: 'round',
          interactive: false, smoothFactor: 1,
        }).addTo(busCasingLayer);
        L.polyline(points, {
          color: validColor(properties.code_couleur) || FALLBACK_COLOR,
          weight: 3, opacity: 0.92, lineCap: 'round', lineJoin: 'round',
          interactive: false, smoothFactor: 1,
        }).addTo(busLinesLayer);
      }
    }
    keepTransitLineOrder();
  }

  function bustramColor(properties) {
    const geoJsonColor = validColor(properties.code_couleur);
    if (geoJsonColor) return geoJsonColor;
    const routeA = [...routeById.values()].find((route) =>
      String(route.short_name || '').trim().toUpperCase() === 'A'
      || normalizeText(route.long_name).includes('bustram a'));
    return validColor(routeA?.color) || '#8a1538';
  }

  function drawBustramLines() {
    bustramCasingLayer.clearLayers();
    bustramLinesLayer.clearLayers();
    for (const feature of bustramLineFeatures) {
      const geometry = feature.geometry;
      const properties = feature.properties || {};
      const lineName = normalizeText(properties.nom_ligne);
      if (properties.mode && !String(properties.mode).toLowerCase().includes('bhns')
        && !lineName.includes('bustram a')) continue;
      const paths = geometry?.type === 'LineString'
        ? [geometry.coordinates]
        : geometry?.type === 'MultiLineString' ? geometry.coordinates : [];
      for (const coordinates of paths) {
        const points = (coordinates || [])
          .filter((coordinate) => Array.isArray(coordinate) && coordinate.length >= 2)
          .map(([longitude, latitude]) => L.latLng(Number(latitude), Number(longitude)))
          .filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lng));
        if (points.length < 2) continue;
        L.polyline(points, {
          color: '#ffffff', weight: 7.2, opacity: 0.92, lineCap: 'round', lineJoin: 'round',
          interactive: false, smoothFactor: 1,
        }).addTo(bustramCasingLayer);
        L.polyline(points, {
          color: bustramColor(properties), weight: 4.2, opacity: 0.98, lineCap: 'round', lineJoin: 'round',
          interactive: false, smoothFactor: 1,
        }).addTo(bustramLinesLayer);
      }
    }
    keepTransitLineOrder();
  }

  function normalizeText(value) {
    return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  }

  function stopKey(stop) {
    const id = String(stop.id || '').trim();
    return id
      ? `${stop.mode}:${id}`
      : `${stop.mode}:${normalizeText(stop.name)}:${Number(stop.latitude).toFixed(5)}:${Number(stop.longitude).toFixed(5)}`;
  }

  function isFavoriteStop(stop) {
    return favoriteStopKeys.has(stopKey(stop));
  }

  function updateFavoriteButton() {
    if (!selectedStop) return;
    const favorite = isFavoriteStop(selectedStop);
    favoriteButton.textContent = favorite ? '★' : '☆';
    favoriteButton.setAttribute('aria-pressed', String(favorite));
    favoriteButton.setAttribute('aria-label', favorite ? 'Retirer des arrêts favoris' : 'Ajouter aux arrêts favoris');
    favoriteButton.title = favorite ? 'Retirer des favoris' : 'Ajouter aux favoris';
  }

  function renderFavoriteStops() {
    const favorites = [...favoriteStopKeys]
      .map((key) => stops.find((stop) => stopKey(stop) === key))
      .filter(Boolean);
    favoriteCount.textContent = String(favorites.length);
    favoriteList.replaceChildren();
    if (!favorites.length) {
      favoriteList.innerHTML = '<p class="favorite-empty">Touchez ☆ dans la fiche d’un arrêt pour retrouver ses prochains passages ici.</p>';
      return;
    }
    for (const stop of favorites) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'favorite-stop';
      button.innerHTML = `<span class="favorite-stop-icon" aria-hidden="true">★</span><span class="favorite-stop-copy"><strong>${escapeHtml(stop.name)}</strong><small>${stop.mode === 'tramway' ? 'Arrêt de tramway' : 'Arrêt de bus'} · horaires temps réel</small></span>`;
      button.addEventListener('click', () => {
        favoritesSection.open = false;
        selectedScheduleTab = 'realtime';
        map.flyTo([stop.latitude, stop.longitude], Math.max(map.getZoom(), 16), { duration: 0.55 });
        openStop(stop);
      });
      favoriteList.append(button);
    }
  }

  function refreshStopMarkerStyles() {
    for (const marker of stopMarkers.values()) {
      const stop = marker._tamStop;
      const favorite = stop && isFavoriteStop(stop);
      const isTram = stop?.mode === 'tramway';
      marker.setStyle({
        radius: (isTram ? 4.5 : 3.5) + (favorite ? 1 : 0),
        color: favorite ? '#c48b1e' : (isTram ? '#126b54' : '#64776d'),
        weight: favorite ? 2 : 1.5,
        fillColor: favorite ? '#fff5dc' : '#ffffff',
        fillOpacity: 0.95,
      });
    }
  }

  function toggleFavoriteStop() {
    if (!selectedStop) return;
    const key = stopKey(selectedStop);
    if (favoriteStopKeys.has(key)) favoriteStopKeys.delete(key);
    else favoriteStopKeys.add(key);
    saveFavoriteStopKeys();
    updateFavoriteButton();
    renderFavoriteStops();
    refreshStopMarkerStyles();
  }

  function clearNetworkSearch() {
    searchInput.value = '';
    searchResults.replaceChildren();
    searchResults.hidden = true;
  }

  function makeSearchResult(title, subtitle, badge, color, onSelect) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'search-result';
    button.innerHTML = `<span class="search-result-badge" style="--swatch:${color}">${escapeHtml(badge)}</span><span class="search-result-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></span>`;
    button.addEventListener('click', () => {
      clearNetworkSearch();
      onSelect();
    });
    return button;
  }

  function focusRouteFromSearch(route) {
    const routeId = String(route.route_id);
    selectedRouteIds = new Set([routeId]);
    saveSelectedRouteIds();
    renderLineList();
    renderMap();
    const vehicle = vehicles.find((item) => String(item.route_id || '') === routeId);
    if (vehicle) map.flyTo([vehicle.latitude, vehicle.longitude], Math.max(map.getZoom(), 15), { duration: 0.55 });
  }

  function focusStopFromSearch(stop) {
    map.flyTo([stop.latitude, stop.longitude], Math.max(map.getZoom(), 16), { duration: 0.55 });
    openStop(stop);
  }

  function renderNetworkSearch() {
    const query = normalizeText(searchInput.value);
    searchResults.replaceChildren();
    if (!query) {
      searchResults.hidden = true;
      return;
    }
    searchResults.hidden = false;

    const routes = [...routeById.values()].map((route) => {
      const view = {
        ...route,
        route_mode: route.mode,
        route_short_name: route.short_name,
        route_long_name: route.long_name,
        route_color: route.color,
        route_text_color: route.text_color,
      };
      return { route, view, label: routeLabel(view), text: normalizeText(`${routeLabel(view)} ${route.long_name || ''}`) };
    }).filter((item) => item.text.includes(query))
      .sort((a, b) => Number(!a.text.startsWith(query)) - Number(!b.text.startsWith(query))
        || a.label.localeCompare(b.label, 'fr', { numeric: true }))
      .slice(0, 5);
    const stopMatches = stops.filter((stop) => normalizeText(stop.name).includes(query))
      .sort((a, b) => Number(!normalizeText(a.name).startsWith(query)) - Number(!normalizeText(b.name).startsWith(query))
        || a.name.localeCompare(b.name, 'fr'))
      .slice(0, 7);

    if (!routes.length && !stopMatches.length) {
      const empty = document.createElement('p');
      empty.className = 'search-empty';
      empty.textContent = 'Aucun arrêt ou ligne trouvé.';
      searchResults.append(empty);
      return;
    }
    if (routes.length) {
      const heading = document.createElement('p');
      heading.className = 'route-group-title';
      heading.textContent = 'LIGNES';
      searchResults.append(heading);
      for (const item of routes) {
        const color = routeColor(item.view);
        searchResults.append(makeSearchResult(item.label, item.route.long_name || 'Réseau TAM', item.label, color,
          () => focusRouteFromSearch(item.route)));
      }
    }
    if (stopMatches.length) {
      const heading = document.createElement('p');
      heading.className = 'route-group-title';
      heading.textContent = 'ARRÊTS';
      searchResults.append(heading);
      for (const stop of stopMatches) {
        const modeLabel = stop.mode === 'tramway' ? 'Arrêt de tramway' : 'Arrêt de bus';
        const badge = stop.mode === 'tramway' ? 'T' : 'B';
        const color = stop.mode === 'tramway' ? '#19785e' : '#63766b';
        searchResults.append(makeSearchResult(stop.name, modeLabel, badge, color, () => focusStopFromSearch(stop)));
      }
    }
  }

  function openVehiclePopup(marker) {
    if (!marker?._tamPopupContent) return;
    const popup = L.popup({ maxWidth: 250, offset: [0, -8] })
      .setLatLng(marker.getLatLng())
      .setContent(marker._tamPopupContent);
    popup.on('add', () => {
      const button = popup.getElement()?.querySelector('.trip-open-button');
      button?.addEventListener('click', () => {
        map.closePopup(popup);
        openVehicleTrip(marker._tamVehicle);
      });
    });
    popup.openOn(map);
  }

  async function openVehicleTrip(vehicle) {
    if (!vehicle?.trip_id) return;
    const requestId = ++tripRequestId;
    selectedStop = null;
    scheduleRequestId += 1;
    if (scheduleRefreshTimer) window.clearInterval(scheduleRefreshTimer);
    scheduleRefreshTimer = null;
    stopPanel.hidden = true;
    tripPanel.hidden = false;
    map.closePopup();

    const label = routeLabel(vehicle);
    const color = routeColor(vehicle);
    const mode = vehicle.route_mode === 'tramway' ? 'Tram' : 'Bus';
    const vehicleNumber = vehicle.label || vehicle.id || '—';
    tripLineBadge.textContent = label;
    tripLineBadge.style.setProperty('--journey-color', color);
    tripPanelDirection.textContent = directionForVehicle(vehicle);
    tripPanelVehicle.textContent = `${mode} ${vehicleNumber}`;
    tripPanelStatus.textContent = STATUS_LABELS[vehicle.current_status] || 'Parcours du véhicule';
    tripStops.innerHTML = '<p class="schedule-loading">Chargement des arrêts…</p>';

    const params = new URLSearchParams({ trip_id: vehicle.trip_id });
    if (vehicle.current_stop_sequence !== null && vehicle.current_stop_sequence !== undefined) {
      params.set('current_stop_sequence', String(vehicle.current_stop_sequence));
    }
    if (vehicle.current_status !== null && vehicle.current_status !== undefined) {
      params.set('current_status', String(vehicle.current_status));
    }
    if (vehicle.stop_id) params.set('current_stop_id', vehicle.stop_id);
    try {
      const response = await fetch(`/api/trip-stops?${params}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Impossible de charger le parcours de ce véhicule.');
      if (requestId !== tripRequestId || tripPanel.hidden) return;
      renderVehicleTrip(data, vehicle);
    } catch (error) {
      if (requestId !== tripRequestId || tripPanel.hidden) return;
      tripStops.innerHTML = `<p class="schedule-empty">${escapeHtml(error.message || 'Impossible de charger les prochains arrêts.')}</p>`;
    }
  }

  function renderVehicleTrip(data, vehicle) {
    const stops = data.stops || [];
    const status = STATUS_LABELS[data.current_status ?? vehicle.current_status] || 'Trajet en cours';
    tripPanelDirection.textContent = data.direction || directionForVehicle(vehicle);
    if (!stops.length) {
      tripPanelStatus.textContent = 'Les horaires de ce trajet ne sont pas disponibles.';
      tripStops.innerHTML = '<p class="schedule-empty">Aucun arrêt n’a été trouvé pour ce trajet dans les horaires du réseau.</p>';
      return;
    }
    const firstRemaining = stops.findIndex((stop) => ['current', 'next', 'upcoming'].includes(stop.state));
    const visibleStops = stops.slice(firstRemaining >= 0 ? firstRemaining : stops.length - 1);
    tripPanelStatus.textContent = `${status} · ${visibleStops.length} arrêts à venir`;
    const color = validColor(data.color) || routeColor(vehicle);
    tripLineBadge.style.setProperty('--journey-color', color);
    tripStops.style.setProperty('--journey-color', color);
    tripStops.innerHTML = `<ol class="journey-list">${visibleStops.map((stop) => {
      const state = ['passed', 'current', 'next'].includes(stop.state) ? stop.state : 'upcoming';
      let delay = '';
      if (stop.delay !== null && stop.delay !== undefined && Math.abs(stop.delay) > 60) {
        const minutes = Math.round(Math.abs(stop.delay) / 60);
        delay = stop.delay > 0 ? `+${minutes} min` : `${minutes} min d’avance`;
      }
      const stateLabel = state === 'current' ? 'Véhicule à cet arrêt'
        : state === 'next' ? 'Prochain arrêt'
          : state === 'passed' ? 'Déjà desservi' : '';
      const timeLabel = stop.realtime ? 'Temps réel' : 'Horaire prévu';
      return `<li class="journey-stop ${state}"${state === 'current' ? ' aria-current="step"' : ''}><div class="journey-time">${escapeHtml(formatDepartureTime(stop.time))}<small>${stateLabel || (stop.minutes === 0 ? 'À l’approche' : `dans ${stop.minutes} min`)}</small></div><div class="journey-rail" aria-hidden="true"><span class="journey-dot"></span></div><div class="journey-stop-info"><strong class="journey-stop-name">${escapeHtml(stop.name)}</strong><span class="journey-stop-meta"><span class="realtime-label">${timeLabel}</span>${delay ? `<span class="journey-delay">${escapeHtml(delay)}</span>` : ''}</span></div></li>`;
    }).join('')}</ol>`;
  }

  function activateMapItem(item) {
    if (item.kind === 'stop') openStop(item.marker._tamStop);
    else openVehiclePopup(item.marker);
  }

  function handleMapItemClick(event, primary) {
    if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
    const origin = map.latLngToContainerPoint(primary.marker.getLatLng());
    const nearby = [];
    const addIfNearby = (item) => {
      const point = map.latLngToContainerPoint(item.marker.getLatLng());
      const distance = origin.distanceTo(point);
      if (distance <= 28 && !nearby.some((candidate) => candidate.marker === item.marker)) {
        nearby.push({ ...item, distance });
      }
    };
    for (const marker of stopMarkers.values()) {
      if (stopsLayer.hasLayer(marker)) addIfNearby({ kind: 'stop', marker });
    }
    for (const marker of vehicleMarkers.values()) {
      if (vehiclesLayer.hasLayer(marker)) addIfNearby({ kind: 'vehicle', marker });
    }
    nearby.sort((a, b) => a.marker === primary.marker ? -1 : b.marker === primary.marker ? 1 : a.distance - b.distance);
    if (nearby.length < 2) {
      activateMapItem(primary);
      return;
    }

    const container = document.createElement('div');
    container.className = 'overlap-picker-content';
    const heading = document.createElement('p');
    heading.className = 'overlap-picker-title';
    heading.textContent = 'Plusieurs éléments à cet endroit';
    const list = document.createElement('div');
    list.className = 'overlap-picker-list';
    for (const item of nearby) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'overlap-picker-option';
      let title;
      let subtitle;
      let badge;
      let color;
      if (item.kind === 'stop') {
        const stop = item.marker._tamStop;
        title = stop.name;
        subtitle = stop.mode === 'tramway' ? 'Arrêt de tramway · horaires' : 'Arrêt de bus · horaires';
        badge = 'Arrêt';
        color = '#19785e';
      } else {
        const vehicle = item.marker._tamVehicle || {};
        const label = routeLabel(vehicle);
        const number = vehicle.label || vehicle.id || '';
        title = `${label} · ${vehicle.route_mode === 'tramway' ? 'Tram' : 'Bus'} ${number}`;
        subtitle = directionForVehicle(vehicle);
        badge = label;
        color = routeColor(vehicle);
      }
      button.innerHTML = `<span class="overlap-picker-badge" style="--swatch:${color}">${escapeHtml(badge)}</span><span class="overlap-picker-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></span>`;
      button.addEventListener('click', () => {
        map.closePopup();
        activateMapItem(item);
      });
      list.append(button);
    }
    container.append(heading, list);
    L.popup({ className: 'overlap-picker', maxWidth: 280, autoPanPadding: [18, 18] })
      .setLatLng(primary.marker.getLatLng())
      .setContent(container)
      .openOn(map);
  }

  function nearestPointOnShape(point, shape) {
    const scaleY = 111_320;
    const scaleX = scaleY * Math.cos(point.lat * Math.PI / 180);
    const px = point.lng * scaleX;
    const py = point.lat * scaleY;
    let best = null;
    for (let i = 0; i < shape.points.length - 1; i += 1) {
      const a = shape.points[i];
      const b = shape.points[i + 1];
      const ax = a.lng * scaleX;
      const ay = a.lat * scaleY;
      const bx = b.lng * scaleX;
      const by = b.lat * scaleY;
      const dx = bx - ax;
      const dy = by - ay;
      const denominator = dx * dx + dy * dy;
      const fraction = denominator ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / denominator)) : 0;
      const projectedX = ax + dx * fraction;
      const projectedY = ay + dy * fraction;
      const distance = Math.hypot(px - projectedX, py - projectedY);
      if (!best || distance < best.distance) {
        best = {
          distance,
          distanceAlong: shape.cumulative[i] + (shape.cumulative[i + 1] - shape.cumulative[i]) * fraction,
        };
      }
    }
    return best;
  }

  function pointAtShapeDistance(shape, distance) {
    const target = Math.max(0, Math.min(shape.length, distance));
    let low = 0;
    let high = shape.cumulative.length - 1;
    while (low < high - 1) {
      const middle = Math.floor((low + high) / 2);
      if (shape.cumulative[middle] <= target) low = middle;
      else high = middle;
    }
    const segmentLength = shape.cumulative[low + 1] - shape.cumulative[low];
    const fraction = segmentLength ? (target - shape.cumulative[low]) / segmentLength : 0;
    const a = shape.points[low];
    const b = shape.points[low + 1];
    return L.latLng(a.lat + (b.lat - a.lat) * fraction, a.lng + (b.lng - a.lng) * fraction);
  }

  function bearingBetween(start, end) {
    const lat1 = start.lat * Math.PI / 180;
    const lat2 = end.lat * Math.PI / 180;
    const deltaLng = (end.lng - start.lng) * Math.PI / 180;
    const y = Math.sin(deltaLng) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(deltaLng);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }

  function angularDifference(first, second) {
    return Math.abs(((first - second + 540) % 360) - 180);
  }

  function shapeBearingAt(shape, distanceAlong) {
    const offset = Math.min(35, shape.length / 4);
    const start = pointAtShapeDistance(shape, Math.max(0, distanceAlong - offset));
    const end = pointAtShapeDistance(shape, Math.min(shape.length, distanceAlong + offset));
    return bearingBetween(start, end);
  }

  function observeVehicleMovement(nextVehicles) {
    const now = Date.now();
    const nextPositions = new Map();
    for (const vehicle of nextVehicles) {
      const id = String(vehicle.id || `${vehicle.route_id || ''}:${vehicle.trip_id || ''}`);
      const point = L.latLng(Number(vehicle.latitude), Number(vehicle.longitude));
      if (!Number.isFinite(point.lat) || !Number.isFinite(point.lng)) continue;
      const previous = previousVehiclePositions.get(id);
      if (previous && now - previous.time < 60_000 && previous.point.distanceTo(point) >= 12) {
        vehicleMotion.set(id, { bearing: bearingBetween(previous.point, point), time: now });
      }
      nextPositions.set(id, { point, time: now });
    }
    previousVehiclePositions.clear();
    for (const [id, position] of nextPositions) previousVehiclePositions.set(id, position);
    for (const [id, motion] of vehicleMotion) {
      if (now - motion.time > 90_000) vehicleMotion.delete(id);
    }
  }

  function headingForVehicle(vehicle) {
    const id = String(vehicle.id || `${vehicle.route_id || ''}:${vehicle.trip_id || ''}`);
    const observed = vehicleMotion.get(id);
    if (observed && Date.now() - observed.time < 75_000) return observed.bearing;
    if (vehicle.bearing === null || vehicle.bearing === undefined || vehicle.bearing === '') return null;
    const bearing = Number(vehicle.bearing);
    return Number.isFinite(bearing) && bearing >= 0 && bearing <= 360 ? bearing : null;
  }

  function shapeTerminals(shape) {
    let name = String(shape.name || '').replace(/^L\d+\s+/i, '').trim();
    name = name.replace(/\s+(?:Aller|Retour)(?:\s*-\s*V\d+)?\s*$/i, '').trim();
    const arrow = name.match(/^(.+?)\s*>\s*(.+)$/);
    if (arrow) return { from: arrow[1].trim(), to: arrow[2].trim() };
    if (shape.number === 3) {
      const version = normalizeText(shape.direction).match(/v\s*([12])/);
      const branch = version?.[1] === '2' ? 'Lattes Centre' : 'Pérols Étang de l’Or';
      return normalizeText(shape.direction).includes('retour')
        ? { from: 'Juvignac', to: branch }
        : { from: branch, to: 'Juvignac' };
    }
    return null;
  }

  function findControlPoint(shape, aliases, fallback) {
    const stop = stops.find((item) => item.mode === 'tramway'
      && aliases.some((alias) => normalizeText(item.name).includes(normalizeText(alias))));
    const point = stop
      ? L.latLng(stop.latitude, stop.longitude)
      : L.latLng(fallback[0], fallback[1]);
    const projected = nearestPointOnShape(point, shape);
    return projected && projected.distance <= 220 ? projected : null;
  }

  function passedControlPoint(current, control, movingForward) {
    if (!control) return false;
    return movingForward
      ? current.distanceAlong > control.distanceAlong + 130
      : current.distanceAlong < control.distanceAlong - 130;
  }

  function inferTramDirection(vehicle) {
    const number = routeNumber(vehicle);
    const heading = headingForVehicle(vehicle);
    if (number === null || heading === null) return 'Direction inconnue';
    const point = L.latLng(Number(vehicle.latitude), Number(vehicle.longitude));
    const candidates = [];
    for (const shape of tramShapes.filter((item) => item.number === number)) {
      const nearest = nearestPointOnShape(point, shape);
      if (!nearest || nearest.distance > 180) continue;
      const routeBearing = shapeBearingAt(shape, nearest.distanceAlong);
      const difference = angularDifference(heading, routeBearing);
      const movingForward = difference <= 90;
      const terminals = shapeTerminals(shape);
      if (!terminals) continue;
      candidates.push({
        shape,
        nearest,
        movingForward,
        difference,
        score: nearest.distance + difference * 1.7,
        destination: movingForward ? terminals.to : terminals.from,
      });
    }
    candidates.sort((a, b) => a.score - b.score);
    if (!candidates.length || candidates[0].difference > 75) return 'Direction inconnue';

    const best = candidates[0];
    if (number === 3) {
      const competingBranch = candidates.find((candidate) =>
        candidate !== best
        && candidate.score <= best.score + 55
        && normalizeText(candidate.destination) !== normalizeText(best.destination)
        && normalizeText(candidate.destination).includes('juvignac') === normalizeText(best.destination).includes('juvignac'));
      if (competingBranch && !normalizeText(best.destination).includes('juvignac')) {
        return 'Lattes Centre/Pérols Étang de l’Or';
      }
    }

    const destination = best.destination;
    if (number === 2 && normalizeText(destination).includes('jacou')) {
      const sablassou = findControlPoint(best.shape, ['Sablassou'], [43.6342244704, 3.9222884194]);
      return passedControlPoint(best.nearest, sablassou, best.movingForward)
        ? 'Jacou' : 'Nd de Sablassou/Jacou';
    }
    if (number === 2 && normalizeText(destination).includes('vedas')) {
      const sabines = findControlPoint(best.shape, ['Sabines'], [43.5838028699, 3.8601801702]);
      return passedControlPoint(best.nearest, sabines, best.movingForward)
        ? 'St Jean de Védas' : 'Sabines/St Jean de Védas';
    }
    return destination || 'Direction inconnue';
  }

  function directionForVehicle(vehicle) {
    const direction = String(vehicle.direction || '').trim();
    const routeName = String(vehicle.route_long_name || '').trim();
    if (direction && normalizeText(direction) !== normalizeText(routeName)) return direction;
    return vehicle.route_mode === 'tramway' ? inferTramDirection(vehicle) : 'Direction inconnue';
  }

  function pathBetweenShapePoints(shape, fromDistance, toDistance) {
    const start = Math.min(fromDistance, toDistance);
    const end = Math.max(fromDistance, toDistance);
    const path = [pointAtShapeDistance(shape, start)];
    for (let i = 1; i < shape.points.length - 1; i += 1) {
      if (shape.cumulative[i] > start && shape.cumulative[i] < end) path.push(shape.points[i]);
    }
    path.push(pointAtShapeDistance(shape, end));
    if (toDistance < fromDistance) path.reverse();
    return path;
  }

  function tramPathBetween(start, end, route) {
    const number = routeNumber(route);
    if (number === null) return null;
    const candidates = tramShapes.filter((shape) => shape.number === number);
    if (!candidates.length) return null;
      const direction = normalizeText(route.direction);
    let best = null;
    for (const shape of candidates) {
      const from = nearestPointOnShape(start, shape);
      const to = nearestPointOnShape(end, shape);
      if (!from || !to || from.distance > 120 || to.distance > 120) continue;
      const shapeName = normalizeText(shape.name);
      const directionBonus = direction && shapeName.includes(direction) ? 35 : 0;
      const score = from.distance + to.distance - directionBonus;
      if (!best || score < best.score) best = { shape, from, to, score };
    }
    if (!best) return null;
    const path = pathBetweenShapePoints(best.shape, best.from.distanceAlong, best.to.distanceAlong);
    if (path.length < 2 || path[0].distanceTo(path[path.length - 1]) < 2) return null;
    path[0] = start;
    path[path.length - 1] = end;
    return path;
  }

  function animateMarker(marker, target, route) {
    const start = marker.getLatLng();
    const end = L.latLng(target.latitude, target.longitude);
    if (start.distanceTo(end) < 1) return;

    let path = route.route_mode === 'tramway' ? tramPathBetween(start, end, route) : null;
    if (!path) path = [start, end];
    const cumulative = [0];
    for (let i = 1; i < path.length; i += 1) {
      cumulative.push(cumulative[i - 1] + path[i - 1].distanceTo(path[i]));
    }
    const totalDistance = cumulative[cumulative.length - 1];
    if (!totalDistance) return;

    const animationId = (animationIds.get(marker) || 0) + 1;
    animationIds.set(marker, animationId);
    const startedAt = performance.now();
    const duration = 2000;
    const drawFrame = (now) => {
      if (animationIds.get(marker) !== animationId) return;
      const progress = Math.min(1, (now - startedAt) / duration);
      const distance = totalDistance * progress;
      let segment = 0;
      while (segment < cumulative.length - 2 && cumulative[segment + 1] < distance) segment += 1;
      const segmentLength = cumulative[segment + 1] - cumulative[segment];
      const part = segmentLength ? (distance - cumulative[segment]) / segmentLength : 0;
      const a = path[segment];
      const b = path[segment + 1];
      marker.setLatLng(L.latLng(a.lat + (b.lat - a.lat) * part, a.lng + (b.lng - a.lng) * part));
      if (progress < 1) window.requestAnimationFrame(drawFrame);
      else marker.setLatLng(end);
    };
    window.requestAnimationFrame(drawFrame);
  }

  function readableTextColor(route, background) {
    const explicit = validColor(route.route_text_color);
    if (explicit) return explicit;
    const rgb = background.slice(1).match(/../g).map((part) => parseInt(part, 16));
    return (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000 > 150 ? '#17211c' : '#ffffff';
  }

  function addFallbackRoutes() {
    for (const vehicle of vehicles) {
      const key = String(vehicle.route_id || '');
      if (!key || routeById.has(key)) continue;
      routeById.set(key, {
        route_id: key,
        short_name: vehicle.route_short_name || key,
        long_name: vehicle.route_long_name || '',
        mode: vehicle.route_mode || 'autre',
        color: validColor(vehicle.route_color) || FALLBACK_COLOR,
        text_color: validColor(vehicle.route_text_color),
      });
    }
  }

  async function loadRoutes() {
    const response = await fetch('/api/routes', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Impossible de charger les lignes du réseau.');
    routeById = new Map((data.routes || []).map((route) => [String(route.route_id), route]));
    addFallbackRoutes();
    drawBustramLines();
    renderLineList();
    renderNetworkSearch();
    renderMap();
  }

  async function loadTramLines() {
    const response = await fetch('/api/lines', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Impossible de charger les tracés de tram.');
    lineFeatures = (data.features || []).filter((feature) => feature.geometry);
    prepareTramShapes();
    drawTramLines();
  }

  async function loadBusLines() {
    const response = await fetch('/api/bus-lines', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Impossible de charger les tracés de bus.');
    busLineFeatures = (data.features || []).filter((feature) => feature.geometry);
    drawBusLines();
  }

  async function loadBustramLines() {
    const response = await fetch('/api/bustram-lines', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Impossible de charger le tracé du Bustram A.');
    bustramLineFeatures = (data.features || []).filter((feature) => feature.geometry);
    drawBustramLines();
  }

  function loadTramLinesWithRetry() {
    loadTramLines().then(() => {
      if (notice.dataset.kind === 'line-error') notice.hidden = true;
    }).catch((error) => {
      showNotice(`Tracés de tram indisponibles. Nouvelle tentative automatique. ${error.message || ''}`, 0, 'line-error');
      window.setTimeout(loadTramLinesWithRetry, 30_000);
    });
  }

  function loadBusLinesWithRetry() {
    loadBusLines().then(() => {
      if (notice.dataset.kind === 'bus-line-error') notice.hidden = true;
    }).catch((error) => {
      showNotice(`Tracés de bus indisponibles. Nouvelle tentative automatique. ${error.message || ''}`, 0, 'bus-line-error');
      window.setTimeout(loadBusLinesWithRetry, 30_000);
    });
  }

  function loadBustramLinesWithRetry() {
    loadBustramLines().then(() => {
      if (notice.dataset.kind === 'bustram-line-error') notice.hidden = true;
    }).catch((error) => {
      showNotice(`Tracé du Bustram A indisponible. Nouvelle tentative automatique. ${error.message || ''}`, 0, 'bustram-line-error');
      window.setTimeout(loadBustramLinesWithRetry, 30_000);
    });
  }

  function selected(routeId) {
    return selectedRouteIds === null || !routeId || selectedRouteIds.has(String(routeId));
  }

  function renderLineList() {
    lineList.replaceChildren();
    const routes = [...routeById.values()].sort((a, b) => {
      const modeOrder = { tramway: 0, bus: 1, autre: 2 };
      return (modeOrder[a.mode] ?? 2) - (modeOrder[b.mode] ?? 2)
        || String(a.short_name || '').localeCompare(String(b.short_name || ''), 'fr', { numeric: true });
    });
    const groups = [
      { mode: 'tramway', title: 'TRAMWAYS' },
      { mode: 'bus', title: 'BUS' },
      { mode: 'autre', title: 'AUTRES VÉHICULES' },
    ];

    for (const groupInfo of groups) {
      const items = routes.filter((route) => (route.mode || 'autre') === groupInfo.mode);
      if (!items.length) continue;
      const group = document.createElement('section');
      group.className = 'route-group';
      const heading = document.createElement('h3');
      heading.className = 'route-group-title';
      heading.textContent = groupInfo.title;
      group.append(heading);

      for (const route of items) {
        const key = String(route.route_id);
        const color = routeColor({
          ...route,
          route_mode: route.mode,
          route_short_name: route.short_name,
          route_long_name: route.long_name,
          route_color: route.color,
          route_text_color: route.text_color,
        });
        const count = vehicles.filter((vehicle) => String(vehicle.route_id || '') === key).length;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `line-option${selected(key) ? ' selected' : ''}`;
        button.setAttribute('aria-pressed', String(selected(key)));
        button.style.setProperty('--swatch', color);
        button.innerHTML = `<span class="line-swatch"></span><span class="line-number">${escapeHtml(route.mode === 'tramway' && !/^T/i.test(route.short_name || '') ? `T${route.short_name || ''}` : route.short_name || '?')}</span><span class="line-text"><span class="line-name">${escapeHtml(route.long_name || (route.mode === 'tramway' ? 'Tramway' : 'Ligne de bus'))}</span></span><span class="line-vehicle-count">${count}</span>`;
        button.addEventListener('click', () => {
          if (selectedRouteIds === null) selectedRouteIds = new Set(routes.map((item) => String(item.route_id)));
          if (selectedRouteIds.has(key)) selectedRouteIds.delete(key);
          else selectedRouteIds.add(key);
          if (routes.every((item) => selectedRouteIds.has(String(item.route_id)))) selectedRouteIds = null;
          saveSelectedRouteIds();
          renderLineList();
          renderMap();
        });
        group.append(button);
      }
      lineList.append(group);
    }
  }

  function renderMap() {
    const activeMarkers = new Set();
    for (const rawVehicle of vehicles) {
      const vehicle = routeForVehicle(rawVehicle);
      const routeId = String(vehicle.route_id || '');
      const markerId = String(vehicle.id || `${routeId}:${vehicle.trip_id || ''}`);
      activeMarkers.add(markerId);
      const label = routeLabel(vehicle);
      const markerLabel = label.length <= 3 ? label : (label.match(/\d+/)?.[0] || label.slice(0, 1).toUpperCase());
      const color = routeColor(vehicle);
      const textColor = readableTextColor(vehicle, color);
      const isTram = vehicle.route_mode === 'tramway';
      const markerSize = isTram ? 32 : 28;
      const bearing = vehicle.bearing === null || vehicle.bearing === undefined || vehicle.bearing === ''
        ? Number.NaN : Number(vehicle.bearing);
      const arrow = Number.isFinite(bearing)
        ? `<span class="vehicle-arrow" style="--bearing:${bearing}deg;--arrow-distance:${-(isTram ? 16 : 13)}px"></span>`
        : '';
      const icon = L.divIcon({
        className: '',
        html: `<span class="vehicle-marker-shell ${isTram ? 'tram' : 'bus'}">${arrow}<span class="tram-marker" style="--vehicle-color:${color};--vehicle-text:${textColor}">${escapeHtml(markerLabel)}</span></span>`,
        iconSize: [markerSize, markerSize],
        iconAnchor: [markerSize / 2, markerSize / 2],
      });
      const vehicleNumber = vehicle.label || vehicle.id || 'Numéro indisponible';
      const modeLabel = vehicle.route_mode === 'tramway' ? 'Tram' : vehicle.route_mode === 'bus' ? 'Bus' : 'Véhicule';
      const lineName = label === '?' ? 'Ligne non précisée' : `Ligne ${label}`;
      const direction = directionForVehicle(vehicle);
      const age = vehicle.timestamp
        ? `${Math.max(0, Math.round(Date.now() / 1000 - vehicle.timestamp))} s`
        : 'inconnue';
      const status = STATUS_LABELS[vehicle.current_status] || 'Statut non précisé';
      const tripButton = vehicle.route_mode === 'tramway' && vehicle.trip_id
        ? '<button class="trip-open-button" type="button"><span aria-hidden="true">↗</span> Voir le trajet et les prochains arrêts</button>'
        : '';
      const popup = `<div class="vehicle-popup"><div class="popup-line-heading"><span>Ligne</span><span class="popup-line-badge" style="background:${color};color:${textColor}">${escapeHtml(label)}</span></div><div class="popup-direction">Direction <strong>${escapeHtml(direction)}</strong></div><div class="popup-vehicle"><span>${escapeHtml(modeLabel)}</span><strong>${escapeHtml(vehicleNumber)}</strong></div><div class="popup-meta">${escapeHtml(status)} · position reçue il y a ${escapeHtml(age)}</div>${tripButton}</div>`;
      let marker = vehicleMarkers.get(markerId);
      if (!marker) {
        marker = L.marker([vehicle.latitude, vehicle.longitude], {
          icon,
          title: `${lineName} · ${modeLabel} ${vehicleNumber}`,
        });
        marker.on('click', (event) => handleMapItemClick(event, { kind: 'vehicle', marker }));
        vehicleMarkers.set(markerId, marker);
      } else {
        marker.options.title = `${lineName} · ${modeLabel} ${vehicleNumber}`;
        marker.setIcon(icon);
        animateMarker(marker, vehicle, vehicle);
      }
      marker._tamVehicle = vehicle;
      marker._tamPopupContent = popup;

      if (selected(routeId)) {
        if (!vehiclesLayer.hasLayer(marker)) marker.addTo(vehiclesLayer);
      } else if (vehiclesLayer.hasLayer(marker)) {
        vehiclesLayer.removeLayer(marker);
      }
    }

    for (const [markerId, marker] of vehicleMarkers) {
      if (activeMarkers.has(markerId)) continue;
      animationIds.set(marker, (animationIds.get(marker) || 0) + 1);
      vehiclesLayer.removeLayer(marker);
      vehicleMarkers.delete(markerId);
    }
  }

  function formatDepartureTime(isoTime) {
    const date = new Date(isoTime);
    return Number.isNaN(date.getTime())
      ? '—'
      : date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  }

  function renderDepartures(departures) {
    if (!selectedStop) return;
    if (!departures.length) {
      stopDepartures.innerHTML = `<p class="schedule-empty">${selectedScheduleTab === 'realtime'
        ? 'Aucune prédiction temps réel disponible pour le moment.'
        : 'Aucun passage théorique à venir pour cet arrêt.'}</p>`;
      return;
    }
    stopDepartures.innerHTML = `<div class="departure-list">${departures.map((departure) => {
      const color = validColor(departure.color) || FALLBACK_COLOR;
      const delay = departure.delay === null || departure.delay === undefined
        ? ''
        : departure.delay > 60 ? `+${Math.round(departure.delay / 60)} min`
          : departure.delay < -60 ? `${Math.round(departure.delay / 60)} min`
            : 'À l’heure';
      const countdown = departure.minutes === 0 ? 'À l’approche' : `dans ${departure.minutes} min`;
      return `<article class="departure-item"><div class="departure-main"><span class="departure-line" style="--departure-color:${color}">${escapeHtml(departure.route)}</span><span class="departure-destination">${escapeHtml(departure.direction || 'Direction inconnue')}${departure.vehicle ? `<small>${escapeHtml(selectedStop.mode === 'tramway' ? 'Tram' : 'Bus')} ${escapeHtml(departure.vehicle)}</small>` : ''}</span><strong class="departure-time">${escapeHtml(formatDepartureTime(departure.time))}<small>${escapeHtml(countdown)}</small></strong></div>${delay ? `<div class="departure-meta">${selectedScheduleTab === 'realtime' ? 'Temps réel' : 'Théorique'} · ${escapeHtml(delay)}</div>` : ''}</article>`;
    }).join('')}</div>`;
  }

  async function loadStopSchedules() {
    if (!selectedStop) return;
    const requestId = ++scheduleRequestId;
    stopDepartures.innerHTML = '<p class="schedule-loading">Chargement des horaires…</p>';
    const params = new URLSearchParams({
      mode: selectedStop.mode,
      tab: selectedScheduleTab,
      stop_ids: (selectedStop.gtfs_stop_ids || []).join(','),
      name: selectedStop.name || '',
      latitude: String(selectedStop.latitude),
      longitude: String(selectedStop.longitude),
      points: JSON.stringify(selectedStop.schedule_match_points || []),
    });
    try {
      const response = await fetch(`/api/stop-schedules?${params}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Les horaires sont momentanément indisponibles.');
      if (requestId !== scheduleRequestId || !selectedStop) return;
      if (!data.stop_ids?.length) {
        stopDepartures.innerHTML = '<p class="schedule-empty">Aucun horaire GTFS correspondant à cet arrêt.</p>';
        return;
      }
      if (!selectedStop.gtfs_stop_ids?.length && data.stop_ids?.length) {
        selectedStop.gtfs_stop_ids = data.stop_ids;
      }
      renderDepartures(data.departures || []);
    } catch (error) {
      if (requestId !== scheduleRequestId || !selectedStop) return;
      stopDepartures.innerHTML = `<p class="schedule-empty">${escapeHtml(error.message || 'Impossible de charger les horaires.')}</p>`;
    }
  }

  function setScheduleTab(tab) {
    selectedScheduleTab = tab;
    document.querySelectorAll('.schedule-tab').forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    loadStopSchedules();
    if (scheduleRefreshTimer) window.clearInterval(scheduleRefreshTimer);
    if (tab === 'realtime' && selectedStop) {
      scheduleRefreshTimer = window.setInterval(loadStopSchedules, 15_000);
    }
  }

  function openStop(stop) {
    map.closePopup();
    tripRequestId += 1;
    tripPanel.hidden = true;
    selectedStop = stop;
    stopPanelName.textContent = stop.name;
    stopPanelMode.textContent = stop.mode === 'tramway' ? 'Arrêt de tramway' : 'Arrêt de bus';
    updateFavoriteButton();
    stopPanel.hidden = false;
    setScheduleTab(selectedScheduleTab);
  }

  function locateUser() {
    if (!navigator.geolocation) {
      showNotice('La géolocalisation n’est pas disponible dans ce navigateur.');
      return;
    }
    myLocationButton.disabled = true;
    navigator.geolocation.getCurrentPosition((position) => {
      const { latitude, longitude, accuracy } = position.coords;
      userLocationLayer.clearLayers();
      if (Number.isFinite(accuracy) && accuracy > 0 && accuracy < 20_000) {
        L.circle([latitude, longitude], {
          pane: 'stopsPane',
          radius: accuracy,
          color: '#2878e8',
          weight: 1,
          opacity: 0.65,
          fillColor: '#2878e8',
          fillOpacity: 0.1,
          interactive: false,
        }).addTo(userLocationLayer);
      }
      const icon = L.divIcon({
        className: 'user-location-icon',
        html: '<span class="user-location-dot" aria-label="Ma position"></span>',
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([latitude, longitude], { icon, zIndexOffset: 1000, title: 'Ma position' }).addTo(userLocationLayer);
      map.flyTo([latitude, longitude], Math.max(map.getZoom(), 16), { duration: 0.65 });
      myLocationButton.disabled = false;
    }, (error) => {
      const message = error.code === 1
        ? 'Autorisez l’accès à votre position dans les réglages du navigateur.'
        : error.code === 3
          ? 'La recherche de votre position a expiré. Réessayez.'
          : 'Votre position est momentanément indisponible.';
      showNotice(message, 8000);
      myLocationButton.disabled = false;
    }, { enableHighAccuracy: true, maximumAge: 30_000, timeout: 12_000 });
  }

  async function loadStops() {
    try {
      const response = await fetch('/api/stops', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Impossible de charger les arrêts.');
      stops = data.stops || [];
      stopsLayer.clearLayers();
      stopMarkers.clear();
      for (const stop of stops) {
        const isTram = stop.mode === 'tramway';
        const favorite = isFavoriteStop(stop);
        const marker = L.circleMarker([stop.latitude, stop.longitude], {
          pane: 'stopsPane',
          radius: (isTram ? 4.5 : 3.5) + (favorite ? 1 : 0),
          color: favorite ? '#c48b1e' : (isTram ? '#126b54' : '#64776d'),
          weight: favorite ? 2 : 1.5,
          opacity: 0.95,
          fillColor: favorite ? '#fff5dc' : '#ffffff',
          fillOpacity: 0.95,
          bubblingMouseEvents: false,
        });
        marker._tamStop = stop;
        marker.bindTooltip(escapeHtml(stop.name), { direction: 'top', offset: [0, -5], opacity: 0.95 });
        marker.on('click', (event) => handleMapItemClick(event, { kind: 'stop', marker }));
        marker.addTo(stopsLayer);
        stopMarkers.set(stopKey(stop), marker);
      }
      renderFavoriteStops();
      renderNetworkSearch();
      renderMap();
      stopsRetryDelay = 15_000;
      if (notice.dataset.kind === 'stops-error') notice.hidden = true;
    } catch (error) {
      showNotice('Les arrêts TAM sont temporairement indisponibles. Nouvelle tentative automatique.', 0, 'stops-error');
      window.setTimeout(loadStops, stopsRetryDelay);
      stopsRetryDelay = Math.min(stopsRetryDelay * 2, 5 * 60_000);
    }
  }

  async function refreshVehicles() {
    if (pollInProgress) return;
    pollInProgress = true;
    try {
      const response = await fetch('/api/vehicles', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Le flux de positions est indisponible.');
      const nextVehicles = data.vehicles || [];
      observeVehicleMovement(nextVehicles);
      vehicles = nextVehicles;
      addFallbackRoutes();
      vehicleCount.textContent = String(data.count ?? vehicles.length);
      const temporarilyLimited = Boolean(data.temporarily_limited);
      const fetchedAt = data.fetched_at ? new Date(data.fetched_at) : null;
      updatedAt.textContent = fetchedAt
        ? fetchedAt.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '—';
      setConnection(!temporarilyLimited, temporarilyLimited ? 'Réessai différé' : 'Flux en direct');
      renderLineList();
      renderNetworkSearch();
      renderMap();
    } catch (error) {
      setConnection(false, 'Flux indisponible');
      showNotice(error.message || 'Impossible de récupérer les positions des véhicules.');
    } finally {
      pollInProgress = false;
    }
  }

  document.getElementById('all-lines').addEventListener('click', () => {
    selectedRouteIds = null;
    saveSelectedRouteIds();
    renderLineList();
    renderMap();
  });
  document.getElementById('locate-map').addEventListener('click', () => {
    const bounds = tramLinesLayer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [38, 38] });
  });
  myLocationButton.addEventListener('click', locateUser);
  favoriteButton.addEventListener('click', toggleFavoriteStop);
  searchInput.addEventListener('input', renderNetworkSearch);
  searchInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      searchResults.querySelector('.search-result')?.click();
    } else if (event.key === 'Escape') {
      clearNetworkSearch();
    }
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.network-search')) searchResults.hidden = true;
  });
  document.querySelectorAll('.schedule-tab').forEach((button) => {
    button.addEventListener('click', () => setScheduleTab(button.dataset.tab));
  });
  document.getElementById('close-stop-panel').addEventListener('click', () => {
    selectedStop = null;
    scheduleRequestId += 1;
    stopPanel.hidden = true;
    map.closePopup();
    if (scheduleRefreshTimer) window.clearInterval(scheduleRefreshTimer);
    scheduleRefreshTimer = null;
  });
  document.getElementById('close-trip-panel').addEventListener('click', () => {
    tripRequestId += 1;
    tripPanel.hidden = true;
  });

  loadRoutes().catch((error) => showNotice(error.message || 'Chargement des lignes indisponible.'));
  loadTramLinesWithRetry();
  loadBusLinesWithRetry();
  loadBustramLinesWithRetry();
  loadStops();
  refreshVehicles();
  window.setInterval(refreshVehicles, REFRESH_MS);
  window.addEventListener('resize', () => map.invalidateSize());
})();
