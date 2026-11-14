(async function () {
  const ROOT = window.ROOT || "";
  const $ = (id) => document.getElementById(id);
  const from = $("from"), to = $("to"), city = $("city"), category = $("category"),
        out = $("calendar"), empty = $("cal-empty");
  const events = await fetch(ROOT + "data/events.json").then((r) => r.json());
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  from.value = new Date().toISOString().slice(0, 10);

  function update() {
    const hits = events.filter((e) =>
      (!from.value || e.day >= from.value) && (!to.value || e.day <= to.value) &&
      (!city.value || e.city === city.value) && (!category.value || e.category === category.value));
    const byDay = new Map();
    hits.forEach((e) => { if (!byDay.has(e.day)) byDay.set(e.day, []); byDay.get(e.day).push(e); });
    out.innerHTML = [...byDay].map(([day, list]) => `
      <section class="cal-day">
        <h2>${esc(list[0].date_label)}</h2>
        <ol class="event-list">${list.map((e) => `
          <li>
            <time>${esc(e.time || "Heldag")}</time>
            <div>
              ${e.category ? `<span class="pill">${esc(e.category)}</span>` : ""}
              <strong>${esc(e.title)}</strong>
              <p class="muted">${e.org_slug ? `<a href="${ROOT}forsamling/${esc(e.org_slug)}/">${esc(e.org)}</a>` : esc(e.location || "")}${e.city ? ", " + esc(e.city) : ""}</p>
              ${e.description ? `<p>${esc(e.description)}</p>` : ""}
            </div>
          </li>`).join("")}
        </ol>
      </section>`).join("");
    empty.hidden = hits.length > 0;
  }
  [from, to, city, category].forEach((el) => el.addEventListener("change", update));
  update();
})();
