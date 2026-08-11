"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function field(initialValue = "") {
  const listeners = {};
  return {
    value: initialValue,
    addEventListener(name, callback) { listeners[name] = callback; },
    dispatch(name) { listeners[name]({ target: this }); }
  };
}

const callsign = field();
const fullName = field();
const options = {
  querySelectorAll() {
    return [{ value: "S51Z", dataset: { fullName: "Zoki" } }];
  }
};
let ready;
const document = {
  addEventListener(name, callback) {
    if (name === "DOMContentLoaded") ready = callback;
  },
  getElementById(id) {
    return {
      "participant-callsign": callsign,
      "participant-full-name": fullName,
      "callsign-options": options
    }[id] || null;
  },
  querySelector() { return null; },
  querySelectorAll() { return []; }
};

const source = fs.readFileSync("static/app.js", "utf8");
vm.runInNewContext(source, {
  document,
  window: { setInterval() {}, setTimeout() {}, fetch() {} },
  console
});
ready();

callsign.value = "S5";
callsign.dispatch("input");
assert.equal(fullName.value, "", "delni znak ne sme samodejno izpolniti imena");

callsign.value = "S51Z";
callsign.dispatch("input");
assert.equal(fullName.value, "Zoki", "natančen zadetek mora izpolniti ime");

callsign.value = "S51ZM";
callsign.dispatch("input");
assert.equal(fullName.value, "", "neznani znak mora počistiti prej samodejno ime");

callsign.value = "S51Z";
callsign.dispatch("input");
fullName.value = "Ročno ime";
fullName.dispatch("input");
callsign.value = "S51ZM";
callsign.dispatch("input");
assert.equal(fullName.value, "Ročno ime", "ročno vpisano ime mora ostati");

console.log("callsign autofill: OK");
