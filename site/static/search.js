(async function () {
  const ROOT = window.ROOT || "";
  const $ = (id) => document.getElementById(id);
  const q = $("q"), county = $("county"), samfundSel = $("samfund"), results = $("results"), count = $("result-count"),
        empty = $("empty"), clear = $("clear"), nearBtn = $("near-me"), geoStatus = $("geo-status"),
        mapPanel = $("map-panel"), chips = [...document.querySelectorAll(".chip")],
        viewBtns = [...document.querySelectorAll("[data-view]")];
  const orgs = await fetch(ROOT + "data/organizations.json?v=" + (window.V || "")).then((r) => r.json());
  const fold = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  orgs.forEach((o) => (o._hay = fold([o.name, o.city, o.municipality, o.county, o.denomination, (o.samfund || []).join(" ")].join(" "))));
  const hasCoords = (o) => typeof o.lat === "number" && typeof o.lng === "number";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const selected = new Set();
  const legend = document.getElementById("status-legend");

  // Colour code: does the church have upcoming calendar events and/or a website?
  const STATUS = {
    both: { label: "Kalender och webbplats", color: "#1f5c4f" },
    cal:  { label: "Bara kalender",          color: "#d08a0b" },
    web:  { label: "Bara webbplats",         color: "#2f6db5" },
    none: { label: "Varken kalender eller webbplats", color: "#9aa3a0" },
  };
  const statusOf = (o) => (o.n_events > 0 ? (o.website ? "both" : "cal") : (o.website ? "web" : "none"));
  orgs.forEach((o) => (o._status = statusOf(o)));
  const statusFilter = new Set();
  let view = "lista", me = null, map = null, layer = null, meMarker = null, lastHits = [];
  const PAGE = 48;               // cards rendered at a time: thousands of cards are slow on phones
  let shown = PAGE;
  const moreWrap = $("more-wrap"), moreBtn = $("show-more");

  // Restore state from URL so filtered views can be shared
  const params = new URLSearchParams(location.search);
  q.value = params.get("q") || "";
  county.value = params.get("lan") || "";
  if (samfundSel) samfundSel.value = params.get("samfund") || "";
  (params.get("har") || "").split("|").filter(Boolean).forEach((a) => selected.add(a));
  (params.get("status") || "").split("|").filter((s) => STATUS[s]).forEach((s) => statusFilter.add(s));
  if (params.get("vy") === "karta" && mapPanel) view = "karta";
  chips.forEach((c) => c.setAttribute("aria-pressed", selected.has(c.dataset.activity)));

  // Distance in km (haversine)
  function km(a, b) {
    const R = 6371, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  const fmtKm = (d) => (d < 10 ? d.toFixed(1).replace(".", ",") : Math.round(d)) + " km";

  function place(o) {
    const city = o.city || "", muni = o.municipality && o.municipality !== o.city ? o.municipality : "";
    return [city, muni].filter(Boolean).join(", ") || o.county || "";
  }

  function card(o) {
    const acts = [...o.activities].sort((a, b) => selected.has(b) - selected.has(a));
    const tags = acts.slice(0, 4).map((a) => `<li${selected.has(a) ? ' class="hit"' : ""}>${esc(a)}</li>`).join("");
    const more = o.activities.length > 4 ? `<li class="more">+${o.activities.length - 4}</li>` : "";
    const dist = o._dist != null ? `<span class="distance">${fmtKm(o._dist)}</span>` : "";
    const badges = [
      o.n_events > 0 ? `<span class="badge badge-cal">${ICON_CAL} Kalender · ${o.n_events}</span>` : "",
      o.website ? `<span class="badge badge-web">${ICON_WEB} Webbplats</span>` : "",
      o._status === "none" ? `<span class="badge badge-none">Ingen kalender eller webbplats</span>` : "",
    ].join("");
    return `<li class="card status-${o._status}" style="--st:${STATUS[o._status].color}">
      <a href="${ROOT}forsamling/${esc(o.slug)}/" title="${esc(STATUS[o._status].label)}">
      <span class="card-kicker">${esc((o.samfund || []).join(" · "))}${dist}</span>
      <h3>${esc(o.name)}</h3><p class="card-place">${esc(place(o))}</p>
      <div class="badges">${badges}</div>
      <ul class="tags">${tags}${more}</ul></a></li>`;
  }

  // ---------- Status legend (also a filter) ----------
  const ICON_CAL = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 10h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="2"/></svg>';
  const ICON_WEB = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" fill="none" stroke="currentColor" stroke-width="2"/></svg>';

  function renderLegend(base) {
    if (!legend) return;
    const counts = { both: 0, cal: 0, web: 0, none: 0 };
    base.forEach((o) => counts[o._status]++);
    legend.innerHTML = Object.entries(STATUS).filter(([key]) => counts[key] > 0 || statusFilter.has(key)).map(([key, s]) => `
      <button type="button" class="legend-item" data-status="${key}" aria-pressed="${statusFilter.has(key)}"
        style="--st:${s.color}"><i></i>${esc(s.label)} <b>${counts[key]}</b></button>`).join("")
      + `<span class="legend-hint">Klicka för att filtrera</span>`;
    legend.querySelectorAll(".legend-item").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.status;
      statusFilter.has(k) ? statusFilter.delete(k) : statusFilter.add(k);
      update();
    }));
  }

  // ---------- Map ----------
  function ensureMap() {
    if (map || !window.L) return map;
    map = L.map("map", { scrollWheelZoom: false, zoomControl: true, preferCanvas: true }).setView([62.5, 16.5], 5);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    layer = L.featureGroup().addTo(map);
    map.on("focus", () => map.scrollWheelZoom.enable());
    map.on("blur", () => map.scrollWheelZoom.disable());
    return map;
  }

  function drawMap(hits) {
    if (!ensureMap()) return;
    layer.clearLayers();
    const rank = { none: 0, web: 1, cal: 2, both: 3 };     // coloured pins drawn on top of grey ones
    const pts = hits.filter(hasCoords).sort((a, b) => rank[a._status] - rank[b._status]);
    pts.forEach((o) => {
      const grey = o._status === "none";
      const m = L.circleMarker([o.lat, o.lng], {
        radius: grey ? 6 : 8, weight: 2, color: "#ffffff", fillColor: STATUS[o._status].color, fillOpacity: 1,
      });
      const acts = o.activities.slice(0, 5).map(esc).join(" · ");
      const dist = o._dist != null ? ` · ${fmtKm(o._dist)}` : "";
      m.bindPopup(`<div class="map-pop">
        <span class="card-kicker">${esc((o.samfund || []).join(" · "))}</span>
        <strong>${esc(o.name)}</strong>
        <span class="muted">${esc(place(o))}${dist}</span>
        ${acts ? `<span class="map-pop-acts">${acts}</span>` : ""}
        <span class="map-pop-status"><i style="background:${STATUS[o._status].color}"></i>${esc(STATUS[o._status].label)}${o.n_events ? ` · ${o.n_events} händelser` : ""}</span>
        <span class="map-pop-links"><a href="${ROOT}forsamling/${esc(o.slug)}/">Visa församlingen →</a>
        ${o.website ? `<a href="${esc(o.website)}" target="_blank" rel="noopener">Webbplats ↗</a>` : ""}</span></div>`);
      m.addTo(layer);
    });
    if (meMarker) meMarker.addTo(map);
    setTimeout(() => {
      map.invalidateSize();
      if (me) {
        const near = pts.slice(0, 8).map((o) => [o.lat, o.lng]).concat([[me.lat, me.lng]]);
        map.fitBounds(near, { padding: [40, 40], maxZoom: 12 });
      } else if (pts.length) {
        map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 12 });
      }
    }, 0);
  }

  function setView(v) {
    view = v;
    viewBtns.forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === v));
    if (mapPanel) mapPanel.hidden = v !== "karta";
    results.hidden = v === "karta";
    if (moreWrap) moreWrap.hidden = v === "karta" || lastHits.length <= shown;
    if (v === "karta") {
      if (window.L) drawMap(lastHits);
      else window.addEventListener("load", () => drawMap(lastHits), { once: true });
    }
    syncUrl();
  }

  // ---------- Near me ----------
  function locate() {
    if (!navigator.geolocation) {
      showStatus("Din webbläsare kan inte dela position.");
      return;
    }
    nearBtn.disabled = true;
    showStatus("Hämtar din position …");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        me = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        nearBtn.disabled = false;
        nearBtn.setAttribute("aria-pressed", "true");
        showStatus("Sorterat efter avstånd från dig.");
        if (window.L) {
          ensureMap();
          if (meMarker) meMarker.remove();
          meMarker = L.circleMarker([me.lat, me.lng], {
            radius: 9, weight: 3, color: "#ffffff", fillColor: "#b8562f", fillOpacity: 1,
          }).bindTooltip("Du är här");
        }
        update();
      },
      () => {
        nearBtn.disabled = false;
        showStatus("Kunde inte hämta din position. Sök på ort i stället.");
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 },
    );
  }
  function showStatus(msg) {
    geoStatus.textContent = msg;
    geoStatus.hidden = !msg;
  }

  // ---------- Filtering ----------
  function syncUrl() {
    const p = new URLSearchParams();
    if (q.value) p.set("q", q.value);
    if (county.value) p.set("lan", county.value);
    if (samfundSel && samfundSel.value) p.set("samfund", samfundSel.value);
    if (selected.size) p.set("har", [...selected].join("|"));
    if (statusFilter.size) p.set("status", [...statusFilter].join("|"));
    if (view === "karta") p.set("vy", "karta");
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
  }

  function update() {
    const terms = fold(q.value).split(/\s+/).filter(Boolean);
    const base = orgs.filter((o) =>
      terms.every((t) => o._hay.includes(t)) &&
      (!county.value || o.county === county.value) &&
      (!samfundSel || !samfundSel.value || (o.samfund || []).includes(samfundSel.value)) &&
      [...selected].every((a) => o.activities.includes(a)));
    renderLegend(base);
    let hits = statusFilter.size ? base.filter((o) => statusFilter.has(o._status)) : base;
    orgs.forEach((o) => (o._dist = me && hasCoords(o) ? km(me, o) : null));
    if (me) hits = [...hits].sort((a, b) => (a._dist ?? Infinity) - (b._dist ?? Infinity));
    lastHits = hits;
    shown = PAGE;
    renderCards();
    count.textContent = `${hits.length} ${hits.length === 1 ? "träff" : "träffar"}`;
    empty.hidden = hits.length > 0;
    clear.hidden = !(terms.length || county.value || (samfundSel && samfundSel.value) || selected.size || statusFilter.size);
    if (view === "karta" && window.L) drawMap(hits);
    syncUrl();
  }

  function renderCards() {
    results.innerHTML = lastHits.slice(0, shown).map(card).join("");
    const left = lastHits.length - shown;
    if (moreWrap) {
      moreWrap.hidden = left <= 0;
      moreBtn.textContent = `Visa fler (${left} till)`;
    }
  }
  if (moreBtn) moreBtn.addEventListener("click", () => {
    const first = shown;
    shown += PAGE;
    results.insertAdjacentHTML("beforeend", lastHits.slice(first, shown).map(card).join(""));
    const left = lastHits.length - shown;
    moreWrap.hidden = left <= 0;
    moreBtn.textContent = `Visa fler (${left} till)`;
  });

  q.addEventListener("input", update);
  county.addEventListener("change", update);
  if (samfundSel) samfundSel.addEventListener("change", update);
  chips.forEach((c) => c.addEventListener("click", () => {
    const a = c.dataset.activity;
    selected.has(a) ? selected.delete(a) : selected.add(a);
    c.setAttribute("aria-pressed", selected.has(a));
    update();
  }));
  clear.addEventListener("click", () => {
    q.value = ""; county.value = ""; if (samfundSel) samfundSel.value = ""; selected.clear(); statusFilter.clear();
    chips.forEach((c) => c.setAttribute("aria-pressed", "false"));
    update();
  });
  viewBtns.forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  nearBtn.addEventListener("click", locate);

  update();
  if (view === "karta") setView("karta");
})();
