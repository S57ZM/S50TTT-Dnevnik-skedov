(function () {
  "use strict";

  const DB_NAME = "s50ttt-offline";
  const DB_VERSION = 1;
  const SNAPSHOTS = "snapshots";
  const OPERATIONS = "operations";
  const META = "meta";
  let deferredInstallPrompt = null;

  function openDatabase() {
    return new Promise(function (resolve, reject) {
      if (!("indexedDB" in window)) {
        reject(new Error("IndexedDB ni na voljo."));
        return;
      }
      const request = window.indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(SNAPSHOTS)) {
          db.createObjectStore(SNAPSHOTS, { keyPath: "net_id" });
        }
        if (!db.objectStoreNames.contains(OPERATIONS)) {
          const store = db.createObjectStore(OPERATIONS, { keyPath: "operation_id" });
          store.createIndex("net_id", "net_id", { unique: false });
        }
        if (!db.objectStoreNames.contains(META)) {
          db.createObjectStore(META, { keyPath: "key" });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function storeRequest(storeName, mode, callback) {
    return openDatabase().then(function (db) {
      return new Promise(function (resolve, reject) {
        const transaction = db.transaction(storeName, mode);
        const store = transaction.objectStore(storeName);
        let request;
        let result;
        try {
          request = callback(store);
        } catch (error) {
          reject(error);
          return;
        }
        if (request) {
          request.onsuccess = function () { result = request.result; };
          request.onerror = function () { reject(request.error); };
        }
        transaction.oncomplete = function () { resolve(result); };
        transaction.onerror = function () { reject(transaction.error); };
        transaction.onabort = function () { reject(transaction.error); };
      });
    });
  }

  function putRecord(storeName, value) {
    return storeRequest(storeName, "readwrite", function (store) { return store.put(value); });
  }

  function getRecord(storeName, key) {
    return storeRequest(storeName, "readonly", function (store) { return store.get(key); });
  }

  function getAllRecords(storeName) {
    return storeRequest(storeName, "readonly", function (store) { return store.getAll(); });
  }

  function deleteRecord(storeName, key) {
    return storeRequest(storeName, "readwrite", function (store) { return store.delete(key); });
  }

  function setMeta(key, value) {
    return putRecord(META, { key: key, value: value });
  }

  function getMeta(key) {
    return getRecord(META, key).then(function (record) { return record ? record.value : null; });
  }

  function clearOfflineData() {
    return openDatabase().then(function (db) {
      return new Promise(function (resolve, reject) {
        const transaction = db.transaction([SNAPSHOTS, OPERATIONS, META], "readwrite");
        transaction.objectStore(SNAPSHOTS).clear();
        transaction.objectStore(OPERATIONS).clear();
        transaction.objectStore(META).clear();
        transaction.oncomplete = resolve;
        transaction.onerror = function () { reject(transaction.error); };
      });
    });
  }

  function saveSnapshot(snapshot) {
    if (!snapshot || !snapshot.net || !snapshot.net.id) return Promise.resolve();
    const rawNetId = String(snapshot.net.id);
    snapshot.net_id = rawNetId.startsWith("local:") ? rawNetId : Number(rawNetId);
    snapshot.local_saved_at = new Date().toISOString();
    return putRecord(SNAPSHOTS, snapshot)
      .then(function () { return setMeta("current_net_id", snapshot.net_id); });
  }

  function currentSnapshot() {
    return getMeta("current_net_id").then(function (netId) {
      if (netId !== null) return getRecord(SNAPSHOTS, netId);
      return getAllRecords(SNAPSHOTS).then(function (snapshots) {
        snapshots.sort(function (left, right) {
          return String(right.local_saved_at || "").localeCompare(String(left.local_saved_at || ""));
        });
        return snapshots[0] || null;
      });
    });
  }

  function operationId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID().replace(/-/g, "");
    }
    const random = Math.random().toString(36).slice(2) + Date.now().toString(36);
    return (random + Math.random().toString(36).slice(2)).slice(0, 32);
  }

  function queueOperation(netId, action, data, id) {
    const rawNetId = String(netId);
    const operation = {
      operation_id: id || operationId(),
      net_id: rawNetId.startsWith("local:") ? rawNetId : Number(rawNetId),
      action: action,
      data: data,
      created_at: new Date().toISOString()
    };
    return putRecord(OPERATIONS, operation).then(function () { return operation; });
  }

  function pendingOperations(netId) {
    return getAllRecords(OPERATIONS).then(function (operations) {
      return operations.filter(function (operation) {
        return netId === undefined || String(operation.net_id) === String(netId);
      }).sort(function (left, right) {
        return String(left.created_at).localeCompare(String(right.created_at));
      });
    });
  }

  function showStatus(message, tone) {
    document.querySelectorAll("[data-pwa-status]").forEach(function (element) {
      element.textContent = message || "";
      element.hidden = !message;
      element.classList.remove("success", "warning", "danger");
      if (tone) element.classList.add(tone);
    });
  }

  function rememberCurrentCsrf() {
    const element = document.querySelector('meta[name="csrf-token"]');
    if (element && element.content) return setMeta("csrf_token", element.content);
    return Promise.resolve();
  }

  function rememberCurrentUser() {
    const element = document.querySelector('meta[name="offline-user-id"]');
    const currentUserId = element ? String(element.content || "") : "";
    if (!currentUserId) return Promise.resolve();
    return getMeta("offline_user_id").then(function (savedUserId) {
      if (savedUserId && String(savedUserId) !== currentUserId) {
        return clearOfflineData().then(function () {
          return setMeta("offline_user_id", currentUserId);
        });
      }
      return setMeta("offline_user_id", currentUserId);
    });
  }

  function updateOnlineOnlyControls() {
    document.querySelectorAll("[data-online-only]").forEach(function (root) {
      root.classList.toggle("is-disabled", !navigator.onLine);
      root.setAttribute("aria-disabled", String(!navigator.onLine));
      if (root.matches("a")) root.tabIndex = navigator.onLine ? 0 : -1;
      root.querySelectorAll("button, input, select, textarea").forEach(function (control) {
        control.disabled = !navigator.onLine;
      });
      root.title = navigator.onLine ? "" : "Ta funkcija potrebuje povezavo.";
    });
  }

  function updateConnectionStatus() {
    updateOnlineOnlyControls();
    if (!window.isSecureContext) {
      showStatus("Za namestitev in delo brez povezave odpri portal prek HTTPS.", "warning");
      return;
    }
    pendingOperations().then(function (operations) {
      if (!navigator.onLine) {
        showStatus("Brez povezave · spremembe se varno shranjujejo v napravi.", "warning");
      } else if (operations.length) {
        showStatus("Čaka na sinhronizacijo: " + operations.length + " sprememb.", "warning");
      } else {
        showStatus("", "");
      }
    }).catch(function () {});
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register("/service-worker.js", { scope: "/" })
      .catch(function () { showStatus("Offline način se ni mogel pripraviti.", "danger"); });
  }

  function setupInstallUi() {
    const buttons = document.querySelectorAll("[data-pwa-install]");
    window.addEventListener("beforeinstallprompt", function (event) {
      event.preventDefault();
      deferredInstallPrompt = event;
      buttons.forEach(function (button) { button.hidden = false; });
    });
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        if (!deferredInstallPrompt) return;
        deferredInstallPrompt.prompt();
        deferredInstallPrompt.userChoice.finally(function () {
          deferredInstallPrompt = null;
          buttons.forEach(function (item) { item.hidden = true; });
        });
      });
    });
    const isAppleMobile = /iphone|ipad|ipod/i.test(navigator.userAgent);
    const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;
    if (isAppleMobile && !standalone && window.isSecureContext) {
      document.querySelectorAll("[data-pwa-ios-hint]").forEach(function (hint) { hint.hidden = false; });
    }
  }

  function captureOnlineSnapshot() {
    const script = document.getElementById("offline-net-snapshot");
    if (!script) return Promise.resolve(null);
    try {
      const snapshot = JSON.parse(script.textContent);
      return saveSnapshot(snapshot).then(function () { return snapshot; });
    } catch (_error) {
      return Promise.resolve(null);
    }
  }

  function captureOfflineBootstrap() {
    const script = document.getElementById("offline-bootstrap");
    if (!script) return Promise.resolve(null);
    try {
      const bootstrap = JSON.parse(script.textContent);
      return setMeta("offline_bootstrap", bootstrap).then(function () { return bootstrap; });
    } catch (_error) {
      return Promise.resolve(null);
    }
  }

  function normaliseCallsign(value) {
    return String(value || "").trim().toUpperCase().replace(/\s+/g, "");
  }

  function applyOfflineOpenSelection(bootstrap) {
    const select = document.getElementById("offline-net-kind");
    const title = document.getElementById("offline-net-title");
    const netDate = document.getElementById("offline-net-date");
    const startedTime = document.getElementById("offline-net-time");
    if (!select || !title || !netDate || !startedTime || !bootstrap) return;
    const regularMatch = String(select.value).match(/^regular:(\d+)$/);
    const regular = regularMatch ? (bootstrap.regular_nets || [])[Number(regularMatch[1])] : null;
    if (regular) {
      title.value = regular.label || regular.title || "Redni sked";
      netDate.value = regular.net_date;
      startedTime.value = regular.started_time;
      title.readOnly = true;
      netDate.readOnly = true;
      startedTime.readOnly = true;
    } else {
      title.readOnly = false;
      netDate.readOnly = false;
      startedTime.readOnly = false;
      title.value = "";
      netDate.value = (bootstrap.extra_defaults || {}).net_date || "";
      startedTime.value = (bootstrap.extra_defaults || {}).started_time || "";
    }
  }

  function localIsoDate() {
    const now = new Date();
    return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");
  }

  function regularAvailableOffline(regular) {
    return Boolean(
      regular
      && !regular.existing_id
      && regular.exception_action !== "canceled"
      && (regular.can_open || (regular.open_date && localIsoDate() >= regular.open_date))
    );
  }

  function prepareOfflineOpenForm(bootstrap) {
    const form = document.querySelector("form[data-offline-create]");
    const select = document.getElementById("offline-net-kind");
    const help = document.querySelector("[data-offline-open-help]");
    if (!form || !select) return;
    select.replaceChildren();
    const extra = document.createElement("option");
    extra.value = "extra";
    extra.textContent = "Drug ali izredni sked";
    select.appendChild(extra);
    if (!bootstrap || !bootstrap.user) {
      form.querySelectorAll("input, select, button").forEach(function (control) { control.disabled = true; });
      text(help, "Za offline odprtje se najprej prijavi in enkrat obišči domačo stran portala, ko je povezava na voljo.");
      return;
    }
    form.querySelectorAll("input, select, button").forEach(function (control) { control.disabled = false; });
    (bootstrap.regular_nets || []).forEach(function (regular, index) {
      if (!regularAvailableOffline(regular)) return;
      const option = document.createElement("option");
      option.value = "regular:" + index;
      option.textContent = (regular.label || "Redni sked") + " · " + formatDate(regular.net_date) + " ob " + regular.started_time;
      select.appendChild(option);
    });
    text(help, "Izberi shranjeni redni termin ali odpri izredni sked. Ob vrnitvi povezave se dnevnik samodejno ustvari na strežniku.");
    applyOfflineOpenSelection(bootstrap);
  }

  function createOfflineNet(form) {
    return getMeta("offline_bootstrap").then(function (bootstrap) {
      if (!bootstrap || !bootstrap.user) {
        showStatus("Za offline odprtje najprej obišči portal s povezavo.", "warning");
        return false;
      }
      const data = new FormData(form);
      const kind = String(data.get("net_kind") || "extra");
      const regularMatch = kind.match(/^regular:(\d+)$/);
      const regular = regularMatch ? (bootstrap.regular_nets || [])[Number(regularMatch[1])] : null;
      let createData;
      let displayTitle;
      let repeater = null;
      let controlCallsign = null;
      if (regular) {
        if (!regularAvailableOffline(regular)) {
          showStatus("Ta redni termin ni na voljo za offline odprtje.", "warning");
          return false;
        }
        createData = {
          schedule_type: regular.schedule_type,
          scheduled_date: regular.scheduled_date,
          net_date: regular.net_date,
          started_time: regular.started_time
        };
        displayTitle = regular.label || regular.title;
        repeater = regular.repeater || null;
        controlCallsign = regular.control_callsign || null;
      } else {
        const netDate = String(data.get("net_date") || "");
        const startedTime = String(data.get("started_time") || "");
        const title = String(data.get("title") || "").trim().slice(0, 120);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(netDate) || !/^\d{2}:\d{2}$/.test(startedTime)) {
          showStatus("Vnesi veljaven datum in začetno uro.", "danger");
          return false;
        }
        createData = { title: title, net_date: netDate, started_time: startedTime };
        displayTitle = title || "Sked " + formatDate(netDate);
      }
      const id = operationId();
      const localNetId = "local:" + id;
      const snapshot = {
        schema_version: 1,
        saved_at: new Date().toISOString(),
        net: {
          id: localNetId,
          title: displayTitle,
          net_date: createData.net_date,
          started_at: createData.net_date + " " + createData.started_time + ":00",
          status: "open",
          leader_name: bootstrap.user.full_name,
          leader_callsign: bootstrap.user.callsign,
          repeater: repeater,
          control_callsign: controlCallsign,
          notes: "",
          can_sync: true,
          pending_create: true
        },
        participants: [],
        directory: (bootstrap.directory || []).map(function (entry) {
          return { callsign: entry.callsign, full_name: entry.full_name };
        })
      };
      return queueOperation(localNetId, "create_net", createData, id)
        .then(function () { return saveSnapshot(snapshot); })
        .then(function () { form.reset(); return true; });
    });
  }

  function queueAddParticipant(form, snapshot) {
    const data = new FormData(form);
    const callsign = normaliseCallsign(data.get("callsign"));
    const fullName = String(data.get("full_name") || "").trim();
    const checkinTime = String(data.get("checkin_time") || "").trim();
    if (!/^[A-Z0-9](?:[A-Z0-9/-]{0,22}[A-Z0-9])?$/.test(callsign) || !fullName || !/^\d{2}:\d{2}$/.test(checkinTime)) {
      showStatus("Preveri ime, klicni znak in uro prijave.", "danger");
      return Promise.resolve(false);
    }
    if (snapshot.participants.some(function (participant) { return normaliseCallsign(participant.callsign) === callsign; })) {
      showStatus("Klicni znak " + callsign + " je že vpisan.", "warning");
      return Promise.resolve(false);
    }
    const id = operationId();
    const operationData = { callsign: callsign, full_name: fullName, checkin_time: checkinTime };
    snapshot.participants.push({
      id: "local:" + id,
      callsign: callsign,
      full_name: fullName,
      checkin_time: checkinTime,
      entered_by_name: "Ta naprava",
      pending: true
    });
    if (!snapshot.directory.some(function (entry) { return normaliseCallsign(entry.callsign) === callsign; })) {
      snapshot.directory.push({ callsign: callsign, full_name: fullName });
    }
    return queueOperation(snapshot.net.id, "add_participant", operationData, id)
      .then(function () { return saveSnapshot(snapshot); })
      .then(function () { form.reset(); return true; });
  }

  function queueDeleteParticipant(form, snapshot) {
    const participantId = String(form.dataset.participantId || "");
    const index = snapshot.participants.findIndex(function (participant) {
      return String(participant.id) === participantId;
    });
    if (index < 0) return Promise.resolve(false);
    snapshot.participants.splice(index, 1);
    if (participantId.startsWith("local:")) {
      return deleteRecord(OPERATIONS, participantId.slice(6))
        .then(function () { return saveSnapshot(snapshot); })
        .then(function () { return true; });
    }
    return queueOperation(snapshot.net.id, "delete_participant", { participant_id: Number(participantId) })
      .then(function () { return saveSnapshot(snapshot); })
      .then(function () { return true; });
  }

  function queueNotes(form, snapshot) {
    const data = new FormData(form);
    const notes = String(data.get("notes") || "").trim().slice(0, 5000);
    const baseNotes = String(snapshot.net.notes || "");
    snapshot.net.notes = notes;
    return queueOperation(snapshot.net.id, "update_notes", { notes: notes, base_notes: baseNotes })
      .then(function () { return saveSnapshot(snapshot); })
      .then(function () { return true; });
  }

  function queueFormOperation(form) {
    const netId = String(form.dataset.netId || "");
    const lookupId = netId.startsWith("local:") ? netId : Number(netId || 0);
    return (lookupId ? getRecord(SNAPSHOTS, lookupId) : currentSnapshot()).then(function (snapshot) {
      if (!snapshot || !snapshot.net.can_sync) {
        showStatus("Ta sked ni pripravljen za spreminjanje brez povezave.", "danger");
        return false;
      }
      form.dataset.netId = snapshot.net.id;
      if (form.dataset.offlineAction === "add-participant") return queueAddParticipant(form, snapshot);
      if (form.dataset.offlineAction === "delete-participant") return queueDeleteParticipant(form, snapshot);
      if (form.dataset.offlineAction === "update-notes") return queueNotes(form, snapshot);
      return false;
    });
  }

  function postOfflineJson(path, body, csrfToken) {
    return window.fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken
      },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (response.status === 401) {
          const authError = new Error("auth");
          authError.code = "auth";
          throw authError;
        }
        if (!response.ok) {
          const requestError = new Error(payload.message || "Sinhronizacija ni uspela.");
          requestError.code = payload.error || "sync";
          throw requestError;
        }
        return payload;
      });
    });
  }

  function applyOfflineSyncPayload(payload, state) {
    const removals = (payload.results || []).map(function (result) {
      if (result.status === "conflict" || result.status === "invalid") {
        state.conflicts += 1;
        state.notices.push(result.message);
      }
      if (result.operation_id) state.synced += 1;
      return result.operation_id ? deleteRecord(OPERATIONS, result.operation_id) : Promise.resolve();
    });
    return Promise.all(removals).then(function () {
      return payload.snapshot ? saveSnapshot(payload.snapshot) : null;
    });
  }

  function syncExistingNet(netId, operations, csrfToken, state) {
    if (!operations.length) return Promise.resolve();
    return postOfflineJson(
      "/api/offline/sync",
      { net_id: Number(netId), operations: operations },
      csrfToken
    ).then(function (payload) { return applyOfflineSyncPayload(payload, state); });
  }

  function syncLocalNet(localNetId, operations, csrfToken, state) {
    const createOperation = operations.find(function (operation) { return operation.action === "create_net"; });
    if (!createOperation) {
      const missing = new Error("Manjka podatek za ustvarjanje offline skeda.");
      missing.code = "missing_create";
      return Promise.reject(missing);
    }
    return postOfflineJson(
      "/api/offline/nets",
      { operation_id: createOperation.operation_id, data: createOperation.data },
      csrfToken
    ).then(function (payload) {
      const realNetId = Number(payload.result && payload.result.net_id);
      if (!realNetId || !payload.snapshot) {
        const invalid = new Error("Strežnik ni vrnil ustvarjenega skeda.");
        invalid.code = "invalid_create_response";
        throw invalid;
      }
      const remaining = operations.filter(function (operation) {
        return operation.operation_id !== createOperation.operation_id;
      });
      state.synced += 1;
      return getRecord(SNAPSHOTS, localNetId).then(function (localSnapshot) {
        const mappedSnapshot = payload.snapshot;
        if (localSnapshot) {
          mappedSnapshot.net.notes = localSnapshot.net.notes || "";
          const combinedParticipants = (mappedSnapshot.participants || []).slice();
          (localSnapshot.participants || []).forEach(function (participant) {
            if (!combinedParticipants.some(function (existing) {
              return normaliseCallsign(existing.callsign) === normaliseCallsign(participant.callsign);
            })) combinedParticipants.push(participant);
          });
          mappedSnapshot.participants = combinedParticipants;
          mappedSnapshot.directory = localSnapshot.directory || mappedSnapshot.directory;
        }
        mappedSnapshot.net.pending_create = false;
        return deleteRecord(OPERATIONS, createOperation.operation_id)
          .then(function () {
            return Promise.all(remaining.map(function (operation) {
              operation.net_id = realNetId;
              return putRecord(OPERATIONS, operation);
            }));
          })
          .then(function () { return deleteRecord(SNAPSHOTS, localNetId); })
          .then(function () { return saveSnapshot(mappedSnapshot); })
          .then(function () { return syncExistingNet(realNetId, remaining, csrfToken, state); });
      });
    });
  }

  function syncAll() {
    if (!navigator.onLine) return Promise.resolve({ synced: 0, conflicts: 0 });
    return Promise.all([pendingOperations(), getMeta("csrf_token")]).then(function (values) {
      const operations = values[0];
      const csrfToken = values[1];
      if (!operations.length) return { synced: 0, conflicts: 0 };
      if (!csrfToken) {
        showStatus("Za sinhronizacijo se ponovno prijavi.", "warning");
        return { synced: 0, conflicts: 0 };
      }
      const groups = {};
      operations.forEach(function (operation) {
        const key = String(operation.net_id);
        if (!groups[key]) groups[key] = [];
        groups[key].push(operation);
      });
      const state = { synced: 0, conflicts: 0, notices: [] };
      let chain = Promise.resolve();
      Object.keys(groups).forEach(function (netId) {
        chain = chain.then(function () {
          if (netId.startsWith("local:")) {
            return syncLocalNet(netId, groups[netId], csrfToken, state);
          }
          const regularOperations = groups[netId].filter(function (operation) {
            return operation.action !== "create_net";
          });
          return syncExistingNet(netId, regularOperations, csrfToken, state);
        });
      });
      return chain.then(function () {
        const message = state.conflicts
          ? "Sinhronizacija končana z opozorili: " + state.notices.join(" ")
          : "Sinhronizacija je končana.";
        return setMeta("last_sync_notice", message).then(function () {
          showStatus(message, state.conflicts ? "warning" : "success");
          return { synced: state.synced, conflicts: state.conflicts };
        });
      }).catch(function (error) {
        if (error.code === "auth") {
          showStatus("Za sinhronizacijo se ponovno prijavi v portal.", "warning");
        } else if (error.message) {
          showStatus(error.message + " Offline podatki so ostali v napravi.", "warning");
        } else {
          showStatus("Sinhronizacija trenutno ni uspela; spremembe ostajajo v napravi.", "warning");
        }
        return { synced: 0, conflicts: state.conflicts + 1 };
      });
    });
  }

  function text(element, value) {
    if (element) element.textContent = value == null ? "" : String(value);
  }

  function formatDate(value) {
    const parts = String(value || "").split("-");
    return parts.length === 3 ? parts[2] + ". " + parts[1] + ". " + parts[0] : value;
  }

  function createCell(row, value, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value;
    row.appendChild(cell);
    return cell;
  }

  function renderOfflinePage() {
    if (!document.body.hasAttribute("data-offline-page")) return Promise.resolve();
    return Promise.all([currentSnapshot(), pendingOperations(), getMeta("offline_bootstrap")]).then(function (values) {
      const snapshot = values[0];
      const operations = values[1];
      const bootstrap = values[2];
      const empty = document.querySelector("[data-offline-empty]");
      const content = document.querySelector("[data-offline-content]");
      if (!snapshot) {
        empty.hidden = false;
        content.hidden = true;
        prepareOfflineOpenForm(bootstrap);
        return;
      }
      empty.hidden = true;
      content.hidden = false;
      const net = snapshot.net;
      text(document.querySelector("[data-offline-net-title]"), net.title);
      const details = formatDate(net.net_date) + " · začetek " + String(net.started_at).slice(11, 16)
        + " · operater " + net.leader_name + " (" + net.leader_callsign + ")";
      text(document.querySelector("[data-offline-net-details]"), details);
      const status = document.querySelector("[data-offline-net-status]");
      text(status, net.pending_create ? "Čaka na prenos" : (net.status === "open" ? "Odprt" : "Zaključen"));
      status.className = "badge " + (net.pending_create ? "postponed" : (net.status === "open" ? "open" : "closed"));
      const netOperations = operations.filter(function (operation) { return String(operation.net_id) === String(net.id); });
      text(document.querySelector("[data-offline-pending-count]"), netOperations.length ? netOperations.length + " neposlanih sprememb" : "Vse je sinhronizirano");

      document.querySelectorAll("[data-offline-action]").forEach(function (form) {
        form.dataset.netId = net.id;
        form.querySelectorAll("input, textarea, button").forEach(function (control) {
          control.disabled = !net.can_sync;
        });
      });
      const notes = document.querySelector('[data-offline-action="update-notes"] textarea');
      if (notes) notes.value = net.notes || "";
      const timeInput = document.getElementById("offline-checkin-time");
      if (timeInput && !timeInput.value) {
        const now = new Date();
        timeInput.value = String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0");
      }
      const dataList = document.getElementById("offline-callsign-options");
      dataList.replaceChildren();
      (snapshot.directory || []).forEach(function (entry) {
        const option = document.createElement("option");
        option.value = entry.callsign;
        option.dataset.fullName = entry.full_name;
        dataList.appendChild(option);
      });

      const body = document.querySelector("[data-offline-participants]");
      body.replaceChildren();
      (snapshot.participants || []).forEach(function (participant, index) {
        const row = document.createElement("tr");
        if (participant.pending) row.className = "offline-pending";
        createCell(row, index + 1);
        createCell(row, participant.checkin_time, "nowrap");
        const callsignCell = createCell(row, participant.callsign);
        callsignCell.classList.add("offline-callsign");
        createCell(row, participant.full_name);
        const actionCell = createCell(row, "");
        if (net.can_sync) {
          const form = document.createElement("form");
          form.dataset.offlineAction = "delete-participant";
          form.dataset.netId = net.id;
          form.dataset.participantId = participant.id;
          form.dataset.confirm = "Izbrišem " + participant.callsign + "?";
          const button = document.createElement("button");
          button.type = "submit";
          button.className = "btn btn-danger btn-small";
          button.textContent = "Izbriši";
          form.appendChild(button);
          actionCell.appendChild(form);
        }
        body.appendChild(row);
      });
      text(document.querySelector("[data-offline-participant-count]"), snapshot.participants.length);
      document.querySelector("[data-offline-no-participants]").hidden = snapshot.participants.length > 0;
      const openOnline = document.querySelector("[data-open-online]");
      openOnline.hidden = String(net.id).startsWith("local:");
      openOnline.href = openOnline.hidden ? "/" : "/nets/" + net.id;
    });
  }

  function setupOfflineAutofill() {
    const callsign = document.getElementById("offline-callsign");
    const fullName = document.getElementById("offline-full-name");
    const root = document.getElementById("offline-callsign-options");
    if (!callsign || !fullName || !root) return;
    callsign.addEventListener("input", function () {
      callsign.value = normaliseCallsign(callsign.value);
      const option = Array.from(root.options).find(function (item) {
        return normaliseCallsign(item.value) === callsign.value;
      });
      if (option && !fullName.value) fullName.value = option.dataset.fullName || "";
    });
  }

  function setupOfflineOpenForm() {
    const form = document.querySelector("form[data-offline-create]");
    const select = document.getElementById("offline-net-kind");
    if (!form || !select) return;
    select.addEventListener("change", function () {
      getMeta("offline_bootstrap").then(applyOfflineOpenSelection);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      currentSnapshot().then(function (snapshot) {
        if (snapshot) {
          showStatus("Na tej napravi je že pripravljen odprt sked.", "warning");
          return false;
        }
        return createOfflineNet(form);
      }).then(function (created) {
        if (!created) return null;
        showStatus("Sked je odprt v napravi. Lahko začneš z vpisovanjem.", "success");
        return renderOfflinePage().then(function () { return syncAll(); }).then(renderOfflinePage);
      }).catch(function () {
        showStatus("Offline skeda ni bilo mogoče odpreti v tej napravi.", "danger");
      });
    });
  }

  function setupFormInterception() {
    document.addEventListener("submit", function (event) {
      const form = event.target.closest("form[data-offline-action]");
      if (!form || event.defaultPrevented) return;
      const offlineDocument = document.body.hasAttribute("data-offline-page");
      if (navigator.onLine && !offlineDocument) return;
      event.preventDefault();
      if (offlineDocument && form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      queueFormOperation(form).then(function (queued) {
        if (!queued) return;
        showStatus("Sprememba je shranjena v napravi.", "success");
        if (offlineDocument) {
          renderOfflinePage().then(function () { return syncAll(); }).then(renderOfflinePage);
        } else {
          window.location.assign("/static/offline.html");
        }
      }).catch(function () {
        showStatus("Spremembe ni bilo mogoče shraniti v napravo.", "danger");
      });
    });

    document.querySelectorAll("form[data-clear-offline-data]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.dataset.offlineCleared === "1") return;
        event.preventDefault();
        clearOfflineData().finally(function () {
          form.dataset.offlineCleared = "1";
          form.submit();
        });
      });
    });
    document.addEventListener("click", function (event) {
      const link = event.target.closest("a[data-online-only].is-disabled");
      if (link) event.preventDefault();
    });
  }

  function setupOfflineButtons() {
    const sync = document.querySelector("[data-sync-now]");
    if (sync) {
      sync.addEventListener("click", function () {
        sync.disabled = true;
        syncAll().then(function (result) {
          return renderOfflinePage().then(function () {
            if (navigator.onLine && result.synced) {
              return currentSnapshot().then(function (snapshot) {
                if (snapshot) window.location.assign("/nets/" + snapshot.net.id);
              });
            }
          });
        }).finally(function () { sync.disabled = false; });
      });
    }
    const clear = document.querySelector("[data-clear-offline-now]");
    if (clear) {
      clear.addEventListener("click", function () {
        if (!window.confirm("Odstranim shranjeni sked in vse neposlane spremembe iz te naprave?")) return;
        clearOfflineData().then(function () {
          showStatus("Lokalni podatki so odstranjeni.", "success");
          return renderOfflinePage();
        });
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    registerServiceWorker();
    setupInstallUi();
    setupFormInterception();
    setupOfflineButtons();
    setupOfflineAutofill();
    setupOfflineOpenForm();
    rememberCurrentUser()
      .then(rememberCurrentCsrf)
      .then(captureOfflineBootstrap)
      .then(captureOnlineSnapshot)
      .then(function () { return renderOfflinePage(); })
      .then(function () { return getMeta("last_sync_notice"); })
      .then(function (notice) {
        if (!notice) return null;
        showStatus(notice, notice.includes("opozorili") ? "warning" : "success");
        return deleteRecord(META, "last_sync_notice");
      })
      .then(function () { return syncAll(); })
      .then(function (result) {
        updateConnectionStatus();
        if (result.synced && !document.body.hasAttribute("data-offline-page")) {
          window.location.reload();
        } else if (result.synced && document.body.hasAttribute("data-offline-page")) {
          renderOfflinePage().then(function () {
            currentSnapshot().then(function (snapshot) {
              if (snapshot) window.location.assign("/nets/" + snapshot.net.id);
            });
          });
        }
      })
      .catch(updateConnectionStatus);
  });

  window.addEventListener("online", function () {
    updateConnectionStatus();
    syncAll().then(function (result) {
      if (document.body.hasAttribute("data-offline-page")) {
        renderOfflinePage().then(function () {
          if (result.synced) {
            currentSnapshot().then(function (snapshot) {
              if (snapshot) window.location.assign("/nets/" + snapshot.net.id);
            });
          }
        });
      }
    });
  });
  window.addEventListener("offline", updateConnectionStatus);
}());
