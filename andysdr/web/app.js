/* AndySDR front panel – works locally (desktop window) and remotely (any browser). */
"use strict";
const $ = (id) => document.getElementById(id);
const fmtMHz = (hz, d = 5) => (hz / 1e6).toFixed(d);

const MODES = ["NFM", "FM", "AM", "WFM", "USB", "LSB", "CW", "DIG", "DIG25"];
const MODE_BW = { NFM: 6000, FM: 8500, WFM: 100000, AM: 4500, USB: 1500, LSB: 1500, CW: 250, DIG: 6250, DIG25: 12500 };
const STEPS = [100, 500, 1000, 5000, 6250, 8330, 10000, 12500, 25000, 50000, 100000];
const THEME_TEXT = {
  pro2045: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  ubcd260: ["DIGITAL BASE/MOBILE · DMR · P25 · NXDN", "SDR DIGITAL SCANNER"],
  trx2: ["DIGITAL TRUNKING · DMR · NXDN · P25", "SDR SCANNER-RECEIVER"],
};
const UK_PRESETS = [
  ["Airband", 118, 137, 8330, "AM"], ["2m Amateur", 144, 146, 12500, "NFM"],
  ["70cm Amateur", 430, 440, 12500, "NFM"], ["PMR446", 446.0, 446.2, 6250, "NFM"],
  ["Marine VHF", 156, 162.025, 25000, "NFM"], ["CB 27/81", 27.60125, 27.99125, 10000, "NFM"],
  ["VHF business", 163, 174, 12500, "NFM"], ["UHF business", 453, 470, 12500, "NFM"],
  ["FM broadcast", 87.5, 108, 100000, "WFM"], ["Weather sats", 137, 138, 25000, "DIG25"],
  ["Pagers", 153, 154, 12500, "DIG"],
];

let ws = null, token = sessionStorage.getItem("andysdr_token") || "", isLocal = true;
let S = {};           // radio state
let MEM = null;       // memory banks
let DEV = {};         // device info
let curBank = 0;
let entry = "";       // keypad entry
let bankMode = false;
let decoders = [];
let spec = null;      // last spectrum frame
let SCH = null;       // scheduler status

/* ======================================================= connection */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 2000); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") { onJSON(JSON.parse(ev.data)); return; }
    const dv = new DataView(ev.data);
    const t = dv.getUint8(0);
    if (t === 1) onSpectrum(dv.getFloat64(1, true), dv.getFloat64(9, true), dv.getFloat64(17, true), new Uint8Array(ev.data, 25));
    else if (t === 2) player.push(new Int16Array(ev.data.slice(1)));
  };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function setConn(on) {
  $("conn").textContent = on ? "CONNECTED" : "OFFLINE";
  $("conn").className = "pill " + (on ? "pill-on" : "pill-off");
}

function onJSON(m) {
  const d = m.data;
  switch (m.type) {
    case "auth_required":
      $("loginModal").hidden = false;
      if (token) { send({ cmd: "auth", password: token }); }
      setTimeout(() => $("loginPass").focus(), 50);
      break;
    case "auth_failed":
      token = ""; sessionStorage.removeItem("andysdr_token");
      $("loginErr").textContent = "Wrong password";
      break;
    case "hello": onHello(d); break;
    case "state": S = d; renderState(); break;
    case "memory": MEM = d; renderMemory(); break;
    case "activity": renderActivity(d); break;
    case "device": DEV = d; renderDevice(); break;
    case "devices": renderDevices(d); break;
    case "decoder": decLine(d.text, d.t); break;
    case "decoders": decoders = d; renderDecoders(); break;
    case "image": addImage(d.file); break;
    case "signal": S.signal = d; renderSignal(); break;
    case "schedule": SCH = d; renderSchedule(); break;
    case "images": renderImages(d); break;
    case "event": toast(d.text, d.level); if (d.level === "error") decLine("! " + d.text); break;
    case "import_result":
      $("csvResult").textContent = `Imported ${d.added} channels` + (d.talkgroups ? `, ${d.talkgroups} talkgroups` : "") +
        (d.skipped ? `, skipped ${d.skipped} rows` : "") + (d.overflow ? `, ${d.overflow} did not fit` : "");
      toast($("csvResult").textContent);
      break;
  }
}

function onHello(h) {
  $("loginModal").hidden = true;
  if (h.token) { token = h.token; sessionStorage.setItem("andysdr_token", token); }
  isLocal = h.local;
  document.querySelectorAll(".local-only").forEach((e) => (e.style.display = isLocal ? "" : "none"));
  S = h.state; MEM = h.memory; DEV = h.device; decoders = h.decoders;
  renderDevices(h.devices); renderDevice(); renderMemory(); renderActivity(h.activity);
  renderDecoders(); renderImages(h.images); renderState();
  if (h.schedule) { SCH = h.schedule; renderSchedule(); }
  const st = h.settings || {};
  $("setDelay").value = st.scan_delay; $("setDwell").value = st.scan_dwell;
  $("setDeemph").value = String(st.deemphasis_us || 50);
  $("reqLocal").checked = !!st.require_password_local;
  if (!localStorage.getItem("andysdr_theme") && st.theme) setTheme(st.theme, false);
  const port = h.server.port;
  $("netAddrs").innerHTML = (h.server.addresses.length ? h.server.addresses : [location.hostname])
    .map((a) => `<span>http://${a}:${port}</span>`).join("") + `<span class="small muted">or http://${h.server.host}:${port}</span>`;
  $("netPass").textContent = h.server.password || "(shown on the main PC only)";
  $("csvExport").href = `/api/export.csv?token=${encodeURIComponent(token)}`;
  $("sStart").value = fmtMHz(S.search.start, 4); $("sStop").value = fmtMHz(S.search.stop, 4);
  $("sStep").value = String(S.search.step); $("sThr").value = S.search.threshold; $("sThrO").textContent = S.search.threshold + " dB";
  document.querySelector(`input[name=smode][value=${S.search.mode}]`).checked = true;
}

/* ======================================================= 7-segment LCD */
const SEGMAP = {
  "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc",
  "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "", "E": "afged", "r": "eg", "o": "cdeg", "P": "abefg",
  "L": "fed", "C": "afed", "H": "fbgec", "n": "ceg", "_": "d", "A": "abcefg", "S": "afgcd",
};
function hseg(y) { return `9,${y} 12,${y - 3} 28,${y - 3} 31,${y} 28,${y + 3} 12,${y + 3}`; }
function vseg(x, a, b) { return `${x},${a} ${x + 3},${a + 3} ${x + 3},${b - 3} ${x},${b} ${x - 3},${b - 3} ${x - 3},${a + 3}`; }
const SEGS = { a: hseg(5), g: hseg(35), d: hseg(65), f: vseg(7, 7, 33), b: vseg(33, 7, 33), e: vseg(7, 37, 63), c: vseg(33, 37, 63) };
function segDigit(ch, dp, h) {
  const on = SEGMAP[ch] ?? "";
  let s = `<svg class="seg" viewBox="0 0 44 70" height="${h}" width="${h * 44 / 70}"><g transform="skewX(-7) translate(5,0)">`;
  for (const k in SEGS) s += `<polygon points="${SEGS[k]}" class="${on.includes(k) ? "s-on" : "s-off"}"/>`;
  s += `<circle cx="39" cy="65" r="3.2" class="${dp ? "s-on" : "s-off"}"/></g></svg>`;
  return s;
}
function segText(txt, len, h) {
  const out = [];
  for (let i = 0; i < txt.length; i++) {
    const c = txt[i];
    if (c === ".") { if (out.length) out[out.length - 1].dp = true; continue; }
    out.push({ c, dp: false });
  }
  while (out.length < len) out.unshift({ c: " ", dp: false });
  return out.slice(-len).map((o) => segDigit(o.c, o.dp, h)).join("");
}

function renderLCD() {
  let ftxt;
  if (entry) ftxt = entry.length > 10 ? entry.slice(-10) : entry;
  else ftxt = fmtMHz(S.freq || 0, 5).replace(/^0+(?=\d)/, "");
  $("lcdFreq").innerHTML = segText(ftxt, 9, 84);
  $("lcdFreqTxt").textContent = entry ? entry + "_" : fmtMHz(S.freq || 0, 4);
  const ch = S.channel ? String(S.channel.bank * 100 + S.channel.ch).padStart(3, "0") : "---";
  $("lcdCh").innerHTML = segText(ch, 3, 30);
  $("lcdMode").textContent = S.mode || "";
  const st = S.step || 0;
  $("lcdStep").textContent = st >= 1000 ? (st / 1000).toString().replace(/\.0+$/, "") + "k" : st + "Hz";
  let tag = "";
  if (entry) tag = bankMode ? "BANK ? (0-9)" : "ENTER FREQ (E) OR CH (MAN)";
  else if (S.job) tag = "SCHEDULED: " + S.job.name.toUpperCase();
  else if (bankMode) tag = "PRESS 0-9 TO TOGGLE BANK";
  else if (S.channel) tag = S.channel.name || `BANK ${S.channel.bank} CH ${S.channel.ch}`;
  else if (S.scan === "search" || S.scan_prev === "search") tag = `SEARCH ${fmtMHz(S.search.start, 3)}-${fmtMHz(S.search.stop, 3)}`;
  else { const b = S.freq ? bandsAt(S.freq).filter((x) => !x.point)[0] : null; tag = b ? b.name.toUpperCase() : "MANUAL"; }
  $("lcdTag").textContent = tag;
  const flag = (id, on) => $(id).classList.toggle("on", !!on);
  flag("fScan", S.scan === "scan" || (S.scan === "hold" && S.scan_prev === "scan"));
  flag("fSrch", S.scan === "search" || (S.scan === "hold" && S.scan_prev === "search"));
  flag("fHold", S.scan === "hold");
  flag("fMan", S.scan === "manual");
  flag("fPri", !!S.priority);
  flag("fLo", S.channel && S.channel.lockout);
  flag("fAgc", S.agc);
  flag("fRec", S.recording);
  flag("fDec", !!S.decoder);
  const chEntry = S.channel && MEM ? MEM.banks[S.channel.bank].channels[S.channel.ch] : null;
  flag("fDly", chEntry ? chEntry.delay !== false : true);
  // banks row
  if (MEM) {
    $("lcdBanks").innerHTML = Array.from({ length: 10 }, (_, b) => {
      const bk = MEM.banks[b];
      const cls = [(bk.enabled !== false && Object.keys(bk.channels).length) ? "on" : "",
        S.channel && S.channel.bank === b ? "cur" : ""].join(" ");
      return `<span class="${cls}">${b}</span>`;
    }).join("");
  }
  // S-meter: 24 bars from -110 to -20 dBFS
  const bars = 24, lo = -110, hi = -20;
  const lit = Math.round(Math.max(0, Math.min(1, (S.level - lo) / (hi - lo))) * bars);
  const sqi = S.squelch <= -119 ? -1 : Math.round(Math.max(0, Math.min(1, (S.squelch - lo) / (hi - lo))) * bars);
  $("smeter").innerHTML = Array.from({ length: bars }, (_, i) =>
    `<i class="${i < lit ? "on" : ""} ${i === sqi ? "sq" : ""}"></i>`).join("");
  $("lcdDb").textContent = (S.level ?? -120).toFixed(0).padStart(4, " ");
}

