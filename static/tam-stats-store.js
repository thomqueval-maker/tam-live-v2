(() => {
  const DATABASE_NAME = 'tam-live-statistics';
  const DATABASE_VERSION = 1;
  const STORE_NAME = 'samples';
  const RETENTION_DAYS = 45;
  const LINE_SAMPLE_MINUTES = 5;
  let databasePromise;
  let lastSavedMinute = null;
  let savingMinute = null;

  function openDatabase() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('IndexedDB indisponible dans ce navigateur.'));
    if (databasePromise) return databasePromise;
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          database.createObjectStore(STORE_NAME, { keyPath: 'timestamp' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Impossible d’ouvrir l’historique local.'));
      request.onblocked = () => reject(new Error('Ferme les autres onglets TAM en direct pour initialiser les statistiques.'));
    }).catch((error) => {
      databasePromise = null;
      throw error;
    });
    return databasePromise;
  }

  function getDelay(vehicle) {
    if (vehicle.delay_seconds === null || vehicle.delay_seconds === undefined) return null;
    const delay = Number(vehicle.delay_seconds);
    return Number.isFinite(delay) ? delay : null;
  }

  function routeLabel(vehicle) {
    const shortName = String(vehicle.route_short_name || '').trim();
    if (vehicle.route_mode === 'tramway' && shortName && !/^T/i.test(shortName)) return `T${shortName}`;
    return shortName || (vehicle.route_long_name ? String(vehicle.route_long_name) : 'Ligne inconnue');
  }

  function buildSample(vehicles, timestamp) {
    const knownVehicles = vehicles
      .map((vehicle) => ({ vehicle, delay: getDelay(vehicle) }))
      .filter((item) => item.delay !== null);
    const lateCount = knownVehicles.filter((item) => item.delay > 300).length;
    const totalDelaySeconds = knownVehicles.reduce((sum, item) => sum + Math.max(0, item.delay), 0);
    const lines = new Map();

    for (const { vehicle, delay } of knownVehicles) {
      const routeId = String(vehicle.route_id || `${vehicle.route_mode || 'other'}:${routeLabel(vehicle)}`);
      let line = lines.get(routeId);
      if (!line) {
        line = {
          id: routeId,
          label: routeLabel(vehicle),
          mode: vehicle.route_mode || 'autre',
          color: /^#[0-9a-f]{6}$/i.test(String(vehicle.route_color || '')) ? vehicle.route_color : '#19785e',
          known: 0,
          late: 0,
          delaySeconds: 0,
        };
        lines.set(routeId, line);
      }
      line.known += 1;
      if (delay > 300) line.late += 1;
      line.delaySeconds += Math.max(0, delay);
    }

    const date = new Date(timestamp);
    return {
      timestamp,
      totalVehicles: vehicles.length,
      knownVehicles: knownVehicles.length,
      lateVehicles: lateCount,
      totalDelaySeconds,
      lineData: date.getMinutes() % LINE_SAMPLE_MINUTES === 0
        ? [...lines.values()]
          .sort((a, b) => a.label.localeCompare(b.label, 'fr', { numeric: true }))
          .map((line) => [line.id, line.label, line.mode, line.color, line.known, line.late, line.delaySeconds])
        : null,
    };
  }

  async function record(vehicles) {
    if (!Array.isArray(vehicles)) return false;
    const timestamp = Math.floor(Date.now() / 60_000) * 60_000;
    if (timestamp === lastSavedMinute || timestamp === savingMinute) return true;
    savingMinute = timestamp;
    try {
      const database = await openDatabase();
      await new Promise((resolve, reject) => {
        const transaction = database.transaction(STORE_NAME, 'readwrite');
        const store = transaction.objectStore(STORE_NAME);
        store.put(buildSample(vehicles, timestamp));
        const cutoff = timestamp - RETENTION_DAYS * 24 * 60 * 60_000;
        const cursorRequest = store.openCursor(IDBKeyRange.upperBound(cutoff));
        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (cursor) {
            cursor.delete();
            cursor.continue();
          }
        };
        transaction.oncomplete = resolve;
        transaction.onerror = () => reject(transaction.error || new Error('Écriture des statistiques impossible.'));
        transaction.onabort = () => reject(transaction.error || new Error('Enregistrement des statistiques annulé.'));
      });
      lastSavedMinute = timestamp;
      return true;
    } catch (error) {
      console.info('Historique TAM non enregistré :', error);
      return false;
    } finally {
      savingMinute = null;
    }
  }

  async function readAll(since = Date.now() - RETENTION_DAYS * 24 * 60 * 60_000) {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, 'readonly');
      const request = transaction.objectStore(STORE_NAME).getAll(IDBKeyRange.lowerBound(since));
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error || new Error('Lecture de l’historique impossible.'));
    });
  }

  window.TamStats = { record, readAll };
})();
