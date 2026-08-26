/* recall-radar dashboard client.
   All decisions come from the API (which delegates to matcher.check) - this
   file only renders them. It never re-implements matching. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");

const STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
  + "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY").split(" ");

/* Severity is never colour alone: each badge pairs a hue with an icon and a
   word, so it still reads in greyscale or with colourblindness. */
const ICONS = {
  certain: '<path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  likely: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  possible: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  clear: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
};
const WORDS = { certain: "Recalled", likely: "Likely recalled", possible: "Check the label", clear: "No match" };

function badge(kind) {
  return `<span class="badge ${kind}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${ICONS[kind]}</svg>${WORDS[kind]}</span>`;
}

function clsBadge(c) {
  const t = String(c || "");
  const k = t === "Class I" ? "c1" : t === "Class II" ? "c2" : "";
  return `<span class="cls ${k}">${esc(t || "Unclassified")}</span>`;
}

function recallCard(m, kind) {
  const k = kind || m.verdict || "neutral";
  const why = (m.reasons || []).join("; ");
  return `<div class="recall ${k}">
    <div class="recall-head">
      ${m.verdict ? badge(m.verdict) : ""}
      <span class="recall-firm">${esc(m.firm)}</span>
      ${clsBadge(m.classification)}
      <span class="recall-meta">${esc(m.source)} ${esc(m.recall_number || "")} &middot; ${esc(m.report_date || "")}</span>
    </div>
    <dl>
      <dt>Product</dt><dd>${esc(m.product)}</dd>
      <dt>Reason</dt><dd class="sub">${esc(m.reason)}</dd>
      ${m.distribution ? `<dt>Sold in</dt><dd class="sub">${esc(m.distribution)}</dd>` : ""}
      ${m.codes ? `<dt>Codes</dt><dd class="sub">${esc(m.codes)}</dd>` : ""}
      ${why ? `<dt>Why</dt><dd><span class="why">${esc(why)}</span></dd>` : ""}
    </dl>
    ${m.url ? `<div style="margin-top:8px"><a href="${esc(m.url)}" target="_blank" rel="noopener">Official notice &rarr;</a></div>` : ""}
  </div>`;
}

/* ---- search ------------------------------------------------------------ */
async function runCheck() {
  const q = $("q").value.trim(), upc = $("upc").value.trim();
  const box = $("results");
  if (!q && !upc) { box.style.display = "none"; return; }

  const p = new URLSearchParams();
  if (upc) p.set("upc", upc); else p.set("q", q);
  if ($("opt-all").checked) p.set("all", "1");
  if ($("opt-state").value) p.set("state", $("opt-state").value);

  box.style.display = "";
  box.innerHTML = `<div class="card-panel pad"><div class="empty">Checking&hellip;</div></div>`;
  const r = await fetch("/api/check?" + p);
  const d = await r.json();

  if (d.error) {
    box.innerHTML = `<div class="card-panel pad"><div class="err">${esc(d.error)}</div></div>`;
    return;
  }
  const label = esc(d.query);
  if (!d.matches.length) {
    box.innerHTML = `<div class="card-panel pad">
      <div class="clear-banner">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
          stroke-linecap="round" stroke-linejoin="round">${ICONS.clear}</svg>
        <div><div><strong>${label}</strong> does not appear in any ${$("opt-all").checked ? "" : "open "}recall on file.</div>
        <div class="sub">Checked ${fmt(d.searched)} recalls.</div></div>
      </div></div>`;
    return;
  }
  const worst = d.matches[0].verdict;
  const head = { certain: "This barcode is on a recall",
                 likely: "The name matches a recall",
                 possible: "Worth checking the label" }[worst];
  box.innerHTML = `<div class="card-panel pad">
    <h2 class="section-title">${d.matches.length} match${d.matches.length > 1 ? "es" : ""} for
      &ldquo;${label}&rdquo; <span class="count">&mdash; ${esc(head)}</span></h2>
    ${d.matches.map((m) => recallCard(m)).join("")}</div>`;
}

/* ---- stats ------------------------------------------------------------- */
async function loadStats() {
  const d = await (await fetch("/api/stats")).json();
  $("stats").innerHTML = [
    ["recalls tracked", fmt(d.recalls), "", ""],
    ["open recalls", fmt(d.ongoing), "", "not yet terminated"],
    ["open class I", fmt(d.open_class_i), "alarm", "serious health risk"],
    ["with a barcode", fmt(d.with_upc), "", "scannable directly"],
  ].map(([label, v, cls, note]) => `<div class="card-panel stat-card">
      <div class="stat-value ${cls}">${v}</div>
      <div class="stat-label">${label}</div>
      ${note ? `<div class="stat-note">${note}</div>` : ""}
    </div>`).join("");

  const src = d.by_source.map((s) => `${s.source} to ${s.newest}`).join(" &middot; ");
  $("source-line").innerHTML = `${fmt(d.recalls)} recalls &middot; ${src || "no data yet"}`;
  $("live-dot").style.background = d.recalls ? "var(--pos)" : "var(--warning)";
}