function renderState() {
  renderLCD();
  renderVfo();
  renderSignal();
  $("autoId").checked = $("autoId2").checked = S.auto_identify !== false;
  $("devname").textContent = S.device ? `· ${S.device}` : "";
  $("listeners").textContent = S.listeners ? `· ${S.listeners} connected` : "";
  if (!knobs.sq.dragging) knobs.sq.set(S.squelch, false);
  if (!knobs.gain.dragging) knobs.gain.set(S.gain, false);
  knobs.gain.render();
  $("agcChk").checked = !!S.agc;
  document.querySelectorAll("[data-k=scan]").forEach((b) => b.classList.toggle("on", S.scan === "scan"));
  document.querySelectorAll("[data-k=srch]").forEach((b) => b.classList.toggle("on", S.scan === "search"));
  document.querySelectorAll("[data-k=rec]").forEach((b) => b.classList.toggle("on", S.recording));
  document.querySelectorAll("#decBtns .btn").forEach((b) => b.classList.toggle("on", b.dataset.id === S.decoder));
  highlightMemRow();
}

/* ======================================================= knobs */
class Knob {
  constructor(el, out, o) {
    Object.assign(this, { el, out, min: o.min, max: o.max, step: o.step || 1, def: o.def, fmt: o.fmt, onChange: o.onChange });
    this.value = o.value ?? o.def; this.dragging = false;
    el.tabIndex = 0;
    let y0 = 0, v0 = 0;
    el.addEventListener("pointerdown", (e) => { if (explainOn) return; this.dragging = true; y0 = e.clientY; v0 = this.value; el.setPointerCapture(e.pointerId); });
    el.addEventListener("pointermove", (e) => {
      if (!this.dragging) return;
      const range = this.max - this.min;
      this.set(v0 + (y0 - e.clientY) / 180 * range, true);
    });
    const end = () => { this.dragging = false; };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
    el.addEventListener("wheel", (e) => { e.preventDefault(); this.set(this.value + (e.deltaY < 0 ? 1 : -1) * this.step, true); }, { passive: false });
    el.addEventListener("keydown", (e) => {
      if (e.key === "ArrowUp" || e.key === "ArrowRight") { this.set(this.value + this.step, true); e.preventDefault(); e.stopPropagation(); }
      if (e.key === "ArrowDown" || e.key === "ArrowLeft") { this.set(this.value - this.step, true); e.preventDefault(); e.stopPropagation(); }
    });
    el.addEventListener("dblclick", () => this.set(this.def, true));
    this.render();
  }
  set(v, user) {
    v = Math.max(this.min, Math.min(this.max, Math.round(v / this.step) * this.step));
    if (v === this.value && !user) { this.render(); return; }
    this.value = v; this.render();
    if (user && this.onChange) { clearTimeout(this.t); this.t = setTimeout(() => this.onChange(v), 60); }
  }
  render() {
    const p = (this.value - this.min) / ((this.max - this.min) || 1);
    this.el.querySelector(".knob-cap").style.setProperty("--rot", `${-135 + p * 270}deg`);
    this.out.textContent = this.fmt(this.value);
  }
}
const knobs = {
  vol: new Knob($("kVol"), $("oVol"), { min: 0, max: 100, step: 2, def: 70, value: +(localStorage.getItem("andysdr_vol") || 70),
    fmt: (v) => v + "%", onChange: (v) => { localStorage.setItem("andysdr_vol", v); player.setVolume(v / 100); } }),
  sq: new Knob($("kSq"), $("oSq"), { min: -120, max: -10, step: 1, def: -60, value: -60,
    fmt: (v) => (v <= -119 ? "OPEN" : v + "dB"), onChange: (v) => send({ cmd: "squelch", value: v }) }),
  gain: new Knob($("kGain"), $("oGain"), { min: 0, max: 50, step: 1, def: 30, value: 30,
    fmt: (v) => (S.agc ? "AUTO" : v.toFixed(0) + (DEV.kind === "sdrplay" || /sdrplay/.test(DEV.key || "") ? "%" : "dB")),
    onChange: (v) => { if (S.agc) send({ cmd: "agc", on: false }); send({ cmd: "gain", value: v }); } }),
};

/* ======================================================= keypad */
function key(k) {
  if (/^[0-9]$/.test(k)) {
    if (bankMode) { toggleBank(+k); bankMode = false; renderLCD(); return; }
    entry = (entry + k).slice(0, 12); renderLCD(); return;
  }
  switch (k) {
    case ".": if (!entry.includes(".")) entry = (entry || "0") + "."; break;
    case "clr": entry = ""; bankMode = false; break;
    case "e":
      if (entry) {
        const v = parseFloat(entry);
        if (v > 0) send({ cmd: "tune", freq: Math.round(v * 1e6) });
        entry = "";
      }
      break;
    case "man":
      if (entry && !entry.includes(".")) { const n = parseInt(entry, 10); send({ cmd: "recall", bank: Math.floor(n / 100) % 10, ch: n % 100 }); }
      else send({ cmd: "manual" });
      entry = ""; break;
    case "scan": entry = ""; send({ cmd: "scan" }); break;
    case "srch": entry = ""; startSearch(); break;
    case "pgm": {
      if (entry && !entry.includes(".")) { const n = parseInt(entry, 10); send({ cmd: "store", bank: Math.floor(n / 100) % 10, ch: n % 100 }); }
      else send({ cmd: "store", bank: curBank });
      entry = ""; break;
    }
    case "lo": send({ cmd: "lockout" }); break;
    case "pri":
      if (S.priority) send({ cmd: "priority" });
      else if (S.channel) send({ cmd: "priority", bank: S.channel.bank, ch: S.channel.ch });
      else toast("Recall a memory channel first, then press PRI", "warn");
      break;
    case "mode": send({ cmd: "mode", mode: MODES[(MODES.indexOf(S.mode) + 1) % MODES.length] }); break;
    case "step": send({ cmd: "stepsize", hz: STEPS[(STEPS.indexOf(S.step) + 1) % STEPS.length] }); break;
    case "bank":
      if (entry && /^\d$/.test(entry)) { toggleBank(+entry); entry = ""; }
      else bankMode = !bankMode;
      break;
    case "dly":
      if (S.channel && MEM) {
        const c = MEM.banks[S.channel.bank].channels[S.channel.ch];
        send({ cmd: "mem_update", bank: S.channel.bank, ch: S.channel.ch, fields: { delay: !(c.delay !== false) } });
      } else toast("DLY applies to memory channels", "warn");
      break;
    case "rec": send({ cmd: "record", on: !S.recording }); break;
    case "dec": showTab("dec"); break;
    case "up": send({ cmd: "step", dir: 1 }); break;
    case "down": send({ cmd: "step", dir: -1 }); break;
  }
  renderLCD();
}
function toggleBank(b) {
  if (!MEM) return;
  const on = MEM.banks[b].enabled !== false;
  send({ cmd: "mem_bank", bank: b, fields: { enabled: !on } });
  toast(`Bank ${b} ${on ? "removed from" : "added to"} scan`);
}
document.querySelectorAll(".radio [data-k]").forEach((b) => b.addEventListener("click", () => { if (!explainOn) key(b.dataset.k); }));

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || !$("loginModal").hidden) return;
  if (e.key === "Escape") { if (explainOn) setExplain(false); $("helpModal").hidden = true; key("clr"); return; }
  const map = { Enter: "e", ArrowUp: "up", ArrowDown: "down", Backspace: "clr", ".": ".", s: "scan", m: "man", p: "pgm", l: "lo" };
  if (/^[0-9]$/.test(e.key)) key(e.key);
  else if (e.key === "h" || e.key === "?") openHelp();
  else if (map[e.key]) { key(map[e.key]); e.preventDefault(); }
});

/* ======================================================= audio */
const player = {
  ctx: null, gain: null, node: null, ring: new Float32Array(48000 * 4), w: 0, r: 0, buffering: true, on: false, vol: knobs.vol.value / 100,
  start() {
    try { this.ctx = new AudioContext({ sampleRate: 48000 }); } catch (e) { this.ctx = new AudioContext(); }
    this.gain = this.ctx.createGain(); this.gain.gain.value = this.vol;
    this.node = this.ctx.createScriptProcessor(2048, 0, 1);
    this.node.onaudioprocess = (ev) => this.pull(ev.outputBuffer.getChannelData(0));
    this.node.connect(this.gain).connect(this.ctx.destination);
    this.on = true;
  },
  stop() { if (this.ctx) this.ctx.close(); this.ctx = null; this.on = false; },
  setVolume(v) { this.vol = v; if (this.gain) this.gain.gain.value = v * v * 1.6; },
  avail() { return (this.w - this.r + this.ring.length) % this.ring.length; },
  push(pcm) {
    if (!this.on) return;
    const ratio = this.ctx.sampleRate / 48000, N = this.ring.length;
    const n = Math.floor(pcm.length * ratio);
    for (let i = 0; i < n; i++) {
      this.ring[this.w] = pcm[Math.min(pcm.length - 1, Math.floor(i / ratio))] / 32768;
      this.w = (this.w + 1) % N;
    }
    if (this.avail() > this.ctx.sampleRate * 1.2) this.r = (this.w - Math.floor(this.ctx.sampleRate * 0.3) + N) % N; // too much lag
  },
  pull(out) {
    const N = this.ring.length;
    if (this.buffering && this.avail() < this.ctx.sampleRate * 0.15) { out.fill(0); return; }
    this.buffering = false;
    for (let i = 0; i < out.length; i++) {
      if (this.r === this.w) { out.fill(0, i); this.buffering = true; return; }
      out[i] = this.ring[this.r]; this.r = (this.r + 1) % N;
    }
  },
};
player.setVolume(knobs.vol.value / 100);
$("pwrBtn").addEventListener("click", () => {
  if (explainOn) return;
  if (player.on) player.stop(); else { player.start(); player.setVolume(knobs.vol.value / 100); }
  $("pwrLed").classList.toggle("on", player.on);
  send({ cmd: "audio", on: player.on && $("myAudio").checked });
});
$("myAudio").addEventListener("change", (e) => send({ cmd: "audio", on: e.target.checked && player.on }));

/* ======================================================= spectrum + waterfall */
const specC = $("spec"), wfC = $("wf");
const sctx = specC.getContext("2d"), wctx = wfC.getContext("2d");
let palette = [];
let autoLo = null;
function makePalette() {
  const stops = [[0, [0, 0, 8]], [0.25, [10, 30, 110]], [0.45, [0, 140, 200]], [0.62, [60, 210, 90]], [0.78, [250, 220, 40]], [0.9, [250, 110, 20]], [1, [255, 255, 255]]];
  palette = [];
  for (let i = 0; i < 256; i++) {
    const p = i / 255; let k = 0;
    while (k < stops.length - 2 && p > stops[k + 1][0]) k++;
    const [p0, c0] = stops[k], [p1, c1] = stops[k + 1], t = (p - p0) / (p1 - p0);
    palette.push(c0.map((v, j) => Math.round(v + (c1[j] - v) * t)));
  }
}
makePalette();
function sizeCanvases() {
  const dpr = window.devicePixelRatio || 1;
  for (const c of [specC, wfC, $("bandmap")]) {
    if (!c.clientWidth) continue;
    const w = Math.floor(c.clientWidth * dpr), h = Math.floor(c.clientHeight * dpr);
    if (c.width !== w || c.height !== h) {
      if (c === wfC && c.width) { const tmp = document.createElement("canvas"); tmp.width = c.width; tmp.height = c.height; tmp.getContext("2d").drawImage(c, 0, 0); c.width = w; c.height = h; wctx.drawImage(tmp, 0, 0, w, h); }
      else { c.width = w; c.height = h; }
    }
  }
}
window.addEventListener("resize", sizeCanvases);
sizeCanvases();

