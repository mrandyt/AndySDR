/* The RECEIVER 2 screen (the Nooelec / RTL-SDR dongle) and the status strip shown under the tabs on every screen. */
const RX2 = { live: null, log: [], spec: null, best: null, ctlKey: "", wfLast: 0, preset: "meteor", custom: "", lastDraw: 0 };
const RX2_PRESETS = [
  { id: "meteor", label: "Meteor-M satellite, 137.900 MHz", f: 137.9e6, bw: 60e3, sat: true },
  { id: "meteor2", label: "Meteor-M satellite, 137.100 MHz", f: 137.1e6, bw: 60e3, sat: true },
  { id: "acars", label: "Aircraft messages (ACARS) 131.725 MHz – busy all day, good for aiming", f: 131.725e6, bw: 8e3 },
  { id: "acars2", label: "Aircraft messages (ACARS) 131.525 MHz", f: 131.525e6, bw: 8e3 },
  { id: "acars3", label: "Aircraft messages (ACARS) 131.825 MHz", f: 131.825e6, bw: 8e3 },
  { id: "custom", label: "Another frequency …", f: 0, bw: 12e3 },
];
const rx2Lbl = (t) => String(t || "").replace(/\s*::.*$/, "");
const rx2$ = (id) => document.getElementById(id);
const rx2Visible = () => { const e = rx2$("tab-rx2"); return !!(e && e.offsetParent); };
function rx2Cd(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h} h ${m} min` : m >= 5 ? `${m} min` : m ? `${m} min ${s} s` : `${s} s`;
}
const rx2Hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/* ---------- what is it doing right now? (one place, used by the banner, the strip and the tiles) ---------- */
function rx2State() {
  const r = SCH && SCH.satrx;
  if (!r) return { k: "na", chip: "not available", title: "Not available", sub: "Waiting for the program to report in …" };
  const lv = RX2.live && RX2.live.on !== undefined ? RX2.live : r.live;
  const name = r.using || (r.devices && r.devices[0] && r.devices[0].label) || "";
  if (lv && lv.on) {
    if (lv.preview) return { k: "view", chip: `live view${lv.left != null ? " · " + lv.left + " s left" : ""}`, title: "LIVE VIEW – nothing is being recorded",
      sub: `Listening on ${fmtMHz(lv.freq, 3)} MHz so you can see it working${lv.left != null ? " · stops by itself in " + lv.left + " s" : ""}` };
    return { k: "rec", chip: `RECORDING ${lv.name ? String(lv.name).split(" pass")[0] : ""}`.trim(), title: `RECORDING – ${lv.name ? String(lv.name).split(" pass")[0] : "a pass"}`,
      sub: `${fmtMHz(lv.freq, 4)} MHz · ${rx2Cd(lv.elapsed)} so far${lv.el != null ? " · satellite " + lv.el + "° up" : ""}${lv.images ? " · " + lv.images + " picture" + (lv.images > 1 ? "s" : "") + " so far" : ""}` };
  }
  if (!r.enabled) return { k: "off", chip: "switched off", title: "SWITCHED OFF", sub: "The Nooelec is not being used. Switch it on and the Meteor passes will be recorded on it." };
  if (!r.driver) return { k: "bad", chip: "driver missing", title: "PROBLEM – the RTL-SDR driver is missing", sub: "rtlsdr.dll was not found. Reinstall AndySDR." };
  if (!(r.devices || []).length) return { k: "bad", chip: "not found", title: "PROBLEM – no dongle found", sub: "Plug the Nooelec in (and check it uses the WinUSB driver in Zadig). The main radio will take the passes meanwhile." };
  if (!name) return { k: "bad", chip: "not found", title: "PROBLEM – the chosen dongle is not plugged in", sub: "Pick another one in Scheduler → Second receiver, or plug it back in." };
  if (r.error && r.fail_in > 0) return { k: "bad", chip: "problem", title: "PROBLEM – it could not start", sub: `${r.error}. The main radio takes passes for the next ${rx2Cd(r.fail_in)}.` };
  const now = Date.now() / 1000, nx = (r.next || []).find((p) => p.los > now);
  if (!nx) return { k: "wait", chip: "ready", title: "READY – waiting", sub: SCH.sats && SCH.sats.observer ? "No Meteor pass in the next few days." : "Set your position in Scheduler so it can work out the passes." };
  if (nx.aos <= now + 120) return { k: "wait", chip: "pass starting", title: "PASS STARTING", sub: `${String(nx.name).split(" ")[0]} ${nx.name.replace(/^\S+\s*/, "")} is rising · up to ${nx.max_el}°` };
  return { k: "wait", chip: `next pass in ${rx2Cd(nx.aos - now)}`, title: "READY – waiting for the next pass",
    sub: `${nx.name} at ${rx2Hm(nx.aos)}, up to ${nx.max_el}° – starts in ${rx2Cd(nx.aos - now)}. It switches itself on and off.` };
}

/* ---------- the strip under the tabs (both receivers, on every screen) ---------- */
function rxStripRender() {
  const el = rx2$("rxStrip"); if (!el || !S) return;
  let main = "Listening";
  if (S.scan === "scan") main = "Scanning the memory"; else if (S.scan === "search") main = "Searching for signals";
  else if (S.freq) main = `Tuned to ${fmtMHz(S.freq, 4)} MHz ${S.mode || ""}`;
  const run = SCH && SCH.running;
  if (run) main = `Recording: ${String(run.name).split(" pass")[0]} until ${run.until}`;
  const st = rx2State(), r = SCH && SCH.satrx;
  const dev2 = r && (r.using || (r.devices && r.devices[0] && r.devices[0].label)) || "";
  el.innerHTML = `<div class="rxs-chip on"><i class="rxs-dot ok"></i><div><small>RECEIVER 1</small><b>${esc(S.device || "–")}</b><span>${esc(main)}</span></div></div>
    <div class="rxs-chip rx2s ${st.k}" role="button" tabindex="0" data-go="rx2" title="Open the 2nd receiver screen"><i class="rxs-dot"></i><div><small>RECEIVER 2</small><b title="${esc(dev2)}">${esc(rx2Lbl(dev2) || (r && r.enabled ? "no dongle found" : "not in use"))}</b><span>${esc(st.chip)}</span></div></div>`;
  const go = el.querySelector("[data-go]");
  go.onclick = () => showTab("rx2");
  const b = rx2$("rx2Badge"); if (b) b.hidden = st.k !== "rec" && st.k !== "view";
}

/* ---------- manual RF gain (works while it is recording or in live view, and is remembered) ---------- */
const RX2G = { drag: false, sendT: 0, last: null };
function rx2GainSet(v, final) {
  const r = SCH && SCH.satrx; if (!r) return;
  v = Math.max(r.gain_min, Math.min(r.gain_max, Math.round(v * 2) / 2));
  rx2$("rx2GVal").textContent = v.toFixed(1) + " dB"; rx2$("rx2GRange").value = v;
  const now = Date.now();
  if (final || now - RX2G.sendT > 150) { RX2G.sendT = now; RX2G.last = v; send({ cmd: "satrx_gain", gain: v, final: !!final }); }
}
(function () {
  const rg = rx2$("rx2GRange"); if (!rg) return;
  rg.oninput = () => { RX2G.drag = true; rx2GainSet(+rg.value, false); };
  rg.onchange = () => { rx2GainSet(+rg.value, true); setTimeout(() => (RX2G.drag = false), 400); };
  rx2$("rx2GDn").onclick = () => rx2GainSet(+rg.value - 1, true);
  rx2$("rx2GUp").onclick = () => rx2GainSet(+rg.value + 1, true);
})();
function rx2GainShow() {
  const r = SCH && SCH.satrx, rg = rx2$("rx2GRange"); if (!r || !rg) return;
  rg.min = r.gain_min; rg.max = r.gain_max;
  const lv = RX2.live && RX2.live.on ? RX2.live : null;
  const g = lv && lv.gain != null ? Number(lv.gain) : r.gain;
  if (!RX2G.drag && document.activeElement !== rg) { rg.value = g; rx2$("rx2GVal").textContent = g.toFixed(1) + " dB"; }
  rx2$("rx2GHelp").textContent = r.gain_mode === "auto"
    ? "'Learn the best' is on (Settings): each pass starts from this number and the program tries a little lower and higher on later passes. Changing it here sets the new starting point."
    : "Turn it up until the background noise (the grass on the trace) just starts to rise, but stop before the waterfall turns bright all over or lots of false spikes appear – that means it is too high. There is no single right number - it depends on your antenna and dongle, so try a few steps over a pass and keep the one that gives the best SNR.";
}

/* ---------- the screen ---------- */
function rx2Render() {
  const r = SCH && SCH.satrx; if (!r || !rx2Visible()) return;
  const st = rx2State(), lv = (RX2.live && RX2.live.on !== undefined ? RX2.live : r.live) || { on: false };
  const ban = rx2$("rx2Banner"); ban.className = "rx2-banner " + st.k;
  rx2$("rx2Title").textContent = st.title; rx2$("rx2Sub").textContent = st.sub;
  const btn = [];
  if (!r.enabled) btn.push(`<button class="btn primary" data-a="on">Switch it on</button>`);
  btn.push(`<button class="btn" data-a="check" ${lv.on && !lv.preview ? "disabled" : ""}>Check it</button>`);
  btn.push(`<button class="btn" data-a="settings">⚙ Settings</button>`);
  const bb = rx2$("rx2Btns"); bb.innerHTML = btn.join("");
  bb.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    const a = b.dataset.a;
    if (a === "on") send({ cmd: "satrx_cfg", enabled: true });
    else if (a === "check") { send({ cmd: "satrx_check" }); toast("Checking the second receiver …"); }
    else showSettings("receivers", "satRxBox");
  }));

  const nx = (r.next || []).find((p) => p.los > Date.now() / 1000), lp = r.last;
  const now = Date.now() / 1000;
  const mode = r.gain_mode === "auto" ? "learning the best" : "fixed";
  const tile = (t, big, sm) => `<div class="rx2-tile"><small>${t}</small><b>${big}</b><span>${sm || ""}</span></div>`;
  rx2$("rx2Tiles").innerHTML =
    tile("DONGLE", esc(rx2Lbl(r.using || (r.devices && r.devices[0] && r.devices[0].label)) || "none found"), `gain ${r.gain} dB (${mode})${r.bias ? " · Bias-T on" : ""}`) +
    tile("NEXT PASS", nx ? esc(String(nx.name).replace(/ \(.*/, "")) : "–", nx ? `${rx2Hm(nx.aos)} · up to ${nx.max_el}° · in ${rx2Cd(Math.max(0, nx.aos - now))}` : "none soon") +
    tile("RIGHT NOW", lv.on ? (lv.snr == null ? "measuring …" : `${lv.snr} dB`) : "idle",
      lv.on ? (lv.preview ? "signal above the noise" : `${lv.el != null ? lv.el + "° up · " : ""}${lv.range ? lv.range + " km away" : "signal above the noise"}`) : "it only wakes up for a pass") +
    tile("LAST PASS", lp ? esc(String(lp.name || lp.sat).replace(/ \(.*/, "")) : "none yet", lp ? `${fmtLocal(lp.start)} · ${esc(lp.result)}${lp.snr != null ? " · best " + lp.snr + " dB" : ""}` : "nothing recorded on this one yet");

  // live view controls
  const key = [lv.on, lv.preview, RX2.preset, r.enabled, !!(r.devices || []).length].join("|");
  if (key !== RX2.ctlKey) {
    RX2.ctlKey = key;
    const pv = lv.on && lv.preview, busy = lv.on && !lv.preview;
    rx2$("rx2Ctl").innerHTML = `<label class="small">Look at <select id="rx2Pre" ${lv.on ? "disabled" : ""}>${RX2_PRESETS.map((p) => `<option value="${p.id}" ${p.id === RX2.preset ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select></label>
      <input class="inp" id="rx2Cus" placeholder="MHz" style="width:6.5em" value="${esc(RX2.custom)}" ${RX2.preset === "custom" && !lv.on ? "" : "hidden"}>
      ${pv ? `<button class="btn danger" id="rx2Go">■ Stop live view</button>` : `<button class="btn primary" id="rx2Go" ${busy || !r.enabled || !(r.devices || []).length ? "disabled" : ""}>▶ Watch live (90 s)</button>`}
      <button class="btn sm" id="rx2Reset" title="Forget the best reading so far">Reset best</button>
      <span class="muted small">${busy ? "A pass is being recorded – this is its live view." : "Opens the dongle for 90 seconds, nothing is recorded. A real pass always takes over."}</span>`;
    const sel = rx2$("rx2Pre"); if (sel) sel.onchange = () => { RX2.preset = sel.value; RX2.ctlKey = ""; rx2Render(); };
    const cu = rx2$("rx2Cus"); if (cu) cu.oninput = () => (RX2.custom = cu.value);
    rx2$("rx2Reset").onclick = () => { RX2.best = null; rx2Meter(); };
    rx2$("rx2Go").onclick = () => {
      if (pv) { send({ cmd: "satrx_view", on: false }); return; }
      const p = RX2_PRESETS.find((x) => x.id === RX2.preset) || RX2_PRESETS[0];
      let f = p.f;
      if (p.id === "custom") { f = parseFloat(String(RX2.custom).replace(",", ".")) * 1e6; if (!(f >= 30e6 && f <= 1700e6)) { toast("Type a frequency in MHz, e.g. 137.9", "warn"); return; } }
      RX2.best = null; send({ cmd: "satrx_view", on: true, freq: Math.round(f), bw: p.bw });
    };
  }
  const idle = rx2$("rx2Idle");
  idle.hidden = !!lv.on;
  idle.innerHTML = lv.on ? "" : `<b>Nothing to show right now</b><span>${st.k === "wait" ? "This lights up by itself when a pass starts, or press “Watch live” to look at it now." : st.k === "off" ? "Switch the second receiver on first." : "Fix the problem above, then press “Check it”."}</span>`;
  rx2$("rx2LiveNote").textContent = lv.on ? `${fmtMHz(lv.freq, 4)} MHz · gain ${lv.gain != null ? Number(lv.gain).toFixed(1) : "–"} dB` : "";
  rx2$("rx2Hint").textContent = lv.on && lv.freq < 136e6
    ? "Aircraft messages are short bursts, so the bar jumps only while a plane is sending – watch the “best so far”. Turn the antenna a little, wait a minute, and compare. Reset between tries."
    : "The red box marks where a Meteor-M signal appears (a soft hump about 120 kHz wide). It drifts a little as the satellite passes (Doppler).";
  rx2GainShow();
  rx2Meter();
  rx2Pics(lv);
  rx2Passes();
  rx2LogRender();
}

