(async function () {
  const ROOT = window.ROOT || "";
  const $ = (id) => document.getElementById(id);
  const q = $("q"), county = $("county"), results = $("results"), count = $("result-count"),
        empty = $("empty"), clear = $("clear"), nearBtn = $("near-me"), geoStatus = $("geo-status"),
        mapPanel = $("map-panel"), chips = [...document.querySelectorAll(".chip")],
        viewBtns = [...document.querySelectorAll("[data-view]")];
  const orgs = await fetch(ROOT + "data/organizations.json").then((r) => r.json());
  const fold = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  orgs.forEach((o) => (o._hay = fold([o.name, o.city, o.municipality, o.county, o.denomination].join(" "))));
  const hasCoords = (o) => typeof o.lat === "number" && typeof o.lng === "number";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const selected = new Set();
  let view = "lista", me = null, map = null, layer = null, meMarker = null, lastHits = [];

  // Restore state from URL so filtered views can be shared
  const params = new URLSearchParams(location.search);
  q.value = params.get("q") || "";
  county.value = params.get("lan") || "";
  (params.get("har") || "").split("|").filter(Boolean).forEach((a) => selected.add(a));
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
    return o.city + (o.municipality && o.municipality !== o.city ? ", " + o.municipality : "");
  }

  function card(o) {
    const acts = [...o.activities].sort((a, b) => selected.has(b) - selected.has(a));
    const tags = acts.slice(0, 4).map((a) => `<li${selected.has(a) ? ' class="hit"' : ""}>${esc(a)}</li>`).join("");
    const more = o.activities.length > 4 ? `<li class="more">+${o.activities.length - 4}</li>` : "";
    const dist = o._dist != null ? `<span class="distance">${fmtKm(o._dist)}</span>` : "";
    return `<li class="card"><a href="${ROOT}forsamling/${esc(o.slug)}/">
      <span class="card-kicker">${esc(o.denomination || o.kind || "")}${dist}</span>
      <h3>${esc(o.name)}</h3><p class="card-place">${esc(place(o))}</p>
      <ul class="tags">${tags}${more}</ul></a></li>`;
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
    const pts = hits.filter(hasCoords);
    pts.forEach((o) => {
      const m = L.circleMarker([o.lat, o.lng], {
        radius: 8, weight: 2, color: "#ffffff", fillColor: "#1f5c4f", fillOpacity: 1,
      });
      const acts = o.activities.slice(0, 5).map(esc).join(" · ");
      const dist = o._dist != null ? ` · ${fmtKm(o._dist)}` : "";
      m.bindPopup(`<div class="map-pop">
        <span class="card-kicker">${esc(o.denomination || o.kind || "")}</span>
        <strong>${esc(o.name)}</strong>
        <span class="muted">${esc(place(o))}${dist}</span>
        ${acts ? `<span class="map-pop-acts">${acts}</span>` : ""}
        <a href="${ROOT}forsamling/${esc(o.slug)}/">Visa församlingen →</a></div>`);
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
    if (selected.size) p.set("har", [...selected].join("|"));
    if (view === "karta") p.set("vy", "karta");
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
  }

  function update() {
    const terms = fold(q.value).split(/\s+/).filter(Boolean);
    let hits = orgs.filter((o) =>
      terms.every((t) => o._hay.includes(t)) &&
      (!county.value || o.county === county.value) &&
      [...selected].every((a) => o.activities.includes(a)));
    orgs.forEach((o) => (o._dist = me && hasCoords(o) ? km(me, o) : null));
    if (me) hits = [...hits].sort((a, b) => (a._dist ?? Infinity) - (b._dist ?? Infinity));
    lastHits = hits;
    results.innerHTML = hits.map(card).join("");
    count.textContent = `${hits.length} ${hits.length === 1 ? "träff" : "träffar"}`;
    empty.hidden = hits.length > 0;
    clear.hidden = !(terms.length || county.value || selected.size);
    if (view === "karta" && window.L) drawMap(hits);
    syncUrl();
  }

  q.addEventListener("input", update);
  county.addEventListener("change", update);
  chips.forEach((c) => c.addEventListener("click", () => {
    const a = c.dataset.activity;
    selected.has(a) ? selected.delete(a) : selected.add(a);
    c.setAttribute("aria-pressed", selected.has(a));
    update();
  }));
  clear.addEventListener("click", () => {
    q.value = ""; county.value = ""; selected.clear();
    chips.forEach((c) => c.setAttribute("aria-pressed", "false"));
    update();
  });
  viewBtns.forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  nearBtn.addEventListener("click", locate);

  update();
  if (view === "karta") setView("karta");
})();