function onSpectrum(center, fs, freq, data) {
  spec = { center, fs, freq, data };
  const W = specC.width, H = specC.height, dpr = window.devicePixelRatio || 1;
  const n = data.length;
  if ($("scopeAuto").checked) {
    const sorted = Array.from(data).sort((a, b) => a - b);
    const floor = -130 + sorted[Math.floor(n / 2)] / 255 * 120;
    autoLo = autoLo === null ? floor - 12 : autoLo * 0.95 + (floor - 12) * 0.05;
    $("scopeLo").value = Math.round(autoLo); $("scopeHi").value = Math.round(autoLo + 70);
  }
  const lo = +$("scopeLo").value, hi = Math.max(lo + 10, +$("scopeHi").value);
  const col = new Float32Array(W);
  for (let x = 0; x < W; x++) {
    const a = Math.floor(x * n / W), b = Math.max(a + 1, Math.floor((x + 1) * n / W));
    let m = 0; for (let i = a; i < b; i++) if (data[i] > m) m = data[i];
    col[x] = -130 + m / 255 * 120;
  }
  const css = getComputedStyle(document.body);
  sctx.fillStyle = "#07090b"; sctx.fillRect(0, 0, W, H);
  // grid
  sctx.strokeStyle = "rgba(255,255,255,0.07)"; sctx.fillStyle = "rgba(255,255,255,0.45)";
  sctx.font = `${11 * dpr}px Consolas, monospace`; sctx.lineWidth = 1;
  for (let db = Math.ceil(lo / 10) * 10; db <= hi; db += 10) {
    const y = H - (db - lo) / (hi - lo) * H;
    sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(W, y); sctx.stroke();
    sctx.fillText(db + "", 4, y - 2);
  }
  const span = fs, f0 = center - span / 2;
  const nice = [10e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6, 2e6].find((s) => span / s <= Math.max(3, Math.min(12, W / (95 * dpr)))) || 2e6;
  for (let f = Math.ceil(f0 / nice) * nice; f < f0 + span; f += nice) {
    const x = (f - f0) / span * W;
    sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
    sctx.fillText(fmtMHz(f, nice < 1e5 ? 3 : 2), x + 3, H - 4);
  }
  // tuned channel
  const bw = MODE_BW[S.mode] || 6000;
  const xf = (freq - f0) / span * W, xb = bw / span * W;
  sctx.fillStyle = "rgba(255,60,60,0.16)"; sctx.fillRect(xf - xb, 0, xb * 2, H);
  // trace
  const y = (v) => H - (v - lo) / (hi - lo) * H;
  sctx.beginPath(); sctx.moveTo(0, H);
  for (let x = 0; x < W; x++) sctx.lineTo(x, y(col[x]));
  sctx.lineTo(W, H); sctx.closePath();
  sctx.fillStyle = css.getPropertyValue("--scope-fill"); sctx.fill();
  sctx.beginPath();
  for (let x = 0; x < W; x++) x ? sctx.lineTo(x, y(col[x])) : sctx.moveTo(x, y(col[x]));
  sctx.strokeStyle = css.getPropertyValue("--scope-line"); sctx.lineWidth = 1.2 * dpr; sctx.stroke();
  const stripH = drawBandStrip(sctx, f0, span, W, dpr);
  sctx.strokeStyle = "#ff4040"; sctx.lineWidth = 1.5 * dpr; sctx.beginPath(); sctx.moveTo(xf, stripH); sctx.lineTo(xf, H); sctx.stroke();
  drawVfoFlag(sctx, xf, stripH, W, freq, dpr);
  // waterfall
  const WH = wfC.height;
  wctx.drawImage(wfC, 0, 0, W, WH - 1, 0, 1, W, WH - 1);
  const row = wctx.createImageData(W, 1);
  for (let x = 0; x < W; x++) {
    const p = Math.max(0, Math.min(255, Math.round((col[x] - lo) / (hi - lo) * 255)));
    const c = palette[p];
    row.data[x * 4] = c[0]; row.data[x * 4 + 1] = c[1]; row.data[x * 4 + 2] = c[2]; row.data[x * 4 + 3] = 255;
  }
  wctx.putImageData(row, 0, 0);
  wctx.fillStyle = "rgba(255,64,64,0.8)"; wctx.fillRect(xf - dpr / 2, 0, dpr, 3);
  $("scopeInfo").textContent = `centre ${fmtMHz(center, 4)} MHz · span ${(fs / 1e6).toFixed(2)} MHz · tuned ${fmtMHz(freq, 5)}`;
  drawBandMap();
}
function scopeFreq(e, c) {
  if (!spec) return null;
  const r = c.getBoundingClientRect();
  const f = spec.center + ((e.clientX - r.left) / r.width - 0.5) * spec.fs;
  const st = S.step || 12500;
  return Math.round(f / st) * st;
}
["scopeLo", "scopeHi"].forEach((id) => $(id).addEventListener("input", () => ($("scopeAuto").checked = false)));
for (const c of [specC, wfC]) {
  c.addEventListener("click", (e) => {
    if (explainOn) return;
    const f = scopeFreq(e, c); if (!f) return;
    if (c === specC && bpSize) {     // a click on the band-plan strip also selects that section
      const r = c.getBoundingClientRect();
      if (e.clientY - r.top < BP_STRIP[bpSize]) {
        const b = bandsAt(f).filter((x) => !x.point && !bpHidden.has(x.cat))[0];
        if (b) { setPick(b); toast(`${b.name} · ${fmtRange(b)} – see UK BAND PLAN tab`); }
      }
    }
    send({ cmd: "tune", freq: f });
  });
  c.addEventListener("wheel", (e) => { e.preventDefault(); send({ cmd: "step", dir: e.deltaY < 0 ? 1 : -1 }); }, { passive: false });
  c.addEventListener("mousemove", (e) => {
    const f = scopeFreq(e, c); if (!f) return;
    const b = bandsAt(f).filter((x) => !x.point)[0];
    c.title = fmtMHz(f, 4) + " MHz" + (b ? " · " + b.name : "") + " – click to tune";
  });
}

/* ======================================================= UK band plan + VFO */
const BP_STRIP = [0, 12, 30, 44];          // band strip height on the spectrum (CSS px) for Off/Small/Medium/Large
const BP_MAP = [0, 22, 30, 42];            // overview band map height
const MAP_LO = 9e3, MAP_HI = 6.5e9;        // overview covers 9 kHz – 6.5 GHz on a log scale
let bpSize = +(localStorage.getItem("andysdr_bpsize") ?? 2);
let bpHidden = new Set();
try { bpHidden = new Set(JSON.parse(localStorage.getItem("andysdr_bphide") || "[]")); } catch (e) { /* first run */ }
const bandmapC = $("bandmap"), bmctx = bandmapC.getContext("2d");
let lastVfoKey = "";