function rx2Meter() {
  const lv = RX2.live; if (!rx2Visible()) return;
  const snr = lv && lv.on ? lv.snr : null;
  if (snr != null && (RX2.best == null || snr > RX2.best)) RX2.best = snr;
  const fill = rx2$("rx2Fill"), txt = rx2$("rx2SnrTxt");
  const pct = snr == null ? 0 : Math.max(0, Math.min(100, snr / 15 * 100));
  fill.style.width = pct + "%";
  fill.className = snr == null ? "" : snr < 1.5 ? "lo" : snr < 4 ? "mid" : "hi";
  const word = snr == null ? (lv && lv.on ? "measuring …" : "no reading – the receiver is idle") : snr < 1.5 ? "no signal" : snr < 3 ? "very faint" : snr < 5 ? "weak" : snr < 8 ? "good" : "strong";
  txt.innerHTML = `<b>${snr == null ? "–" : snr.toFixed(1)} dB</b> ${word}${RX2.best != null ? ` <span class="muted">· best so far ${RX2.best.toFixed(1)} dB</span>` : ""}`;
  const c = rx2$("rx2Spark"), dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height);
  const hist = (lv && lv.hist) || []; if (hist.length < 2) return;
  g.strokeStyle = getComputedStyle(document.body).getPropertyValue("--scope-line") || "#6cf"; g.lineWidth = 1.5 * dpr; g.beginPath();
  hist.forEach((v, i) => { const x = i / (hist.length - 1) * c.width, y = c.height - Math.max(0, Math.min(1, v / 15)) * (c.height - 4 * dpr) - 2 * dpr; i ? g.lineTo(x, y) : g.moveTo(x, y); });
  g.stroke();
}

