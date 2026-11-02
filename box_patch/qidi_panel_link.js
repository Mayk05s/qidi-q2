// Adds a QIDI Box button to the bottom of Fluidd's side menu by copying the Settings item.
// The panel opens inside Fluidd, in the content area next to the menu.
(function () {
  var ID = "qidi-panel-link";
  var ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6M12 15v6"/></svg>';

  function panelUrl() {
    return location.protocol + "//" + location.hostname + ":18770/";
  }

  var FRAME_ID = "qidi-panel-frame";
  var ACTIVE = "v-list-item--active";

  function placeFrame(frame) {
    var main = document.querySelector(".v-main");
    var area = main ? main.getBoundingClientRect() : { left: 56, top: 56 };
    var style = getComputedStyle(main || document.body);
    var left = area.left + (parseFloat(style.paddingLeft) || 0);
    var top = area.top + (parseFloat(style.paddingTop) || 0);
    frame.style.cssText = "position:fixed;border:0;z-index:6;background:#161615;left:" + left + "px;top:" + top +
      "px;width:calc(100vw - " + left + "px);height:calc(100vh - " + top + "px)";
  }

  function closePanel() {
    var frame = document.getElementById(FRAME_ID);
    if (frame) frame.style.display = "none";
    var link = document.getElementById(ID);
    if (link) (link.tagName === "A" ? link : link.querySelector("a")).classList.remove(ACTIVE);
  }

  function openPanel(link) {
    var frame = document.getElementById(FRAME_ID);
    if (!frame) {
      frame = document.createElement("iframe");
      frame.id = FRAME_ID;
      frame.title = "QIDI Box";
      frame.src = panelUrl();
      document.body.appendChild(frame);
      window.addEventListener("resize", function () {
        if (frame.style.display !== "none") placeFrame(frame);
      });
    }
    placeFrame(frame);
    link.classList.add(ACTIVE);
  }

  // Any other menu item returns to Fluidd's own page.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a");
    if (link && link.closest(".nav-items") && !link.closest("#" + ID)) closePanel();
  }, true);
  window.addEventListener("hashchange", closePanel);

  function addLink() {
    if (document.getElementById(ID)) return;
    var settings = document.querySelector('a[href$="/settings"]');
    if (!settings) return;
    // Copy the wrapper as well when the link is its only content, to keep the spacing.
    var source = settings.parentElement && settings.parentElement.children.length === 1 ? settings.parentElement : settings;
    var copy = source.cloneNode(true);
    var link = copy.tagName === "A" ? copy : copy.querySelector("a");
    copy.id = ID;
    link.href = panelUrl();
    link.title = "QIDI Box";
    link.addEventListener("click", function (event) {
      event.preventDefault();
      openPanel(link);
    });
    link.setAttribute("aria-label", "QIDI Box: слоты и материалы");
    link.classList.remove("v-list-item--active", "router-link-active", "router-link-exact-active");
    link.removeAttribute("aria-current");
    var icon = link.querySelector(".v-icon") || link.querySelector("svg");
    if (icon) icon.innerHTML = ICON;
    source.parentElement.insertBefore(copy, source.nextSibling);
  }

  // Fluidd's panel looks slots up in a dictionary of old numeric codes, while the firmware
  // now stores profile ids and color codes. Adding them in the browser fixes the display;
  // nothing is written to the printer.
  function missingEntries(dict, variables, materials) {
    var added = 0;
    var colors = (dict.colordict = Object.assign({}, dict.colordict));
    (variables.colorSlot || []).concat([(variables.rackExtra || [])[0]]).forEach(function (value) {
      var hex = String(value == null ? "" : value).toUpperCase();
      if (/^[0-9A-F]{6}$/.test(hex) && !colors[hex]) {
        colors[hex] = "#" + hex;
        added++;
      }
    });
    (variables.filamentSlot || []).concat([(variables.rackExtra || [])[1]]).forEach(function (value) {
      var key = "fila" + value;
      var material = materials[value];
      if (material && !dict[key]) {
        dict[key] = { filament: material.name, type: material.type || material.name.split(" ").pop() };
        added++;
      }
    });
    return added;
  }

  function rawColor(value) {
    return Array.isArray(value) ? String(value[0]) : value;
  }

  // Values as saved on the printer; Fluidd's own getter turns them into numbers and loses them.
  function realVariables(variables, raw) {
    var out = Object.assign({}, variables);
    out.colorSlot = (variables.colorSlot || []).slice();
    out.filamentSlot = (variables.filamentSlot || []).slice();
    for (var i = 0; i < 16; i++) {
      if (raw["color_slot" + i] !== undefined) out.colorSlot[i] = rawColor(raw["color_slot" + i]);
      if (raw["filament_slot" + i] !== undefined) out.filamentSlot[i] = raw["filament_slot" + i];
    }
    if (raw.color_slot16 !== undefined) out.rackExtra = [rawColor(raw.color_slot16), raw.filament_slot16, raw.vendor_slot16];
    return out;
  }

  var getterPatched = false;

  function patchGetter(store) {
    if (getterPatched) return;
    var watchers = store._vm && store._vm._computedWatchers;
    var watcher = watchers && watchers["printer/getVariables"];
    if (!watcher) return;
    var original = watcher.getter;
    watcher.getter = function () {
      var saved = store.state.printer.printer.save_variables;
      return realVariables(original.apply(this, arguments), (saved && saved.variables) || {});
    };
    watcher.dirty = true;
    watcher.deps.forEach(function (dep) { dep.notify(); });
    getterPatched = true;
  }

  var materials = null;

  function updatePanel() {
    var root = document.getElementById("app");
    var store = root && root.__vue__ && root.__vue__.$store;
    var list = store && store.state.printer.officiallFilasList;
    if (!list) return;
    patchGetter(store);
    var variables = store.getters["printer/getVariables"];
    if (!variables) return;
    if (!materials) {
      materials = {};
      fetch(panelUrl() + "api/codes").then(function (res) { return res.json(); }).then(function (codes) {
        codes.slot_materials.forEach(function (m) { materials[m.id] = m; });
      }).catch(function () { materials = null; });
      return;
    }
    var copy = Object.assign({}, list);
    if (missingEntries(copy, variables, materials)) store.commit("printer/setOfficiallFilasList", copy);
  }

  if (typeof module !== "undefined") {
    module.exports = { missingEntries: missingEntries, realVariables: realVariables };
    return;
  }
  setInterval(updatePanel, 2000);

  // Grams under the spools and the plastic card on the dashboard; the panel serves that script.
  var card = document.createElement("script");
  card.src = panelUrl() + "fluidd_card.js";
  document.head.appendChild(card);

  // Fluidd draws its menu after loading and may redraw it later.
  new MutationObserver(addLink).observe(document.documentElement, { childList: true, subtree: true });
  addLink();
})();