function bpColor(b, a = 1) {
  const hex = (BANDPLAN_CATS[b.cat] || BANDPLAN_CATS.science).color;
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}
function fitText(ctx, t, w) {
  if (ctx.measureText(t).width <= w) return t;
  while (t.length > 3 && ctx.measureText(t + "…").width > w) t = t.slice(0, -1);
  return t.length > 3 ? t + "…" : "";
}
function drawBandStrip(ctx, f0, span, W, dpr) {
  if (!bpSize) return 0;
  const H = BP_STRIP[bpSize] * dpr, rows = bpSize === 1 ? 1 : 2, rh = H / rows, f1 = f0 + span;
  const X = (f) => (f - f0) / span * W;
  ctx.fillStyle = "rgba(7,9,11,0.82)"; ctx.fillRect(0, 0, W, H);
  ctx.font = `${(bpSize === 3 ? 11 : 10) * dpr}px Segoe UI, Arial, sans-serif`; ctx.textBaseline = "middle";
  for (const b of BANDPLAN) {
    if (b.point || b.stop < f0 || b.start > f1 || bpHidden.has(b.cat)) continue;
    const r = Math.min(b.level, rows - 1); if (b.level >= rows) { if (rows === 1) continue; }
    const x0 = Math.max(0, X(b.start)), x1 = Math.min(W, X(b.stop)), y0 = r * rh;
    ctx.fillStyle = bpColor(b, b.level ? 0.42 : 0.30); ctx.fillRect(x0, y0 + dpr, Math.max(dpr, x1 - x0), rh - 2 * dpr);
    ctx.fillStyle = bpColor(b, 1);
    if (X(b.start) >= 0) ctx.fillRect(X(b.start), y0 + dpr, dpr, rh - 2 * dpr);
    if (X(b.stop) <= W) ctx.fillRect(X(b.stop) - dpr, y0 + dpr, dpr, rh - 2 * dpr);
    const txt = fitText(ctx, b.name, x1 - x0 - 8 * dpr);
    if (txt) { ctx.fillStyle = "#f2f5f8"; ctx.fillText(txt, x0 + 4 * dpr, y0 + rh / 2); }
  }
  if (bpSize >= 2) {   // key single frequencies as small markers
    for (const b of BANDPLAN) {
      if (!b.point || b.start < f0 || b.start > f1 || bpHidden.has(b.cat)) continue;
      const x = X(b.start);
      ctx.fillStyle = bpColor(b, 1);
      ctx.beginPath(); ctx.moveTo(x, H - 1); ctx.lineTo(x - 4 * dpr, H - 7 * dpr); ctx.lineTo(x + 4 * dpr, H - 7 * dpr); ctx.fill();
      if (bpSize === 3) {
        ctx.save(); ctx.font = `${9 * dpr}px Segoe UI, Arial, sans-serif`; ctx.fillStyle = "rgba(255,255,255,.75)";
        ctx.textBaseline = "top"; ctx.fillText(b.name.split(" – ")[0].slice(0, 22), x + 5 * dpr, H + 2 * dpr); ctx.restore();
      }
    }
  }
  ctx.textBaseline = "alphabetic";
  return H;
}
function drawVfoFlag(ctx, xf, top, W, freq, dpr) {
  const t = "VFO " + fmtMHz(freq, 5);
  ctx.font = `bold ${10.5 * dpr}px Consolas, monospace`;
  const w = ctx.measureText(t).width + 10 * dpr, h = 16 * dpr;
  const x = Math.min(W - w, Math.max(0, xf - w / 2)), y = top + 3 * dpr + (bpSize === 3 ? 12 * dpr : 0);
  ctx.fillStyle = "#ff4040"; ctx.fillRect(x, y, w, h);
  ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(t, x + 5 * dpr, y + h / 2); ctx.textBaseline = "alphabetic";
}
const mapX = (f, W) => (Math.log10(Math.max(MAP_LO, f)) - Math.log10(MAP_LO)) / (Math.log10(MAP_HI) - Math.log10(MAP_LO)) * W;
const mapF = (x, W) => Math.pow(10, Math.log10(MAP_LO) + x / W * (Math.log10(MAP_HI) - Math.log10(MAP_LO)));
function drawBandMap() {
  if (!bpSize) return;
  const dpr = window.devicePixelRatio || 1, W = bandmapC.width, H = bandmapC.height;
  if (!W || !H) return;
  const ctx = bmctx, barTop = 2 * dpr, barH = H - 14 * dpr;
  ctx.fillStyle = "#07090b"; ctx.fillRect(0, 0, W, H);
  for (const b of BANDPLAN) {
    if (b.point || b.level || bpHidden.has(b.cat)) continue;
    const x0 = mapX(b.start, W), x1 = mapX(b.stop, W);
    ctx.fillStyle = bpColor(b, 0.85); ctx.fillRect(x0, barTop, Math.max(1, x1 - x0), barH);
  }
  for (const b of BANDPLAN) {   // sub-bands as a thin top line
    if (b.point || !b.level || bpHidden.has(b.cat)) continue;
    ctx.fillStyle = bpColor(b, 1); ctx.fillRect(mapX(b.start, W), barTop, Math.max(1, mapX(b.stop, W) - mapX(b.start, W)), 3 * dpr);
  }
  ctx.font = `${9.5 * dpr}px Consolas, monospace`; ctx.fillStyle = "rgba(255,255,255,.55)"; ctx.strokeStyle = "rgba(255,255,255,.25)";
  for (const [f, l] of [[1e4, "10k"], [1e5, "100k"], [1e6, "1M"], [3e6, "3M"], [1e7, "10M"], [3e7, "30M"], [1e8, "100M"], [3e8, "300M"], [1e9, "1G"], [3e9, "3G"]]) {
    const x = mapX(f, W);
    ctx.beginPath(); ctx.moveTo(x, barTop + barH); ctx.lineTo(x, barTop + barH + 3 * dpr); ctx.stroke();
    ctx.fillText(l, x + 2 * dpr, H - 2 * dpr);
  }
  if (spec) {   // what the spectrum is showing
    const a = mapX(spec.center - spec.fs / 2, W), b = mapX(spec.center + spec.fs / 2, W);
    ctx.fillStyle = "rgba(255,255,255,.22)"; ctx.fillRect(a, 0, Math.max(3 * dpr, b - a), barTop + barH);
    ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = dpr; ctx.strokeRect(a, 0.5, Math.max(3 * dpr, b - a), barTop + barH);
  }
  const f = S.freq || 0;
  if (f) {   // the VFO needle
    const x = mapX(f, W);
    ctx.fillStyle = "#ff4040"; ctx.fillRect(x - dpr, 0, 2 * dpr, barTop + barH);
    ctx.beginPath(); ctx.moveTo(x, barTop + barH - 2 * dpr); ctx.lineTo(x - 5 * dpr, H - 2 * dpr); ctx.lineTo(x + 5 * dpr, H - 2 * dpr); ctx.fill();
    const t = fmtMHz(f, 4) + " MHz";
    ctx.font = `bold ${10 * dpr}px Consolas, monospace`;
    const tw = ctx.measureText(t).width + 8 * dpr, tx = Math.min(W - tw, Math.max(0, x + 8 * dpr));
    ctx.fillStyle = "rgba(255,64,64,.92)"; ctx.fillRect(tx, H - 13 * dpr, tw, 12 * dpr);
    ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(t, tx + 4 * dpr, H - 7 * dpr); ctx.textBaseline = "alphabetic";
  }
}
function mapBandAt(e) {
  const r = bandmapC.getBoundingClientRect();
  const f = mapF((e.clientX - r.left) / r.width * bandmapC.width, bandmapC.width);
  return { f, b: bandsAt(f).filter((x) => !x.point)[0] };
}
bandmapC.addEventListener("mousemove", (e) => {
  const { f, b } = mapBandAt(e);
  if (b && bpHidden.has(b.cat)) { bandmapC.title = ""; return; }
  bandmapC.title = (f >= 1e6 ? fmtMHz(f, 3) + " MHz" : (f / 1e3).toFixed(1) + " kHz") + (b ? ` · ${b.name} (${fmtMHz(b.start, 3)}–${fmtMHz(b.stop, 3)} MHz) – click to go there` : "");
});
bandmapC.addEventListener("click", (e) => {
  if (explainOn) return;
  const { f, b } = mapBandAt(e);
  if (b) tuneBand(b, Math.min(b.stop, Math.max(b.start, f)));
  else send({ cmd: "tune", freq: Math.round(f / 1000) * 1000 });
});
function tuneBand(b, f) {
  const st = b.step || 12500;
  f = f ?? (b.point ? b.start : b.start + (b.stop - b.start) / 2);
  f = b.point ? b.start : Math.min(b.stop, Math.max(b.start, Math.round(f / st) * st));
  send({ cmd: "tune", freq: f, mode: b.mode });
  if (STEPS.includes(b.step) && b.step !== S.step) send({ cmd: "stepsize", hz: b.step });
}
function searchBand(b) {
  $("sStart").value = (b.start / 1e6).toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
  $("sStop").value = (b.stop / 1e6).toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
  const st = String(b.step);
  if (![...$("sStep").options].some((o) => o.value === st)) $("sStep").insertAdjacentHTML("beforeend", `<option value="${st}">${b.step >= 1000 ? b.step / 1000 + " kHz" : b.step + " Hz"}</option>`);
  $("sStep").value = st; $("sMode").value = b.mode;
  showTab("act"); toast(`${b.name} loaded into Search – press ▶ Search`);
}
function setBpSize(n, save = true) {
  bpSize = Math.max(0, Math.min(3, n | 0));
  $("bpSize").value = String(bpSize);
  bandmapC.style.display = bpSize ? "" : "none";
  bandmapC.style.height = BP_MAP[bpSize] + "px";
  document.documentElement.style.setProperty("--bp-strip", BP_STRIP[bpSize] + "px");
  if (save) localStorage.setItem("andysdr_bpsize", bpSize);
  sizeCanvases(); drawBandMap();
}
$("bpSize").onchange = () => setBpSize(+$("bpSize").value);

function renderVfo() {
  const f = S.freq || 0;
  $("vfoFreq").textContent = fmtMHz(f, 5) + " MHz";
  const here = f ? bandsAt(f) : [];
  const key = f + "|" + S.mode + "|" + S.step;
  if (key === lastVfoKey) return;
  lastVfoKey = key;
  const main = here.filter((b) => !b.point).reverse();   // allocation › sub-band
  const pt = here.find((b) => b.point);
  $("vfoBand").innerHTML = main.length || pt
    ? main.map((b) => `<span class="bp-dot" style="background:${bpColor(b)}"></span>${esc(b.name)}`).join(' <span class="muted">›</span> ') +
      (pt ? ` <span class="muted">›</span> <b>${esc(pt.name)}</b>` : "")
    : '<span class="muted">Outside the UK band plan</span>';
  const best = pt || here.find((b) => !b.point);
  const hint = $("vfoHint");
  if (best && (best.mode !== S.mode || (STEPS.includes(best.step) && best.step !== S.step))) {
    hint.innerHTML = `usually ${best.mode} · ${best.step >= 1000 ? best.step / 1000 + " kHz" : best.step + " Hz"} <button class="btn sm" id="vfoUse">Use</button>`;
    $("vfoUse").onclick = () => { send({ cmd: "mode", mode: best.mode }); if (STEPS.includes(best.step)) send({ cmd: "stepsize", hz: best.step }); };
  } else hint.textContent = best && best.note ? best.note : "";
  drawBandMap();
  if ($("tab-bp").classList.contains("active")) markBpRows();
}

/* ----- band plan tab ----- */
$("bpCat").insertAdjacentHTML("beforeend", Object.entries(BANDPLAN_CATS).map(([k, c]) => `<option value="${k}">${esc(c.label)}</option>`).join(""));
$("bpLegend").innerHTML = Object.entries(BANDPLAN_CATS).map(([k, c]) =>
  `<button class="bp-leg" data-cat="${k}"><span class="bp-dot" style="background:${c.color}"></span>${esc(c.label)}</button>`).join("");
$("bpLegend").querySelectorAll("[data-cat]").forEach((b) => b.onclick = () => {
  const c = b.dataset.cat;
  if (bpHidden.has(c)) bpHidden.delete(c); else bpHidden.add(c);
  try { localStorage.setItem("andysdr_bphide", JSON.stringify([...bpHidden])); } catch (e) { /* private window */ }
  renderLegend(); drawBandMap();
});
function renderLegend() {
  $("bpLegend").querySelectorAll("[data-cat]").forEach((b) => {
    b.classList.toggle("off", bpHidden.has(b.dataset.cat));
    b.title = bpHidden.has(b.dataset.cat) ? "Hidden – tap to show on the spectrum" : "Shown – tap to hide from the spectrum";
  });
}
renderLegend();
function renderBandPlan() {
  const q = $("bpFind").value.trim().toLowerCase(), cat = $("bpCat").value, sub = $("bpSub").checked;
  const qf = parseFloat(q.replace(/\s*mhz$/, ""));
  const list = BANDPLAN.filter((b) => {
    if (cat && b.cat !== cat) return false;
    if (!sub && (b.level || b.point)) return false;
    if (!q) return true;
    if (/^[\d.]+(\s*mhz)?$/.test(q) && qf > 0) { const f = qf * 1e6; return b.point ? Math.abs(b.start - f) < 5000 : f >= b.start && f <= b.stop; }
    return (b.name + " " + b.note + " " + BANDPLAN_CATS[b.cat].label).toLowerCase().includes(q);
  });
  $("bpCount").textContent = `${list.length} of ${BANDPLAN.length} entries`;
  $("bpTable").querySelector("tbody").innerHTML = list.map((b) => {
    const i = BANDPLAN.indexOf(b);
    return `<tr data-i="${i}" class="${b.level || b.point ? "bp-sub" : ""}"><td><span class="bp-dot" style="background:${bpColor(b)}"></span></td>
      <td class="f" data-act="tune">${fmtMHz(b.start, b.start < 3e7 ? 4 : 3)}</td><td class="muted">${b.point ? "" : fmtMHz(b.stop, b.stop < 3e7 ? 4 : 3)}</td>
      <td>${esc(b.name)}</td><td>${b.mode}</td><td class="muted">${b.step >= 1e6 ? b.step / 1e6 + " MHz" : b.step >= 1000 ? b.step / 1000 + " kHz" : b.step + " Hz"}</td>
      <td class="muted small">${esc(b.note)}</td>
      <td><button class="btn sm" data-act="tune">Tune</button>${b.point ? "" : ' <button class="btn sm" data-act="search">Search</button>'}</td></tr>`;
  }).join("") || '<tr><td colspan="8" class="muted">Nothing matches.</td></tr>';
  $("bpTable").querySelectorAll("tr[data-i]").forEach((tr) => {
    const b = BANDPLAN[+tr.dataset.i];
    tr.querySelectorAll("[data-act=tune]").forEach((el) => el.onclick = () => tuneBand(b));
    tr.querySelectorAll("[data-act=search]").forEach((el) => el.onclick = () => searchBand(b));
    tr.children[3].onclick = () => setPick(b);
    tr.children[3].style.cursor = "pointer";
  });
  markBpRows();
}
function markBpRows() {
  const f = S.freq || 0;
  $("bpTable").querySelectorAll("tr[data-i]").forEach((tr) => {
    const b = BANDPLAN[+tr.dataset.i];
    tr.classList.toggle("playing", b.point ? Math.abs(f - b.start) <= 2500 : f >= b.start && f <= b.stop);
  });
}
$("bpFind").oninput = renderBandPlan; $("bpCat").onchange = renderBandPlan; $("bpSub").onchange = renderBandPlan;
$("bpHere").onclick = () => {
  $("bpFind").value = fmtMHz(S.freq || 0, 4); $("bpCat").value = ""; $("bpSub").checked = true; renderBandPlan();
  const here = S.freq ? bandsAt(S.freq) : [];
  if (here.length) setPick(here[0]);
};