function rx2Pics(lv) {
  const el = rx2$("rx2Pics"), rec = (lv && lv.recent) || (SCH.satrx && SCH.satrx.live && SCH.satrx.live.recent) || [];
  const sig = rec.join("|"); if (el.dataset.sig === sig) return; el.dataset.sig = sig;
  el.innerHTML = rec.length ? rec.map((f) => `<img loading="lazy" src="${imgUrl(f)}" data-f="${esc(f)}" title="${esc(f)}">`).join("")
    : `<span class="muted small">No pictures from this receiver since AndySDR started. Pictures from every receiver are collected in WEATHER IMAGES.</span>`;
  el.querySelectorAll("img").forEach((im) => (im.onclick = () => {
    const v = document.createElement("div"); v.className = "imgview";
    v.innerHTML = `<div class="imgbar"><span>${esc(im.dataset.f)}</span><span class="muted small">click anywhere to close</span></div><img src="${im.src}">`;
    v.onclick = () => v.remove(); document.body.appendChild(v);
  }));
}
function rx2Passes() {
  const rows = ((SCH && SCH.pass_log) || []).filter((x) => x.rx === "rx2").slice(0, 8);
  rx2$("rx2Passes").querySelector("tbody").innerHTML = rows.length ? rows.map((r) =>
    `<tr><td>${fmtLocal(r.start)}</td><td>${esc(r.name || r.sat)}</td><td>${r.max_el == null ? "–" : Math.round(r.max_el) + "°"}</td><td>${r.gain == null ? "–" : r.gain}</td><td>${r.snr == null ? "–" : r.snr + " dB"}</td><td${r.images > 0 ? ' style="color:var(--ok,#4ddb7a)"' : ""}>${esc(r.result)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">No pass has been recorded on this receiver yet.</td></tr>`;
}
function rx2LogRender() {
  rx2$("rx2Log").innerHTML = RX2.log.length ? RX2.log.map((e) => `<li class="${e.level || ""}"><span class="muted">${new Date(e.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span> ${esc(e.text)}</li>`).join("")
    : `<li class="muted">Nothing since the program started.</li>`;
}

/* ---------- live spectrum + waterfall (frames arrive only while the dongle is working) ---------- */
function rx2OnSpec(center, fs, freq, data) {
  RX2.spec = { center, fs, freq, data };
  if (!rx2Visible()) return;
  const ts = performance.now(); if (ts - RX2.lastDraw < 90) return; RX2.lastDraw = ts;
  rx2DrawScope();
}
function rx2DrawScope() {
  const sp = RX2.spec, c = rx2$("rx2Spec"), wc = rx2$("rx2Wf"); if (!sp || !c) return;
  const dpr = window.devicePixelRatio || 1, W = Math.floor(c.clientWidth * dpr), H = Math.floor(c.clientHeight * dpr), WH = Math.floor(wc.clientHeight * dpr);
  if (!W) return;
  if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  if (wc.width !== W || wc.height !== WH) { wc.width = W; wc.height = WH; }
  const { center, fs, freq, data } = sp, n = data.length, span = 300e3, f0 = freq - span / 2, binHz = fs / n, fLo = center - fs / 2;
  const col = new Float32Array(W);
  for (let x = 0; x < W; x++) {
    const ba = (f0 + x / W * span - fLo) / binHz, bb = (f0 + (x + 1) / W * span - fLo) / binHz;
    let m = 0; for (let i = Math.max(0, Math.floor(ba)); i <= Math.min(n - 1, Math.max(Math.floor(ba), Math.floor(bb))); i++) if (data[i] > m) m = data[i];
    col[x] = -130 + m / 255 * 120;
  }
  const sorted = Array.from(col).sort((a, b) => a - b), floor = sorted[Math.floor(W / 2)];
  RX2.lo = RX2.lo == null ? floor - 6 : RX2.lo * 0.9 + (floor - 6) * 0.1;
  const lo = RX2.lo, hi = lo + 45, g = c.getContext("2d");
  g.fillStyle = "#07090b"; g.fillRect(0, 0, W, H);
  const bw = (RX2.live && RX2.live.bw) || 60e3, xa = (freq - bw - f0) / span * W, xb = (freq + bw - f0) / span * W;
  g.fillStyle = "rgba(255,60,60,0.14)"; g.fillRect(xa, 0, xb - xa, H);
  g.strokeStyle = "rgba(255,80,80,0.8)"; g.lineWidth = dpr; g.strokeRect(xa, 1, xb - xa, H - 2);
  g.fillStyle = "rgba(255,255,255,0.5)"; g.font = `${11 * dpr}px Consolas, monospace`;
  for (let k = -1; k <= 1; k++) { const f = freq + k * 100e3, x = (f - f0) / span * W; g.fillText(fmtMHz(f, 3), Math.min(W - 60 * dpr, Math.max(2, x - 20 * dpr)), H - 4); }
  const y = (v) => H - Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * H;
  g.beginPath(); g.moveTo(0, H); for (let x = 0; x < W; x++) g.lineTo(x, y(col[x])); g.lineTo(W, H); g.closePath();
  g.fillStyle = getComputedStyle(document.body).getPropertyValue("--scope-fill") || "rgba(80,160,255,.3)"; g.fill();
  g.beginPath(); for (let x = 0; x < W; x++) x ? g.lineTo(x, y(col[x])) : g.moveTo(x, y(col[x]));
  g.strokeStyle = getComputedStyle(document.body).getPropertyValue("--scope-line") || "#6cf"; g.lineWidth = 1.2 * dpr; g.stroke();
  const w = wc.getContext("2d"), k = Math.max(1, Math.round(dpr));
  w.drawImage(wc, 0, 0, W, WH - k, 0, k, W, WH - k);
  const row = w.createImageData(W, 1);
  for (let x = 0; x < W; x++) {
    const p = Math.max(0, Math.min(255, Math.round((col[x] - lo) / (hi - lo) * 255))), cc = (typeof palette !== "undefined" && palette[p]) || [p, p, p];
    row.data[x * 4] = cc[0]; row.data[x * 4 + 1] = cc[1]; row.data[x * 4 + 2] = cc[2]; row.data[x * 4 + 3] = 255;
  }
  for (let j = 0; j < k; j++) w.putImageData(row, 0, j);
}

/* ---------- hooks used by app.js ---------- */
function rx2OnLive(d) {
  const was = RX2.live && RX2.live.on;
  RX2.live = d;
  if (!d.on && was) { RX2.lo = null; const c = rx2$("rx2Wf"); if (c) c.getContext("2d").clearRect(0, 0, c.width, c.height); }
  if (d.on && !was) { RX2.ctlKey = ""; }
  if (rx2Visible()) { if (d.on !== was) rx2Render(); else { rx2Meter(); const st = rx2State(); rx2$("rx2Title").textContent = st.title; rx2$("rx2Sub").textContent = st.sub; rx2$("rx2LiveNote").textContent = d.on ? `${fmtMHz(d.freq, 4)} MHz · gain ${d.gain != null ? Number(d.gain).toFixed(1) : "–"} dB` : ""; } }
  rxStripRender();
}
function rx2OnEvent(d) {
  if (d && (d.rx2 || /^Second receiver/.test(d.text || ""))) { RX2.log.unshift({ t: Date.now(), text: d.text, level: d.level }); RX2.log.length = Math.min(RX2.log.length, 12); if (rx2Visible()) rx2LogRender(); }
}
function rx2OnSchedule() { RX2.ctlKey = ""; rxStripRender(); rx2Render(); }
rx2$("rx2AllPics").onclick = () => showTab("img");
setInterval(() => { try { rxStripRender(); if (rx2Visible()) { RX2.ctlKey = RX2.ctlKey; rx2Render(); } } catch (e) { /* the screen must never break the app */ } }, 1000);
