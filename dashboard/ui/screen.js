// The status screen: ask the server what to show, every ten seconds.
// After `agency screen show URL` that is another page, in a frame over
// everything, until `agency screen reset`.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// A long name gets smaller until it fits beside the QR code.
function fitName() {
  const h1 = $("name");
  h1.style.fontSize = "";
  const room = h1.parentElement.parentElement.clientWidth;
  let size = parseFloat(getComputedStyle(h1).fontSize);
  while (h1.scrollWidth > room && size > 24) {
    size *= 0.92;
    h1.style.fontSize = `${size}px`;
  }
}

// The page from `agency screen show`, or none. Changing it reloads the frame.
function showPage(url) {
  let frame = $("show");
  if (!url) {
    frame?.remove();
    return;
  }
  if (frame && frame.dataset.url === url) return;
  frame?.remove();
  frame = document.createElement("iframe");
  frame.id = "show";
  frame.title = "The page chosen with agency screen show";
  frame.dataset.url = url;
  frame.src = url;
  document.body.append(frame);
}

function render(s) {
  showPage(s.show);
  if ($("name").textContent !== s.hostname) {
    $("name").textContent = s.hostname;
    document.title = `${s.hostname} - agency`;
  }
  $("state").textContent = s.state;
  $("tailscale").textContent = s.tailscale || "Not connected";
  $("tailscale").classList.toggle("mono", !!s.tailscale);
  $("desktops").textContent = s.desktops;
  $("system").textContent = s.system;
  $("foot").textContent = s.console
    ? "To open a console, press Ctrl+Alt+F2."
    : "To use the console, set a password over SSH: sudo passwd agent";

  let code;
  if (!s.tailscale) {
    code = `<div class="hint">
      <p>Connect Tailscale to open the dashboard.</p>
      <p class="muted">Over SSH, run:</p>
      <code>sudo tailscale up --ssh</code>
    </div>`;
  } else if (s.qr) {
    code = `<div class="qr" role="img" aria-label="QR code for the dashboard address">${s.qr}</div>
      <p>Scan to open the dashboard<br><span class="url">${esc(s.url)}</span></p>`;
  } else {
    code = `<div class="hint"><p>Setting up the dashboard address.</p>
      <p class="muted">It shows here in a minute.</p></div>`;
  }
  if ($("code").dataset.html !== code) {
    $("code").innerHTML = code;
    $("code").dataset.html = code;
  }
  fitName();
}

async function tick() {
  try {
    const r = await fetch("/api/screen", { cache: "no-store" });
    if (r.ok) render(await r.json());
  } catch {
    // The server restarts during a rebuild; keep the last picture.
  }
}

tick();
setInterval(tick, 10000);
addEventListener("resize", fitName);
document.fonts?.ready.then(fitName);