/* ----- plan › band › section picker ----- */
let pick = null;     // the band or section currently selected
const mainBands = (cat) => BANDPLAN.filter((b) => b.cat === cat && !b.level && !b.point);
const sectionsIn = (band) => BANDPLAN.filter((x) => x !== band && (x.level || x.point) && x.start >= band.start && (x.point ? x.start : x.stop) <= band.stop);
const parentOf = (b) => (b.level || b.point) ? BANDPLAN.filter((x) => !x.level && !x.point && x.start <= b.start && x.stop >= (b.point ? b.start : b.stop))
  .sort((p, q) => (p.cat === b.cat ? -1 : 0) - (q.cat === b.cat ? -1 : 0) || (p.stop - p.start) - (q.stop - q.start))[0] : b;
const fmtRange = (b) => b.point ? fmtMHz(b.start, b.start < 3e7 ? 4 : 4) + " MHz" : `${fmtMHz(b.start, b.start < 3e7 ? 4 : 3)}–${fmtMHz(b.stop, b.stop < 3e7 ? 4 : 3)} MHz`;
$("pkPlan").innerHTML = Object.entries(BANDPLAN_CATS).map(([k, c]) => `<option value="${k}">${esc(c.label)} (${mainBands(k).length})</option>`).join("");
function fillBands(cat, selBand) {
  const list = mainBands(cat);
  $("pkBand").innerHTML = list.map((b) => `<option value="${BANDPLAN.indexOf(b)}">${esc(b.name)} · ${fmtRange(b)}</option>`).join("");
  if (selBand) $("pkBand").value = String(BANDPLAN.indexOf(selBand));
  fillSections();
}
function fillSections(selSec) {
  const band = BANDPLAN[+$("pkBand").value];
  if (!band) { $("pkSec").innerHTML = ""; return; }
  const secs = sectionsIn(band);
  $("pkSec").innerHTML = `<option value="${BANDPLAN.indexOf(band)}">Whole band</option>` +
    secs.map((x) => `<option value="${BANDPLAN.indexOf(x)}">${x.point ? "• " : ""}${esc(x.name)} · ${fmtRange(x)}</option>`).join("");
  $("pkSec").value = String(BANDPLAN.indexOf(selSec || band));
  pickChanged();
}
function pickChanged() {
  pick = BANDPLAN[+$("pkSec").value] || BANDPLAN[+$("pkBand").value];
  if (!pick) return;
  const band = BANDPLAN[+$("pkBand").value];
  const keys = sectionsIn(band).filter((x) => x.point);
  $("pkSearch").disabled = !!pick.point;
  $("pkMem").disabled = !keys.length;
  $("pkMem").textContent = keys.length ? `Add ${keys.length} key frequenc${keys.length > 1 ? "ies" : "y"} to bank` : "No key frequencies here";
  $("pkInfo").innerHTML = `<span class="bp-dot" style="background:${bpColor(pick)}"></span><b>${esc(pick.name)}</b> · ${fmtRange(pick)} · ${pick.mode} · step ${pick.step >= 1000 ? pick.step / 1000 + " kHz" : pick.step + " Hz"}${pick.note ? " · " + esc(pick.note) : ""}`;
}
function setPick(b) {
  const band = parentOf(b) || b;
  $("pkPlan").value = band.cat;
  fillBands(band.cat, band);
  fillSections(b);
}
$("pkPlan").onchange = () => fillBands($("pkPlan").value);
$("pkBand").onchange = () => fillSections();
$("pkSec").onchange = pickChanged;
$("pkTune").onclick = () => pick && tuneBand(pick);
$("pkSearch").onclick = () => pick && !pick.point && searchBand(pick);
$("pkMem").onclick = () => {
  const band = BANDPLAN[+$("pkBand").value];
  const keys = sectionsIn(band).filter((x) => x.point);
  if (!keys.length) return;
  send({ cmd: "mem_add_many", bank: +$("pkBank").value, bank_name: band.name.slice(0, 16),
    channels: keys.map((x) => ({ freq: x.start, mode: x.mode, name: x.name.slice(0, 32), tag: BANDPLAN_CATS[x.cat].label,
      decoder: /SSTV/.test(x.name) ? "sstv" : /WEFAX/.test(x.name) ? "wefax" : /APRS/.test(x.name) ? "data" : /LRPT/.test(x.name) ? "lrpt" : "" })) });
};
fillBands("amateur");

/* ======================================================= tabs */
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "img") send({ cmd: "images" });
  if (name === "dec") send({ cmd: "decoders" });
  if (name === "bp") renderBandPlan();
  if (name === "sch") send({ cmd: "schedule" });
  sizeCanvases();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

/* ----- memory ----- */
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function renderMemory() {
  if (!MEM) return;
  $("bankBtns").innerHTML = Array.from({ length: 10 }, (_, b) => {
    const bk = MEM.banks[b], n = Object.keys(bk.channels).length;
    return `<button class="btn bankbtn ${b === curBank ? "cur" : ""} ${bk.enabled === false ? "off" : ""}" data-b="${b}" title="${esc(bk.name)} – ${n} channels. Right-click: include/exclude from scan">${b}<small> ${n || ""}</small></button>`;
  }).join("");
  $("bankBtns").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { curBank = +b.dataset.b; renderMemory(); };
    b.oncontextmenu = (e) => { e.preventDefault(); toggleBank(+b.dataset.b); };
  });
  $("bankName").value = MEM.banks[curBank].name;
  const pb = $("pkBank").value;
  $("pkBank").innerHTML = Array.from({ length: 10 }, (_, b) => `<option value="${b}">Bank ${b} – ${esc(MEM.banks[b].name)} (${Object.keys(MEM.banks[b].channels).length})</option>`).join("");
  $("pkBank").value = pb || "9";
  $("csvBank").innerHTML = Array.from({ length: 10 }, (_, b) => `<option value="${b}" ${b === Math.max(1, curBank) ? "selected" : ""}>${b} – ${esc(MEM.banks[b].name)}</option>`).join("");
  const chs = MEM.banks[curBank].channels;
  const keys = Object.keys(chs).sort((a, b) => a - b);
  $("memTable").querySelector("tbody").innerHTML = keys.length ? keys.map((k) => {
    const c = chs[k];
    return `<tr data-ch="${k}"><td>${curBank}${String(k).padStart(2, "0")}</td>
      <td class="f" data-act="go">${fmtMHz(c.freq, 5)}</td>
      <td><select data-act="mode">${MODES.map((m) => `<option ${m === c.mode ? "selected" : ""}>${m}</option>`).join("")}</select></td>
      <td data-act="name" title="Click to rename">${esc(c.name) || "<span class=muted>(name)</span>"}</td>
      <td class="muted">${esc(c.tag)}</td>
      <td><select data-act="decoder"><option value="">—</option>${decoders.map((d) => `<option value="${d.id}" ${d.id === c.decoder ? "selected" : ""}>${d.id}</option>`).join("")}</select></td>
      <td><input type="checkbox" data-act="lockout" ${c.lockout ? "checked" : ""}></td>
      <td><input type="checkbox" data-act="delay" ${c.delay !== false ? "checked" : ""}></td>
      <td><button class="btn sm" data-act="go">▶</button> <button class="btn sm" data-act="pri">PRI</button> <button class="btn sm danger" data-act="del">✕</button></td></tr>`;
  }).join("") : `<tr><td colspan="9" class="muted">Bank ${curBank} is empty. Tune a frequency and press PGM, or import a RadioReference CSV above.</td></tr>`;
  $("memTable").querySelectorAll("tr[data-ch]").forEach((tr) => {
    const ch = +tr.dataset.ch;
    tr.querySelectorAll("[data-act]").forEach((el) => {
      const act = el.dataset.act;
      const upd = (fields) => send({ cmd: "mem_update", bank: curBank, ch, fields });
      if (act === "go") el.onclick = () => send({ cmd: "recall", bank: curBank, ch });
      if (act === "del") el.onclick = () => { if (confirm("Delete this channel?")) send({ cmd: "mem_delete", bank: curBank, ch }); };
      if (act === "pri") el.onclick = () => send({ cmd: "priority", bank: curBank, ch });
      if (act === "name") el.onclick = () => { const n = prompt("Channel name (alpha tag):", chs[ch].name || ""); if (n !== null) upd({ name: n.slice(0, 32) }); };
      if (act === "mode") el.onchange = () => upd({ mode: el.value });
      if (act === "decoder") el.onchange = () => upd({ decoder: el.value });
      if (act === "lockout") el.onchange = () => upd({ lockout: el.checked });
      if (act === "delay") el.onchange = () => upd({ delay: el.checked });
    });
  });
  highlightMemRow();
  renderLCD();
}
function highlightMemRow() {
  document.querySelectorAll("#memTable tr[data-ch]").forEach((tr) =>
    tr.classList.toggle("playing", !!(S.channel && S.channel.bank === curBank && S.channel.ch === +tr.dataset.ch)));
}
$("bankRename").onclick = () => send({ cmd: "mem_bank", bank: curBank, fields: { name: $("bankName").value } });
$("bankClear").onclick = () => { if (confirm(`Delete every channel in bank ${curBank}?`)) send({ cmd: "mem_clear", bank: curBank }); };
$("csvImport").onclick = () => {
  const f = $("csvFile").files[0];
  if (!f) { toast("Choose a RadioReference CSV file first", "warn"); return; }
  const rd = new FileReader();
  rd.onload = () => send({ cmd: "import_csv", text: rd.result, bank: +$("csvBank").value, replace: $("csvReplace").checked, bank_name: $("csvName").value || f.name.replace(/\.\w+$/, "").slice(0, 16) });
  rd.readAsText(f);
};

