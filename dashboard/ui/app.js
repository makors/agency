// agency dashboard. Plain JavaScript; the server is dashboard/server.py.
//
// The page asks for /api/state every few seconds and draws each section
// from it: the system's vitals, desktops, the terminal and the apps that are on. Accounts,
// secrets and the system are in Settings (settings.js). Changes start a job
// on the server; while one runs, the others wait and it shows in the bar at
// the bottom (common.js).

let state = null;
let appList = null;
let jobRunning = false;
let appsAfterJob = null;
const shots = {}; // desktop number -> object URL of its newest picture

// ----------------------------------------------------------------- pieces

const dis = () => (jobRunning ? "disabled" : "");

// --------------------------------------------------------------- sections

function drawDesktops(list) {
  const host = location.hostname;
  const last = list.length;
  const html = list.map((d) => {
    const detail = !d.running ? "Stopped"
      : d.windows === null ? "Running"
      : d.windows === 0 ? "No windows open"
      : `${d.windows} window${d.windows === 1 ? "" : "s"} open`;
    const remove = d.n === last && d.n > 1
      ? `<button type="button" class="button button--danger" data-action="desktop-remove" ${dis()}>Remove</button>` : "";
    return `<article class="card">
      <div class="shot" data-shot="${d.n}">${d.running ? `<img alt="What desktop :${d.n} shows" hidden>` : "Stopped"}</div>
      <div class="card-foot">
        <div><p class="card-name">Desktop :${d.n}</p><p class="card-detail">${detail}</p></div>
        <div class="buttons">${remove}
          <a class="button button--solid" href="${location.protocol}//${esc(host)}:${d.port}/" target="_blank" rel="noopener">Open</a>
        </div>
      </div>
    </article>`;
  }).join("");
  if (setHTML($("#desktop-list"), html)) {
    for (const img of document.querySelectorAll("[data-shot] img")) {
      const n = img.parentElement.dataset.shot;
      if (shots[n]) showShot(img, shots[n]);
    }
  }
}

// Agents: one row per session, the ones waiting for a person first.

const AGENT_NAMES = { claude: "Claude Code", codex: "Codex" };
const AGENT_STATE = { working: "Working", waiting: "Waiting for you", idle: "Idle" };
const AGENT_ORDER = { waiting: 0, working: 1, idle: 2 };

