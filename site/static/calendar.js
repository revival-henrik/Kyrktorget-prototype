(async function () {
  const ROOT = window.ROOT || "";
  const $ = (id) => document.getElementById(id);
  const from = $("from"), to = $("to"), city = $("city"), category = $("category"),
        out = $("calendar"), empty = $("cal-empty");
  const events = await fetch(ROOT + "data/events.json?v=" + (window.V || "")).then((r) => r.json());
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  from.value = new Date().toISOString().slice(0, 10);
  const PAGE = 150;              // events rendered at a time (thousands are slow on phones)
  const moreWrap = $("cal-more-wrap"), moreBtn = $("cal-more");
  let hits = [], shown = PAGE;

  function update() {
    hits = events.filter((e) =>
      (!from.value || e.day >= from.value) && (!to.value || e.day <= to.value) &&
      (!city.value || e.city === city.value) && (!category.value || e.category === category.value));
    shown = PAGE;
    render();
    empty.hidden = hits.length > 0;
  }

  function render() {
    const byDay = new Map();
    hits.slice(0, shown).forEach((e) => { if (!byDay.has(e.day)) byDay.set(e.day, []); byDay.get(e.day).push(e); });
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
              ${e.description ? `<p class="desc">${esc(e.description)}</p>` : ""}
            </div>
          </li>`).join("")}
        </ol>
      </section>`).join("");
    const left = hits.length - shown;
    moreWrap.hidden = left <= 0;
    moreBtn.textContent = `Visa fler (${left} till)`;
  }
  moreBtn.addEventListener("click", () => { shown += PAGE; render(); });
  [from, to, city, category].forEach((el) => el.addEventListener("change", update));
  update();
})();