/* ----- activity / search ----- */
$("sMode").innerHTML = MODES.map((m) => `<option>${m}</option>`).join("");
$("sThr").oninput = () => ($("sThrO").textContent = $("sThr").value + " dB");
$("sPresets").innerHTML = '<span class="muted small">UK quick ranges:</span>' + UK_PRESETS.map((p, i) => `<button class="btn sm" data-i="${i}">${p[0]}</button>`).join("");
$("sPresets").querySelectorAll("button").forEach((b) => b.onclick = () => {
  const p = UK_PRESETS[+b.dataset.i];
  $("sStart").value = p[1]; $("sStop").value = p[2]; $("sStep").value = String(p[3]); $("sMode").value = p[4];
});
function startSearch() {
  const a = parseFloat($("sStart").value), b = parseFloat($("sStop").value);
  if (!(a > 0 && b > 0)) { toast("Set the search range in the Live Activity tab", "warn"); showTab("act"); return; }
  send({ cmd: "search", start: Math.round(a * 1e6), stop: Math.round(b * 1e6), step: +$("sStep").value, mode: $("sMode").value,
    search_mode: document.querySelector("input[name=smode]:checked").value });
  send({ cmd: "settings", values: { search_threshold_db: +$("sThr").value }, quiet: true });
}
$("sThr").onchange = () => send({ cmd: "settings", values: { search_threshold_db: +$("sThr").value }, quiet: true });
$("sGo").onclick = startSearch;
$("sStopBtn").onclick = () => send({ cmd: "manual" });
$("sClear").onclick = () => send({ cmd: "clear_activity" });
function renderActivity(list) {
  $("actTable").querySelector("tbody").innerHTML = (list || []).map((a) =>
    `<tr><td class="f" data-f="${a.freq}">${fmtMHz(a.freq, 5)}</td><td>${esc(a.name)}</td><td>${a.count}</td><td>${a.level}</td><td>${a.first}</td><td>${a.last}</td>
     <td><button class="btn sm" data-f="${a.freq}" data-store="1">Store</button></td></tr>`).join("") ||
    `<tr><td colspan="7" class="muted">No signals logged yet. Pick a range (or a UK quick range) and press Search, or press SRCH on the radio.</td></tr>`;
  $("actTable").querySelectorAll("[data-f]").forEach((el) => el.onclick = () => {
    if (el.dataset.store) { send({ cmd: "tune", freq: +el.dataset.f, mode: $("sMode").value }); setTimeout(() => send({ cmd: "store", bank: curBank }), 300); }
    else send({ cmd: "tune", freq: +el.dataset.f });
  });
}

/* ----- decoders ----- */
function renderDecoders() {
  $("decBtns").innerHTML = decoders.map((d) => `<button class="btn" data-id="${d.id}" title="${esc(d.help)}">${esc(d.name)}</button>`).join("");
  $("decBtns").querySelectorAll("button").forEach((b) => b.onclick = () => send({ cmd: "decoder", id: b.dataset.id, sub: $("dsdSub").value }));
  $("decStatus").innerHTML = decoders.map((d) => `<div class="decitem"><b>${esc(d.name)}</b>${esc(d.help)}<br>
    ${d.ready ? `<span class="ok">✔ ready</span> <span class="muted">${esc(d.path)}</span>` : `<span class="no">✖ program not found – press ⬇ Download missing decoders</span>`}</div>`).join("");
  renderState && S.freq && renderState();
}
$("decOff").onclick = () => send({ cmd: "decoder", id: "" });
$("decClear").onclick = () => ($("decLog").textContent = "");
$("decFetch").onclick = () => { decLine("Downloading missing decoder programs from GitHub …"); send({ cmd: "fetch_decoders" }); };
$("dsdSub").onchange = () => { if (S.decoder === "dsd") send({ cmd: "decoder", id: "dsd", sub: $("dsdSub").value }); };
function decLine(text, t) {
  const log = $("decLog");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent += `[${t || new Date().toLocaleTimeString()}] ${text}\n`;
  if (log.textContent.length > 120000) log.textContent = log.textContent.slice(-90000);
  if (atBottom) log.scrollTop = log.scrollHeight;
  $("lcdDecode").textContent = text;
}

