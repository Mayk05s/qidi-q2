// Adds a QIDI Box button to the bottom of Fluidd's side menu by copying the Settings item.
// The panel opens inside Fluidd, in the content area next to the menu.
(function () {
  var ID = "qidi-panel-link";
  var ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6M12 15v6"/></svg>';

  function panelUrl() {
    return location.protocol + "//" + location.hostname + ":18770/";
  }

  var ACTIVE = "v-list-item--active";

  function placeFrame(frame) {
    var main = document.querySelector(".v-main");
    var area = main ? main.getBoundingClientRect() : { left: 56, top: 56 };
    var style = getComputedStyle(main || document.body);
    var left = Math.max(0, area.left + (parseFloat(style.paddingLeft) || 0));
    // The content area moves up when Fluidd's own page was scrolled, as in the QIDI Studio
    // printer page: the frame then went above the window and its top could not be reached.
    // It starts below Fluidd's top bar instead, or at the top of the window.
    var bar = document.querySelector("header.v-app-bar, .v-app-bar");
    var top = bar ? Math.max(0, bar.getBoundingClientRect().bottom)
      : Math.max(0, area.top + (parseFloat(style.paddingTop) || 0));
    frame.style.cssText = "position:fixed;border:0;z-index:6;background:#161615;left:" + left + "px;top:" + top +
      "px;width:calc(100vw - " + left + "px);height:calc(100vh - " + top + "px)";
  }

  // The guide gets its own menu button when the "guide" module is installed.
  var GUIDE_ID = "qidi-guide-link";
  var GUIDE_ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6"/><path d="M12 17.5v.5"/></svg>';
  var guideEnabled = false;

  function linkOf(item) {
    return item.tagName === "A" ? item : item.querySelector("a");
  }

  function closePanel() {
    [ID, GUIDE_ID].forEach(function (id) {
      var frame = document.getElementById(id + "-frame");
      if (frame) frame.style.display = "none";
      var item = document.getElementById(id);
      if (item) linkOf(item).classList.remove(ACTIVE);
    });
  }

  // Each button keeps its own frame, so switching between them reloads nothing.
  function openPanel(link, url, id) {
    closePanel();
    var frame = document.getElementById(id + "-frame");
    if (!frame) {
      frame = document.createElement("iframe");
      frame.id = id + "-frame";
      frame.title = link.title;
      frame.src = url;
      document.body.appendChild(frame);
      ["resize", "scroll"].forEach(function (name) {
        window.addEventListener(name, function () {
          if (frame.style.display !== "none") placeFrame(frame);
        }, true);
      });
    }
    placeFrame(frame);
    link.classList.add(ACTIVE);
  }

  // Any other menu item returns to Fluidd's own page.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a");
    if (link && link.closest(".nav-items") && !link.closest("#" + ID) && !link.closest("#" + GUIDE_ID)) closePanel();
  }, true);
  window.addEventListener("hashchange", closePanel);

  function menuItem(source, id, url, title, label, icon) {
    var copy = source.cloneNode(true);
    var link = linkOf(copy);
    copy.id = id;
    link.href = url;
    link.title = title;
    link.addEventListener("click", function (event) {
      event.preventDefault();
      openPanel(link, url, id);
    });
    link.setAttribute("aria-label", label);
    link.classList.remove("v-list-item--active", "router-link-active", "router-link-exact-active");
    link.removeAttribute("aria-current");
    var svg = link.querySelector(".v-icon") || link.querySelector("svg");
    if (svg) svg.innerHTML = icon;
    return copy;
  }

  function addLink() {
    var settings = document.querySelector('a[href$="/settings"]');
    if (!settings) return;
    // Copy the wrapper as well when the link is its only content, to keep the spacing.
    var source = settings.parentElement && settings.parentElement.children.length === 1 ? settings.parentElement : settings;
    var panel = document.getElementById(ID);
    if (!panel) {
      panel = menuItem(source, ID, panelUrl(), "QIDI Box", "QIDI Box: слоты и материалы", ICON);
      source.parentElement.insertBefore(panel, source.nextSibling);
    }
    if (guideEnabled && !document.getElementById(GUIDE_ID)) {
      var guide = menuItem(source, GUIDE_ID, panelUrl() + "guide", "Справка", "Справка: сушка и пластик", GUIDE_ICON);
      panel.parentElement.insertBefore(guide, panel.nextSibling);
    }
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

  fetch(panelUrl() + "api/codes").then(function (res) { return res.json(); }).then(function (codes) {
    guideEnabled = (codes.features || []).indexOf("guide") >= 0;
    addLink();
  }).catch(function () {});

  // Fluidd draws its menu after loading and may redraw it later.
  new MutationObserver(addLink).observe(document.documentElement, { childList: true, subtree: true });
  addLink();
})();
