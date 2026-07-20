(function () {
  "use strict";

  function setupNavigation() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const links = document.querySelector("[data-nav-links]");
    if (!toggle || !links) return;
    toggle.addEventListener("click", function () {
      const open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
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
        output.textContent = (days ? days + " dni · " : "") +
          pad(hours) + ":" + pad(minutes) + ":" + pad(seconds);
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

  function setupDirectoryAutofill() {
    const callsign = document.getElementById("participant-callsign");
    const fullName = document.getElementById("participant-full-name");
    if (!callsign || !fullName) return;
    const directory = {};
    document.querySelectorAll("#callsign-options option").forEach(function (option) {
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
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupNavigation();
    setupCountdowns();
    setupEarlyOpen();
    setupDirectoryAutofill();
  });
}());