/* ---- watchlist --------------------------------------------------------- */
async function loadWatchlist() {
  const p = new URLSearchParams();
  if ($("opt-state").value) p.set("state", $("opt-state").value);
  const d = await (await fetch("/api/scan?" + p)).json();
  const box = $("watchlist");
  $("watch-count").textContent = d.items.length ? `${d.items.length} item${d.items.length > 1 ? "s" : ""}` : "";

  if (!d.items.length) {
    box.innerHTML = `<div class="empty">Nothing on the list yet. Add the things you buy
      regularly and they will be checked against every new recall.</div>`;
    return;
  }
  box.innerHTML = d.items.map((it) => {
    const kind = it.worst || "clear";
    const n = it.matches.length;
    return `<div class="watch-item" data-id="${it.id}">
      <div class="watch-head" data-toggle="${it.id}">
        <svg class="chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        <div class="watch-main">
          <div class="watch-name">${esc(it.label)}</div>
          ${it.brand ? `<div class="watch-brand">${esc(it.brand)}</div>` : ""}
        </div>
        ${badge(kind)}
        ${n ? `<span class="cls">${n}</span>` : ""}
        <button class="del" data-del="${it.id}" title="Remove" aria-label="Remove ${esc(it.label)}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      ${n ? `<div class="watch-body" id="wb-${it.id}" hidden>${it.matches.map((m) => recallCard(m)).join("")}</div>` : ""}
    </div>`;
  }).join("");
}

async function addWatch() {
  const label = $("w-label").value.trim();
  if (!label) { $("w-label").focus(); return; }
  await fetch("/api/watchlist", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, brand: $("w-brand").value.trim() }),
  });
  $("w-label").value = ""; $("w-brand").value = "";
  loadWatchlist();
}

/* ---- feed -------------------------------------------------------------- */
async function loadFeed() {
  const p = new URLSearchParams({ days: "120" });
  if ($("feed-class-i").checked) p.set("class_i", "1");
  if ($("opt-state").value) p.set("state", $("opt-state").value);
  const d = await (await fetch("/api/recent?" + p)).json();
  $("feed-count").textContent = `${fmt(d.total)} in last ${d.days} days`;
  $("feed").innerHTML = d.items.length
    ? d.items.map((m) => `<div class="feed-item">
        <div class="feed-head">
          <span class="feed-firm">${esc(m.firm)}</span>${clsBadge(m.classification)}
          <span class="feed-date">${esc(m.source)} &middot; ${esc(m.report_date)}</span>
        </div>
        <div class="feed-prod">${esc(m.product).slice(0, 210)}</div>
        <div class="feed-reason">${esc(m.reason)}</div>
      </div>`).join("")
    : `<div class="empty">No open recalls match that filter.</div>`;
}

/* ---- volume chart ------------------------------------------------------
   One series, one hue, recessive grid. The peak month is directly labelled;
   the rest are reachable on hover and in the table view below, so no value
   is locked behind a tooltip. */
/* Round the axis top to a readable number rather than the raw data max, so
   ticks land on 0 / 125 / 250 instead of 0 / 108 / 216. */
function niceMax(v) {
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.25, 2, 2.5, 5, 10]) {
    if (m * pow >= v) return m * pow;
  }
  return 10 * pow;
}
const peakless = (ms) => {
  const p = ms.find((m) => m.partial);
  return p ? `${p.label} ${p.year}` : "The current month";
};