/* ----- images ----- */
const imgUrl = (f) => `/images/${f.split("/").map(encodeURIComponent).join("/")}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
function renderImages(list) {
  $("imgGrid").innerHTML = (list || []).map((f) => `<figure><img loading="lazy" src="${imgUrl(f)}" data-f="${esc(f)}"><figcaption>${esc(f)}</figcaption></figure>`).join("") ||
    '<p class="muted">No pictures yet. Tune a WEFAX station in USB and start the WEFAX decoder, or record a Meteor-M pass with the LRPT decoder.</p>';
  $("imgGrid").querySelectorAll("img").forEach((im) => im.onclick = () => {
    const v = document.createElement("div"); v.className = "imgview"; v.innerHTML = `<img src="${im.src}">`; v.onclick = () => v.remove(); document.body.appendChild(v);
  });
}
function addImage(f) {
  const ex = [...$("imgGrid").querySelectorAll("img")].find((i) => i.dataset.f === f);
  if (ex) ex.src = imgUrl(f); else send({ cmd: "images" });
}
$("imgRefresh").onclick = () => send({ cmd: "images" });

/* ----- receiver ----- */
function renderDevices(list) {
  $("devSel").innerHTML = (list || []).map((d) => `<option value="${esc(d.key)}" ${d.key === DEV.key ? "selected" : ""}>${esc(d.label)} (${d.kind})</option>`).join("");
}
function renderDevice() {
  if (!DEV || !DEV.key) return;
  if (knobs.gain) { knobs.gain.min = DEV.gain_min; knobs.gain.max = DEV.gain_max; knobs.gain.render(); }
  [...$("devSel").options].forEach((o) => (o.selected = o.value === DEV.key));
  $("rateSel").innerHTML = (DEV.rates || []).filter((r) => r % 240000 === 0).map((r) => `<option value="${r}" ${r === DEV.sample_rate ? "selected" : ""}>${(r / 1e6).toFixed(2)} MS/s</option>`).join("");
  $("antSel").innerHTML = (DEV.antennas || []).map((a) => `<option>${esc(a)}</option>`).join("") || "<option>default</option>";
  const box = $("devSettings");
  box.innerHTML = (DEV.settings || []).map((s, i) => {
    let ctl;
    if (s.type === 0) ctl = `<input type="checkbox" data-i="${i}" ${String(s.value).toLowerCase() === "true" ? "checked" : ""}>`;
    else if (s.options && s.options.length) ctl = `<select data-i="${i}">${s.options.map((o) => `<option ${o === s.value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else ctl = `<input class="inp" data-i="${i}" value="${esc(s.value)}" style="width:7em"><button class="btn sm" data-set="${i}">Set</button>`;
    return `<label title="${esc(s.description)}">${ctl} ${esc(s.name)}</label>`;
  }).join("");
  box.querySelectorAll("input[type=checkbox][data-i], select[data-i]").forEach((el) => el.onchange = () => {
    const s = DEV.settings[+el.dataset.i]; send({ cmd: "device_setting", key: s.key, value: el.type === "checkbox" ? String(el.checked) : el.value });
  });
  box.querySelectorAll("[data-set]").forEach((b) => b.onclick = () => {
    const s = DEV.settings[+b.dataset.set]; send({ cmd: "device_setting", key: s.key, value: box.querySelector(`input[data-i="${b.dataset.set}"]`).value });
  });
  const so = DEV.soapy || {}, sp = so.sdrplay || {};
  $("soapyInfo").textContent = (sp.available ? `SDRplay API ${sp.version} ready. ` : `SDRplay API not found (install it from sdrplay.com for RSP receivers). `) +
    (so.available ? `SoapySDR ${so.version || ""} – driver modules: ${(so.modules || []).map((m) => m.split(/[\\/]/).pop()).join(", ")}`
    : `SoapySDR not loaded (${so.error || "not installed"}) – RTL-SDR/Airspy/HackRF need it; see Help › Receivers.`);
}
$("devOpen").onclick = () => send({ cmd: "device", key: $("devSel").value });
$("devRefresh").onclick = () => send({ cmd: "refresh_devices" });
$("tcpAdd").onclick = () => { if ($("tcpAddr").value.includes(":")) send({ cmd: "add_rtltcp", addr: $("tcpAddr").value.trim() }); else toast("Use host:port, e.g. 192.168.1.20:1234", "warn"); };
$("rateSel").onchange = () => send({ cmd: "device_setting", key: "sample_rate", value: +$("rateSel").value });
$("antSel").onchange = () => send({ cmd: "device_setting", key: "antenna", value: $("antSel").value });
$("ppmSet").onclick = () => send({ cmd: "device_setting", key: "ppm", value: +$("ppmInp").value });
$("agcChk").onchange = () => send({ cmd: "agc", on: $("agcChk").checked });

/* ----- settings / remote ----- */
$("saveSettings").onclick = () => send({ cmd: "settings", values: { scan_delay: +$("setDelay").value, scan_dwell: +$("setDwell").value, deemphasis_us: +$("setDeemph").value } });
$("setPass").onclick = () => {
  const p = $("newPass").value.trim();
  if (p.length < 4) { toast("Password must be at least 4 characters", "warn"); return; }
  send({ cmd: "settings", values: { password: p } }); $("netPass").textContent = p; $("newPass").value = "";
};
$("reqLocal").onchange = () => send({ cmd: "settings", values: { require_password_local: $("reqLocal").checked } });
$("loginGo").onclick = () => { token = $("loginPass").value; send({ cmd: "auth", password: token }); };
$("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("loginGo").click(); });

/* ----- theme ----- */
function setTheme(t, save = true) {
  if (!THEME_TEXT[t]) t = "pro2045";
  document.body.dataset.theme = t;
  $("themeSel").value = t;
  $("modelTag").textContent = THEME_TEXT[t][0];
  $("modelSub").textContent = THEME_TEXT[t][1];
  if (save) { localStorage.setItem("andysdr_theme", t); send({ cmd: "settings", values: { theme: t }, quiet: true }); }
}
$("themeSel").onchange = () => setTheme($("themeSel").value);
setTheme(localStorage.getItem("andysdr_theme") || "pro2045", false);

/* ----- toasts ----- */
function toast(text, level = "info") {
  const t = document.createElement("div"); t.className = "toast " + level; t.textContent = text;
  $("toasts").appendChild(t); setTimeout(() => t.remove(), level === "error" ? 9000 : 4500);
}

/* ======================================================= auto-identify */
const SIG_DIGITAL = /DMR|P25|NXDN|dPMR|Digital|Pager|APRS|DTMF|TETRA/;
function renderSignal() {
  const r = S.signal;
  for (const id of ["sigChip", "sigNow"]) {
    const el = $(id);
    if (!r) { el.hidden = true; continue; }
    el.hidden = false;
    const dec = r.decoder && S.decoder === r.decoder ? " → decoding" : (r.decoder && S.decoder && S.decoder !== r.decoder ? " (your decoder kept)" : "");
    el.textContent = (r.stable === false ? "? " : "◉ ") + r.label + dec;
    el.className = "sig-chip " + (r.kind === "SSTV" || r.kind === "WEFAX" ? "pic" : SIG_DIGITAL.test(r.kind) ? "dig" : "ana");
    el.title = `Identified automatically · SNR ${r.snr ?? "?"} dB · bandwidth ${r.bw ?? "?"} kHz` + (r.baud ? ` · ${r.baud} Bd` : "");
  }
  if (r && r.stable !== false && !S.decoder) $("lcdDecode").textContent = "ID: " + r.label;
}
for (const id of ["autoId", "autoId2"]) $(id).onchange = (e) => { send({ cmd: "auto_identify", on: e.target.checked }); $("autoId").checked = $("autoId2").checked = e.target.checked; };

/* ======================================================= scheduler */
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let schEdit = null;
$("schMode").innerHTML = MODES.map((m) => `<option>${m}</option>`).join("");
$("schDays").innerHTML = DAYS.map((d, i) => `<label class="day"><input type="checkbox" value="${i}">${d}</label>`).join("") +
  '<span class="muted small">(none ticked = every day)</span>';
function renderSchedule() {
  if (!SCH) return;
  $("schNow").textContent = SCH.now;
  if (!$("schAction").options.length) {
    $("schAction").innerHTML = Object.entries(SCH.actions).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $("schPreset").insertAdjacentHTML("beforeend", SCH.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join(""));
  }
  const run = SCH.running;
  $("schRunning").hidden = $("schStop").hidden = !run;
  if (run) { $("schRunning").textContent = `● Capturing: ${run.name} until ${run.until}`; $("schRunning").className = "sig-chip pic"; }
  const next = Object.fromEntries((SCH.next || []).map((n) => [n.id, n.at]));
  $("schTable").querySelector("tbody").innerHTML = SCH.jobs.map((j) => `<tr data-id="${esc(j.id)}" class="${run && run.id === j.id ? "playing" : ""}">
    <td><input type="checkbox" data-act="en" ${j.enabled ? "checked" : ""}></td>
    <td>${esc(j.start)} ${j.date ? "on " + esc(j.date) : j.days.length ? j.days.map((d) => DAYS[d]).join(" ") : "daily"}</td>
    <td>${esc(j.name)}</td><td class="f" data-act="tune">${fmtMHz(j.freq, j.freq < 3e7 ? 4 : 4)}</td><td>${j.mode}</td>
    <td>${esc(SCH.actions[j.action] || j.action)}</td><td>${j.duration} min</td><td class="muted">${j.enabled ? esc(next[j.id] || "–") : "off"}</td>
    <td><button class="btn sm" data-act="run">▶ Now</button> <button class="btn sm" data-act="edit">Edit</button> <button class="btn sm danger" data-act="del">✕</button></td></tr>`).join("") ||
    '<tr><td colspan="9" class="muted">No captures yet. Pick a station from Quick pick, set the start time and length, then Add capture.</td></tr>';
  $("schTable").querySelectorAll("tr[data-id]").forEach((tr) => {
    const j = SCH.jobs.find((x) => x.id === tr.dataset.id);
    tr.querySelector("[data-act=en]").onchange = (e) => send({ cmd: "schedule_save", job: { ...j, enabled: e.target.checked } });
    tr.querySelector("[data-act=run]").onclick = () => send({ cmd: "schedule_run", id: j.id });
    tr.querySelector("[data-act=del]").onclick = () => { if (confirm(`Delete "${j.name}"?`)) send({ cmd: "schedule_delete", id: j.id }); };
    tr.querySelector("[data-act=tune]").onclick = () => send({ cmd: "tune", freq: j.freq, mode: j.mode });
    tr.querySelector("[data-act=edit]").onclick = () => fillSchForm(j);
  });
}
function fillSchForm(j) {
  schEdit = j ? j.id : null;
  $("schName").value = j ? j.name : ""; $("schFreq").value = j ? (j.freq / 1e6).toString() : "";
  $("schMode").value = j ? j.mode : "USB"; $("schAction").value = j ? j.action : "wefax";
  $("schStart").value = j ? j.start : "12:00"; $("schDur").value = j ? j.duration : 20; $("schDate").value = j ? j.date : "";
  $("schDays").querySelectorAll("input").forEach((c) => (c.checked = !!(j && j.days.includes(+c.value))));
  $("schAdd").textContent = j ? "Save changes" : "Add capture";
}
$("schPreset").onchange = () => {
  const p = SCH && SCH.presets[+$("schPreset").value]; if (!p) return;
  $("schName").value = p.name; $("schFreq").value = (p.freq / 1e6).toString(); $("schMode").value = p.mode; $("schAction").value = p.action;
};
$("schAdd").onclick = () => {
  const f = parseFloat($("schFreq").value);
  if (!(f > 0)) { toast("Enter the frequency in MHz", "warn"); return; }
  const job = { id: schEdit || undefined, name: $("schName").value || `${f} MHz`, freq: Math.round(f * 1e6), mode: $("schMode").value,
    action: $("schAction").value, start: $("schStart").value || "12:00", duration: +$("schDur").value || 10, date: $("schDate").value,
    days: [...$("schDays").querySelectorAll("input:checked")].map((c) => +c.value), enabled: true };
  send({ cmd: "schedule_save", job }); fillSchForm(null); $("schPreset").value = "";
  toast(`Capture saved: ${job.name} at ${job.start}`);
};
$("schClear").onclick = () => fillSchForm(null);
$("schStop").onclick = () => send({ cmd: "schedule_stop" });

/* ======================================================= help */
const HELP = {
  conn: ["Connection light", "Green CONNECTED means this screen is talking to the AndySDR receiver (on this PC or across the network). If it shows OFFLINE, AndySDR is not running or the network is down – it reconnects by itself."],
  theme: ["Style", "Changes the faceplate look: PRO-2045 style (amber LCD, silver band), UBCD260DN style (black base/mobile with white-blue display) or TRX-2 style (rugged gunmetal with green-grey display). Controls work the same in every style; each computer remembers its own choice."],
  help: ["HELP", "Opens this guide. Tick 'Explain mode' and then click any knob, key or panel to get a description of just that control. Press Esc to leave Explain mode."],
  volume: ["VOLUME knob", "Loudness on THIS computer only (each remote listener has their own). Drag up/down, use the mouse wheel, or click it and use the arrow keys. Double-click resets to 70%."],
  squelch: ["SQUELCH knob", "Mutes the hiss when nobody is transmitting. Turn it up until the noise just disappears – the little raised bar on the S-meter shows where it is set. OPEN (fully left) = always hear everything. Scanning and searching need squelch set above the noise so they know when a channel is quiet."],
  gain: ["RF GAIN knob", "Receiver amplification. Too little = weak signals lost; too much = overload, false signals and a raised noise floor. Start in the middle, watch the waterfall, and turn down if you see lots of 'ghost' signals. For SDRplay receivers 0-100% controls the LNA state and IF gain together. Turning it switches AGC off; tick Hardware AGC in the Receiver tab to make it automatic."],
  power: ["AUDIO ON/OFF", "Starts or stops sound on this computer. Browsers need one click before they may play audio, so press this once after opening AndySDR. The LED shows green when audio is playing."],
  lcd: ["Display", "Top row: status flags – SCAN, SRCH (search), HOLD (stopped on a signal), MAN (manual), PRI (priority on), DLY (resume delay), L/O (channel locked out), AGC, REC (recording), DEC (decoder running). Second row: banks 0-9, lit if the bank will be scanned, underlined = bank in use. CH = channel number (bank × 100 + channel). Then the frequency in MHz, mode, channel name and step size. S-meter shows signal strength in dBFS."],
  k_scan: ["SCAN", "Scans all memory channels in the lit banks, skipping locked-out channels. AndySDR uses the spectrum to skip silent channels instantly, so scanning is very fast. It stops on activity, waits the resume delay after the transmission ends, then carries on. ▲/▼ change scan direction or skip the current channel."],
  k_man: ["MAN", "Manual: stops scanning/searching and stays on the current frequency. Type a channel number then MAN to jump to that memory channel (e.g. 1 2 MAN = channel 012)."],
  k_srch: ["SRCH", "Searches a frequency range for ANY live transmission (not just stored ones), using the range in the Live Activity / Search tab. Every signal found is logged in the activity list with how often it was heard. 'Stop on signal' listens to each one; 'Sweep & log only' keeps sweeping to map a band."],
  k_pgm: ["PGM", "Program (store) the current frequency and mode into memory. Type a channel number then PGM to store in that exact channel (e.g. 2 0 5 PGM = bank 2 channel 05), or just press PGM to use the next free channel in the bank selected in the Memory tab."],
  k_lo: ["L/O", "Lockout. On a memory channel: scanning skips it (press again to unlock). While searching: that frequency is ignored for the rest of the session – handy for constant carriers and birdies."],
  k_pri: ["PRI", "Priority. Recall a memory channel and press PRI – AndySDR checks it every 2 seconds and jumps to it when it becomes active (if it is within the received bandwidth). Press PRI again to turn priority off."],
  k_mode: ["MODE", "Cycles the demodulator: NFM (12.5 kHz FM), FM (25 kHz), AM (airband), WFM (broadcast), USB/LSB (HF voice, WEFAX), CW (Morse), DIG (12.5 kHz digital: DMR, P25, NXDN, dPMR, M17, pagers) and DIG25 (25 kHz: TETRA, Meteor-M satellites)."],
  k_step: ["STEP", "Cycles the tuning step used by ▲/▼, the mouse wheel and click-to-tune: 100 Hz up to 100 kHz. UK common steps: 12.5 kHz (PMR/amateur), 6.25 kHz (PMR446, narrow), 8.33 kHz (airband), 25 kHz (marine), 100 kHz (FM broadcast)."],
  k_bank: ["BANK", "Choose which banks are scanned: press BANK then a digit 0-9 to switch that bank in or out (the bank number on the LCD lights up when included). Right-clicking a bank button in the Memory tab does the same."],
  k_dly: ["DLY", "Delay on/off for the current memory channel. With delay on, the scanner waits (default 2 s) after a transmission ends so you catch the reply."],
  k_rec: ["REC", "Records what you hear to a WAV file in %APPDATA%\\AndySDR\\recordings. Press again to stop."],
  k_dec: ["DEC", "Opens the Decoders tab to pick a decoder for digital voice, pagers, weather fax or satellite images."],
  k_dot: ["· (decimal point)", "Separates MHz from the rest when typing a frequency, e.g. 1 4 5 · 5 E = 145.500 MHz. For HF use MHz too: 8 · 0 4 E = 8.040 MHz."],
  k_e: ["E (Enter)", "Tunes the frequency you typed (in MHz). Keyboard Enter does the same."],
  k_arrows: ["▲ ▼", "Step up/down by the STEP size. While scanning or searching they set the direction / skip to the next channel."],
  k_clr: ["CLR", "Clears what you were typing (keyboard: Backspace or Esc)."],
  scope: ["Spectrum & waterfall", "Live picture of the radio band around you. The top graph shows signal strength across the received bandwidth; the waterfall below shows history – transmissions appear as bright streaks. Click anywhere to tune there, use the mouse wheel to step. The red line and shading show the channel you are listening to. 'Range' and 'Top' set the display contrast."],
  banks: ["Memory banks", "10 banks × 100 channels = 1000 memories. Click a bank to view it, right-click to include/exclude it from scanning. In the table: click a frequency or ▶ to listen, click the name to rename, change mode/decoder in the drop-downs, tick L/O to lock out, PRI to make it the priority channel, ✕ to delete."],
  import: ["RadioReference UK import", "On radioreference.com open a UK county/agency database page and use its CSV download (premium) – or select the frequency table on the page, copy it and save it as a .txt file. Choose the file, pick the bank, and Import. AndySDR recognises Frequency, Alpha Tag, Description, Mode, Tag and Tone columns, converts FMN/AM/DMR/P25/NXDN/TETRA modes, and attaches the right decoder. Talkgroup lists (Decimal/Alpha Tag) are stored as talkgroups. Banks overflow into the next free bank."],
  search: ["Search", "Set From/To in MHz, the channel step, mode and sensitivity (lower dB = finds weaker signals but more false hits), then Search. UK quick ranges fill the boxes for you. Results list each active frequency with hit count and strength – click a frequency to listen, Store to save it to the selected bank."],
  decoders: ["Decoders", "Pick what to decode on the current frequency. Digital voice uses DSD-FME (choose DMR, P25, NXDN, dPMR, YSF, M17 or Auto); pagers use multimon-ng (POCSAG & FLEX); WEFAX and SSTV are built in; Meteor-M LRPT records the satellite pass and hands it to SatDump. Decoded text appears in the log and on the LCD bottom line. Memory channels can start their decoder automatically."],
  receiver: ["Receiver", "Choose which SDR to use: every SoapySDR device (RTL-SDR/Nooelec, SDRplay RSP1A/RSP2/RSP2pro/RSPdx/RSPduo, Airspy, HackRF, LimeSDR, Pluto…), rtl_tcp servers on other machines, or the Simulator for practice. Sample rate sets how wide the waterfall is. Antenna picks the input (RSP2: Antenna A, B or Hi-Z). Device switches (bias-T, notch filters, direct sampling) appear underneath automatically."],
  autoid: ["Auto ID (automatic signal identification)", "When ticked, AndySDR analyses whatever you are listening to and shows what it is next to the VFO: DMR (it spots the 30 ms TDMA bursts), P25, NXDN, dPMR, pagers (POCSAG/FLEX), APRS, DTMF, SSTV, WEFAX, TETRA or analogue voice (with any CTCSS tone). It then switches to the right mode and starts the matching decoder by itself, and stops it again when the channel goes back to voice. SSTV pictures are caught from their very first line. A decoder you pick yourself, or one stored on a memory channel, is never overridden. Untick to turn it off."],
  sched: ["Scheduler", "Captures that run by themselves at set times – ideal for weather fax charts, ISS SSTV events and Meteor-M satellite passes. Pick a station from Quick pick (or type a frequency), choose what to do (WEFAX, SSTV, LRPT, record…), the start time on this PC's clock and how long, and either tick days of the week or pick a single date. At the start time AndySDR stops scanning, tunes, runs the decoder; at the end it saves the pictures and puts the radio back exactly as it was. ▶ Now runs one straight away. AndySDR must be running (it can run as a server in the background)."],
  bppick: ["Plan › Band › Section", "Drill down through the UK band plan: pick a Plan (amateur, aviation, marine, PMR…), then a Band in it, then a Section (sub-band or key frequency). Tune goes there in the right mode and step; Search section loads it into the Live Activity search; 'Add key frequencies to bank' stores every key frequency in that band (e.g. marine Ch 16/70/AIS, airband 121.5/123.1) into the memory bank you choose. Clicking a coloured strip on the spectrum or a band name in the list also selects it here. Tap the coloured plan buttons underneath to show or hide each plan on the spectrum."],
  bandplan: ["Band plan size", "Shows the UK band plan as coloured strips across the top of the spectrum and in the band map above it. Off hides it; Small shows main allocations only; Medium adds sub-bands (e.g. 2 m FM simplex) and markers for key frequencies (Ch 16, 121.5, APRS…); Large makes the strips bigger and names the key frequencies. Each computer remembers its own choice."],
  vfo: ["VFO bar", "The VFO is the frequency you are tuned to. This bar shows it, which UK band and sub-band it is in, and – if the band normally uses a different mode or step – a 'Use' button that sets them for you. The red line and red VFO flag on the spectrum mark the same frequency."],
  bandmap: ["Band map", "The whole radio spectrum from 9 kHz to 6.5 GHz on one strip, coloured by who uses it (see the legend in the UK BAND PLAN tab). The red needle is your VFO and the white box is the part the spectrum display is showing. Hover to see a band's name; click a band to jump there in the right mode and step."],
  bptab: ["UK band plan list", "Every UK allocation, amateur sub-band and key frequency from 9 kHz to 6.5 GHz. Type a name or a frequency (e.g. 156.8) to find it, pick a category or click the coloured legend, untick sub-bands for the big picture. Tune goes straight there in the right mode; Search loads the band into the Live Activity search so you can find everything active in it. 'Where am I?' lists the bands your VFO is in."],
  remote: ["Remote control", "AndySDR is also a server: any PC, laptop, tablet or phone on your home network can open the address shown and control the radio with full audio and waterfall. Enter the password shown here. Windows may ask to allow AndySDR through the firewall – choose 'Private networks'. Do not forward the port to the internet."],
};

function openHelp() {
  const ctl = (keys) => `<dl>${keys.map((k) => `<dt>${HELP[k][0]}</dt><dd>${HELP[k][1]}</dd>`).join("")}</dl>`;
  $("helpBody").innerHTML = `
  <h3>QUICK START</h3>
  <ol>
    <li>Plug in your SDR, open the <b>RECEIVER</b> tab, pick it and press <b>Use this receiver</b>.</li>
    <li>Press <b>AUDIO ON/OFF</b> once so this computer can play sound.</li>
    <li>Type a frequency on the keypad, e.g. <code>1 4 5 · 5 E</code>, or click a signal on the waterfall.</li>
    <li>Turn <b>SQUELCH</b> up until the hiss stops, set <b>RF GAIN</b> so the noise floor is low and signals stand out.</li>
    <li>Store channels with <b>PGM</b> or import from RadioReference in the <b>MEMORY</b> tab, then press <b>SCAN</b>.</li>
    <li>Press <b>SRCH</b> to hunt for live transmissions across a band.</li>
  </ol>
  <h3>KNOBS &amp; DISPLAY</h3>${ctl(["volume", "squelch", "gain", "power", "lcd"])}
  <h3>KEYPAD &amp; FUNCTION KEYS</h3>${ctl(["k_scan", "k_man", "k_srch", "k_pgm", "k_lo", "k_pri", "k_mode", "k_step", "k_bank", "k_dly", "k_rec", "k_dec", "k_dot", "k_e", "k_arrows", "k_clr"])}
  <p class="small">Keyboard: digits and <code>.</code> type, <code>Enter</code> tunes, <code>↑ ↓</code> step, <code>S</code> scan, <code>M</code> manual, <code>P</code> program, <code>L</code> lockout, <code>H</code> help, <code>Esc</code> clear.</p>
  <h3>SCREENS</h3>${ctl(["scope", "vfo", "autoid", "bandmap", "bandplan", "bptab", "bppick", "sched", "banks", "import", "search", "decoders", "receiver", "remote", "theme", "conn"])}
  <h3>DECODER PROGRAMS</h3>
  <p>AndySDR has WEFAX and SSTV built in (SSTV: Martin, Scottie, Robot and PD modes – ISS 145.800 NFM, 2 m 144.500 NFM, 20 m 14.230 USB). DSD-FME, multimon-ng and SatDump now come bundled in the <code>decoders</code> folder next to <code>AndySDR.exe</code>. If one shows ✖ in the DECODERS tab, press <b>⬇ Download missing decoders</b> and AndySDR fetches it from GitHub into <code>%APPDATA%\\AndySDR\\decoders</code>. You can also unzip your own copy into either folder (any sub-folder is fine – AndySDR finds the .exe):</p>
  <dl><dt>DSD-FME</dt><dd>github.com/lwvmobile/dsd-fme – DMR, P25 1/2, NXDN, dPMR, YSF, M17. AndySDR feeds it like SDR++'s network sink (TCP 127.0.0.1:7355, 48 kHz); DSD-FME plays decoded voice on your speakers itself (AndySDR starts the PulseAudio sound server that comes with it).</dd>
  <dt>multimon-ng</dt><dd>github.com/EliasOenal/multimon-ng – POCSAG 512/1200/2400, FLEX, APRS, DTMF, ZVEI, EAS.</dd>
  <dt>SatDump</dt><dd>satdump.org – Meteor-M N2-3 / N2-4 LRPT (137.9 or 137.1 MHz, 72k). Use DIG25 mode, start the LRPT decoder as the satellite rises, stop it when it sets – pictures appear in WEATHER IMAGES. SatDump needs the Microsoft Visual C++ 2015-2022 x64 runtime (the AndySDR installer adds it).</dd>
  <dt>TETRA</dt><dd>AndySDR streams the channel as 48 kHz complex float32 over UDP port 42000 for an external TETRA demodulator (there is no ready-made Windows TETRA decoder that can be bundled; the auto-identify still recognises TETRA). Encrypted networks (including UK Airwave) cannot and must not be decoded.</dd></dl>
  <h3>RECEIVERS</h3>
  <dl><dt>SDRplay (RSP2pro etc.)</dt><dd>Install SDRplay's API 3.x for Windows from sdrplay.com first – AndySDR talks to the RSP through it.</dd>
  <dt>RTL-SDR / Nooelec</dt><dd>Install the WinUSB driver once with Zadig (select "Bulk-In, Interface 0").</dd>
  <dt>Airspy / HackRF / others</dt><dd>Use the manufacturer's Windows driver; AndySDR finds them through SoapySDR.</dd>
  <dt>Weather fax</dt><dd>UK Met Office (Northwood) HF fax: 2618.5, 4610, 8040, 11086.5 kHz – use USB mode, start the WEFAX decoder; charts appear in WEATHER IMAGES. Needs an HF-capable receiver (RSP2pro is ideal, or an RTL-SDR in direct-sampling mode).</dd></dl>
  <h3>LEGAL (UK)</h3>
  <p>Under the Wireless Telegraphy Act 2006 you may listen to broadcast, amateur and CB transmissions, but it is an offence to listen to other transmissions you are not authorised to receive or to pass on anything you hear. Encrypted signals cannot be decoded by AndySDR. Check the law where you live.</p>`;
  $("helpModal").hidden = false;
}
let explainOn = false;
function setExplain(on) {
  explainOn = on; $("explainMode").checked = on;
  document.body.classList.toggle("explain-mode", on);
  if (!on) $("explainTip").hidden = true;
  if (on) { $("helpModal").hidden = true; toast("Explain mode: click any control. Esc to finish."); }
}
$("helpBtn").onclick = () => { if (explainOn) setExplain(false); else openHelp(); };
$("helpClose").onclick = () => ($("helpModal").hidden = true);
$("explainMode").onchange = (e) => setExplain(e.target.checked);
$("helpModal").addEventListener("click", (e) => { if (e.target === $("helpModal")) $("helpModal").hidden = true; });
document.addEventListener("click", (e) => {
  if (!explainOn) return;
  const el = e.target.closest("[data-help]");
  if (!el || el.id === "helpBtn") return;
  e.preventDefault(); e.stopPropagation();
  const h = HELP[el.dataset.help]; if (!h) return;
  const tip = $("explainTip");
  tip.innerHTML = `<b>${h[0]}</b>${h[1]}`;
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  tip.style.left = Math.min(window.innerWidth - 360, Math.max(10, r.left)) + "px";
  tip.style.top = (r.bottom + 8 + tip.offsetHeight > window.innerHeight ? Math.max(10, r.top - tip.offsetHeight - 8) : r.bottom + 8) + "px";
}, true);
document.querySelectorAll("[data-help]").forEach((el) => { const h = HELP[el.dataset.help]; if (h && !el.title) el.title = h[0] + " – " + h[1].split(". ")[0] + "."; });

setBpSize(bpSize, false);
renderLCD();
connect();
