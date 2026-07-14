(function () {
  "use strict";
  var PINS = window.__ALLCPR_PINS__ || [];
  var RADIUS_MI = window.__ALLCPR_RADIUS_MILES__ || 0;
  var mapEl = document.getElementById("allcpr-map");
  if (!mapEl || !PINS.length || typeof L === "undefined") { return; }

  // Tier palette is injected by map_view.py (single source of truth);
  // the literal here is only a fallback if the injection is missing.
  var TIER_COLORS = window.__ALLCPR_TIER_COLORS__ || {
    A: "#15803d", B: "#0f766e", C: "#b45309", D: "#c2410c", F: "#b91c1c"
  };
  var METERS_PER_MILE = 1609.34;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  var map = L.map(mapEl, { scrollWheelZoom: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  var entries = {};
  var coords = [];

  function popupHtml(p) {
    var strat = (p.strategies && p.strategies.length) ? p.strategies[0] : "n/a";
    return '<div class="map-popup"><strong>' + esc(p.name) + "</strong>" +
      "<div>Site score: <b>" + Number(p.site_score).toFixed(1) + "</b></div>" +
      "<div>Tier " + esc(p.tier) + " &middot; " + esc(p.readiness) + "</div>" +
      "<div>Strategy: " + esc(strat) + "</div>" +
      '<a href="#' + esc(p.card_id) + '" data-card="' + esc(p.card_id) +
      '" class="map-popup-link">View full card &darr;</a></div>';
  }

  PINS.forEach(function (p) {
    var color = TIER_COLORS[p.tier] || "#5f6c72";
    var circle = L.circle([p.lat, p.lon], {
      radius: RADIUS_MI * METERS_PER_MILE,
      color: color, weight: 1, opacity: 0.35,
      fillColor: color, fillOpacity: 0.10
    }).addTo(map);
    var marker = L.circleMarker([p.lat, p.lon], {
      radius: 9, color: "#fff", weight: 2,
      fillColor: color, fillOpacity: 0.95
    }).addTo(map);
    marker.bindPopup(popupHtml(p));
    marker.on("mouseover", function () { setHover(p.rank, true); });
    marker.on("mouseout", function () { setHover(p.rank, false); });
    marker.on("click", function () { selectRank(p.rank, false); });
    coords.push([p.lat, p.lon]);
    entries[p.rank] = { pin: p, marker: marker, circle: circle, row: null };
  });

  if (coords.length === 1) { map.setView(coords[0], 12); }
  else { map.fitBounds(coords, { padding: [30, 30] }); }

  var legend = L.control({ position: "bottomleft" });
  legend.onAdd = function () {
    var div = L.DomUtil.create("div", "map-legend");
    var rows = ["A", "B", "C", "D", "F"].map(function (t) {
      return '<i style="background:' + (TIER_COLORS[t] || "#5f6c72") +
        '"></i>Tier ' + t + "<br>";
    }).join("");
    div.innerHTML = rows + "<span>Faint circle = search radius</span>";
    return div;
  };
  legend.addTo(map);

  Array.prototype.forEach.call(
    document.querySelectorAll(".map-row"),
    function (row) {
      var rank = row.getAttribute("data-rank");
      if (entries[rank]) { entries[rank].row = row; }
      row.addEventListener("click", function () { selectRank(rank, true); });
      row.addEventListener("mouseover", function () { setHover(rank, true); });
      row.addEventListener("mouseout", function () { setHover(rank, false); });
    }
  );

  function setHover(rank, on) {
    var e = entries[rank];
    if (!e) { return; }
    if (e.row) { e.row.classList.toggle("hover", on); }
    e.marker.setRadius(on ? 13 : 9);
  }

  var selected = null;
  function selectRank(rank, fly) {
    var e = entries[rank];
    if (!e) { return; }
    if (selected && entries[selected] && entries[selected].row) {
      entries[selected].row.classList.remove("selected");
    }
    selected = rank;
    if (e.row) { e.row.classList.add("selected"); }
    if (fly) { map.flyTo([e.pin.lat, e.pin.lon], Math.max(map.getZoom(), 13)); }
    e.marker.openPopup();
  }

  document.addEventListener("click", function (ev) {
    var link = ev.target.closest ? ev.target.closest(".map-popup-link") : null;
    if (!link) { return; }
    ev.preventDefault();
    var card = document.getElementById(link.getAttribute("data-card"));
    if (!card) { return; }
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.classList.remove("allcpr-card-flash");
    void card.offsetWidth;
    card.classList.add("allcpr-card-flash");
  });

  var countEl = document.getElementById("allcpr-map-count");

  function checkedValues(group) {
    var out = {};
    Array.prototype.forEach.call(
      document.querySelectorAll('input[data-filter="' + group + '"]'),
      function (cb) { if (cb.checked) { out[cb.value] = true; } }
    );
    return out;
  }

  function applyFilters() {
    var tiers = checkedValues("tier");
    var readies = checkedValues("readiness");
    var strats = checkedValues("strategy");
    var shown = 0;
    PINS.forEach(function (p) {
      var sl = p.strategies || [];
      // A pin with no strategies is not excluded by the strategy filter.
      var stratOk = sl.length === 0 || sl.some(function (k) { return strats[k]; });
      var visible = !!tiers[p.tier] && !!readies[p.readiness] && stratOk;
      var e = entries[p.rank];
      if (visible) {
        if (!map.hasLayer(e.marker)) { e.marker.addTo(map); }
        if (!map.hasLayer(e.circle)) { e.circle.addTo(map); }
        shown++;
      } else {
        if (map.hasLayer(e.marker)) { map.removeLayer(e.marker); }
        if (map.hasLayer(e.circle)) { map.removeLayer(e.circle); }
      }
      if (e.row) { e.row.style.display = visible ? "" : "none"; }
      var card = document.getElementById(p.card_id);
      if (card) { card.style.display = visible ? "" : "none"; }
    });
    if (countEl) { countEl.textContent = String(shown); }
    var empty = document.getElementById("allcpr-map-empty");
    if (empty) { empty.style.display = shown ? "none" : "block"; }
  }

  Array.prototype.forEach.call(
    document.querySelectorAll("input[data-filter]"),
    function (cb) { cb.addEventListener("change", applyFilters); }
  );

  var resetBtn = document.getElementById("allcpr-map-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      Array.prototype.forEach.call(
        document.querySelectorAll("input[data-filter]"),
        function (cb) { cb.checked = true; }
      );
      applyFilters();
    });
  }

  var toggle = document.getElementById("allcpr-map-toggle");
  var toolbar = document.getElementById("allcpr-map-toolbar");
  if (toggle && toolbar) {
    toggle.addEventListener("click", function () {
      toolbar.classList.toggle("collapsed");
    });
  }

  applyFilters();
})();