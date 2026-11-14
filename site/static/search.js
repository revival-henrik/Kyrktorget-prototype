(async function () {
  const ROOT = window.ROOT || "";
  const $ = (id) => document.getElementById(id);
  const q = $("q"), county = $("county"), results = $("results"), count = $("result-count"),
        empty = $("empty"), clear = $("clear"), chips = [...document.querySelectorAll(".chip")];
  const orgs = await fetch(ROOT + "data/organizations.json").then((r) => r.json());
  const fold = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  orgs.forEach((o) => (o._hay = fold([o.name, o.city, o.municipality, o.county, o.denomination].join(" "))));

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const selected = new Set();

  // Restore state from URL so filtered views can be shared
  const params = new URLSearchParams(location.search);
  q.value = params.get("q") || "";
  county.value = params.get("lan") || "";
  (params.get("har") || "").split("|").filter(Boolean).forEach((a) => selected.add(a));
  chips.forEach((c) => c.setAttribute("aria-pressed", selected.has(c.dataset.activity)));

  function card(o) {
    const acts = [...o.activities].sort((a, b) => selected.has(b) - selected.has(a));
    const tags = acts.slice(0, 4).map((a) => `<li${selected.has(a) ? ' class="hit"' : ""}>${esc(a)}</li>`).join("");
    const more = o.activities.length > 4 ? `<li class="more">+${o.activities.length - 4}</li>` : "";
    const place = o.city + (o.municipality && o.municipality !== o.city ? ", " + o.municipality : "");
    return `<li class="card"><a href="${ROOT}forsamling/${esc(o.slug)}/">
      <span class="card-kicker">${esc(o.denomination || o.kind || "")}</span>
      <h3>${esc(o.name)}</h3><p class="card-place">${esc(place)}</p>
      <ul class="tags">${tags}${more}</ul></a></li>`;
  }

  function update() {
    const terms = fold(q.value).split(/\s+/).filter(Boolean);
    const hits = orgs.filter((o) =>
      terms.every((t) => o._hay.includes(t)) &&
      (!county.value || o.county === county.value) &&
      [...selected].every((a) => o.activities.includes(a)));
    results.innerHTML = hits.map(card).join("");
    count.textContent = `${hits.length} ${hits.length === 1 ? "träff" : "träffar"}`;
    empty.hidden = hits.length > 0;
    const active = terms.length || county.value || selected.size;
    clear.hidden = !active;
    const p = new URLSearchParams();
    if (q.value) p.set("q", q.value);
    if (county.value) p.set("lan", county.value);
    if (selected.size) p.set("har", [...selected].join("|"));
    history.replaceState(null, "", p.toString() ? "?" + p : location.pathname);
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
  update();
})();