function forHowLong(stamp) {
  const s = Math.max(0, Date.now() / 1000 - stamp);
  if (s < 60) return "under a minute";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

function drawAgents(list) {
  if (!list.length) {
    setHTML($("#agent-list"), `<p class="row muted">No agent sessions running.</p>`);
    return;
  }
  const rows = [...list].sort((a, b) => AGENT_ORDER[a.state] - AGENT_ORDER[b.state] || a.began - b.began);
  setHTML($("#agent-list"), rows.map((a) => {
    const name = AGENT_NAMES[a.tool] || a.tool;
    const detail = [a.folder && `<span class="mono">${esc(a.folder)}</span>`, a.activity && esc(a.activity)]
      .filter(Boolean).join(" · ");
    const old = a.manual && Date.now() / 1000 - a.updated > 3600 ? ` · <span class="muted">last heard ${ago(a.updated)}</span>` : "";
    return `<div class="row">
      <div class="row-main"><p class="row-name">${esc(name)}</p>${detail || old ? `<p class="row-detail">${detail}${old}</p>` : ""}</div>
      <div class="row-end"><span class="agent-state is-${a.state}">${AGENT_STATE[a.state]}</span>
        <span class="row-detail">${a.since ? forHowLong(a.since) : ""}</span></div>
    </div>`;
  }).join(""));
}

// System: CPU and memory over the last ten minutes, and every disk.

const pct = (n) => `${Math.round(n)}%`;
const SPAN = 600;   // seconds shown
const COLS = 60;    // columns of SPAN / COLS seconds each

function uptimeText(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

const clock = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

// Samples ([time, percent]) into COLS columns, newest on the right; each
// column is the mean of its samples, or null where there are none.
function columns(points) {
  const end = points.length ? points[points.length - 1][0] : 0;
  const sums = Array.from({ length: COLS }, () => [0, 0]);
  for (const [t, v] of points) {
    const k = Math.floor((end - t) / (SPAN / COLS));
    if (k < COLS) { sums[COLS - 1 - k][0] += v; sums[COLS - 1 - k][1]++; }
  }
  return sums.map(([sum, n], i) => n ? { v: sum / n, t: end - (COLS - 1 - i) * (SPAN / COLS) } : null);
}

// A column chart in el. Hovering a column puts its value in the card's big
// number and its time under the name; leaving puts back the newest.
// fmt(percent) is the big number's text.
const charts = {};

function chart(el, points, now, when, fmt) {
  let c = charts[el.id];
  if (!c) {
    const w = 100 / COLS;
    el.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" shape-rendering="crispEdges" aria-hidden="true">
        ${Array.from({ length: COLS }, (_, i) => `<rect class="spark-well" x="${(i * w + w * 0.14).toFixed(3)}" width="${(w * 0.72).toFixed(3)}" y="0" height="100"/>`).join("")}
        ${Array.from({ length: COLS }, (_, i) => `<rect class="spark-bar" x="${(i * w + w * 0.14).toFixed(3)}" width="${(w * 0.72).toFixed(3)}" y="100" height="0"/>`).join("")}
      </svg>`;
    el.insertAdjacentHTML("afterend", `<div class="spark-axis"><span>10 min ago</span><span>Now</span></div>`);
    c = charts[el.id] = { cols: [], at: null };
    const move = (x) => {
      const r = el.getBoundingClientRect();
      c.at = Math.min(COLS - 1, Math.max(0, Math.floor(((x - r.left) / r.width) * COLS)));
      hover(c);
    };
    el.addEventListener("pointermove", (e) => move(e.clientX));
    el.addEventListener("pointerdown", (e) => move(e.clientX));
    el.addEventListener("pointerleave", () => { c.at = null; hover(c); });
  }
  Object.assign(c, { el, now, when, fmt, cols: columns(points) });
  const bars = el.querySelectorAll(".spark-bar");
  c.cols.forEach((col, i) => {
    // Anything above zero gets at least a sliver, so idle is not empty.
    const h = col ? Math.max(3, Math.min(100, col.v)) : 0;
    bars[i].setAttribute("y", (100 - h).toFixed(2));
    bars[i].setAttribute("height", h.toFixed(2));
  });
  const last = c.cols[COLS - 1];
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", `${el.dataset.label} over the last ten minutes${last ? `, now ${pct(last.v)}` : ""}`);
  hover(c);
}

function hover(c) {
  const col = c.at === null ? null : c.cols[c.at];
  c.el.classList.toggle("is-hover", c.at !== null);
  c.el.querySelectorAll(".spark-bar").forEach((b, i) => b.classList.toggle("is-on", i === c.at));
  if (col && c.at < COLS - 1) {
    c.now.textContent = c.fmt(col.v);
    c.when.textContent = `At ${clock(col.t)}`;
  } else if (c.at !== null && !col) {
    c.now.textContent = "–";
    c.when.textContent = "No reading";
  } else {
    const last = c.cols[COLS - 1];
    c.now.textContent = last ? c.fmt(last.v) : "–";
    c.when.textContent = "Now";
  }
}

function drawSystem(v) {
  if (!v) return;
  const h = v.history || [];
  const mem = v.memory;

  chart($("#cpu-chart"), h.map((p) => [p[0], p[1]]), $("#cpu-now"), $("#cpu-when"), pct);
  const cpuBits = [`${v.cores} cores`];
  if (v.load) cpuBits.push(`load ${v.load[0].toFixed(2)}`);
  if (v.temp) cpuBits.push(`${v.temp} °C`);
  if (v.uptime) cpuBits.push(`up ${uptimeText(v.uptime)}`);
  $("#cpu-detail").textContent = cpuBits.join(" · ");

  if (mem) {
    chart($("#mem-chart"), h.filter((p) => p[2] !== null).map((p) => [p[0], (100 * p[2]) / mem.total]),
      $("#mem-now"), $("#mem-when"), pct);
    const bits = [`${gb(mem.used)} of ${gb(mem.total)} GB in use`];
    if (v.swap) bits.push(`swap ${gb(v.swap.used)} GB`);
    $("#mem-detail").textContent = bits.join(" · ");
  }

  const disks = v.disks || [];
  // The big number: the share used of all the space mounted from them.
  const used = disks.reduce((n, d) => n + d.used, 0);
  const room = disks.reduce((n, d) => n + d.fsSize, 0);
  const tb = (b) => (b >= 1e12 ? `${(b / 1e12).toFixed(1)} TB` : `${gb(b)} GB`);
  $("#disk-now").textContent = room ? pct((100 * used) / room) : "–";
  $("#disk-when").textContent = disks.length
    ? `${disks.length} disk${disks.length === 1 ? "" : "s"}, ${tb(disks.reduce((n, d) => n + d.size, 0))}` : "None found";
  setHTML($("#disk-list"), disks.map((d) => {
    const where = d.system ? "system disk" : d.mounts.length ? d.mounts[0] : "not mounted";
    const kind = [d.kind, d.removable ? "removable" : ""].filter(Boolean).join(", ");
    const share = d.fsSize ? (100 * d.used) / d.fsSize : 0;
    const full = share >= 90;
    // At least one block when anything is used.
    const width = share >= 0.5 ? `max(6px, ${share.toFixed(1)}%)` : "0";
    return `<div class="disk" title="${esc(d.model || d.name)}">
      <p class="disk-name">${esc(d.model || d.name)}</p>
      <p class="disk-use">${d.fsSize ? `${gb(d.used)} <span class="muted">/ ${gb(d.fsSize)} GB</span>` : `<span class="muted">${gb(d.size)} GB</span>`}</p>
      <div class="meter${full ? " is-full" : ""}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(share)}" aria-label="${esc(d.model || d.name)} space used"><div class="meter-fill" style="width:${width}"></div></div>
      <p class="disk-detail"><span class="mono">${esc(d.name)}</span>${kind ? ` · ${esc(kind)}` : ""} · ${esc(where)}${full ? ` · <span class="disk-full">almost full</span>` : ""}</p>
    </div>`;
  }).join(""));
}

// The home page lists the apps that are on; the catalog (/apps) has the rest.
function drawApps() {
  if (!appList) return;
  if (appList.error) {
    setHTML($("#app-list"), `<p class="row muted">The list did not load: ${esc(appList.error)}</p>`);
    return;
  }
  // A compact grid of names, so a long list stays short. Past a dozen,
  // the rest is a link to the catalog, which has them all.
  const on = appList.apps.filter((a) => a.on);
  if (!on.length) {
    setHTML($("#app-list"), `<p class="row muted">No apps yet.</p>`);
    return;
  }
  const max = on.length > 12 ? 11 : 12;
  const more = on.length - max;
  setHTML($("#app-list"), `<div class="app-grid">${on.slice(0, max).map((a) => `<div class="app-tile" title="${esc(a.label)}, ${esc(a.group)}">
      ${appLogo(a)}<span>${esc(a.label)}</span>
    </div>`).join("")}${more > 0 ? `<a class="app-tile app-more" href="/apps">${more} more</a>` : ""}</div>`);
}

function draw(s) {
  state = s;
  const wasRunning = jobRunning;
  jobRunning = !!(s.job && s.job.state === "running");
  drawHeader(s);
  drawDock(s.job);
  // Here only when there is something to do; Settings has the rest.
  drawUpdates(["behind", "unlinked"].includes(s.updates.status) ? s.updates : null, jobRunning);
  drawSystem(s.vitals);
  drawAgents(s.agents || []);
  drawDesktops(s.desktops);
  drawApps();
  for (const b of document.querySelectorAll("[data-job]")) b.disabled = jobRunning;
  // A job ended: the app list may have changed.
  if (s.job && !jobRunning && s.job.id !== appsAfterJob) {
    appsAfterJob = s.job.id;
    if (wasRunning) loadApps();
  }
}

// ---------------------------------------------------------------- polling

let timer = null;

async function refresh() {
  clearTimeout(timer);
  try {
    const s = await api("/api/state");
    if (s.error) showNotice(s.error);
    else {
      draw(s);
      remember("state", { ...s, job: null });
    }
  } catch {
    // The dashboard restarts during some rebuilds. Try again shortly.
  }
  timer = setTimeout(refresh, jobRunning ? 1500 : 6000);
}

async function loadApps() {
  try {
    appList = await api("/api/apps");
    if (!appList.error) remember("apps", appList);
  } catch {
    appList = { apps: [], error: "the server did not answer" };
  }
  drawApps();
}

// Desktop pictures: a new one every few seconds while the section is on screen.
let desktopsVisible = false;
new IntersectionObserver((entries) => {
  desktopsVisible = entries.some((e) => e.isIntersecting);
}).observe($("#desktops"));

function showShot(img, url) {
  img.onload = () => { img.hidden = false; };
  img.onerror = () => { img.hidden = true; };
  img.src = url;
}

async function refreshShots() {
  if (desktopsVisible && !document.hidden && state) {
    for (const d of state.desktops.filter((x) => x.running)) {
      try {
        const r = await fetch(`/api/desktops/${d.n}/shot`, { cache: "no-store" });
        if (!r.ok) continue;
        const url = URL.createObjectURL(await r.blob());
        if (shots[d.n]) URL.revokeObjectURL(shots[d.n]);
        shots[d.n] = url;
        const img = document.querySelector(`[data-shot="${d.n}"] img`);
        if (img) showShot(img, url);
      } catch {
        // Skip this round.
      }
    }
  }
  setTimeout(refreshShots, 4000);
}

// The terminal starts a shell, so load it only once it is near the screen.
function openTerminal(src) {
  const frame = $("#term");
  if (frame.getAttribute("src") !== src) frame.src = src;
}
// In tmux, xterm.js turns the wheel into arrow keys, which walk the shell's
// history. Keep the wheel from xterm.js; with nothing to scroll in the
// frame, the browser scrolls this page, at its usual speed. The frame is
// same-origin.
$("#term").addEventListener("load", (ev) => {
  let win;
  try { win = ev.target.contentWindow; win.document; } catch { return; }
  win.addEventListener("wheel", (e) => e.stopImmediatePropagation(), { capture: true, passive: true });
});
whenShown(() => new IntersectionObserver((entries, obs) => {
  if (entries.some((e) => e.isIntersecting)) {
    if (!$("#term").getAttribute("src")) openTerminal("/terminal/");
    obs.disconnect();
  }
}, { rootMargin: "200px" }).observe($("#terminal")));

// ---------------------------------------------------------------- actions

// Sign-ins that run in the terminal come here from Settings: /?signin=<tool>.
function signInHere(tool) {
  openTerminal(`/terminal/?arg=${encodeURIComponent(tool)}`);
  $("#term-note").innerHTML = `Signing in to ${esc(NAMES[tool] || tool)}. Follow the steps in the terminal. <button type="button" class="button" data-action="term-shell">Back to the shell</button>`;
  $("#terminal").scrollIntoView();
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  const action = t.dataset.action;

  switch (action) {
    case "update":
      return startJob("/api/update");
    case "desktop-add":
      return startJob("/api/desktops/add");
    case "desktop-remove":
      if (!confirm(`Remove desktop :${state.desktops.length}? Apps open on it close.`)) return;
      return startJob("/api/desktops/remove");
    case "job-dismiss":
      await api("/api/job/dismiss", {});
      return refresh();
    case "notice-close":
      return showNotice("");
    case "term-shell":
      openTerminal("/terminal/");
      $("#term-note").textContent = "A tmux shell, in your browser—how cool!";
      return;
  }
});

// Draw what the last visit saw until the server answers, so the sections
// have their size from the start. The pill says Loading until then.
const last = recall("state");
if (last) {
  drawHeader({ ...last, state: null });
  drawUpdates(["behind", "unlinked"].includes(last.updates?.status) ? last.updates : null);
  drawSystem(last.vitals);
  drawDesktops(last.desktops || []);
}
appList = recall("apps");
drawApps();

refresh();
loadApps();
whenShown(refreshShots);
const signin = new URLSearchParams(location.search).get("signin");
if (signin) {
  history.replaceState(null, "", "/#terminal");
  whenShown(() => signInHere(signin));
}
