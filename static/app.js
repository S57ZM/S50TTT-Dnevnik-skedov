(function () {
  "use strict";

  function setupNavigation() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const links = document.querySelector("[data-nav-links]");
    if (!toggle || !links) return;
    const icon = toggle.querySelector("[data-nav-icon]");
    const label = toggle.querySelector("[data-nav-label]");

    function setOpen(open) {
      links.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Zapri glavni meni" : "Odpri glavni meni");
      if (icon) icon.textContent = open ? "×" : "☰";
      if (label) label.textContent = open ? "Zapri" : "Meni";
    }

    toggle.addEventListener("click", function () { setOpen(!links.classList.contains("open")); });
    links.addEventListener("click", function (event) {
      if (event.target.closest("a, button")) setOpen(false);
    });
    document.addEventListener("click", function (event) {
      if (links.classList.contains("open") && !links.contains(event.target) && !toggle.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && links.classList.contains("open")) {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  function setupCountdowns() {
    document.querySelectorAll("[data-countdown]").forEach(function (card) {
      const output = card.querySelector("[data-countdown-value]");
      const target = Date.parse(card.dataset.countdown);
      if (!output || Number.isNaN(target)) return;
      function pad(value) { return String(value).padStart(2, "0"); }
      function update() {
        const remaining = target - Date.now();
        if (remaining <= 0) {
          output.textContent = "Sked se je začel";
          return;
        }
        const total = Math.floor(remaining / 1000);
        const days = Math.floor(total / 86400);
        const hours = Math.floor((total % 86400) / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const seconds = total % 60;
        output.textContent = (days ? days + " dni · " : "") + pad(hours) + ":" + pad(minutes) + ":" + pad(seconds);
      }
      update();
      window.setInterval(update, 1000);
    });
  }

  function setupEarlyOpen() {
    document.querySelectorAll("[data-early-open]").forEach(function (button) {
      let presses = 0;
      let resetTimer;
      const original = button.textContent;
      button.addEventListener("click", function () {
        presses += 1;
        window.clearTimeout(resetTimer);
        if (presses >= 5) {
          button.form.querySelector("[data-early-unlock]").value = "1";
          window.alert("Ti si pravi Heker 😄");
          button.textContent = "Odpiram …";
          button.form.requestSubmit();
          return;
        }
        button.textContent = "Še " + (5 - presses) + "× pritisni";
        resetTimer = window.setTimeout(function () {
          presses = 0;
          button.textContent = original;
        }, 4000);
      });
    });
  }

  function setupConfirmations() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.dataset.confirm)) event.preventDefault();
      });
    });
  }

  function setupWindowActions() {
    document.querySelectorAll("[data-window-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (button.dataset.windowAction === "print") window.print();
        if (button.dataset.windowAction === "close") window.close();
      });
    });
  }

  function setupDirectoryAutofill() {
    [
      ["participant-callsign", "participant-full-name", "callsign-options"],
      ["edit-participant-callsign", "edit-participant-full-name", "edit-callsign-options"]
    ].forEach(function (ids) {
      const callsign = document.getElementById(ids[0]);
      const fullName = document.getElementById(ids[1]);
      const optionsRoot = document.getElementById(ids[2]);
      if (!callsign || !fullName || !optionsRoot) return;
      const directory = {};
      optionsRoot.querySelectorAll("option").forEach(function (option) {
        directory[option.value.toUpperCase()] = option.dataset.fullName;
      });
      let lastAutofill = "";
      function suggest() {
        const value = callsign.value.trim().toUpperCase().replace(/\s+/g, "");
        callsign.value = value;
        const knownName = directory[value];
        if (knownName && (!fullName.value || fullName.value === lastAutofill)) {
          fullName.value = knownName;
          lastAutofill = knownName;
        }
      }
      callsign.addEventListener("input", suggest);
      callsign.addEventListener("change", suggest);
    });
  }

  function setupScheduleException() {
    const action = document.getElementById("exception-action");
    const fields = document.getElementById("postponed-fields");
    if (!action || !fields) return;
    function update() {
      const postponed = action.value === "postponed";
      fields.hidden = !postponed;
      fields.querySelectorAll("input").forEach(function (input) { input.required = postponed; });
    }
    action.addEventListener("change", update);
    update();
  }

  function formatBytes(value) {
    if (value === null || value === undefined) return "Ni na voljo";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = Number(value);
    let index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index === 0 ? size.toFixed(0) : size.toFixed(1)) + " " + units[index];
  }

  function formatDuration(value) {
    if (value === null || value === undefined) return "Ni na voljo";
    const seconds = Math.max(0, Number(value));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return (days ? days + " d " : "") + ((hours || days) ? hours + " h " : "") + minutes + " min";
  }

  function setStatus(root, name, status) {
    const dot = root.querySelector('[data-status-dot="' + name + '"]');
    if (!dot) return;
    ["status-ok", "status-warning", "status-danger", "status-unavailable"].forEach(function (className) { dot.classList.remove(className); });
    dot.classList.add("status-" + status);
  }

  function setupSystemMetrics() {
    const root = document.querySelector("[data-system-metrics]");
    if (!root) return;
    const endpoint = root.dataset.endpoint;
    function text(name, value) {
      const element = root.parentElement.querySelector('[data-metric="' + name + '"]');
      if (element) element.textContent = value;
    }
    function progress(name, value) {
      const element = root.querySelector('[data-metric-progress="' + name + '"]');
      if (element) element.value = Math.min(100, Math.max(0, Number(value) || 0));
    }
    function render(metrics) {
      text("checked_at", "Osveženo " + metrics.checked_at.replace(" ", " ob ").slice(0, 16));
      text("temperature", metrics.temperature_c === null ? "Ni na voljo" : Number(metrics.temperature_c).toFixed(1) + " °C");
      text("disk-free", metrics.disk_free_bytes === null ? "Ni na voljo" : formatBytes(metrics.disk_free_bytes) + " prosto");
      text("disk-detail", metrics.disk_used_percent === null ? "Podatek ni na voljo" : metrics.disk_used_percent + " % zasedeno od " + formatBytes(metrics.disk_total_bytes));
      text("memory", metrics.memory_used_bytes === null ? "Ni na voljo" : formatBytes(metrics.memory_used_bytes) + " / " + formatBytes(metrics.memory_total_bytes));
      text("memory-detail", metrics.memory_used_percent === null ? "Podatek ni na voljo" : metrics.memory_used_percent + " % uporabljeno");
      text("load", metrics.load_1m === null ? "Ni na voljo" : String(metrics.load_1m));
      text("load-detail", "1-minutno povprečje · " + metrics.cpu_count + " jeder");
      text("uptime", formatDuration(metrics.uptime_seconds));
      progress("disk", metrics.disk_used_percent);
      progress("memory", metrics.memory_used_percent);
      progress("load", metrics.load_percent);
      setStatus(root, "temperature", metrics.temperature_status);
      setStatus(root, "disk", metrics.disk_status);
      setStatus(root, "memory", metrics.memory_status);
      setStatus(root, "load", metrics.load_status);
    }
    function refresh() {
      window.fetch(endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) { if (!response.ok) throw new Error("metrics"); return response.json(); })
        .then(render)
        .catch(function () { text("checked_at", "Osveževanje ni uspelo"); });
    }
    refresh();
    window.setInterval(refresh, 15000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupNavigation();
    setupCountdowns();
    setupEarlyOpen();
    setupConfirmations();
    setupWindowActions();
    setupDirectoryAutofill();
    setupScheduleException();
    setupSystemMetrics();
  });
}());