async function loadChart() {
  const d = await (await fetch("/api/volume")).json();
  const ms = d.months;
  if (!ms.length) return;

  const W = 780, H = 190, L = 34, R = 8, T = 16, B = 24;
  const iw = W - L - R, ih = H - T - B;
  const raw = Math.max(...ms.map((m) => m.count), 1);
  const max = niceMax(raw);                  // round axis top, not the data max
  const step = iw / ms.length;
  const bw = Math.max(6, step - 6);          // 6px gap keeps bars from touching
  // The partial month is excluded from the peak: it is not comparable yet.
  const complete = ms.filter((m) => !m.partial);
  const peak = complete.reduce((a, b) => (b.count > a.count ? b : a), complete[0] || ms[0]);
  const y = (v) => T + ih - (v / max) * ih;

  const ticks = [0, max / 2, max];
  const grid = ticks.map((t) => `
    <line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>
    <text class="axis-text" x="${L - 7}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end">${t}</text>`).join("");

  const bars = ms.map((m, i) => {
    const x = L + i * step + (step - bw) / 2;
    const h = Math.max(m.count > 0 ? 2 : 0, ih - (y(m.count) - T));
    const top = T + ih - h;
    const r = Math.min(4, bw / 2, h);        // 4px rounded data-end, flat base
    const path = h <= 0 ? "" :
      `M${x.toFixed(1)},${(top + h).toFixed(1)} V${(top + r).toFixed(1)}
       Q${x.toFixed(1)},${top.toFixed(1)} ${(x + r).toFixed(1)},${top.toFixed(1)}
       H${(x + bw - r).toFixed(1)} Q${(x + bw).toFixed(1)},${top.toFixed(1)} ${(x + bw).toFixed(1)},${(top + r).toFixed(1)}
       V${(top + h).toFixed(1)} Z`;
    // Direct-label the peak and the in-progress month; everything else is on
    // hover and in the table below.
    const showLabel = (m.month === peak.month || m.partial) && m.count > 0;
    const label = showLabel
      ? `<text class="bar-label" x="${(x + bw / 2).toFixed(1)}" y="${(top - 5).toFixed(1)}">${m.count}</text>` : "";
    const cls = m.partial ? "bar partial" : "bar";
    const alt = `${m.label} ${m.year}: ${m.count} recalls${m.partial ? " so far (month still in progress)" : ""}`;
    const tick = m.partial ? `${m.label}*` : m.label;
    return `${h > 0 ? `<path class="${cls}" d="${path}"/>` : ""}${label}
      <rect class="bar-hit" x="${(L + i * step).toFixed(1)}" y="${T}" width="${step.toFixed(1)}" height="${ih}"
        data-i="${i}"><title>${alt}</title></rect>
      <text class="axis-text" x="${(L + i * step + step / 2).toFixed(1)}" y="${H - 7}" text-anchor="middle">${tick}</text>`;
  }).join("");

  $("chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img"
     aria-label="Recalls reported per month for the last 12 months, peaking at ${peak.count} in ${peak.label} ${peak.year}">
     ${grid}${bars}</svg>`;

  $("chart-table").querySelector("tbody").innerHTML = ms.map((m) =>
    `<tr><td>${m.label} ${m.year}${m.partial ? " (to date)" : ""}</td><td>${m.count}</td></tr>`).join("");
  $("chart-note").textContent = `* ${peakless(ms)} is still in progress as of ${d.as_of}.`;

  const tip = $("chart-tip"), wrap = $("chart").parentElement;
  $("chart").querySelectorAll(".bar-hit").forEach((el) => {
    el.addEventListener("mousemove", (e) => {
      const m = ms[+el.dataset.i];
      tip.innerHTML = `${m.label} ${m.year} &middot; <span class="tv">${m.count}</span> recalls`
        + (m.partial ? ' <span style="color:var(--faint)">so far</span>' : "");
      tip.style.opacity = "1";
      const b = wrap.getBoundingClientRect();
      tip.style.left = Math.min(e.clientX - b.left + 12, wrap.clientWidth - tip.offsetWidth - 4) + "px";
      tip.style.top = (e.clientY - b.top - 34) + "px";
    });
    el.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
  });
}

/* ---- sync -------------------------------------------------------------- */
async function doSync() {
  const btn = $("sync-btn");
  btn.disabled = true;
  $("sync-icon").classList.add("spin");
  $("sync-label").textContent = "Syncing";
  try {
    const d = await (await fetch("/api/sync", { method: "POST" })).json();
    const failed = Object.entries(d.report).filter(([, v]) => v.error);
    $("sync-label").textContent = failed.length ? `${failed[0][0]} failed` : "Synced";
    await Promise.all([loadStats(), loadWatchlist(), loadFeed(), loadChart()]);
  } catch (e) {
    $("sync-label").textContent = "Sync failed";
  } finally {
    $("sync-icon").classList.remove("spin");
    btn.disabled = false;
    setTimeout(() => { $("sync-label").textContent = "Sync"; }, 3500);
  }
}

/* ---- wiring ------------------------------------------------------------ */
function init() {
  const sel = $("opt-state");
  STATES.forEach((s) => sel.add(new Option(s, s)));

  $("check-btn").addEventListener("click", runCheck);
  for (const id of ["q", "upc"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runCheck(); });
  }
  $("opt-all").addEventListener("change", runCheck);
  sel.addEventListener("change", () => { runCheck(); loadWatchlist(); loadFeed(); });
  $("feed-class-i").addEventListener("change", loadFeed);
  $("w-add").addEventListener("click", addWatch);
  $("w-label").addEventListener("keydown", (e) => { if (e.key === "Enter") addWatch(); });
  $("sync-btn").addEventListener("click", doSync);

  $("watchlist").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      await fetch("/api/watchlist/" + del.dataset.del, { method: "DELETE" });
      loadWatchlist();
      return;
    }
    const head = e.target.closest("[data-toggle]");
    if (!head) return;
    const body = $("wb-" + head.dataset.toggle);
    if (!body) return;
    body.hidden = !body.hidden;
    head.querySelector(".chev").classList.toggle("open", !body.hidden);
  });

  loadStats(); loadWatchlist(); loadFeed(); loadChart();
}

document.addEventListener("DOMContentLoaded", init);
