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
  sdrpp: ["RADIO", ""],
  bluedark: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  bluelight: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  modern: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  modernlight: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  softdark: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
  softlight: ["1000 CHANNEL · PROGRAMMABLE", "SDR SCANNER"],
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
let BWS = {};         // selectable bandwidths per mode (from the server)
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
  ws.onopen = () => {
    setConn(true);
    // after a dropped link (server restart, update, Wi-Fi blip) the server has forgotten this screen wants sound
    if (typeof player !== "undefined" && player.on && $("myAudio").checked) send({ cmd: "audio", on: true });
  };
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
  if ($("ledLink")) $("ledLink").classList.toggle("on", on);
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
    case "state": S = d; renderState(); adsbStat(); break;
    case "memory": MEM = d; renderMemory(); break;
    case "activity": renderActivity(d); break;
    case "device": DEV = d; renderDevice(); break;
    case "bias": renderBias(d); break;
    case "devices": renderDevices(d); break;
    case "decoder": decLine(d.text, d.t); break;
    case "decoders": decoders = d; renderDecoders(); break;
    case "image": addImage(d.file); break;
    case "ft8": onFt8(d); break;
    case "morse": onMorse(d); break;
    case "signal": S.signal = d; renderSignal(); break;
    case "schedule": SCH = d; renderSchedule(); break;
    case "images": renderImages(d); break;
    case "recordings": renderRecordings(d); break;
    case "eibi": renderEibi(d); break;
    case "mesh": MESH = d; renderMesh(); break;
    case "mesh_msg": meshAddMsg(d); break;
    case "mesh_history": MESHMSG = d; renderMeshList(); renderMeshMsgs(); break;
    case "mesh_ble": meshBleFound(d); break;
    case "adsb": onAdsb(d); break;
    case "meshsdr": onMeshSdr(d); break;
    case "adsb_cfg": ADS.cfg = d; adsbCfg(); adsbStat(); break;
    case "pager": addPager(d); break;
    case "pager_cfg": PAG.cfg = d; renderPager(); break;
    case "pager_all": PAG.msgs = d.messages || []; PAG.cfg = d; renderPager(); break;
    case "update": onUpdate(d); break;
    case "update_progress": UPD.progress = d.progress; renderUpdateBar(); break;
    case "event": toast(d.text, d.level); if (d.level === "error") decLine("! " + d.text); break;
    case "import_result":
      $("csvResult").textContent = `Imported ${d.added} channels` + (d.file ? ` from ${d.file}` : "") + (d.talkgroups ? `, ${d.talkgroups} talkgroups` : "") +
        (d.skipped ? `, skipped ${d.skipped} rows` : "") + (d.overflow ? `, ${d.overflow} did not fit – raise "using … bank(s)" (only empty banks are used) or split the file` : "");
      toast($("csvResult").textContent);
      break;
  }
}

function onHello(h) {
  $("loginModal").hidden = true;
  if (h.token) { token = h.token; sessionStorage.setItem("andysdr_token", token); }
  isLocal = h.local;
  document.querySelectorAll(".local-only").forEach((e) => (e.style.display = isLocal ? "" : "none"));
  if (h.bias) renderBias(h.bias);
  S = h.state; MEM = h.memory; DEV = h.device; applyDeviceBandplan(); decoders = h.decoders; BWS = h.bandwidths || BWS;
  renderDevices(h.devices); renderDevice(); renderMemory(); renderActivity(h.activity);
  renderDecoders(); renderImages(h.images); renderState();
  if (CR.on) Object.values(CR.docks).forEach((d) => d.page && tabEffects(d.page));
  if (h.schedule) { SCH = h.schedule; renderSchedule(); }
  if (h.mesh) { MESH = h.mesh; MESHMSG = h.mesh_history || []; renderMesh(); }
  if (h.meshsdr) onMeshSdr(h.meshsdr);
  if (h.pager) { PAG.msgs = h.pager.messages || []; PAG.cfg = h.pager; PAG.unseen = 0; renderPager(); }
  $("pagExport").href = `/api/pager.csv?token=${encodeURIComponent(token)}`;
  $("adsbExport").href = `/api/adsb.csv?token=${encodeURIComponent(token)}`;
  if (h.adsb) adsbHello(h.adsb);
  FT8.msgs = h.ft8 || []; renderFt8();
  if (h.morse) { MORSE_TXT = h.morse.text || ""; renderMorse(); if (h.morse.status) morseStatus(h.morse.status); }
  const st = h.settings || {};
  $("updAuto").checked = st.update_check !== false; $("updDl").checked = !!st.update_auto_download;
  if (h.update) onUpdate(h.update, true);
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
  if (typeof morseOpen === "function" && !$("morseBox").hidden) renderMorse();
  renderVfo();
  renderSignal();
  $("autoId").checked = $("autoId2").checked = S.auto_identify !== false;
  $("muteDig").checked = S.mute_digital_audio !== false;
  $("devname").textContent = S.device ? `· ${S.device}` : "";
  $("listeners").textContent = S.listeners ? `· ${S.listeners} connected` : "";
  if (!knobs.sq.dragging) knobs.sq.set(S.squelch, false);
  if (!knobs.gain.dragging) knobs.gain.set(S.gain, false);
  knobs.gain.render();
  $("agcChk").checked = !!S.agc;
  $("gainAgc").classList.toggle("on", !!S.agc); $("gainAgc").style.fontWeight = S.agc ? "700" : "400"; $("gainAgc").textContent = S.agc ? "AGC ON" : "AGC OFF";
  renderQuick(); renderTop(); renderBw();
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
  else if (e.key === "i") send({ cmd: "lockout_temp", minutes: 10 });
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
    this.wake();
  },
  // browsers / WebView2 can leave the audio engine "suspended" (autoplay rules, minimised window, PC sleep): wake it
  wake() { if (this.on && this.ctx && this.ctx.state !== "running" && this.ctx.state !== "closed") this.ctx.resume().catch(() => {}); },
  stop() { if (this.ctx) this.ctx.close(); this.ctx = null; this.on = false; },
  muted: false,
  setVolume(v) { this.vol = v; if (this.gain) this.gain.gain.value = this.muted ? 0 : v * v * 1.6; },
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
setInterval(() => player.wake(), 1500);
for (const ev of ["click", "keydown", "touchstart", "focus", "visibilitychange"]) window.addEventListener(ev, () => player.wake(), true);
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
  const modern = /^modern/.test(document.body.dataset.theme || "");
  const stops = modern
    ? [[0, [0, 0, 10]], [0.25, [10, 30, 120]], [0.5, [20, 120, 220]], [0.72, [60, 200, 235]], [0.88, [170, 235, 250]], [1, [255, 255, 255]]]
    : [[0, [0, 0, 8]], [0.25, [10, 30, 110]], [0.45, [0, 140, 200]], [0.62, [60, 210, 90]], [0.78, [250, 220, 40]], [0.9, [250, 110, 20]], [1, [255, 255, 255]]];
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

/* ---- zoom / pan: the view is a window (viewC centre, fs/zoom wide) into the received span ---- */
let zoom = 1, viewC = null, follow = true;      // follow = view stays centred on the tuned frequency while zoomed
let smooth = null, wfAcc = 0, wfLast = 0, frameEma = 60, lastFrameT = 0, drawQueued = false, wfView = null;
function viewOf(sp) {
  const span = sp.fs / zoom;
  let c = follow || viewC === null ? sp.freq : viewC;
  const lo = sp.center - sp.fs / 2 + span / 2, hi = sp.center + sp.fs / 2 - span / 2;
  c = Math.min(hi, Math.max(lo, c));            // never look outside what the receiver is actually capturing
  return { f0: c - span / 2, span, c };
}
function setZoom(z, anchorFrac) {
  if (!spec) { zoom = z; return; }
  const v = viewOf(spec), fa = v.f0 + v.span * (anchorFrac ?? 0.5);       // keep the frequency under the pointer still
  zoom = Math.min(32, Math.max(1, z));
  const span = spec.fs / zoom;
  viewC = fa - span * ((anchorFrac ?? 0.5) - 0.5); follow = zoom === 1 ? true : false;
  if (anchorFrac === undefined && S.freq) { viewC = S.freq; follow = true; }
  syncZoomUi(); queueDraw();
}
function panView(frac) {
  if (!spec || zoom === 1) return;
  const v = viewOf(spec); viewC = v.c + frac * v.span; follow = false; syncZoomUi(); queueDraw();
}
function syncZoomUi() {
  $("zoomTxt").textContent = zoom === 1 ? "Full" : zoom.toFixed(zoom < 10 ? 1 : 0) + "×";
  $("zoomFollow").checked = follow;
}
$("zoomIn").onclick = () => setZoom(zoom * 1.6);
$("zoomOut").onclick = () => setZoom(zoom / 1.6);
$("zoomFit").onclick = () => { zoom = 1; follow = true; viewC = null; syncZoomUi(); queueDraw(); };
$("zoomFollow").onchange = (e) => { follow = e.target.checked; if (!follow && spec) viewC = viewOf(spec).c; queueDraw(); };

function onSpectrum(center, fs, freq, data) {
  const n = data.length, now = performance.now();
  if (lastFrameT) frameEma = frameEma * 0.9 + Math.min(60, Math.max(2, 1000 / Math.max(1, now - lastFrameT))) * 0.1;
  lastFrameT = now;
  // light temporal smoothing so the trace and waterfall flow instead of flicker
  if (!smooth || smooth.length !== n) smooth = Float32Array.from(data);
  else for (let i = 0; i < n; i++) smooth[i] += (data[i] - smooth[i]) * 0.55;
  spec = { center, fs, freq, data: smooth };
  if (!scopeLoop) scopeLoop = requestAnimationFrame(scopeTick);   // drawing runs on the display clock, not when data happens to arrive
}
let scopeLoop = 0, scopeLastDraw = 0;
function scopeTick(ts) {
  scopeLoop = requestAnimationFrame(scopeTick);
  if (ts - scopeLastDraw < 30) return;              // ~30 fps keeps the waterfall even and the CPU low
  scopeLastDraw = ts; drawScope(ts);
}
function queueDraw() { if (!drawQueued && spec) { drawQueued = true; requestAnimationFrame(drawScope); } }
function drawScope(ts) {
  drawQueued = false;
  if (!spec) return;
  const { center, fs, freq, data } = spec;
  const W = specC.width, H = specC.height, dpr = window.devicePixelRatio || 1;
  const n = data.length;
  if ($("scopeAuto").checked) {
    const sorted = Array.from(data).sort((a, b) => a - b);
    const floor = -130 + sorted[Math.floor(n / 2)] / 255 * 120;
    autoLo = autoLo === null ? floor - 12 : autoLo * 0.95 + (floor - 12) * 0.05;
    $("scopeLo").value = Math.round(autoLo); $("scopeHi").value = Math.round(autoLo + 70);
  }
  const lo = +$("scopeLo").value, hi = Math.max(lo + 10, +$("scopeHi").value);
  const V = viewOf(spec), f0 = V.f0, span = V.span;
  // spectrum columns: interpolate between bins when zoomed in, take the peak when several bins share a pixel
  const binHz = fs / n, fLo = center - fs / 2;
  const col = new Float32Array(W);
  for (let x = 0; x < W; x++) {
    const fa = f0 + x / W * span, fb = f0 + (x + 1) / W * span;
    const ba = (fa - fLo) / binHz, bb = (fb - fLo) / binHz;
    let m;
    if (bb - ba > 1) { m = 0; for (let i = Math.max(0, Math.floor(ba)); i <= Math.min(n - 1, Math.floor(bb)); i++) if (data[i] > m) m = data[i]; }
    else { const bc = (ba + bb) / 2 - 0.5, i0 = Math.min(n - 1, Math.max(0, Math.floor(bc))), i1 = Math.min(n - 1, i0 + 1), t = Math.min(1, Math.max(0, bc - i0)); m = data[i0] * (1 - t) + data[i1] * t; }
    col[x] = -130 + m / 255 * 120;
  }
  const css = getComputedStyle(document.body);
  sctx.fillStyle = "#07090b"; sctx.fillRect(0, 0, W, H);
  sctx.strokeStyle = "rgba(255,255,255,0.07)"; sctx.fillStyle = "rgba(255,255,255,0.45)";
  sctx.font = `${11 * dpr}px Consolas, monospace`; sctx.lineWidth = 1;
  for (let db = Math.ceil(lo / 10) * 10; db <= hi; db += 10) {
    const y = H - (db - lo) / (hi - lo) * H;
    sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(W, y); sctx.stroke();
    sctx.fillText(db + "", 4, y - 2);
  }
  const nice = [1e3, 2.5e3, 5e3, 10e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6, 2e6].find((s) => span / s <= Math.max(3, Math.min(12, W / (95 * dpr)))) || 2e6;
  for (let f = Math.ceil(f0 / nice) * nice; f < f0 + span; f += nice) {
    const x = (f - f0) / span * W;
    sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
    sctx.fillText(fmtMHz(f, nice < 1e5 ? 4 : 2), x + 3, H - 4);
  }
  const bw = S.bw ? S.bw / 2 : (MODE_BW[S.mode] || 6000);
  const xf = (freq - f0) / span * W, xb = bw / span * W;
  sctx.fillStyle = "rgba(255,60,60,0.16)"; sctx.fillRect(xf - xb, 0, xb * 2, H);
  const y = (v) => H - (v - lo) / (hi - lo) * H;
  sctx.beginPath(); sctx.moveTo(0, H);
  for (let x = 0; x < W; x++) sctx.lineTo(x, y(col[x]));
  sctx.lineTo(W, H); sctx.closePath();
  sctx.fillStyle = css.getPropertyValue("--scope-fill"); sctx.fill();
  sctx.beginPath();
  for (let x = 0; x < W; x++) x ? sctx.lineTo(x, y(col[x])) : sctx.moveTo(x, y(col[x]));
  sctx.strokeStyle = css.getPropertyValue("--scope-line"); sctx.lineWidth = 1.2 * dpr; sctx.stroke();
  const stripH = drawBandStrip(sctx, f0, span, W, dpr);
  if (xf >= 0 && xf <= W) {
    sctx.strokeStyle = "#ff4040"; sctx.lineWidth = 1.5 * dpr; sctx.beginPath(); sctx.moveTo(xf, stripH); sctx.lineTo(xf, H); sctx.stroke();
    drawVfoFlag(sctx, xf, stripH, W, freq, dpr);
  }
  // waterfall: rows scroll at the measured frame rate, timed by the display, so the motion is even
  const WH = wfC.height;
  if (wfView && (Math.abs(wfView.f0 - f0) > 1 || Math.abs(wfView.span - span) > 1) && wfView.W === W) {
    const tmp = document.createElement("canvas"); tmp.width = W; tmp.height = WH; tmp.getContext("2d").drawImage(wfC, 0, 0);
    wctx.fillStyle = "#000"; wctx.fillRect(0, 0, W, WH);
    wctx.drawImage(tmp, (f0 - wfView.f0) / wfView.span * W, 0, span / wfView.span * W, WH, 0, 0, W, WH);   // keep the old picture, re-scaled to the new view
  }
  wfView = { f0, span, W };
  const dt = wfLast ? Math.min(100, ts - wfLast) : 16; wfLast = ts;
  wfAcc += dt / 1000 * frameEma * dpr;
  const k = Math.min(Math.floor(wfAcc), 8); wfAcc -= Math.floor(wfAcc);
  if (k > 0) {
    wctx.drawImage(wfC, 0, 0, W, WH - k, 0, k, W, WH - k);
    const row = wctx.createImageData(W, 1);
    for (let x = 0; x < W; x++) {
      const p = Math.max(0, Math.min(255, Math.round((col[x] - lo) / (hi - lo) * 255)));
      const c = palette[p];
      row.data[x * 4] = c[0]; row.data[x * 4 + 1] = c[1]; row.data[x * 4 + 2] = c[2]; row.data[x * 4 + 3] = 255;
    }
    for (let j = 0; j < k; j++) wctx.putImageData(row, 0, j);
  }
  if (xf >= 0 && xf <= W) { wctx.fillStyle = "rgba(255,64,64,0.8)"; wctx.fillRect(xf - dpr / 2, 0, dpr, 3); }
  $("scopeInfo").textContent = `centre ${fmtMHz(V.c, 4)} MHz · view ${(span / 1e6).toFixed(span < 1e6 ? 3 : 2)} MHz of ${(fs / 1e6).toFixed(2)} · tuned ${fmtMHz(freq, 5)}`;
  drawBandMap();
}
function scopeFreq(e, c) {
  if (!spec) return null;
  const r = c.getBoundingClientRect(), V = viewOf(spec);
  const f = V.f0 + ((e.clientX - r.left) / r.width) * V.span;
  const st = S.step || 12500;
  return Math.round(f / st) * st;
}
["scopeLo", "scopeHi"].forEach((id) => $(id).addEventListener("input", () => ($("scopeAuto").checked = false)));
/* mouse / touchpad / touch:  wheel or two-finger scroll = move the view left/right,  pinch / stretch (or Ctrl+wheel) = zoom,
   drag = pan,  click = tune,  Shift+wheel = step the tuning frequency as before */
const pointers = new Map();
let pinch0 = null, dragged = false, dragStart = null;
for (const c of [specC, wfC]) {
  c.style.touchAction = "none";
  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    if (e.shiftKey) { send({ cmd: "step", dir: e.deltaY < 0 ? 1 : -1 }); return; }
    if (e.ctrlKey) {                                   // touchpad pinch arrives as Ctrl+wheel; Ctrl+wheel with a mouse zooms too
      setZoom(zoom * Math.exp(-e.deltaY * 0.01), (e.clientX - r.left) / r.width);
      return;
    }
    const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    if (zoom === 1) { send({ cmd: "step", dir: d < 0 ? 1 : -1 }); return; }     // nothing to pan when the whole span is showing
    panView(Math.max(-0.5, Math.min(0.5, d * (e.deltaMode ? 0.06 : 0.0016))));
  }, { passive: false });
  c.addEventListener("pointerdown", (e) => {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY }); c.setPointerCapture(e.pointerId);
    dragged = false; dragStart = { x: e.clientX, c: spec ? viewOf(spec).c : 0 };
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch0 = { d: Math.hypot(a.x - b.x, a.y - b.y), z: zoom }; dragged = true; }
  });
  c.addEventListener("pointermove", (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = c.getBoundingClientRect();
    if (pointers.size === 2 && pinch0) {               // two fingers: stretch = zoom in, pinch = zoom out
      const [a, b] = [...pointers.values()];
      setZoom(pinch0.z * Math.hypot(a.x - b.x, a.y - b.y) / pinch0.d, ((a.x + b.x) / 2 - r.left) / r.width);
      return;
    }
    if (pointers.size === 1 && dragStart && spec && zoom > 1 && Math.abs(e.clientX - dragStart.x) > 5) {   // one finger / button held: drag the view
      dragged = true;
      const V = viewOf(spec); viewC = dragStart.c - (e.clientX - dragStart.x) / r.width * V.span; follow = false; syncZoomUi(); queueDraw();
      return;
    }
    if (!pointers.size) {
      const f = scopeFreq(e, c); if (!f) return;
      const b = bandsAt(f).filter((x) => !x.point)[0];
      c.title = fmtMHz(f, 4) + " MHz" + (b ? " · " + b.name : "") + " – click to tune · scroll to move · pinch or Ctrl+scroll to zoom";
    }
  });
  const up = (e) => {
    const wasPinch = pointers.size === 2;
    pointers.delete(e.pointerId); if (pointers.size < 2) pinch0 = null;
    if (explainOn || dragged || wasPinch || e.type === "pointercancel") { if (!pointers.size) dragStart = null; return; }
    dragStart = null;
    const f = scopeFreq(e, c); if (!f) return;
    if (c === specC && bpSize) {     // a click on the band-plan strip also selects that section
      const r = c.getBoundingClientRect();
      if (e.clientY - r.top < BP_STRIP[bpSize]) {
        const b = bandsAt(f).filter((x) => !x.point && !bpHidden.has(x.cat))[0];
        if (b) { setPick(b); toast(`${b.name} · ${fmtRange(b)} – see UK BAND PLAN tab`); }
      }
    }
    send({ cmd: "tune", freq: f });
  };
  c.addEventListener("pointerup", up);
  c.addEventListener("pointercancel", up);
}

/* ======================================================= UK band plan + VFO */
const BP_STRIP = [0, 14, 38, 66, 100];     // band strip height on the spectrum (CSS px) for Off/Small/Medium/Large/Extra large
const BP_FONT = [0, 10, 13, 17, 23];       // band names on the strip (CSS px)
const BP_LABEL = [0, 0, 0, 12, 16];        // key-frequency labels under the strip (0 = none)
const BP_MAP = [0, 22, 34, 56, 76];        // overview band map height
const BP_MAPFONT = [0, 9.5, 10.5, 13, 16]; // overview band map text
let MAP_LO = 9e3, MAP_HI = 6.5e9;        // overview covers 9 kHz – 6.5 GHz on a log scale
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
  ctx.font = `600 ${BP_FONT[bpSize] * dpr}px Segoe UI, Arial, sans-serif`; ctx.textBaseline = "middle";
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
      const mk = Math.max(4, BP_FONT[bpSize] * 0.4) * dpr;
      ctx.beginPath(); ctx.moveTo(x, H - 1); ctx.lineTo(x - mk, H - 1 - mk * 1.5); ctx.lineTo(x + mk, H - 1 - mk * 1.5); ctx.fill();
      if (BP_LABEL[bpSize]) {
        ctx.save(); ctx.font = `600 ${BP_LABEL[bpSize] * dpr}px Segoe UI, Arial, sans-serif`; ctx.fillStyle = "rgba(255,255,255,.75)";
        ctx.textBaseline = "top"; ctx.shadowColor = "#000"; ctx.shadowBlur = 4 * dpr; ctx.fillText(b.name.split(" – ")[0].slice(0, 36), x + 5 * dpr, H + 2 * dpr); ctx.restore();
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
  const x = Math.min(W - w, Math.max(0, xf - w / 2)), y = top + 3 * dpr + (BP_LABEL[bpSize] ? (BP_LABEL[bpSize] + 5) * dpr : 0);
  ctx.fillStyle = "#ff4040"; ctx.fillRect(x, y, w, h);
  ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(t, x + 5 * dpr, y + h / 2); ctx.textBaseline = "alphabetic";
}
const mapX = (f, W) => (Math.log10(Math.max(MAP_LO, f)) - Math.log10(MAP_LO)) / (Math.log10(MAP_HI) - Math.log10(MAP_LO)) * W;
const mapF = (x, W) => Math.pow(10, Math.log10(MAP_LO) + x / W * (Math.log10(MAP_HI) - Math.log10(MAP_LO)));
function drawBandMap() {
  if (!bpSize) return;
  const dpr = window.devicePixelRatio || 1, W = bandmapC.width, H = bandmapC.height;
  if (!W || !H) return;
  const ctx = bmctx, mf = BP_MAPFONT[bpSize], barTop = 2 * dpr, barH = H - (mf + 5) * dpr;
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
  ctx.font = `${mf * dpr}px Consolas, monospace`; ctx.fillStyle = "rgba(255,255,255,.7)"; ctx.strokeStyle = "rgba(255,255,255,.25)";
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
    ctx.font = `bold ${(mf + 0.5) * dpr}px Consolas, monospace`;
    const tw = ctx.measureText(t).width + 8 * dpr, th = (mf + 3) * dpr, tx = Math.min(W - tw, Math.max(0, x + 8 * dpr));
    ctx.fillStyle = "rgba(255,64,64,.92)"; ctx.fillRect(tx, H - th - dpr, tw, th);
    ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(t, tx + 4 * dpr, H - th / 2 - dpr); ctx.textBaseline = "alphabetic";
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
  bpSize = Math.max(0, Math.min(4, n | 0));
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
function tabEffects(name) {
  if (name === "img") send({ cmd: "images" });
  if (name === "rec") send({ cmd: "recordings" });
  if (name === "eibi") eibiSearch();
  if (name === "dec") send({ cmd: "decoders" });
  if (name === "bp") renderBandPlan();
  if (name === "sch") send({ cmd: "schedule" });
  if (name === "ft8") { FT8.unseen = 0; $("ft8Badge").hidden = true; renderFt8(); }
  if (name === "mesh") { meshRead(meshConv); renderMesh(); setTimeout(() => $("meshMsgs").scrollTop = 1e9, 0); }
  if (name === "pag") { PAG.unseen = 0; pagBadge(); }
  if (name === "adsb") setTimeout(() => { adsbBadge(); renderAdsbTable(); adsbStat(); adsDraw(); }, 0);
  sizeCanvases();
}
function showTab(name) {
  if (CR.on) { crShow(name); return; }
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  tabEffects(name);
}
/* ----- control-room layout: the tab pages can sit in docks around the spectrum, several visible at once ----- */
const CR = { on: false, docks: {}, home: [], names: {}, focus: "B2",
  def: { L1: "mem", B1: "dec", B2: "img", B3: "sch", R1: "rec", R2: "act", R3: "pag" } };
(function crInit() {
  const tabs = document.querySelector("section.tabs");
  CR.home = [...document.querySelectorAll(".tabpage")];
  document.querySelectorAll(".tab").forEach((t) => (CR.names[t.dataset.tab] = t.textContent.replace(/\s+\d*\s*$/, "").trim()));
  let saved = {}; try { saved = JSON.parse(localStorage.getItem("andysdr_docks") || "{}"); } catch (e) {}
  const main = document.querySelector("main.wrap"), radio = $("radio");
  const mk = (id, parent) => {
    const d = document.createElement("div"); d.className = "dock"; d.id = "dock" + id;
    d.innerHTML = `<div class="dock-head"><select title="Choose what this panel shows"></select><button class="btn small" title="Make this panel big / small">⤢</button></div><div class="dock-body"></div>`;
    d.querySelector("select").innerHTML = Object.entries(CR.names).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    d.querySelector("select").onchange = (e) => { CR.focus = id; crPut(id, e.target.value); };
    d.querySelector("button").onclick = () => { d.classList.toggle("max"); setTimeout(() => { sizeCanvases(); window.dispatchEvent(new Event("resize")); }, 30); };
    parent.appendChild(d); CR.docks[id] = { el: d, page: null };
  };
  const bottom = document.createElement("div"); bottom.id = "crBottom";
  const right = document.createElement("div"); right.id = "crRight";
  main.appendChild(bottom); main.appendChild(right);
  mk("L1", radio); ["B1", "B2", "B3"].forEach((i) => mk(i, bottom)); ["R1", "R2", "R3"].forEach((i) => mk(i, right));
  CR.saved = Object.assign({}, CR.def, saved);
  CR.tabs = tabs;
})();
function crPut(id, name) {
  const page = $("tab-" + name), dock = CR.docks[id];
  if (!page || !dock) return;
  const other = Object.entries(CR.docks).find(([k, v]) => k !== id && v.page === name);
  const prev = dock.page && dock.page !== name ? dock.page : null;
  dock.page = name;
  dock.el.querySelector(".dock-body").appendChild(page);
  if (other) {                                              // that page was in another dock: swap, so no panel is left empty
    const [oid, o] = other; o.page = null;
    if (prev) crPut(oid, prev);
  } else if (prev) {                                        // the page it replaces goes back to the (hidden) store
    const old = $("tab-" + prev); if (old) { CR.tabs.appendChild(old); old.classList.remove("active"); }
  }
  page.classList.add("active");
  dock.el.querySelector("select").value = name;
  const st = {}; Object.entries(CR.docks).forEach(([k, v]) => (st[k] = v.page)); try { localStorage.setItem("andysdr_docks", JSON.stringify(st)); } catch (e) {}
  tabEffects(name);
}
function crShow(name) {                                      // quick keys / jobs ask for a page: show it in a dock (swap if it is not shown yet)
  const have = Object.entries(CR.docks).find(([, v]) => v.page === name);
  if (have) { have[1].el.animate([{ outline: "3px solid var(--accent)" }, { outline: "0 solid transparent" }], { duration: 900 }); tabEffects(name); return; }
  crPut(CR.focus, name);
}
function crEnable(on) {
  if (on === CR.on) return;
  CR.on = on; document.body.classList.toggle("cr", on);
  if (on) {
    const pick = Object.assign({}, CR.def, CR.saved);
    const used = new Set();
    Object.entries(CR.docks).forEach(([id]) => { let n = pick[id]; if (!n || used.has(n) || !$("tab-" + n)) n = null; if (n) used.add(n); pick[id] = n; });
    Object.entries(pick).forEach(([id, n]) => { if (n) crPut(id, n); });
  } else {
    CR.home.forEach((p) => { CR.tabs.appendChild(p); p.classList.remove("active"); });
    Object.values(CR.docks).forEach((d) => { d.page = null; d.el.classList.remove("max"); });
    showTab(document.querySelector(".tab.active") ? document.querySelector(".tab.active").dataset.tab : "mem");
  }
  document.documentElement.style.setProperty("--topbar-h", (document.querySelector(".topbar").offsetHeight + 2) + "px");
  setTimeout(() => { sizeCanvases(); window.dispatchEvent(new Event("resize")); }, 50);
}
function crAuto() {
  const mode = $("layoutSel").value;
  crEnable(false);          // the Control room layout was withdrawn - the classic layout is the only one
}
$("layoutSel").onchange = crAuto;
window.addEventListener("resize", () => { clearTimeout(CR.t); CR.t = setTimeout(crAuto, 200); });
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
  const src = MEM.banks[curBank].source;
  $("bankSource").textContent = src && src.file ? `Database: ${src.file}` + (src.file_date ? ` · file dated ${src.file_date}` : "") + (src.imported ? ` · imported ${src.imported}` : "") : "";
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
  rd.onload = () => send({ cmd: "import_csv", text: rd.result, bank: +$("csvBank").value, banks: +$("csvBanks").value || 1, filename: f.name, file_date: f.lastModified ? new Date(f.lastModified).toISOString().slice(0, 10) : "", replace: $("csvReplace").checked, bank_name: $("csvName").value || f.name.replace(/\.\w+$/, "").slice(0, 16) });
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

/* ----- pager messages ----- */
const PAG = { msgs: [], cfg: { aliases: {}, highlight: [] }, unseen: 0 };
const PAG_MAX_ROWS = 1000;
const pagAlias = (cap) => (PAG.cfg.aliases || {})[cap] || "";
const pagIsHl = (cap) => (PAG.cfg.highlight || []).includes(cap);
function pagMatch(m) {
  const proto = $("pagProto").value, type = $("pagType").value, q = $("pagSearch").value.trim().toLowerCase();
  if (proto && !m.proto.startsWith(proto)) return false;
  if (type && m.type !== type) return false;
  if ($("pagHideTone").checked && m.type === "Tone") return false;
  if ($("pagStarOnly").checked && !pagIsHl(m.capcode)) return false;
  if (q) {
    const hay = `${m.capcode} ${pagAlias(m.capcode)} ${m.text} ${m.channel || ""} ${m.freq ? fmtMHz(m.freq, 4) : ""}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}
function pagRow(m, isNew) {
  const al = pagAlias(m.capcode), hl = pagIsHl(m.capcode);
  const text = m.type === "Tone" ? '<span class="muted">(tone only – no message)</span>' : esc(m.text);
  const d0 = new Date(), today = `${d0.getFullYear()}-${String(d0.getMonth() + 1).padStart(2, "0")}-${String(d0.getDate()).padStart(2, "0")}`;
  return `<tr data-id="${m.id}" class="${hl ? "hl" : ""}${isNew ? " new" : ""}">
    <td><button class="star" data-cap="${esc(m.capcode)}" title="${hl ? "Stop highlighting" : "Highlight"} capcode ${esc(m.capcode)}">${hl ? "★" : "☆"}</button></td>
    <td class="t" title="${esc(m.date)}">${m.date && m.date !== today ? esc(m.date.slice(5)) + " " : ""}${esc(m.t)}</td>
    <td class="f" ${m.freq ? `data-f="${m.freq}"` : ""} title="${esc(m.channel || "")}">${m.freq ? fmtMHz(m.freq, 4) : "—"}</td>
    <td>${esc(m.proto)}</td>
    <td class="cap" data-q="${esc(m.capcode)}" title="Show only this capcode">${esc(m.capcode)}${m.func !== "" && m.func != null ? `<span class="muted">/${esc(m.func)}</span>` : ""}</td>
    <td class="alias" data-cap="${esc(m.capcode)}" title="Click to name this capcode">${al ? esc(al) : '<span class="muted">+ name</span>'}</td>
    <td><span class="ptype ${esc(m.type)}">${esc(m.type)}</span></td>
    <td class="msg">${text}${m.group ? `<div class="muted small">also to: ${esc(m.group)}</div>` : ""}</td></tr>`;
}
function renderPager() {
  const shown = PAG.msgs.filter(pagMatch).slice(-PAG_MAX_ROWS).reverse();
  $("pagTable").querySelector("tbody").innerHTML = shown.map((m) => pagRow(m, false)).join("") ||
    `<tr><td colspan="8" class="muted">${PAG.msgs.length ? "No messages match the filter." :
      "No pager messages yet. Tune a pager frequency (UK: try the 153.0–153.5 MHz range – the \"Pagers\" quick range in Live Activity / Search finds them) and press ▶ Start pager decoder, or leave Auto-identify on."}</td></tr>`;
  pagCount(shown.length);
  $("pagLogDir").textContent = PAG.cfg.log_dir && isLocal && PAG.cfg.logging !== false ? `Every message is also saved to ${PAG.cfg.log_dir} (one CSV file per day).` : "";
}
function pagCount(n) {
  const total = PAG.msgs.length;
  $("pagCount").textContent = n === total ? `${total} message${total === 1 ? "" : "s"}` : `${n} of ${total} shown`;
}
function pagBadge() {
  $("pagBadge").hidden = !PAG.unseen;
  $("pagBadge").textContent = PAG.unseen > 99 ? "99+" : PAG.unseen;
}
function addPager(m) {
  PAG.msgs.push(m);
  if (PAG.msgs.length > 2000) PAG.msgs.splice(0, PAG.msgs.length - 2000);
  if (!$("tab-pag").classList.contains("active")) { PAG.unseen++; pagBadge(); }
  if (pagIsHl(m.capcode)) toast(`★ ${pagAlias(m.capcode) || m.capcode}: ${m.type === "Tone" ? "(tone)" : m.text}`.slice(0, 160));
  if (!pagMatch(m)) { pagCount($("pagTable").querySelectorAll("tr[data-id]").length); return; }
  const tb = $("pagTable").querySelector("tbody");
  if (!tb.querySelector("tr[data-id]")) tb.innerHTML = "";
  tb.insertAdjacentHTML("afterbegin", pagRow(m, true));
  const rows = tb.querySelectorAll("tr[data-id]");
  for (let i = PAG_MAX_ROWS; i < rows.length; i++) rows[i].remove();
  pagCount(Math.min(rows.length, PAG_MAX_ROWS));
}
$("pagTable").addEventListener("click", (e) => {
  const star = e.target.closest(".star");
  if (star) { send({ cmd: "pager_highlight", capcode: star.dataset.cap, on: !pagIsHl(star.dataset.cap) }); return; }
  const al = e.target.closest("td.alias");
  if (al) {
    const cap = al.dataset.cap;
    const n = prompt(`Name for capcode ${cap} (leave empty to remove):`, pagAlias(cap));
    if (n !== null) send({ cmd: "pager_alias", capcode: cap, name: n.slice(0, 40) });
    return;
  }
  const cap = e.target.closest("td.cap");
  if (cap) { $("pagSearch").value = $("pagSearch").value === cap.dataset.q ? "" : cap.dataset.q; renderPager(); return; }
  const f = e.target.closest("td.f[data-f]");
  if (f) send({ cmd: "tune", freq: +f.dataset.f });
});
["pagProto", "pagType", "pagHideTone", "pagStarOnly"].forEach((id) => ($(id).onchange = renderPager));
$("pagSearch").oninput = renderPager;
$("pagClear").onclick = () => { if (confirm("Clear the pager message list? (Daily log files are kept.)")) send({ cmd: "pager_clear" }); };
$("pagStart").onclick = () => send({ cmd: "decoder", id: "pager" });


/* ----- ADS-B aircraft ----- */
const ADS = { list: [], trails: new Map(), cfg: {}, stats: {}, sel: null, sort: { k: "seen", d: 1 },
  view: { lat: 54.0, lon: -2.0, z: 5, user: false, fitN: 0 }, tiles: new Map(), raf: 0, emSeen: new Set(), hit: [], hitBox: null };
const ADS_EMER = { "7500": "hijack", "7600": "radio failure", "7700": "emergency" };
const ADS_SQK = { "7000": "VFR conspicuity (no controller)", "7001": "military low-level", "7004": "aerobatics", "7010": "circuit flying", "2000": "IFR, no code assigned",
  "1200": "VFR (US)", "7500": "hijack", "7600": "radio failure", "7700": "general emergency", "0020": "air ambulance / rescue helicopter", "7616": "police air support" };
const ADS_CAT = { A1: "Light (under 15,500 lb)", A2: "Small (15,500–75,000 lb)", A3: "Large (75,000–300,000 lb)", A4: "High-vortex large (e.g. B757)", A5: "Heavy (over 300,000 lb)",
  A6: "High-performance (fast jet)", A7: "Rotorcraft / helicopter", B1: "Glider / sailplane", B2: "Lighter-than-air (balloon)", B3: "Parachutist", B4: "Ultralight / paraglider",
  B6: "Drone (UAV)", B7: "Space vehicle", C1: "Emergency vehicle (ground)", C2: "Service vehicle (ground)" };
function adsPhase(a) {
  if (a.ground) return a.gs > 40 ? "taxiing / on the runway" : "parked or stopped";
  if (a.vr == null || a.alt == null) return "";
  if (a.vr > 500) return "climbing"; if (a.vr < -500) return "descending";
  return a.alt > 25000 ? "cruising" : "level flight";
}
function adsGc(lat1, lon1, lat2, lon2) {   // great-circle distance in nautical miles
  const r = Math.PI / 180, a = Math.sin((lat2 - lat1) * r / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 2 * 3440.065 * Math.asin(Math.min(1, Math.sqrt(a)));
}
function adsTrip(a) {   // origin -> destination block: place names, distance left and a rough arrival time
  const rt = a.rt || {}, o = rt.origin, d = rt.dest; if (!o && !d) return "";
  const place = (x) => x ? `${esc(x.city || x.name)}${x.iata ? " (" + esc(x.iata) + ")" : ""}${x.country ? ", " + esc(x.country) : ""}` : "?";
  let h = `<span class="muted small">From</span> ${place(o)}<br><span class="muted small">To</span> ${place(d)}<br>`;
  if (d && d.lat != null && a.lat != null) {
    const left = adsGc(a.lat, a.lon, d.lat, d.lon), tot = o && o.lat != null ? adsGc(o.lat, o.lon, d.lat, d.lon) : null, m = adsMet();
    h += `To go: ${Math.round(m ? left * 1.852 : left).toLocaleString()} ${m ? "km" : "NM"}`;
    if (tot) h += ` of ${Math.round(m ? tot * 1.852 : tot).toLocaleString()} (${Math.max(0, Math.min(100, Math.round(100 * (1 - left / tot))))}% flown)`;
    if (a.gs > 80 && !a.ground) { const mins = Math.round(left / a.gs * 60), t = new Date(Date.now() + mins * 60000); h += ` · about ${adsAgo(mins * 60)} (${t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}) at this speed`; }
    h += "<br>";
  }
  return h;
}
function adsAgo(s) { return s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1) + " h"; }
const adsLS = (k, d) => { try { const v = localStorage.getItem("andysdr_adsb_" + k); return v === null ? d : v; } catch (e) { return d; } };
const adsSetLS = (k, v) => { try { localStorage.setItem("andysdr_adsb_" + k, v); } catch (e) { /* private mode */ } };
const adsMet = () => $("adsbUnits").value === "met";
const adsMapStyle = () => $("adsbTiles").value || "bright";
const adsLight = () => { const m = adsMapStyle(); return m === "bright" || m === "plain"; };
const adsRunning = () => S.decoder === "adsb";
const adsTabOpen = () => $("tab-adsb").classList.contains("active");
const adsAlt = (a) => (a == null ? null : adsMet() ? Math.round(a * 0.3048) : a);
const adsSpd = (v) => (v == null ? null : adsMet() ? Math.round(v * 1.852) : Math.round(v));
const adsVs = (v) => (v == null ? null : adsMet() ? +(v * 0.00508).toFixed(1) : v);
const adsDist = (v) => (v == null ? null : adsMet() ? Math.round(v * 1.852) : v);
const adsN = (v, d = "–") => (v == null ? d : typeof v === "number" ? v.toLocaleString() : v);
const adsRef = () => (ADS.cfg.lat != null && ADS.cfg.lon != null ? [ADS.cfg.lat, ADS.cfg.lon] : null);

function adsbHello(a) {
  ADS.cfg = a.cfg || {}; ADS.list = a.aircraft || []; ADS.stats = a.stats || {};
  ADS.trails.clear();
  for (const x of ADS.list) if (x.trail && x.trail.length) ADS.trails.set(x.icao, x.trail.map((p) => [p[0], p[1], p[2]]));
  ["units", "tiles", "trails", "labels", "posonly"].forEach((k) => {
    const el = { units: "adsbUnits", tiles: "adsbTiles", trails: "adsbTrails", labels: "adsbLabels", posonly: "adsbPosOnly" }[k];
    const v = adsLS(k, null);
    if (v !== null) { if ($(el).type === "checkbox") $(el).checked = v === "1"; else $(el).value = v; }
  });
  if (!$("adsbTiles").value) $("adsbTiles").value = "bright";      // older saved "street map on/off" values
  adsbCfg(); adsbUnitsHdr();
}
function adsbCfg() {
  const c = ADS.cfg || {};
  if (document.activeElement !== $("adsbLat")) $("adsbLat").value = c.lat != null ? c.lat : "";
  if (document.activeElement !== $("adsbLon")) $("adsbLon").value = c.lon != null ? c.lon : "";
  $("adsbShare").checked = !!c.share; $("adsbLog").checked = c.log !== false; $("adsbLookup").checked = c.lookup !== false;
  $("adsbShareInfo").textContent = `SBS/BaseStation text on port ${c.sbs_port || 30003} and raw AVR on port ${c.raw_port || 30002} – for Virtual Radar Server, tar1090, PlanePlotter, FlightAware/FR24 feeders and similar. Windows may ask to allow AndySDR through the firewall. Keep it on your home network.`;
  $("adsbNote").innerHTML = (c.lat == null ? "Set your position (above) to see distance and bearing and to get the range rings on the map. " : "") +
    (isLocal && c.log && c.log_dir ? `Positions are logged to ${esc(c.log_dir)} (one CSV per day).` : "");
  adsDraw();
}
function adsbUnitsHdr() {
  const m = adsMet();
  $("thAlt").textContent = m ? "Alt (m)" : "Alt (ft)"; $("thGs").textContent = m ? "km/h" : "Speed (kt)";
  $("thVr").textContent = m ? "V/S (m/s)" : "V/S (fpm)"; $("thDist").textContent = m ? "Dist (km)" : "Dist (NM)";
}
function onAdsb(d) {
  ADS.list = d.aircraft || []; ADS.stats = d.stats || {};
  const live = new Set();
  for (const a of ADS.list) {
    live.add(a.icao);
    if (a.lat == null || a.seen > 90) continue;
    let tr = ADS.trails.get(a.icao);
    if (!tr) ADS.trails.set(a.icao, (tr = []));
    const last = tr[tr.length - 1];
    if (!last || Math.abs(last[0] - a.lat) + Math.abs(last[1] - a.lon) > 0.0015) { tr.push([a.lat, a.lon, a.alt]); if (tr.length > 40) tr.shift(); }
    const em = a.squawk && ADS_EMER[a.squawk];
    if (em && !ADS.emSeen.has(a.icao + a.squawk)) { ADS.emSeen.add(a.icao + a.squawk); toast(`⚠ ${a.flight || a.icao} squawking ${a.squawk} (${em})`, "warn"); }
  }
  for (const k of [...ADS.trails.keys()]) if (!live.has(k)) ADS.trails.delete(k);
  const nPos = ADS.list.filter((a) => a.lat != null && a.seen <= 90).length;
  if (!ADS.view.user && nPos > (ADS.view.fitN || 0)) { adsFit(); ADS.view.fitN = nPos; }     // keep zooming out as more aircraft appear, until you touch the map
  adsbBadge();
  if (adsTabOpen()) {
    // the list only re-sorts every ADS_TABLE_MS so rows stay put long enough to click (map and stats still update every second)
    const now = Date.now();
    if (now - (ADS.tableAt || 0) >= ADS_TABLE_MS) { ADS.tableAt = now; renderAdsbTable(); } else adsDetail();
    adsbStat(); adsDraw();
  }
}
const ADS_TABLE_MS = 3000;
function adsbBadge() {
  const n = adsRunning() && ADS.stats ? ADS.stats.aircraft || 0 : 0;
  const b = $("adsbBadge");
  b.hidden = !n || adsTabOpen();
  b.textContent = n;
}
function adsbStat() {
  const st = ADS.stats || {}, run = adsRunning();
  $("adsbStart").classList.toggle("on", run);
  $("adsbStat").innerHTML = run
    ? `<span class="run">● RUNNING</span> · <b>${st.aircraft || 0}</b> aircraft · <b>${st.with_pos || 0}</b> with position · <b>${st.rate || 0}</b> msg/s` +
      (st.max_range ? ` · max range <b>${adsMet() ? Math.round(st.max_range * 1.852) + " km" : st.max_range + " NM"}</b>` : "")
    : `stopped · ${(ADS.list || []).filter((a) => a.seen <= 120).length ? "showing the last aircraft heard" : "press ▶ Start ADS-B"}`;
  adsbBadge();
}
function renderAdsbTable() {
  if (!adsTabOpen()) return;
  const q = $("adsbFind").value.trim().toLowerCase(), posOnly = $("adsbPosOnly").checked;
  const { k, d } = ADS.sort;
  const val = (a) => (k === "airline" ? (a.airline || a.owner || a.country || "") : k === "flight" ? a.flight || "~" : a[k]);
  const rows = ADS.list.filter((a) => a.seen <= 120 && (!posOnly || a.lat != null) &&
    (!q || `${a.icao} ${a.flight} ${a.airline} ${a.country} ${a.squawk} ${a.reg || ""} ${a.type || ""} ${a.tcode || ""} ${a.owner || ""}`.toLowerCase().includes(q)));
  rows.sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null || x === "") return 1; if (y == null || y === "") return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * d;
  });
  $("adsbTable").querySelectorAll("th").forEach((th) => { th.classList.toggle("sorted", th.dataset.k === k); th.classList.toggle("asc", th.dataset.k === k && d < 0); });
  $("adsbTable").querySelector("tbody").innerHTML = rows.map((a) => {
    const em = ADS_EMER[a.squawk];
    return `<tr data-id="${a.icao}" class="${ADS.sel === a.icao ? "sel " : ""}${a.seen > 30 ? "stale " : ""}${em ? "emerg" : ""}">
      <td class="cs">${esc(a.flight || "—")}</td><td class="mono">${esc(a.reg || "–")}</td><td class="mono">${a.icao}</td>
      <td class="mono" title="${esc(a.type || "")}">${esc(a.tcode || "–")}</td>
      <td title="${esc(a.owner || a.country)}">${a.airline ? esc(a.airline) : a.owner ? esc(a.owner) : `<span class="muted">${esc(a.country)}</span>`}</td>
      <td class="num">${a.ground ? "GND" : adsN(adsAlt(a.alt))}</td><td class="num">${adsN(adsSpd(a.gs))}</td>
      <td class="num">${a.trk == null ? "–" : Math.round(a.trk) + "°"}</td><td class="num">${adsN(adsVs(a.vr))}</td>
      <td class="mono">${esc(a.squawk || "–")}</td><td class="num">${adsN(adsDist(a.dist))}</td>
      <td class="num">${a.msgs}</td><td class="num">${a.seen < 1 ? "now" : Math.round(a.seen) + "s"}</td></tr>`;
  }).join("") || `<tr><td colspan="13" class="muted">${adsRunning() ? "Listening for aircraft … (they can take a few seconds to appear; a 1090 MHz antenna with a clear view of the sky helps a lot)" : "No aircraft yet. Press ▶ Start ADS-B (1090 MHz)."}</td></tr>`;
  adsDetail();
}
function adsDetail() {
  const a = ADS.list.find((x) => x.icao === ADS.sel), el = $("adsbDetail");
  if (!a) { el.hidden = true; return; }
  const m = adsMet();
  el.hidden = false;
  el.innerHTML = `<button class="x" id="adsbDetX" title="Close">✕</button><b class="big">${esc(a.flight || a.icao)}</b><br>
    ${a.photo ? `<a href="${esc(a.photo_link || a.photo)}" target="_blank" rel="noopener"><img class="adsb-photo" src="${esc(a.photo)}" alt="${esc(a.reg || a.icao)}"></a><div class="muted small">Photo: ${esc(a.photographer || "planespotters.net")} · planespotters.net</div>` : ""}
    ${a.reg ? `<b>${esc(a.reg)}</b> · ` : ""}${a.type ? esc(a.type) + (a.tcode ? ` (${esc(a.tcode)})` : "") + "<br>" : a.reg ? "<br>" : ""}
    ${a.owner ? "Owner: " + esc(a.owner) + "<br>" : ""}${a.airline ? esc(a.airline) + "<br>" : ""}${a.route ? "Route: " + esc(a.route) + (a.route_names && a.route_names !== a.route ? `<br><span class="muted small">${esc(a.route_names)}</span>` : "") + "<br>" : ""}${adsTrip(a)}${a.mil ? "<b>Military aircraft</b> (hex block)<br>" : ""}${a.rt && a.rt.airline && a.rt.airline !== a.airline ? "Airline: " + esc(a.rt.airline) + "<br>" : ""}${a.owner_country ? "Registered in: " + esc(a.owner_country) + (a.operator_code ? " · operator " + esc(a.operator_code) : "") + "<br>" : ""}Hex ${a.icao} · ${esc(a.country || "country unknown")}<br>
    ${a.cat ? "Category: " + esc(ADS_CAT[a.cat] || a.cat) + "<br>" : ""}${adsPhase(a) ? "Now: " + adsPhase(a) + "<br>" : ""}
    ${a.reg ? `<a target="_blank" rel="noopener" href="https://www.flightradar24.com/data/aircraft/${encodeURIComponent(a.reg.toLowerCase())}">FR24 history</a> · ` : ""}<a target="_blank" rel="noopener" href="https://www.planespotters.net/hex/${a.icao}">Planespotters</a> · <a target="_blank" rel="noopener" href="https://globe.adsbexchange.com/?icao=${a.icao.toLowerCase()}">ADSBexchange</a><br>
    Altitude: ${a.ground ? "on the ground" : a.alt == null ? "–" : adsAlt(a.alt).toLocaleString() + (m ? " m" : " ft")}<br>
    Speed: ${a.gs == null ? "–" : adsSpd(a.gs) + (m ? " km/h" : " kt")} · Heading: ${a.trk == null ? "–" : Math.round(a.trk) + "°"}<br>
    Climb: ${a.vr == null ? "–" : adsVs(a.vr) + (m ? " m/s" : " ft/min")}<br>
    Squawk: ${esc(a.squawk || "–")}${ADS_EMER[a.squawk] ? " ⚠ " + ADS_EMER[a.squawk] : ADS_SQK[a.squawk] ? " – " + ADS_SQK[a.squawk] : ""}<br>
    ${a.dist != null ? `Distance: ${adsDist(a.dist)} ${m ? "km" : "NM"} at ${a.brg}°<br>` : ""}
    ${a.lat != null ? `Position: ${a.lat.toFixed(4)}, ${a.lon.toFixed(4)}<br>` : "No position yet<br>"}
    <span class="muted small">Heard for ${adsAgo(a.first)} · peak ${a.max_alt == null ? "–" : adsAlt(a.max_alt).toLocaleString() + (m ? " m" : " ft")} at ${a.max_gs == null ? "–" : adsSpd(a.max_gs) + (m ? " km/h" : " kt")}${a.min_dist != null ? " · closest " + adsDist(a.min_dist) + (m ? " km" : " NM") : ""}</span><br>
    Signal ${a.rssi} dBFS · ${a.msgs} messages`;
  const x = $("adsbDetX"); if (x) x.onclick = () => { ADS.sel = null; renderAdsbTable(); adsDraw(); };
}
$("adsbTable").querySelector("thead").addEventListener("click", (e) => {
  const th = e.target.closest("th"); if (!th) return;
  ADS.sort = ADS.sort.k === th.dataset.k ? { k: th.dataset.k, d: -ADS.sort.d } : { k: th.dataset.k, d: 1 };
  renderAdsbTable();
});
$("adsbTable").querySelector("tbody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]"); if (!tr) return;
  ADS.sel = ADS.sel === tr.dataset.id ? null : tr.dataset.id;
  if (ADS.sel) send({ cmd: "adsb_lookup", icao: ADS.sel });
  const a = ADS.list.find((x) => x.icao === ADS.sel);
  if (a && a.lat != null) { ADS.view.lat = a.lat; ADS.view.lon = a.lon; ADS.view.user = true; }
  renderAdsbTable(); adsDraw();
});
$("adsbStart").onclick = () => send({ cmd: "adsb_start" });
$("adsbStop").onclick = () => send({ cmd: "adsb_stop" });
$("adsbClear").onclick = () => { if (confirm("Clear the aircraft list?")) { send({ cmd: "adsb_clear" }); ADS.trails.clear(); ADS.list = []; renderAdsbTable(); adsDraw(); } };
$("adsbPosSet").onclick = () => {
  const lat = $("adsbLat").value.trim(), lon = $("adsbLon").value.trim();
  send({ cmd: "adsb_pos", lat: lat === "" ? null : lat.replace(",", "."), lon: lon === "" ? null : lon.replace(",", ".") });
  ADS.view.user = false; ADS.view.fitN = 0;
};
$("adsbPosGps").onclick = () => {
  if (!navigator.geolocation) { toast("This browser cannot give its location – type your latitude and longitude instead", "warn"); return; }
  navigator.geolocation.getCurrentPosition(
    (p) => { send({ cmd: "adsb_pos", lat: p.coords.latitude.toFixed(5), lon: p.coords.longitude.toFixed(5) }); ADS.view.user = false; ADS.view.fitN = 0; },
    () => toast("Location not available here (it only works on https or on the radio PC itself) – type your latitude and longitude instead", "warn"),
    { timeout: 10000 });
};
["adsbUnits", "adsbTiles", "adsbTrails", "adsbLabels", "adsbPosOnly"].forEach((id) => ($(id).onchange = () => {
  const k = { adsbUnits: "units", adsbTiles: "tiles", adsbTrails: "trails", adsbLabels: "labels", adsbPosOnly: "posonly" }[id];
  adsSetLS(k, $(id).type === "checkbox" ? ($(id).checked ? "1" : "0") : $(id).value);
  adsbUnitsHdr(); renderAdsbTable(); adsDraw();
}));
$("adsbFind").oninput = renderAdsbTable;
$("adsbShare").onchange = () => send({ cmd: "adsb_share", on: $("adsbShare").checked });
$("adsbLookup").onchange = () => send({ cmd: "adsb_option", key: "lookup", value: $("adsbLookup").checked });
$("adsbLog").onchange = () => send({ cmd: "adsb_option", key: "log", value: $("adsbLog").checked });
$("adsbFit").onclick = () => { ADS.view.user = false; ADS.view.fitN = 0; adsFit(); adsDraw(); };
$("adsbZoomIn").onclick = () => adsZoom(0.5);
$("adsbZoomOut").onclick = () => adsZoom(-0.5);

/* ---- map (Web Mercator so optional street-map tiles line up with the aircraft) */
const adsMx = (lon, z) => ((lon + 180) / 360) * 256 * 2 ** z;
const adsMy = (lat, z) => { const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180); return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * 256 * 2 ** z; };
const adsLon = (x, z) => (x / (256 * 2 ** z)) * 360 - 180;
const adsLat = (y, z) => { const n = Math.PI - (2 * Math.PI * y) / (256 * 2 ** z); return (180 / Math.PI) * Math.atan(Math.sinh(n)); };
function adsSize() {
  const c = $("adsbMap"), r = c.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.max(200, Math.round(r.width)), h = Math.max(200, Math.round(r.height));
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); c.style.width = w + "px"; c.style.height = h + "px"; }
  return [c.getContext("2d"), w, h, dpr];
}
function adsFit() {
  const r = adsRef(), pts = ADS.list.filter((a) => a.lat != null && a.seen <= 90).map((a) => [a.lat, a.lon]);
  if (r) pts.push(r);
  if (!pts.length) return;
  const [, w, h] = adsSize();
  let cLat, cLon;
  if (r) { [cLat, cLon] = r; } else {
    cLat = (Math.min(...pts.map((p) => p[0])) + Math.max(...pts.map((p) => p[0]))) / 2;
    cLon = (Math.min(...pts.map((p) => p[1])) + Math.max(...pts.map((p) => p[1]))) / 2;
  }
  let z = 12;
  for (; z > 3; z -= 0.25) {
    const cx = adsMx(cLon, z), cy = adsMy(cLat, z);
    if (pts.every((p) => Math.abs(adsMx(p[1], z) - cx) < w * 0.45 && Math.abs(adsMy(p[0], z) - cy) < h * 0.45)) break;
  }
  Object.assign(ADS.view, { lat: cLat, lon: cLon, z: Math.min(z, 11) });
}
function adsZoom(dz, px, py) {
  const [, w, h] = adsSize(), v = ADS.view;
  px = px ?? w / 2; py = py ?? h / 2;
  const z0 = v.z, cx = adsMx(v.lon, z0), cy = adsMy(v.lat, z0);
  const lon = adsLon(cx + px - w / 2, z0), lat = adsLat(cy + py - h / 2, z0);
  v.z = Math.max(3, Math.min(12, v.z + dz)); v.user = true;
  v.lon = lon - (px - w / 2) / (256 * 2 ** v.z) * 360; v.lat = adsLat(adsMy(lat, v.z) - (py - h / 2), v.z);
  adsDraw();
}
function adsAltColor(a, alpha = 1, light = false) {
  if (a.ground) return `hsla(0,0%,60%,${alpha})`;
  if (a.alt == null) return `hsla(0,0%,75%,${alpha})`;
  const hue = 18 + Math.min(Math.max(a.alt, 0), 40000) / 40000 * 222;     // orange (low) -> green -> blue (high)
  return `hsla(${hue},95%,${light ? 42 : 58}%,${alpha})`;
}
const ADS_PLANE = [[0, -10], [1.6, -3], [9, 2], [9, 4], [1.6, 2.5], [1.2, 7], [3.5, 9], [3.5, 10.5], [0, 9.5], [-3.5, 10.5], [-3.5, 9], [-1.2, 7], [-1.6, 2.5], [-9, 4], [-9, 2], [-1.6, -3]];
function adsDraw() { if (!ADS.raf) ADS.raf = requestAnimationFrame(adsRender); }
function adsRender() {
  ADS.raf = 0;
  if (!adsTabOpen()) return;
  const [ctx, w, h, dpr] = adsSize(), v = ADS.view, z = v.z;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const light = adsLight(), mapStyle = adsMapStyle();
  ctx.fillStyle = light ? "#dfe8ee" : "#1c2530"; ctx.fillRect(0, 0, w, h);
  const cx = adsMx(v.lon, z), cy = adsMy(v.lat, z), ox = cx - w / 2, oy = cy - h / 2;
  const P = (lat, lon) => [adsMx(lon, z) - ox, adsMy(lat, z) - oy];
  if (mapStyle === "plain" || mapStyle === "plaindark") {           // offline: faint lat/lon grid
    ctx.strokeStyle = light ? "rgba(60,90,120,.18)" : "rgba(150,180,210,.12)"; ctx.lineWidth = 1;
    const gs = z >= 9 ? 0.25 : z >= 7 ? 1 : z >= 5 ? 2 : 5;
    for (let lo = Math.floor(adsLon(ox, z) / gs) * gs; lo <= adsLon(ox + w, z); lo += gs) { const x = adsMx(lo, z) - ox; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
    for (let la = Math.floor(adsLat(oy + h, z) / gs) * gs; la <= adsLat(oy, z); la += gs) { const y = adsMy(la, z) - oy; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  }
  // street map tiles (optional)
  if (mapStyle === "bright" || mapStyle === "dark") {
    const zi = Math.max(3, Math.min(12, Math.round(z))), n = 2 ** zi, ts = 256 * 2 ** (z - zi);
    ctx.save();
    if (!light && "filter" in ctx) ctx.filter = "invert(1) hue-rotate(180deg) brightness(.95) saturate(.6)";
    for (let tx = Math.floor(ox / ts); tx <= Math.floor((ox + w) / ts); tx++) {
      for (let ty = Math.max(0, Math.floor(oy / ts)); ty <= Math.min(n - 1, Math.floor((oy + h) / ts)); ty++) {
        const key = `${zi}/${((tx % n) + n) % n}/${ty}`;
        let im = ADS.tiles.get(key);
        if (!im) {
          if (ADS.tiles.size > 400) ADS.tiles.delete(ADS.tiles.keys().next().value);
          im = new Image(); im.src = `https://tile.openstreetmap.org/${key}.png`; im._ok = false;
          im.onload = () => { im._ok = true; adsDraw(); };
          ADS.tiles.set(key, im);
        }
        if (im._ok) ctx.drawImage(im, Math.round(tx * ts - ox), Math.round(ty * ts - oy), Math.ceil(ts) + 1, Math.ceil(ts) + 1);
      }
    }
    ctx.restore();
    ctx.fillStyle = "rgba(0,0,0,.55)"; ctx.fillRect(w - 168, h - 17, 168, 17);
    ctx.fillStyle = "#cfd6df"; ctx.font = "10px sans-serif"; ctx.textAlign = "right"; ctx.fillText("© OpenStreetMap contributors", w - 5, h - 5);
  }
  const met = adsMet(), ref = adsRef();
  // range rings around the receiver
  if (ref) {
    const [rx, ry] = P(ref[0], ref[1]);
    const pxPerNM = (256 * 2 ** z) / (40075016.686 * Math.cos((ref[0] * Math.PI) / 180)) * 1852;
    const unitPx = met ? pxPerNM / 1.852 : pxPerNM;
    const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
    const step = steps.find((s) => s * unitPx >= 80) || 1000;
    ctx.lineWidth = 1; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
    const far = Math.hypot(Math.max(rx, w - rx), Math.max(ry, h - ry));
    for (let k = 1; k * step * unitPx < far + 20 && k < 40; k++) {
      const r = k * step * unitPx;
      ctx.strokeStyle = light ? "rgba(30,80,140,.45)" : "rgba(120,170,220,.35)"; ctx.beginPath(); ctx.arc(rx, ry, r, 0, 7); ctx.stroke();
      ctx.fillStyle = light ? "rgba(20,60,110,.9)" : "rgba(170,205,240,.85)"; ctx.fillText(`${k * step} ${met ? "km" : "NM"}`, rx + 4, ry - r + 11);
    }
    ctx.strokeStyle = light ? "rgba(30,80,140,.4)" : "rgba(120,170,220,.4)"; ctx.beginPath(); ctx.moveTo(rx - 9999, ry); ctx.lineTo(rx + 9999, ry); ctx.moveTo(rx, ry - 9999); ctx.lineTo(rx, ry + 9999); ctx.stroke();
    ctx.fillStyle = "#4ddb7a"; ctx.beginPath(); ctx.arc(rx, ry, 4, 0, 7); ctx.fill();
    ctx.strokeStyle = light ? "#fff" : "#0b0e12"; ctx.lineWidth = 1.5; ctx.stroke();
  }
  // trails + aircraft
  const showTrails = $("adsbTrails").checked, showLbl = $("adsbLabels").checked, now = performance.now();
  const q = $("adsbFind").value.trim().toLowerCase();
  ADS.hit = [];
  const draw = ADS.list.filter((a) => a.lat != null && a.seen <= 90);
  if (showTrails) {
    ctx.lineWidth = 1.6; ctx.lineCap = "round";
    for (const a of draw) {
      const tr = ADS.trails.get(a.icao); if (!tr || tr.length < 2) continue;
      for (let i = 1; i < tr.length; i++) {
        const [x0, y0] = P(tr[i - 1][0], tr[i - 1][1]), [x1, y1] = P(tr[i][0], tr[i][1]);
        ctx.strokeStyle = adsAltColor({ alt: tr[i][2], ground: a.ground }, (light ? 0.3 : 0.15) + 0.6 * (i / tr.length), light);
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      }
    }
  }
  ctx.font = "11px sans-serif"; ctx.textAlign = "left";
  for (const a of draw) {
    const [x, y] = P(a.lat, a.lon);
    if (x < -30 || y < -30 || x > w + 30 || y > h + 30) continue;
    const dim = q && !`${a.icao} ${a.flight} ${a.airline} ${a.country}`.toLowerCase().includes(q);
    const fade = (dim ? 0.25 : 1) * (a.seen > 30 ? 0.55 : 1);
    ADS.hit.push([x, y, a.icao]);
    const em = ADS_EMER[a.squawk];
    if (em) { ctx.strokeStyle = `rgba(255,59,48,${0.5 + 0.5 * Math.sin(now / 200)})`; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(x, y, 16, 0, 7); ctx.stroke(); }
    if (ADS.sel === a.icao) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, 15, 0, 7); ctx.stroke(); }
    ctx.save(); ctx.translate(x, y); ctx.globalAlpha = fade;
    if (a.trk != null) ctx.rotate((a.trk * Math.PI) / 180);
    ctx.beginPath();
    if (a.ground || a.trk == null) { ctx.arc(0, 0, 4.5, 0, 7); } else { ADS_PLANE.forEach(([px, py], i) => (i ? ctx.lineTo(px * 0.85, py * 0.85) : ctx.moveTo(px * 0.85, py * 0.85))); ctx.closePath(); }
    ctx.fillStyle = adsAltColor(a, 1, light); ctx.fill(); ctx.strokeStyle = light ? "rgba(0,0,0,.85)" : "rgba(255,255,255,.55)"; ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
    if (showLbl && !dim) {
      const t1 = a.flight || a.icao, t2 = a.ground ? "GND" : a.alt != null ? (met ? Math.round(a.alt * 0.3048) + "m" : "FL" + String(Math.round(a.alt / 100)).padStart(3, "0")) : "";
      ctx.globalAlpha = fade; ctx.lineWidth = 3; ctx.strokeStyle = light ? "rgba(255,255,255,.9)" : "rgba(0,0,0,.8)"; ctx.fillStyle = light ? "#10202e" : "#e8edf3";
      ctx.strokeText(t1, x + 12, y - 1); ctx.fillText(t1, x + 12, y - 1);
      ctx.fillStyle = light ? "#34506a" : "#9fb0c3"; ctx.strokeText(t2, x + 12, y + 10); ctx.fillText(t2, x + 12, y + 10);
      ctx.globalAlpha = 1;
    }
  }
  if (!draw.length) {
    ctx.fillStyle = light ? "#4a5a6a" : "#9fb0c3"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
    ctx.fillText(adsRunning() ? "Waiting for aircraft with a position …" : "Press ▶ Start ADS-B (1090 MHz)", w / 2, h / 2);
  }
  if (ADS.list.some((a) => ADS_EMER[a.squawk] && a.seen <= 90)) setTimeout(adsDraw, 120);   // keep the emergency ring pulsing
}
(function adsMapEvents() {
  const c = $("adsbMap");
  let drag = null;
  c.addEventListener("pointerdown", (e) => { c.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY, lat: ADS.view.lat, lon: ADS.view.lon, moved: false }; c.classList.add("drag"); });
  c.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (!drag.moved) return;
    const z = ADS.view.z;
    ADS.view.lon = adsLon(adsMx(drag.lon, z) - dx, z); ADS.view.lat = adsLat(adsMy(drag.lat, z) - dy, z); ADS.view.user = true; adsDraw();
  });
  c.addEventListener("pointerup", (e) => {
    c.classList.remove("drag");
    if (drag && !drag.moved) {
      const r = c.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      let best = null, bd = 16;
      for (const [hx, hy, id] of ADS.hit) { const dd = Math.hypot(hx - x, hy - y); if (dd < bd) { bd = dd; best = id; } }
      ADS.sel = best === ADS.sel ? null : best; if (ADS.sel) send({ cmd: "adsb_lookup", icao: ADS.sel }); renderAdsbTable(); adsDraw();
    }
    drag = null;
  });
  c.addEventListener("wheel", (e) => { e.preventDefault(); const r = c.getBoundingClientRect(); adsZoom(e.deltaY < 0 ? 0.5 : -0.5, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  c.addEventListener("dblclick", (e) => { const r = c.getBoundingClientRect(); adsZoom(1, e.clientX - r.left, e.clientY - r.top); });
  new ResizeObserver(() => adsDraw()).observe(c.parentElement);
})();
setInterval(() => { if (adsTabOpen()) { renderAdsbTable(); } }, 5000);

/* ----- images ----- */
const imgUrl = (f) => `/images/${f.split("/").map(encodeURIComponent).join("/")}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
const recUrl = (f) => `/recordings/${f.split("/").map(encodeURIComponent).join("/")}?token=${encodeURIComponent(token)}`;
const fmtMHzUnit = (hz) => (hz ? fmtMHz(hz, 4) + " MHz" : "");
let IMGS = {};
const imgCaption = (x) => [x.name || "Picture", fmtMHzUnit(x.freq), x.when].filter(Boolean).join(" · ");
function renderImages(list) {
  const items = (list || []).map((x) => (typeof x === "string" ? { f: x, name: "", freq: 0, when: "", dl: x.split("/").pop() } : x));
  IMGS = {}; items.forEach((x) => (IMGS[x.f] = x));
  $("imgGrid").innerHTML = items.map((x) => `<figure><img loading="lazy" src="${imgUrl(x.f)}" data-f="${esc(x.f)}">
    <figcaption><b>${esc(x.name || "Picture")}</b>
      <div class="cap-meta">${esc([fmtMHzUnit(x.freq), x.when].filter(Boolean).join(" · "))}</div>
      <div class="cap-file">${esc(x.f)}</div>
      <a class="btn small dl" data-kind="images" data-f="${esc(x.f)}" href="${imgUrl(x.f)}" download="${esc(x.dl || x.f.split("/").pop())}" title="Save this picture to your Downloads folder">⬇ Download</a> <button class="btn small danger del" data-kind="images" data-f="${esc(x.f)}" title="Delete this picture">🗑 Delete</button></figcaption></figure>`).join("") ||
    '<p class="muted">No pictures yet. Tune a WEFAX station in USB and start the WEFAX decoder, or record a Meteor-M pass with the LRPT decoder.</p>';
  $("imgGrid").querySelectorAll("img").forEach((im) => im.onclick = () => {
    const x = IMGS[im.dataset.f] || { f: im.dataset.f, dl: im.dataset.f.split("/").pop() };
    const v = document.createElement("div"); v.className = "imgview";
    v.innerHTML = `<div class="imgbar"><span>${esc(imgCaption(x))}</span><a class="btn small dl" data-kind="images" data-f="${esc(x.f)}" href="${im.src}" download="${esc(x.dl)}">⬇ Download</a><button class="btn small danger del" data-kind="images" data-f="${esc(x.f)}" title="Delete this picture">🗑 Delete</button><span class="muted small">click anywhere else to close</span></div><img src="${im.src}">`;
    v.onclick = () => v.remove();
    v.querySelector("a").onclick = (e) => e.stopPropagation();
    v.querySelector("button.del").onclick = (e) => { e.stopPropagation(); v.remove(); deleteCapture("images", x.f); };
    document.body.appendChild(v);
  });
}
function addImage(f) {
  const ex = [...$("imgGrid").querySelectorAll("img")].find((i) => i.dataset.f === f);
  if (ex) ex.src = imgUrl(f); else send({ cmd: "images" });
}
$("imgRefresh").onclick = () => send({ cmd: "images" });
// On the radio PC itself the desktop window cannot do browser downloads, so AndySDR copies the file into the
// Downloads folder; on a remote PC / phone the normal browser download (the link's own href) is used.
document.addEventListener("click", (e) => {
  const a = e.target.closest("a.dl[data-kind]");
  if (!a || !isLocal) return;
  e.preventDefault(); e.stopPropagation();
  send({ cmd: "save_download", kind: a.dataset.kind, f: a.dataset.f, name: a.getAttribute("download") });
}, true);

/* ----- instant replay: what you heard in the last few seconds, for stations you just missed ----- */
try { const v = localStorage.getItem("andysdr_replay_secs"); if (v) $("replaySecs").value = v; } catch (e) {}
$("replaySecs").onchange = () => { try { localStorage.setItem("andysdr_replay_secs", $("replaySecs").value); } catch (e) {} };
function doReplay() {
  const a = $("replayAudio"), secs = $("replaySecs").value;
  a.onerror = () => toast("Nothing was heard in the last " + secs + " seconds", "warn");
  a.src = `/api/replay.wav?secs=${secs}&token=${encodeURIComponent(token)}&t=${Date.now()}`;
  a.play().then(() => toast(`Replaying the last ${secs} s of what you heard`)).catch(() => {});
}
$("replayPlay").onclick = doReplay;
$("replaySave").onclick = () => send({ cmd: "replay_save", secs: +$("replaySecs").value });

function deleteCapture(kind, f) {
  if (confirm(`Delete ${f.split("/").pop()}? This cannot be undone.`)) send({ cmd: "delete_capture", kind, f });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("button.del[data-kind]");
  if (b) { e.stopPropagation(); deleteCapture(b.dataset.kind, b.dataset.f); }
});

/* ----- recordings ----- */
const fmtSize = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");
function renderRecordings(list) {
  $("recList").innerHTML = (list || []).map((x) => `<div class="recrow">
      <div class="recinfo"><b>${esc(x.name)}</b>
        <div class="cap-meta">${esc([fmtMHzUnit(x.freq), x.when, fmtSize(x.size || 0)].filter(Boolean).join(" · "))}</div>
        <div class="cap-file">${esc(x.f)}</div></div>
      ${x.iq ? '<span class="muted small">raw IQ for SatDump</span>' : `<audio controls preload="none" src="${recUrl(x.f)}"></audio>`}
      <a class="btn small dl" data-kind="recordings" data-f="${esc(x.f)}" href="${recUrl(x.f)}" download="${esc(x.dl || x.f)}" title="Save this recording to your Downloads folder">⬇ Download</a> <button class="btn small danger del" data-kind="recordings" data-f="${esc(x.f)}" title="Delete this recording">🗑 Delete</button></div>`).join("") ||
    '<p class="muted">No recordings yet. Press the REC key to record what you are listening to, or add a "Record audio" capture in the SCHEDULER.</p>';
}
$("recRefresh").onclick = () => send({ cmd: "recordings" });

/* ----- receiver ----- */
function renderDevices(list) {
  $("devSel").innerHTML = (list || []).map((d) => `<option value="${esc(d.key)}" ${d.key === DEV.key ? "selected" : ""}>${esc(d.label)} (${d.kind})</option>`).join("");
}
function applyDeviceBandplan() {
  if (!DEV || !DEV.freq_max) return;
  if (!limitBandplan(DEV.freq_min, DEV.freq_max)) return;       // the band plan only lists what this receiver can tune
  MAP_HI = Math.min(6.5e9, Math.max(3e7, DEV.freq_max * 1.12));
  const keep = $("pkPlan").value;
  $("pkPlan").innerHTML = Object.entries(BANDPLAN_CATS).filter(([k]) => mainBands(k).length).map(([k, c]) => `<option value="${k}">${esc(c.label)} (${mainBands(k).length})</option>`).join("");
  if ([...$("pkPlan").options].some((o) => o.value === keep)) $("pkPlan").value = keep;
  fillBands($("pkPlan").value);
  renderBandPlan(); drawBandMap(); queueDraw();
}
function renderDevice() {
  if (!DEV || !DEV.key) return;
  applyDeviceBandplan();
  if (knobs.gain) { knobs.gain.min = DEV.gain_min; knobs.gain.max = DEV.gain_max; knobs.gain.render(); }
  [...$("devSel").options].forEach((o) => (o.selected = o.value === DEV.key));
  $("rateSel").innerHTML = (DEV.rates || []).filter((r) => r >= 960000).map((r) => `<option value="${r}" ${r === DEV.sample_rate ? "selected" : ""}>${(r / 1e6).toFixed(2)} MS/s</option>`).join("");
  $("antSel").innerHTML = (DEV.antennas || []).map((a) => `<option ${a === DEV.antenna ? "selected" : ""}>${esc(a)}</option>`).join("") || "<option>default</option>";
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
const BIAS_CHIP = {
  ok: ["● On – noise floor steady", "pic"], settling: ["… settling (measuring)", ""], off: ["Off", ""], no_path: ["⚠ On, but not reaching the antenna", "warn"],
  drop: ["⚠ Noise floor has dropped – power lost?", "warn"], testing: ["Testing …", ""], unsupported: ["Not available on this receiver", ""],
};
function renderBias(b) {
  const card = $("biasCard"); if (!card) return;
  card.hidden = !b || !b.supported; if (card.hidden) return;
  let c = BIAS_CHIP[b.state] || ["–", ""];
  if (b.state === "ok" && b.result && b.result.state === "ok") c = [`● Verified – powered antenna adds ${(b.result.on_db - b.result.off_db).toFixed(1)} dB`, "pic"];
  else if (b.state === "ok" && b.result && b.result.state === "none") c = ["● On – no change seen in power check", ""];
  $("biasChip").textContent = c[0]; $("biasChip").className = "sig-chip " + c[1];
  $("biasAnt").textContent = b.antenna || "–";
  $("biasEff").textContent = !b.on ? "(bias-T is off)" : b.effective ? "Yes" : "NO – select Antenna B";
  $("biasFloor").textContent = b.floor == null ? "–" : b.floor.toFixed(1) + " dB";
  $("biasDelta").textContent = b.delta == null ? "–" : (b.delta > 0 ? "+" : "") + b.delta.toFixed(1) + " dB";
  $("biasOvl").textContent = `${b.overloads}` + (b.overload_rate > 2 ? ` (${b.overload_rate}/min – lower RF GAIN)` : "");
  const h = b.hist || [];
  if (h.length > 1) {
    const lo = Math.min(...h) - 1, hi = Math.max(...h) + 1, w = 240 / (h.length - 1);
    $("biasSpark").innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${h.map((v, i) => `${(i * w).toFixed(1)},${(38 - ((v - lo) / (hi - lo)) * 36).toFixed(1)}`).join(" ")}"/>`;
  }
  $("biasMsg").textContent = b.result ? b.result.text : "";
  $("biasTest").disabled = b.state === "testing" || !b.on;
}
$("biasTest").onclick = () => send({ cmd: "bias_test" });
$("devOpen").onclick = () => send({ cmd: "device", key: $("devSel").value });
$("devRefresh").onclick = () => send({ cmd: "refresh_devices" });
$("tcpAdd").onclick = () => { if ($("tcpAddr").value.includes(":")) send({ cmd: "add_rtltcp", addr: $("tcpAddr").value.trim() }); else toast("Use host:port, e.g. 192.168.1.20:1234", "warn"); };
$("rateSel").onchange = () => send({ cmd: "device_setting", key: "sample_rate", value: +$("rateSel").value });
$("antSel").onchange = () => send({ cmd: "device_setting", key: "antenna", value: $("antSel").value });
$("ppmSet").onclick = () => send({ cmd: "device_setting", key: "ppm", value: +$("ppmInp").value });
$("gainAgc").onclick = () => send({ cmd: "agc", on: !$("agcChk").checked });
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
  makePalette();
  if (save) { localStorage.setItem("andysdr_theme", t); send({ cmd: "settings", values: { theme: t }, quiet: true }); }
}
$("themeSel").onchange = () => setTheme($("themeSel").value);
setTheme(localStorage.getItem("andysdr_theme") || "modern", false);

/* ----- toasts ----- */
function toast(text, level = "info") {
  const t = document.createElement("div"); t.className = "toast " + level; t.textContent = text;
  $("toasts").appendChild(t); setTimeout(() => t.remove(), level === "error" ? 9000 : 4500);
}

/* ======================================================= quick buttons, status strip, LEDs */
const Q_BANDS = [
  // label, tune Hz, mode, step, range MHz [lo, hi], tooltip
  ["AIR", 121500000, "AM", 8330, [118, 137], "UK airband (118–137 MHz, AM). Jumps to 121.500 MHz, the emergency channel."],
  ["MARINE", 156800000, "NFM", 25000, [156, 162.025], "Marine VHF. Jumps to channel 16 (156.800 MHz), the calling and distress channel."],
  ["PMR", 446006250, "NFM", 6250, [446, 446.2], "PMR446 licence-free handhelds. Jumps to channel 1 (446.00625 MHz)."],
  ["2M", 145500000, "NFM", 12500, [144, 146], "2 m amateur band. Jumps to the 145.500 MHz FM calling channel."],
  ["70CM", 433500000, "NFM", 12500, [430, 440], "70 cm amateur band. Jumps to 433.500 MHz."],
  ["CB", 27601250, "NFM", 10000, [27.6, 27.992], "UK CB (27/81 set, 40 channels). Jumps to channel 1 (27.60125 MHz)."],
  ["FM BC", 100000000, "WFM", 100000, [87.5, 108], "FM broadcast radio. Jumps to 100.0 MHz in wide FM."],
  ["PAGER", 153000000, "DIG", 12500, [153, 154], "UK pager band (153.0–153.5 MHz busiest). Jumps to the bottom of it – then use SRCH or ▲ to find the active channel."],
  ["SATS", 137900000, "DIG25", 25000, [137, 138], "Weather satellites (Meteor-M LRPT on 137.9 MHz). Start the LRPT decoder when a satellite is overhead."],
];
const Q_DECS = [
  ["DMR", "dsd", "dmr"], ["P25", "dsd", "p25p1"], ["NXDN", "dsd", "nxdn48"], ["dPMR", "dsd", "dpmr"], ["M17", "dsd", "m17"],
  ["TETRA", "tetra", ""], ["PAGER", "pager", ""], ["APRS", "data", ""], ["WEFAX", "wefax", ""], ["SSTV", "sstv", ""],
  ["LRPT", "lrpt", ""], ["GOES", "goes", ""], ["ADS-B", "adsb", ""], ["FT8", "ft8", "ft8"], ["FT4", "ft8", "ft4"], ["MORSE", "morse", ""],
];
const Q_DEC_TIPS = { dmr: "DMR digital voice", p25p1: "P25 Phase 1 digital voice", nxdn48: "NXDN 4800 digital voice", dpmr: "dPMR digital voice",
  m17: "M17 digital voice", tetra: "TETRA network and call signalling (25 kHz, DIG25)", pager: "POCSAG / FLEX pagers – messages appear in PAGER MESSAGES",
  data: "APRS, DTMF, ZVEI, EAS", wefax: "Weather fax charts (USB mode)", sstv: "SSTV pictures", lrpt: "Meteor-M satellite pictures",
  adsb: "Aircraft tracking on 1090 MHz – opens the ADS-B tab data", goes: "GOES-East/West HRIT images on 1694.1 MHz (needs a dish and LNA)",
  ft8: "FT8 weak-signal digital (USB, 15 s slots) – messages appear in the FT8 tab", ft4: "FT4 (USB, 7.5 s slots)", morse: "Morse code (CW) decoder – opens a box with a ticker tape and the most popular Morse frequencies" };
let sqPrev = -60;

function buildQuick() {
  $("qMode").innerHTML = MODES.map((m) => `<button class="key q" data-m="${m}" title="Switch to ${m}">${m}</button>`).join("");
  $("qBand").innerHTML = Q_BANDS.map((b, i) => `<button class="key q" data-b="${i}" title="${b[5]}">${b[0]}</button>`).join("");
  $("qDec").innerHTML = `<button class="key q" data-d="autoid" title="Automatic signal identification (Auto ID): AndySDR picks the mode and decoder for you">AUTO</button>` +
    Q_DECS.map((d, i) => `<button class="key q" data-d="${i}" title="${Q_DEC_TIPS[d[2] || d[1]]}">${d[0]}</button>`).join("") +
    `<button class="key q danger" data-d="off" title="Stop the running decoder">OFF</button>`;
  $("qLock").innerHTML = [
    "SKIP 10 MIN|t10|Ignore the channel you are stopped on (or the current one) for 10 minutes, then scan it again. Works while scanning and searching.",
    "SKIP 1 HOUR|t60|Ignore it for an hour.", "SKIP TODAY|t0|Ignore it until AndySDR is closed or you press UNSKIP.",
    "LOCK OUT|perm|Permanent lockout (same as the L/O key): a memory channel stays locked out until you untick L/O in the MEMORY tab; a searched frequency is ignored until UNSKIP.",
    "UNSKIP|clr|Stop ignoring everything you skipped or locked out while searching."]
    .map((x) => { const [l, k, t] = x.split("|"); return `<button class="key q ${k === "clr" ? "" : "danger"}" data-l="${k}" title="${t}">${l}</button>`; }).join("");
  $("qTog").innerHTML = ["AGC|agc|Automatic gain control on/off (turns RF GAIN to automatic)", "MUTE|mute|Silence the sound on this screen without changing the volume knob",
    "MONITOR|mon|Hold the squelch open to listen to the hiss/background – press again to go back to your squelch setting",
    "STEP −|stepdn|Smaller tuning step", "STEP +|stepup|Larger tuning step"]
    .map((x) => { const [l, k, t] = x.split("|"); return `<button class="key q" data-t="${k}" title="${t}">${l}</button>`; }).join("");
  $("quick").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b || explainOn) return;
    if (b.dataset.m) send({ cmd: "mode", mode: b.dataset.m });
    else if (b.dataset.b !== undefined) { const q = Q_BANDS[+b.dataset.b]; send({ cmd: "tune", freq: q[1], mode: q[2] }); send({ cmd: "stepsize", hz: q[3] }); }
    else if (b.dataset.d === "autoid") send({ cmd: "auto_identify", on: !(S.auto_identify !== false) });
    else if (b.dataset.d === "off") send({ cmd: "decoder", id: "" });
    else if (b.dataset.d !== undefined) {
      const d = Q_DECS[+b.dataset.d];
      if (d[1] === "morse") { morseOpen(true); return; }
      send({ cmd: "decoder", id: d[1], sub: d[2] || "auto" });
    }
    else if (b.dataset.l) {
      const k = b.dataset.l;
      if (k === "perm") send({ cmd: "lockout" });
      else if (k === "clr") send({ cmd: "lockout_clear" });
      else send({ cmd: "lockout_temp", minutes: +k.slice(1) });
    }
    else if (b.dataset.f === "replay") doReplay();
    else if (b.dataset.f) showTab(b.dataset.f === "images" ? "img" : "rec");
    else if (b.dataset.t === "agc") send({ cmd: "agc", on: !S.agc });
    else if (b.dataset.t === "mute") { player.muted = !player.muted; player.setVolume(player.vol); renderQuick(); }
    else if (b.dataset.t === "mon") {
      if (S.squelch > -119) { sqPrev = S.squelch; send({ cmd: "squelch", value: -120 }); } else send({ cmd: "squelch", value: sqPrev });
    }
    else if (b.dataset.t === "stepdn" || b.dataset.t === "stepup") {
      const i = STEPS.indexOf(S.step), j = Math.max(0, Math.min(STEPS.length - 1, (i < 0 ? STEPS.findIndex((x) => x >= S.step) : i) + (b.dataset.t === "stepup" ? 1 : -1)));
      send({ cmd: "stepsize", hz: STEPS[j] });
    }
  });
}
function renderQuick() {
  if (!$("qMode").firstChild) return;
  const f = (S.freq || 0) / 1e6;
  $("qMode").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.m === S.mode));
  $("qBand").querySelectorAll("button").forEach((b) => { const r = Q_BANDS[+b.dataset.b][4]; b.classList.toggle("on", f >= r[0] && f <= r[1]); });
  $("qDec").querySelectorAll("button").forEach((b) => {
    const d = b.dataset.d;
    if (d === "autoid") b.classList.toggle("on", S.auto_identify !== false);
    else if (d === "off") b.classList.toggle("on", !S.decoder);
    else { const q = Q_DECS[+d]; b.classList.toggle("on", S.decoder === q[1] && (q[1] !== "dsd" || S.decoder_sub === q[2]));
      const info = decoders.find((x) => x.id === q[1]); b.classList.toggle("na", !!info && !info.ready); }
  });
  const lo = S.lockouts || { count: 0, list: [] };
  $("qLock").querySelectorAll("button").forEach((b) => {
    if (b.dataset.l === "clr") { b.textContent = lo.count ? `UNSKIP (${lo.count})` : "UNSKIP"; b.title = lo.count ? "Ignored now – click to scan them again:\n" + lo.list.join("\n") : "Nothing is being skipped"; b.classList.toggle("on", lo.count > 0); }
    else if (b.dataset.l.startsWith("t")) b.classList.toggle("on", !!lo.current);
  });
  $("qTog").querySelectorAll("button").forEach((b) => {
    const t = b.dataset.t;
    if (t === "agc") b.classList.toggle("on", !!S.agc);
    else if (t === "mute") b.classList.toggle("on", player.muted);
    else if (t === "mon") b.classList.toggle("on", S.squelch <= -119);
  });
}
/* ======================================================= bandwidth selector */
const fmtBw = (hz) => hz >= 1000 ? (hz / 1000).toFixed(hz % 1000 ? 1 : 0).replace(/\.0$/, "") + " kHz" : hz + " Hz";
// What each decoder / identified signal needs: [mode-independent width in Hz, why]
const BW_DECODER = { dsd: [12500, "digital voice channel"], tetra: [25000, "TETRA 25 kHz carrier"], pager: [12500, "pager channel"], data: [12500, "data channel"],
  sstv: null, wefax: [2400, "weather-fax audio"], lrpt: [25000, "Meteor LRPT"], ft8: [2700, "FT8/FT4 audio"] };
const BW_SIGNAL = [[/M17/, 9000], [/TETRA/, 25000], [/DMR|P25|NXDN|dPMR|Digital|Pager|APRS|DTMF/, 12500]];
function nearestBw(mode, want, atLeast) {
  const list = (BWS[mode] || []).map((x) => x[0]);
  if (!list.length) return 0;
  if (atLeast) { const ok = list.filter((h) => h >= want); if (ok.length) return ok[0]; }
  return list.reduce((a, b) => Math.abs(b - want) < Math.abs(a - want) ? b : a);
}
function suggestBw() {
  const mode = S.mode, list = BWS[mode] || [];
  if (!list.length) return null;
  const def = (list.find((x) => /default/.test(x[1])) || list[0])[0];
  if (mode === "WFM") return { hz: list[0][0], why: "broadcast FM is always 200 kHz" };
  // 1. a decoder that is running knows what it needs
  const d = S.decoder === "dsd" && /m17/i.test(S.decoder_sub || "") ? [9000, "M17 voice"] : BW_DECODER[S.decoder];
  if (S.decoder && d) {
    if (["USB", "LSB"].includes(mode) || mode === "CW") return { hz: nearestBw(mode, d[0]), why: "for " + d[1] };
    return { hz: nearestBw(mode, d[0]), why: "for the " + d[1] };
  }
  // analogue FM voice never needs less than the mode's normal width (the band plan's 6.25 kHz is only a grid)
  const fm = (hz) => ["NFM", "FM"].includes(mode) ? Math.max(hz, def) : hz;
  // broadcast stations and CW have a well-known width of their own
  const bb = S.freq ? bandsAt(S.freq).filter((x) => !x.point)[0] : null;
  if (mode === "AM" && bb && bb.cat === "broadcast") {
    const lw = S.freq < 1.7e6;
    return { hz: nearestBw(mode, lw ? 6000 : 8000), why: lw ? "long/medium-wave station (9 kHz channel spacing)" : "shortwave broadcast station" };
  }
  if (mode === "CW") return { hz: nearestBw(mode, 500), why: "standard Morse filter" };
  // 2. what Auto ID measured / recognised
  const sg = S.signal;
  if (sg && sg.stable !== false) {
    const hit = BW_SIGNAL.find(([re]) => re.test(sg.kind || sg.label || ""));
    if (hit && !["USB", "LSB", "CW"].includes(mode)) return { hz: nearestBw(mode, hit[1]), why: "identified: " + (sg.label || sg.kind) };
    if (sg.bw && sg.bw > 1 && !["USB", "LSB", "CW"].includes(mode)) {
      const hz = fm(nearestBw(mode, sg.bw * 1000 * 1.15, true));
      return { hz, why: hz === def ? "normal width for " + mode + " (signal measures " + sg.bw + " kHz)" : `measured ${sg.bw} kHz signal` };
    }
  }
  // 3. the band plan: channel spacing of the band you are tuned to
  const b = S.freq ? bandsAt(S.freq).filter((x) => !x.point)[0] : null;
  const step = b && b.step;
  if (b && step >= 6250 && step <= 25000 && !["USB", "LSB", "CW"].includes(mode)) {
    const ideal = step <= 12500 ? step : step * (mode === "AM" ? 0.6 : 0.68);
    const hz = fm(nearestBw(mode, ideal));
    return { hz, why: `${b.name}: ${fmtBw(step)} channel spacing` };
  }
  if (["USB", "LSB"].includes(mode)) return { hz: 2700, why: "standard SSB voice" };
  return { hz: def, why: "normal width for " + mode };
}
let bwLastAuto = { hz: 0, t: 0 };
function renderBw() {
  const sel = $("bwSel"); if (!sel) return;
  const mode = S.mode, list = BWS[mode] || [];
  const sug = suggestBw();
  const cur = S.bw || 0;
  const auto = S.bw_auto !== false;
  const key = mode + "|" + (sug ? sug.hz : 0) + "|" + cur + "|" + auto;
  if (sel.dataset.k !== key) {
    sel.dataset.k = key;
    let html = `<option value="auto" ${auto ? "selected" : ""}>Auto – ${sug ? fmtBw(sug.hz) : "default"} (suggested)</option>`;
    html += list.map(([hz, lab]) => `<option value="${hz}" ${!auto && hz === cur ? "selected" : ""}>${sug && hz === sug.hz ? "★ " : ""}${lab}</option>`).join("");
    sel.innerHTML = html;
    sel.disabled = mode === "WFM";
    const btn = $("bwSug");
    btn.disabled = !sug || mode === "WFM";
    btn.classList.toggle("suggest", !!sug && (auto ? false : cur !== sug.hz));
    btn.title = sug ? `Use the suggested ${fmtBw(sug.hz)} (${sug.why})` : "";
    $("bwWhy").textContent = sug ? `★ ${fmtBw(sug.hz)} – ${sug.why}` : "";
  }
  // Auto: keep the radio on the suggestion (the first screen to notice sends it; the rest see the new state)
  if (auto && sug && sug.hz !== cur && !(bwLastAuto.hz === sug.hz && Date.now() - bwLastAuto.t < 1500)) {
    bwLastAuto = { hz: sug.hz, t: Date.now() };
    send({ cmd: "bandwidth", bw: sug.hz, auto: true });
  }
}
function bwInit() {
  $("bwSel").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "auto") { const sug = suggestBw(); send({ cmd: "bandwidth", bw: sug ? sug.hz : 0, auto: true }); }
    else send({ cmd: "bandwidth", bw: parseInt(v, 10), auto: false });
  });
  $("bwSug").addEventListener("click", () => { const sug = suggestBw(); if (sug) send({ cmd: "bandwidth", bw: sug.hz, auto: false }); });
}

function renderTop() {
  const set = (id, v) => { const e = $(id); if (e.textContent !== v) e.textContent = v; };
  set("tRx", S.device ? S.device.replace(/^SDRplay\s*/i, "").slice(0, 26) : "–");
  set("tSpan", S.fs ? (S.fs / 1e6).toFixed(2) + " MS/s" : "–");
  set("tBw", S.bw ? fmtBw(S.bw) + " " + S.mode : "–");
  const st = { scan: "SCANNING", search: "SEARCHING", hold: "HOLD", manual: "MANUAL" }[S.scan] || String(S.scan || "").toUpperCase();
  set("tState", S.job ? "JOB: " + (S.job.name || "").slice(0, 14).toUpperCase() : st);
  const dec = S.decoder ? ((decoders.find((x) => x.id === S.decoder) || {}).name || S.decoder).replace(/\s*\(.*$/, "").slice(0, 18) : "OFF";
  set("tDec", dec.toUpperCase());
  set("tLinks", String(S.listeners || 1));
  $("tState").parentElement.classList.toggle("hot", S.scan === "scan" || S.scan === "search" || S.scan === "hold");
  $("tDec").parentElement.classList.toggle("hot", !!S.decoder);
  $("ledRx").classList.toggle("on", !!S.sq_open);
  $("ledScan").classList.toggle("on", S.scan === "scan" || S.scan === "search" || S.scan === "hold");
  $("ledScan").classList.toggle("blink", S.scan === "scan" || S.scan === "search");
  $("ledDec").classList.toggle("on", !!S.decoder);
  $("ledRec").classList.toggle("on", !!S.recording);
}
function renderClock() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  $("tLocal").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  $("tUtc").textContent = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}
buildQuick(); bwInit(); renderClock(); setInterval(renderClock, 1000);

/* ======================================================= updates */
let UPD = { state: "idle", progress: 0 };
const updDismissed = () => sessionStorage.getItem("andysdr_upd_later");
function onUpdate(u, hello = false) {
  const prev = UPD.state;
  UPD = Object.assign({}, u);
  $("updCur").textContent = "v" + u.current;
  const line = $("updLine");
  $("updShow").hidden = !["available", "downloading", "ready"].includes(u.state);
  switch (u.state) {
    case "checking": line.textContent = "– checking…"; break;
    case "uptodate": line.textContent = "– you are up to date"; break;
    case "available": line.textContent = `– version ${u.latest} is available` + (u.skipped ? " (skipped)" : ""); break;
    case "downloading": line.textContent = `– downloading ${u.latest}…`; break;
    case "ready": line.textContent = `– ${u.latest} downloaded, ready to install`; break;
    case "installing": line.textContent = "– installing…"; break;
    case "error": line.textContent = "– " + u.error; break;
    default: line.textContent = "";
  }
  if (u.state === "uptodate" && u.manual && prev === "checking") toast("AndySDR is up to date (v" + u.current + ")");
  if (u.state === "error" && u.manual && prev === "checking") toast("Update check failed: " + u.error, "error");
  // Pop up for a new version unless it was skipped or "remind me later" was pressed in this session;
  // pressing Check always shows it. A freshly finished download always asks about installing.
  const later = updDismissed() === u.latest;
  let show = false;
  if (u.state === "available") show = (!u.skipped || u.manual) && (!later || u.manual);
  else if (u.state === "ready") show = prev === "downloading" || !later;
  else if (!$("updateModal").hidden) show = ["downloading", "installing", "error"].includes(u.state);
  if (show) renderUpdateModal();
  if (["uptodate", "idle", "checking"].includes(u.state)) $("updateModal").hidden = true;
}
function renderUpdateBar() {
  $("updBar").style.width = Math.round((UPD.progress || 0) * 100) + "%";
  if (UPD.state === "downloading") $("updMsg").textContent = `Downloading AndySDR ${UPD.latest}… ${Math.round((UPD.progress || 0) * 100)}%`;
}
function renderUpdateModal() {
  const u = UPD, go = $("updGo");
  $("updateModal").hidden = false;
  const mb = u.size ? ` (${(u.size / 1048576).toFixed(0)} MB)` : "";
  $("updNotes").textContent = u.notes || "";
  $("updErr").textContent = u.state === "error" ? u.error : "";
  $("updBarWrap").hidden = !["downloading", "ready"].includes(u.state);
  $("updBar").style.width = Math.round((u.progress || 0) * 100) + "%";
  $("updLater").hidden = $("updSkip").hidden = ["downloading", "installing"].includes(u.state);
  $("updSkip").hidden = $("updSkip").hidden || u.state === "ready";
  go.hidden = false; go.disabled = false;
  const remote = !isLocal;
  switch (u.state) {
    case "available":
      $("updTitle").textContent = "Update available";
      if (u.can_install) {
        $("updMsg").textContent = `AndySDR ${u.latest} is available – you have ${u.current}. Download${mb} and install it now?`;
        go.textContent = "Download & install"; go.onclick = () => send({ cmd: "update_download" });
      } else {
        $("updMsg").textContent = `AndySDR ${u.latest} is available – you have ${u.current}.` +
          (u.mode === "source" ? " This copy runs from source, so update it with git pull or download the new release." : " The download could not be found – open the release page.");
        go.textContent = "Open release page"; go.onclick = () => { window.open(u.url, "_blank", "noopener"); };
      }
      break;
    case "downloading":
      $("updTitle").textContent = "Downloading update"; renderUpdateBar(); go.hidden = true; break;
    case "ready":
      $("updTitle").textContent = "Ready to install";
      $("updMsg").textContent = `AndySDR ${u.latest} has been downloaded and checked. AndySDR will close, update itself and restart` +
        (remote ? " on the radio PC (this screen reconnects in a few seconds)" : "") + ". Install now?";
      go.textContent = "Install & restart"; go.onclick = () => { go.disabled = true; send({ cmd: "update_install" }); };
      $("updLater").textContent = "Not now"; break;
    case "installing":
      $("updTitle").textContent = "Updating…"; $("updMsg").textContent = "AndySDR is closing to install the update and will restart by itself.";
      $("updBarWrap").hidden = true; go.hidden = true; break;
    case "error":
      $("updTitle").textContent = "Update problem"; $("updMsg").textContent = "The update could not be completed.";
      go.textContent = "Try again"; go.onclick = () => send({ cmd: "update_check" }); break;
    default: break;
  }
  if (u.state !== "ready") $("updLater").textContent = "Remind me later";
}
$("updLater").onclick = () => { sessionStorage.setItem("andysdr_upd_later", UPD.latest || ""); $("updateModal").hidden = true; };
$("updClose").onclick = () => { sessionStorage.setItem("andysdr_upd_later", UPD.latest || ""); $("updateModal").hidden = true; };
$("updSkip").onclick = () => { send({ cmd: "update_skip" }); $("updateModal").hidden = true; toast(`Version ${UPD.latest} skipped. Check for updates now brings it back.`); };
$("updCheck").onclick = () => send({ cmd: "update_check" });
$("updShow").onclick = () => { renderUpdateModal(); };
$("updAuto").onchange = () => send({ cmd: "settings", values: { update_check: $("updAuto").checked }, quiet: true });
$("updDl").onchange = () => send({ cmd: "settings", values: { update_auto_download: $("updDl").checked }, quiet: true });

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
$("muteDig").onchange = (e) => send({ cmd: "settings", values: { mute_digital_audio: e.target.checked }, quiet: true });
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
  renderSats();
  if (!$("schAction").options.length) {
    $("schAction").innerHTML = Object.entries(SCH.actions).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $("schPreset").insertAdjacentHTML("beforeend", SCH.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join(""));
  }
  const run = SCH.running;
  $("schRunning").hidden = $("schStop").hidden = !run;
  if (run) { $("schRunning").textContent = `● Capturing: ${run.name} until ${run.until}`; $("schRunning").className = "sig-chip pic"; }
  const next = Object.fromEntries((SCH.next || []).map((n) => [n.id, n.at]));
  const clashes = SCH.conflicts || [], PRIO = ["Low", "Normal", "High"];
  const clashOf = (id) => clashes.filter((c) => c.a_id === id || c.b_id === id);
  $("schClash").hidden = !clashes.length;
  $("schClash").innerHTML = clashes.length ? `<b>⚠ ${clashes.length} overlapping capture${clashes.length > 1 ? "s" : ""}</b> <span class="muted small">– only one capture can run at a time, so the radio does what it says here:</span><ul class="small">${clashes.map((c) => `<li>${esc(c.text)}</li>`).join("")}</ul>` : "";
  $("schTable").querySelector("tbody").innerHTML = SCH.jobs.map((j) => `<tr data-id="${esc(j.id)}" class="${run && run.id === j.id ? "playing" : ""}">
    <td><input type="checkbox" data-act="en" ${j.enabled ? "checked" : ""}></td>
    <td>${esc(j.start)} ${j.date ? "on " + esc(j.date) : j.days.length ? j.days.map((d) => DAYS[d]).join(" ") : "daily"}</td>
    <td>${j.enabled && clashOf(j.id).length ? `<span class="warnmark" title="${esc(clashOf(j.id).map((c) => c.text).join("\n"))}">⚠</span> ` : ""}${esc(j.name)}</td><td class="f" data-act="tune">${fmtMHz(j.freq, j.freq < 3e7 ? 4 : 4)}</td><td>${j.mode}</td>
    <td>${esc(SCH.actions[j.action] || j.action)}</td><td>${j.duration} min</td><td>${PRIO[j.priority == null ? 1 : j.priority]}</td><td class="muted">${j.enabled ? esc(next[j.id] || "–") : "off"}</td>
    <td><button class="btn sm" data-act="run">▶ Now</button> <button class="btn sm" data-act="edit">Edit</button> <button class="btn sm danger" data-act="del">✕</button></td></tr>`).join("") ||
    '<tr><td colspan="10" class="muted">No captures yet. Pick a station from Quick pick, set the start time and length, then Add capture.</td></tr>';
  $("schTable").querySelectorAll("tr[data-id]").forEach((tr) => {
    const j = SCH.jobs.find((x) => x.id === tr.dataset.id);
    tr.querySelector("[data-act=en]").onchange = (e) => send({ cmd: "schedule_save", job: { ...j, enabled: e.target.checked } });
    tr.querySelector("[data-act=run]").onclick = () => send({ cmd: "schedule_run", id: j.id });
    tr.querySelector("[data-act=del]").onclick = () => { if (confirm(`Delete "${j.name}"?`)) send({ cmd: "schedule_delete", id: j.id }); };
    tr.querySelector("[data-act=tune]").onclick = () => send({ cmd: "tune", freq: j.freq, mode: j.mode });
    tr.querySelector("[data-act=edit]").onclick = () => fillSchForm(j);
  });
}
/* ======================================================= EiBi shortwave schedule */
let eibiRows = [], eibiTimer = null;
function eibiSearch() {
  clearTimeout(eibiTimer);
  send({ cmd: "eibi_query", q: $("eibiQ").value, lang: $("eibiLang").value, fmin: $("eibiFmin").value, fmax: $("eibiFmax").value, now: $("eibiNow").checked });
  eibiTimer = setTimeout(() => { if ($("tab-eibi").classList.contains("active")) eibiSearch(); }, 60000);   // keep "on air now" fresh
}
function renderEibi(d) {
  const st = d.status || {};
  $("eibiStat").textContent = st.count ? `${st.count} broadcasts · schedule saved ${new Date(st.updated * 1000).toLocaleDateString()}` : "no schedule yet – press ⬇ Get latest schedule";
  $("eibiUtc").textContent = d.utc || "--:--";
  eibiRows = d.rows || [];
  $("eibiTable").querySelector("tbody").innerHTML = eibiRows.map((r, i) => `<tr class="${r.now ? "sel" : ""}"><td>${esc(String(r.f))}</td><td>${esc(r.t)}</td><td class="muted">${esc(r.d)}</td>
    <td class="cs">${esc(r.st)}</td><td>${esc(r.l)}</td><td class="muted">${esc(r.tg)}</td><td class="muted">${esc(r.r)}</td>
    <td style="white-space:nowrap"><button class="btn small" data-a="tune" data-i="${i}" title="Tune the radio to this station (AM)">Tune</button>
      <button class="btn small" data-a="mem" data-i="${i}" title="Store in the selected memory bank">★</button>
      <button class="btn small" data-a="sch" data-i="${i}" title="Tune to it automatically every time it is on air">⏰</button></td></tr>`).join("") ||
    `<tr><td colspan="8" class="muted">${st.count ? (d.total === 0 ? "Nothing matches" + ($("eibiNow").checked ? " – try un-ticking “On air now”." : ".") : "") : "Press ⬇ Get latest schedule (or Import file… if the PC has no internet)."}</td></tr>`;
  if ((d.total || 0) > eibiRows.length) $("eibiStat").textContent += ` · showing first ${eibiRows.length} of ${d.total} matches – narrow the search`;
}
$("eibiGo").onclick = eibiSearch;
$("eibiQ").onkeydown = (e) => { if (e.key === "Enter") eibiSearch(); };
$("eibiNow").onchange = eibiSearch;
$("eibiDl").onclick = () => { $("eibiStat").textContent = "downloading from eibispace.de …"; send({ cmd: "eibi_download" }); };
$("eibiFile").onchange = (e) => {
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader(); rd.onload = () => send({ cmd: "eibi_import", text: rd.result }); rd.readAsText(f, "ISO-8859-1"); e.target.value = "";
};
$("eibiTable").querySelector("tbody").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-a]"); if (!b) return;
  const r = eibiRows[+b.dataset.i]; if (!r) return;
  const hz = Math.round(r.f * 1000);
  if (b.dataset.a === "tune") { send({ cmd: "tune", freq: hz, mode: "AM" }); toast(`Tuned to ${r.f} kHz – ${r.st}`); }
  else if (b.dataset.a === "mem") { send({ cmd: "tune", freq: hz, mode: "AM" }); setTimeout(() => send({ cmd: "store", bank: curBank, name: r.st.slice(0, 16) }), 300); toast(`Stored ${r.st} (${r.f} kHz)`); }
  else {
    const [a, z] = r.t.split("-"), m0 = +a.slice(0, 2) * 60 + +a.slice(2, 4), m1 = +z.slice(0, 2) * 60 + +z.slice(2, 4);
    const d = new Date(); d.setUTCHours(+a.slice(0, 2), +a.slice(2, 4), 0, 0);             // that UTC time today -> local time + day shift
    const shift = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())) / 864e5);
    const p2 = (n) => String(n).padStart(2, "0");
    const job = { name: `${r.st} ${r.f} kHz`.slice(0, 60), freq: hz, mode: "AM", action: "listen", start: `${p2(d.getHours())}:${p2(d.getMinutes())}`,
      duration: ((m1 - m0 + 1440) % 1440) || 1440, date: "", days: (r.dl || []).map((x) => (x + shift + 7) % 7), enabled: true };
    send({ cmd: "schedule_save", job });
    toast(`Scheduled: tunes to ${r.st} at ${job.start} (local time) when it is on air`);
  }
});

/* ======================================================= FT8 / FT4 */
const FT8 = { msgs: [], unseen: 0 };
const FT8_BANDS = [["160 m", 1840000], ["80 m", 3573000], ["60 m", 5357000], ["40 m", 7074000], ["30 m", 10136000], ["20 m", 14074000], ["17 m", 18100000],
  ["15 m", 21074000], ["12 m", 24915000], ["10 m", 28074000], ["6 m", 50313000], ["6 m DX", 50323000], ["4 m", 70154000], ["2 m", 144174000]];
$("ft8Band").insertAdjacentHTML("beforeend", FT8_BANDS.map((b, i) => `<option value="${b[1]}">${b[0]} – ${(b[1] / 1e6).toFixed(3)} MHz</option>`).join(""));
function onFt8(m) {
  FT8.msgs.push(m); if (FT8.msgs.length > 300) FT8.msgs.shift();
  if (!$("tab-ft8").classList.contains("active")) { FT8.unseen++; const b = $("ft8Badge"); b.hidden = false; b.textContent = FT8.unseen; }
  renderFt8();
}
function renderFt8() {
  const run = S && S.decoder === "ft8";
  $("ft8Stat").textContent = run ? `running · ${(S.decoder_sub || "ft8").toUpperCase()} · ${FT8.msgs.length} messages` : `stopped · ${FT8.msgs.length} messages kept`;
  if (!$("tab-ft8").classList.contains("active")) return;
  const cq = $("ft8Cq").checked;
  $("ft8Table").querySelector("tbody").innerHTML = FT8.msgs.filter((m) => !cq || m.cq).slice().reverse().map((m) =>
    `<tr class="${m.cq ? "cq" : ""}"><td class="mono">${esc(m.utc || m.slot)}</td><td>${esc(m.proto || "FT8")}</td><td class="num">${m.score}</td><td class="num">${m.dt}</td>
     <td class="num">${m.hz}</td><td class="mono">${m.rf ? (m.rf / 1e6).toFixed(4) : "–"}</td><td>${esc(m.text)}</td><td class="mono">${esc(m.call)}</td><td class="mono">${esc(m.grid)}</td></tr>`).join("") ||
    `<tr><td colspan="9" class="muted">${run ? "Waiting for the end of the current time slot …" : "Not running. Pick a band and press ▶ FT8, or just tune a dial frequency such as 14.074 MHz."}</td></tr>`;
}
$("ft8Cq").onchange = renderFt8;
$("ft8Clear").onclick = () => { FT8.msgs = []; renderFt8(); };
$("ft8Stop").onclick = () => send({ cmd: "decoder", id: "", sub: "auto" });
const ft8Go = (sub) => { const f = +$("ft8Band").value; if (f) send({ cmd: "tune", freq: sub === "ft4" ? (FT4_FOR[f] || f) : f, mode: "USB" }); setTimeout(() => send({ cmd: "decoder", id: "ft8", sub }), 400); };
const FT4_FOR = { 3573000: 3575000, 7074000: 7047500, 10136000: 10140000, 14074000: 14080000, 18100000: 18104000, 21074000: 21140000, 24915000: 24919000, 28074000: 28180000, 50313000: 50318000, 144174000: 144170000 };
$("ft8Start").onclick = () => ft8Go("ft8");
$("ft8Start4").onclick = () => ft8Go("ft4");

/* ---- satellite auto-capture */
const fmtLocal = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const azName = (a) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(a / 45) % 8];
function renderSats() {
  const st = SCH && SCH.sats; if (!st) return;
  const busy = (id) => document.activeElement === $(id);
  if (!busy("satLat") && !busy("satLon")) { $("satLat").value = st.observer ? st.observer[0] : ""; $("satLon").value = st.observer ? st.observer[1] : ""; }
  $("satMsg").textContent = !st.have_sgp4 ? "Orbit maths library missing – reinstall AndySDR." : !st.observer ? "Enter your position (or use my location) to see passes." : st.busy ? "Downloading orbit data …" : st.error || "";
  if ([...document.querySelectorAll("#satTable input")].some((e) => e === document.activeElement)) return;
  $("satTable").querySelector("tbody").innerHTML = st.sats.map((x) => {
    const fr = x.freqs.length > 1 ? `<select data-k="freq">${x.freqs.map((f) => `<option value="${f}" ${f === x.freq ? "selected" : ""}>${(f / 1e6).toFixed(3)} MHz</option>`).join("")}</select>` : `${(x.freqs[0] / 1e6).toFixed(3)} MHz`;
    let note = esc(x.note);
    if (x.kind === "geo" && st.observer && x.el != null) note = (x.visible ? `<b style="color:var(--ok,#4ddb7a)">Above your horizon (${x.el}°).</b> ` : x.above_horizon ? `<b>Only ${x.el}° up – too low to track.</b> ` : `<b style="color:var(--warn,#ffb020)">Below your horizon from here (${x.el}°) – nothing to receive.</b> `) + note;
    const geoNoDates = x.kind === "geo";
    return `<tr data-id="${x.id}"><td><input type="checkbox" data-k="enabled" ${x.enabled ? "checked" : ""}></td><td>${esc(x.name)}</td><td>${fr}</td>
      <td>${x.kind === "geo" ? '<span class="muted">–</span>' : `<input type="checkbox" data-k="track" ${x.track ? "checked" : ""} title="Retune every 2 s to follow the Doppler shift">`}</td>
      <td><input class="inp" data-k="min_el" type="number" min="0" max="80" value="${x.min_el}" style="width:4.5em">°</td>
      <td>${geoNoDates ? '<span class="muted">–</span>' : ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((n, i) => `<label class="small"><input type="checkbox" data-day="${i}" ${(x.days || []).includes(i) ? "checked" : ""}>${n}</label>`).join(" ")}</td>
      <td>${geoNoDates ? '<span class="muted">–</span>' : `<input class="inp" data-k="from" type="date" value="${esc(x.from)}"> – <input class="inp" data-k="to" type="date" value="${esc(x.to)}">`}</td>
      <td class="muted">${x.tle_age_h == null ? "not downloaded" : x.tle_age_h + " h old"}</td><td class="small">${note}</td></tr>`;
  }).join("");
  $("satTable").querySelectorAll("tr[data-id] [data-day]").forEach((el) => {
    el.onchange = () => {
      const tr = el.closest("tr");
      send({ cmd: "sat_cfg", id: tr.dataset.id, days: [...tr.querySelectorAll("[data-day]:checked")].map((c) => +c.dataset.day) });
    };
  });
  $("satTable").querySelectorAll("tr[data-id] [data-k]").forEach((el) => {
    el.onchange = () => {
      const k = el.dataset.k, id = el.closest("tr").dataset.id;
      send({ cmd: "sat_cfg", id, [k]: el.type === "checkbox" ? el.checked : el.value });
    };
  });
  const dayName = (t) => new Date(t * 1000).toLocaleDateString([], { weekday: "long", day: "2-digit", month: "short" });
  const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  let lastDay = "", rows = "";
  for (const p of st.passes) {
    const d = p.geo ? "Now" : dayName(p.aos);
    if (d !== lastDay) { lastDay = d; rows += `<tr class="dayrow"><td colspan="7"><b>${esc(d)}</b></td></tr>`; }
    rows += `<tr><td>${esc(p.name)}</td><td>${p.geo ? "now (continuous)" : hm(p.aos)}</td><td>${hm(p.los)}</td>
      <td>${p.max_el}°</td><td>${p.geo ? "–" : azName(p.az)}</td><td>${(p.freq / 1e6).toFixed(3)}${p.track ? " ± Doppler" : ""}</td>
      <td>${p.state === "recording" ? '<b style="color:var(--ok,#4ddb7a)">● recording</b>' : "scheduled"}</td></tr>`;
  }
  $("satPasses").querySelector("tbody").innerHTML = rows ||
    `<tr><td colspan="7" class="muted">${st.observer ? "Tick Record on a satellite above – every pass for the next 4 days appears here, grouped by day, and is recorded automatically." : "Set your position first."}</td></tr>`;
}
$("satPosSet").onclick = () => send({ cmd: "sat_observer", lat: $("satLat").value.trim().replace(",", "."), lon: $("satLon").value.trim().replace(",", ".") });
$("satPosGps").onclick = () => {
  if (!navigator.geolocation) { toast("This browser has no location service", "warn"); return; }
  navigator.geolocation.getCurrentPosition((p) => send({ cmd: "sat_observer", lat: p.coords.latitude.toFixed(5), lon: p.coords.longitude.toFixed(5), alt: Math.round(p.coords.altitude || 30) }),
    () => toast("Could not get your location – type it in instead", "warn"), { timeout: 10000 });
};
$("satRefresh").onclick = () => send({ cmd: "sat_refresh" });
function fillSchForm(j) {
  schEdit = j ? j.id : null;
  $("schName").value = j ? j.name : ""; $("schFreq").value = j ? (j.freq / 1e6).toString() : "";
  $("schMode").value = j ? j.mode : "USB"; $("schAction").value = j ? j.action : "wefax";
  $("schStart").value = j ? j.start : "12:00"; $("schDur").value = j ? j.duration : 20; $("schDate").value = j ? j.date : "";
  $("schPrio").value = j && j.priority != null ? j.priority : 1;
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
    priority: +$("schPrio").value,
    days: [...$("schDays").querySelectorAll("input:checked")].map((c) => +c.value), enabled: true };
  send({ cmd: "schedule_save", job }); fillSchForm(null); $("schPreset").value = "";
  toast(`Capture saved: ${job.name} at ${job.start}`);
};
$("schClear").onclick = () => fillSchForm(null);
$("schStop").onclick = () => send({ cmd: "schedule_stop" });


/* ======================================================= MeshCore messaging */
let MESH = null, MESHMSG = [], meshConv = "ch:0", meshBle = [];
const meshSeen = JSON.parse((() => { try { return localStorage.getItem("andysdr_mesh_seen") || "{}"; } catch (e) { return "{}"; } })());
function meshSaveSeen() { try { localStorage.setItem("andysdr_mesh_seen", JSON.stringify(meshSeen)); } catch (e) {} }
function meshRead(conv) { meshSeen[conv] = Math.floor(Date.now() / 1000) + 1; meshSaveSeen(); }
function meshUnread(conv) { const t = meshSeen[conv] || 0; return MESHMSG.filter((m) => m.conv === conv && m.dir === "in" && m.rx > t).length; }
function meshTabOpen() { return $("tab-mesh").classList.contains("active"); }
function ago(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "now"; if (s < 3600) return Math.round(s / 60) + "m"; if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
function meshConvName(conv) {
  if (!MESH) return conv;
  if (conv.startsWith("ch:")) { const c = MESH.channels.find((x) => "ch:" + x.idx === conv); return c ? c.name : "Channel " + conv.slice(3); }
  const k = conv.slice(2); const c = MESH.contacts.find((x) => x.key === k || x.key.startsWith(k));
  return c ? c.name : k.slice(0, 12);
}
function renderMesh() {
  if (!MESH) return;
  const st = MESH.status;
  $("meshDot").className = "mesh-dot " + st;
  $("meshStatus").textContent = { connected: "Connected", connecting: "Connecting…", error: "Problem", disconnected: "Not connected" }[st] || st;
  const n = MESH.node || {};
  $("meshNode").textContent = st === "connected"
    ? [n.name, n.model, n.fw && "fw " + n.fw, n.battery_mv && (n.battery_mv / 1000).toFixed(2) + " V"].filter(Boolean).join(" · ") : "";
  $("meshRadio").textContent = n.freq ? `Radio ${n.freq} MHz · BW ${n.bw} kHz · SF${n.sf} · CR${n.cr} · ${n.tx_power} dBm` : "";
  if (document.activeElement !== $("meshName")) $("meshName").value = n.name || "";
  const err = MESH.error || "";
  $("meshErr").hidden = !err; $("meshErr").textContent = err;
  // connection form
  const conn = MESH.conn || {};
  if (!$("meshKind").dataset.touched && conn.kind) $("meshKind").value = conn.kind;
  const ports = MESH.ports || [];
  const cur = $("meshPort").value || conn.target || "";
  $("meshPort").innerHTML = ports.length
    ? ports.map((p) => `<option value="${esc(p.port)}" ${p.port === cur ? "selected" : ""}>${esc(p.port)} – ${esc(p.desc)}${p.likely ? " ★" : ""}</option>`).join("")
    : `<option value="">no COM ports found</option>`;
  if (!$("meshTarget").dataset.touched && conn.kind !== "serial") $("meshTarget").value = conn.target || "";
  meshKindUI();
  renderMeshList();
  renderMeshMsgs();
}
function meshKindUI() {
  const k = $("meshKind").value;
  $("meshPort").hidden = k !== "serial";
  $("meshTarget").hidden = k === "serial";
  $("meshTarget").placeholder = k === "tcp" ? "192.168.1.50:5000" : "Bluetooth address (blank = first found)";
  $("meshPin").hidden = $("meshBleScan").hidden = k !== "ble";
}
function renderMeshList() {
  if (!MESH) return;
  const chans = MESH.channels.length ? MESH.channels : [{ idx: 0, name: "Public" }];
  $("meshChans").innerHTML = chans.map((c) => {
    const conv = "ch:" + c.idx, u = meshUnread(conv);
    return `<div data-conv="${conv}" class="${conv === meshConv ? "on" : ""}"><span class="ic">#</span><span class="nm">${esc(c.name)}</span>` +
      (u ? `<span class="unread">${u}</span>` : "") +
      (c.idx ? `<button class="btn sm" data-leave="${c.idx}" title="Leave channel">✕</button>` : "") + `</div>`;
  }).join("");
  const q = $("meshFind").value.trim().toLowerCase();
  const ic = { chat: "👤", repeater: "📶", room: "🏠", sensor: "🌡" };
  const list = MESH.contacts.filter((c) => !q || c.name.toLowerCase().includes(q));
  $("meshContacts").innerHTML = list.length ? list.map((c) => {
    const conv = "c:" + c.key, u = meshUnread(conv);
    const hops = c.hops < 0 ? "flood" : c.hops + " hop" + (c.hops === 1 ? "" : "s");
    return `<div data-conv="${conv}" class="${conv === meshConv ? "on" : ""}" title="${esc(c.type)} · ${esc(c.key.slice(0, 16))}… · ${hops}">` +
      `<span class="ic">${ic[c.type] || "•"}</span><span class="nm">${esc(c.name)}</span>` +
      (u ? `<span class="unread">${u}</span>` : `<span class="meta">${ago(c.last_advert)}</span>`) + `</div>`;
  }).join("") : `<div class="mesh-empty">${MESH.status === "connected" ? "No contacts yet – nodes appear when they advertise." : "Connect a MeshCore board to see nodes."}</div>`;
  const total = MESHMSG.filter((m) => m.dir === "in" && m.rx > (meshSeen[m.conv] || 0)).length;
  $("meshBadge").hidden = !total; $("meshBadge").textContent = total;
}
function meshMsgHtml(m, prevDay) {
  const d = new Date((m.ts || m.rx) * 1000);
  const day = d.toLocaleDateString();
  let h = day !== prevDay ? `<div class="mesh-day">${esc(day)}</div>` : "";
  const isCh = m.conv.startsWith("ch:");
  const who = m.dir === "in" && isCh ? `<div class="who">${esc(m.from)}</div>` : "";
  const bits = [d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })];
  if (m.dir === "in") { if (m.hops != null && m.hops >= 0 && m.hops < 255) bits.push(m.hops + " hop" + (m.hops === 1 ? "" : "s")); if (m.snr != null) bits.push("SNR " + m.snr); }
  else bits.push({ sending: "sending…", sent: isCh ? "sent" : "sent ✓", delivered: "delivered ✓✓" }[m.status] || m.status || "");
  const bad = String(m.status || "").startsWith("failed");
  return h + `<div class="mesh-msg ${m.dir}" data-id="${esc(m.id)}">${who}<div>${esc(m.text)}</div><div class="ft ${bad ? "bad" : ""}">${esc(bits.join(" · "))}</div></div>`;
}
function renderMeshMsgs() {
  if (!MESH) return;
  $("meshConvName").textContent = (meshConv.startsWith("ch:") ? "# " : "") + meshConvName(meshConv);
  const c = !meshConv.startsWith("ch:") && MESH.contacts.find((x) => "c:" + x.key === meshConv);
  $("meshConvInfo").textContent = c ? `${c.type} · heard ${ago(c.last_advert)} ago` + (c.lat ? ` · ${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}` : "") : "";
  const box = $("meshMsgs");
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const msgs = MESHMSG.filter((m) => m.conv === meshConv);
  let prev = "", html = "";
  for (const m of msgs) { html += meshMsgHtml(m, prev); prev = new Date((m.ts || m.rx) * 1000).toLocaleDateString(); }
  box.innerHTML = html || `<div class="mesh-empty">No messages here yet.</div>`;
  if (atEnd) box.scrollTop = 1e9;
}
function meshAddMsg(m) {
  const i = MESHMSG.findIndex((x) => x.id === m.id);
  if (i >= 0) MESHMSG[i] = m; else MESHMSG.push(m);
  if (MESHMSG.length > 3000) MESHMSG = MESHMSG.slice(-2500);
  if (m.conv === meshConv && meshTabOpen()) meshRead(meshConv);
  if (m.dir === "in" && i < 0 && !(m.conv === meshConv && meshTabOpen())) toast(`💬 ${meshConvName(m.conv)}: ${m.from ? m.from + ": " : ""}${m.text}`.slice(0, 140));
  renderMeshList(); renderMeshMsgs();
}
function meshBleFound(list) {
  meshBle = list || [];
  if (!meshBle.length) { toast("No MeshCore boards found over Bluetooth – is the board on and not connected to a phone?", "error"); return; }
  $("meshTarget").value = meshBle[0].address; $("meshTarget").dataset.touched = "1";
  toast("Found " + meshBle.map((d) => `${d.name} (${d.address})`).join(", "));
}
$("meshKind").onchange = () => { $("meshKind").dataset.touched = "1"; meshKindUI(); };
$("meshTarget").oninput = () => ($("meshTarget").dataset.touched = "1");
$("meshConnect").onclick = () => {
  const kind = $("meshKind").value;
  const target = kind === "serial" ? $("meshPort").value : $("meshTarget").value.trim();
  send({ cmd: "mesh_connect", kind, target, pin: $("meshPin").value.trim() });
};
$("meshDisconnect").onclick = () => send({ cmd: "mesh_disconnect" });
$("meshBleScan").onclick = () => { toast("Searching for MeshCore boards over Bluetooth…"); send({ cmd: "mesh_ble_scan" }); };
$("meshRefresh").onclick = () => send({ cmd: "mesh_refresh" });
$("meshAdvert").onclick = () => send({ cmd: "mesh_advert", flood: true });
$("meshNameSet").onclick = () => send({ cmd: "mesh_name", name: $("meshName").value.trim() });
$("meshUk").onclick = () => {
  const p = (MESH && MESH.uk_preset) || { freq: 869.618, bw: 62.5, sf: 8, cr: 8 };
  if (confirm(`Set the board's radio to ${p.freq} MHz, BW ${p.bw} kHz, SF${p.sf}, CR${p.cr} (UK/EU narrow)?\nAll nodes you talk to must use the same settings.`))
    send({ cmd: "mesh_radio", ...p });
};
$("meshTune").onclick = () => {
  const f = (MESH && MESH.node && MESH.node.freq) || 869.618;
  send({ cmd: "tune", freq: Math.round(f * 1e6), mode: "WFM" });
  toast(`SDR tuned to ${f} MHz – LoRa packets show as short wide bursts on the waterfall (the SDR cannot read them; the board does)`);
};

/* ======================================================= MeshCore with just the SDR (receive only) */
let MSDR = null, msdrView = "nodes";
function onMeshSdr(d) { MSDR = d; renderMeshSdr(); }
const msdrAgo = (t) => { const s = Math.max(0, (Date.now() / 1000) - t); return s < 90 ? Math.round(s) + " s" : s < 5400 ? Math.round(s / 60) + " min" : s < 172800 ? (s / 3600).toFixed(1) + " h" : Math.round(s / 86400) + " d"; };
const msdrTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function renderMeshSdr() {
  if (!MSDR || !$("msdrCard")) return;
  const c = MSDR.cfg, st = MSDR.stats, typing = ["msdrFreq", "msdrBw", "msdrSf"].some((i) => document.activeElement === $(i));
  if (!typing) { $("msdrFreq").value = (c.freq / 1e6).toFixed(3); $("msdrBw").value = (c.bw / 1e3).toString(); $("msdrSf").value = c.sf; }
  $("msdrStart").hidden = MSDR.running; $("msdrStop").hidden = !MSDR.running;
  const mins = st.started ? Math.max(0, (MSDR.now - st.started) / 60) : 0;
  $("msdrStat").textContent = MSDR.running
    ? `Listening · ${st.frames} packets${st.bad_crc ? ", " + st.bad_crc + " damaged" : ""} · ${MSDR.nodes.length} nodes · ${mins < 1 ? "<1" : Math.round(mins)} min` + (st.frames === 0 && mins > 2 ? " · nothing heard yet – check the antenna, frequency and SF" : "")
    : `Stopped · ${MSDR.nodes.length} nodes known`;
  $("msdrChans").innerHTML = MSDR.channels.map((x) => {
    const u = (MSDR.user_channels || []).find((s) => s === x.name);
    return ` <span class="chip">${esc(x.name)}${u ? ` <a href="#" data-chan-del="${esc(u)}" title="Remove">✕</a>` : ""}</span>`;
  }).join("");
  document.querySelectorAll("[data-msdr]").forEach((b) => b.classList.toggle("on", b.dataset.msdr === msdrView));
  $("msdrNodesW").hidden = msdrView !== "nodes"; $("msdrMsgsW").hidden = msdrView !== "msgs"; $("msdrPktsW").hidden = msdrView !== "pkts";
  if (!$("tab-mesh").classList.contains("active")) return;
  const q = $("msdrFind").value.trim().toLowerCase(), ty = $("msdrType").value;
  const nodes = MSDR.nodes.filter((n) => (!ty || n.type === ty) && (!q || `${n.name} ${n.type} ${n.pubkey}`.toLowerCase().includes(q)));
  const icon = { repeater: "🗼", "room server": "🏠", chat: "💬", sensor: "🌡" };
  $("msdrNodes").querySelector("tbody").innerHTML = nodes.map((n) => `<tr>
    <td>${icon[n.type] || ""} <b>${esc(n.name || "(no name)")}</b></td><td>${esc(n.type)}</td>
    <td>${n.lat != null ? `<a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat=${n.lat}&mlon=${n.lon}#map=14/${n.lat}/${n.lon}">${n.lat.toFixed(4)}, ${n.lon.toFixed(4)}</a>` : '<span class="muted">–</span>'}</td>
    <td class="num">${n.hops}</td><td class="num">${n.snr}</td><td>${msdrAgo(n.last)} ago</td><td class="num">${n.count}×</td><td class="mono small">${esc(n.pubkey.slice(0, 12))}…</td></tr>`).join("") ||
    `<tr><td colspan="8" class="muted">${MSDR.running ? "Listening … nodes appear when they send an advert (repeaters and rooms do this every few hours, so it can take a while)." : "Press ▶ Start listening."}</td></tr>`;
  $("msdrMsgs").querySelector("tbody").innerHTML = MSDR.messages.slice().reverse().map((m) => `<tr><td>${msdrTime(m.t)}</td><td>${esc(m.channel)}</td><td>${esc(m.sender || "?")}</td><td>${esc(m.text)}</td><td class="num">${m.hops}</td><td class="num">${m.snr}</td></tr>`).join("") ||
    `<tr><td colspan="6" class="muted">No readable channel messages yet. Public-channel and any #hashtag channels you add are decrypted.</td></tr>`;
  $("msdrPkts").querySelector("tbody").innerHTML = MSDR.packets.slice().reverse().map((p) => `<tr><td>${msdrTime(p.t)}</td><td>${esc(p.type)}</td><td>${esc(p.route)}</td><td class="small">${p.via.length ? esc(p.via.join(" → ")) : "–"}</td><td class="num">${p.len}</td><td class="num">${p.snr}</td><td>${esc(p.text)}</td></tr>`).join("") ||
    `<tr><td colspan="7" class="muted">Nothing heard yet.</td></tr>`;
}
$("msdrStart").onclick = () => send({ cmd: "meshsdr_start", freq: Math.round(parseFloat($("msdrFreq").value) * 1e6), bw: Math.round(parseFloat($("msdrBw").value) * 1e3), sf: +$("msdrSf").value });
$("msdrStop").onclick = () => send({ cmd: "meshsdr_stop" });
$("msdrUk").onclick = () => { $("msdrFreq").value = "869.618"; $("msdrBw").value = "62.5"; $("msdrSf").value = "8"; toast("UK/EU narrow settings filled in – press Start listening"); };
$("msdrClear").onclick = () => { if (confirm("Forget all nodes, messages and the packet log?")) send({ cmd: "meshsdr_clear" }); };
$("msdrChanAdd").onclick = () => { const v = $("msdrChanIn").value.trim(); if (v) { send({ cmd: "meshsdr_channel_add", spec: v }); $("msdrChanIn").value = ""; } };
$("msdrChans").onclick = (e) => { const a = e.target.closest("[data-chan-del]"); if (a) { e.preventDefault(); send({ cmd: "meshsdr_channel_remove", spec: a.dataset.chanDel }); } };
document.querySelectorAll("[data-msdr]").forEach((b) => (b.onclick = () => { msdrView = b.dataset.msdr; renderMeshSdr(); }));
$("msdrFind").oninput = renderMeshSdr; $("msdrType").onchange = renderMeshSdr;
$("msdrCsv").onclick = () => {
  if (!MSDR) return;
  const rows = [["name", "type", "lat", "lon", "hops", "snr", "heard_count", "last_heard", "public_key"]].concat(MSDR.nodes.map((n) => [n.name, n.type, n.lat ?? "", n.lon ?? "", n.hops, n.snr, n.count, new Date(n.last * 1000).toISOString(), n.pubkey]));
  const csv = rows.map((r) => r.map((x) => `"${String(x).replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv" })); a.download = "meshcore_nodes.csv"; a.click();
};
setInterval(() => { if (MSDR && MSDR.running) renderMeshSdr(); }, 5000);
document.querySelector('[data-tab="mesh"]').addEventListener("click", () => setTimeout(renderMeshSdr, 0));

$("meshFind").oninput = renderMeshList;
$("meshChans").onclick = (e) => {
  const lv = e.target.closest("[data-leave]");
  if (lv) { e.stopPropagation(); if (confirm("Leave this channel?")) send({ cmd: "mesh_channel_remove", idx: +lv.dataset.leave }); return; }
  const r = e.target.closest("[data-conv]"); if (r) meshOpen(r.dataset.conv);
};
$("meshContacts").onclick = (e) => { const r = e.target.closest("[data-conv]"); if (r) meshOpen(r.dataset.conv); };
function meshOpen(conv) { meshConv = conv; meshRead(conv); renderMeshList(); renderMeshMsgs(); $("meshMsgs").scrollTop = 1e9; $("meshText").focus(); }
$("meshChAdd").onclick = () => {
  const name = prompt("Channel to join:\n• a public hashtag channel, e.g. #uk or #london\n• or a private channel name (you'll be asked for its key)");
  if (!name) return;
  let secret = "";
  if (!name.trim().startsWith("#")) {
    secret = (prompt("32 hex-digit secret key for '" + name + "' (from whoever shared the channel):") || "").replace(/\s/g, "");
    if (!/^[0-9a-fA-F]{32}$/.test(secret)) { toast("That key isn't 32 hex digits", "error"); return; }
  }
  const used = new Set((MESH && MESH.channels || []).map((c) => c.idx));
  let idx = 1; while (used.has(idx)) idx++;
  send({ cmd: "mesh_channel_add", idx, name: name.trim(), secret });
};
$("meshClearConv").onclick = () => { if (confirm(`Delete the history of ${meshConvName(meshConv)} from AndySDR?`)) send({ cmd: "mesh_clear", conv: meshConv }); };
$("meshText").oninput = () => ($("meshCount").textContent = `${$("meshText").value.length}/160`);
$("meshForm").onsubmit = (e) => {
  e.preventDefault();
  const t = $("meshText").value.trim(); if (!t) return;
  send({ cmd: "mesh_send", conv: meshConv, text: t });
  $("meshText").value = ""; $("meshCount").textContent = "0/160";
};
setInterval(() => { if (MESH && meshTabOpen()) renderMeshList(); }, 30000);

/* ======================================================= help */
const HELP = {
  conn: ["Connection light", "Green CONNECTED means this screen is talking to the AndySDR receiver (on this PC or across the network). If it shows OFFLINE, AndySDR is not running or the network is down – it reconnects by itself."],
  theme: ["Style", "Changes the look: PRO-2045 style (amber LCD, silver band), UBCD260DN style (black base/mobile with white-blue display), TRX-2 style (rugged gunmetal with green-grey display) or SDR++ style (flat dark panels, side bar of controls on the left and a big spectrum and waterfall). Controls work the same in every style; each computer remembers its own choice."],
  help: ["HELP", "Opens this guide. Tick 'Explain mode' and then click any knob, key or panel to get a description of just that control. Press Esc to leave Explain mode."],
  volume: ["VOLUME knob", "Loudness on THIS computer only (each remote listener has their own). Drag up/down, use the mouse wheel, or click it and use the arrow keys. Double-click resets to 70%."],
  squelch: ["SQUELCH knob", "Mutes the hiss when nobody is transmitting. Turn it up until the noise just disappears – the little raised bar on the S-meter shows where it is set. OPEN (fully left) = always hear everything. Scanning and searching need squelch set above the noise so they know when a channel is quiet."],
  gain: ["RF GAIN knob", "Receiver amplification. The AGC button under the knob switches the receiver's automatic gain on or off – turn it off to set the gain yourself with the knob; for digital voice (DMR, P25, TETRA) manual gain set just below overload usually decodes best. Too little = weak signals lost; too much = overload, false signals and a raised noise floor. Start in the middle, watch the waterfall, and turn down if you see lots of 'ghost' signals. For SDRplay receivers 0-100% controls the LNA state and IF gain together. Turning it switches AGC off; tick Hardware AGC in the Receiver tab to make it automatic."],
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
  layout: ["Layout", "Control room puts the radio on the left, a big spectrum in the middle and seven panels around it (memory, decoder log, pictures, scheduler, recordings with instant replay, live activity, pager messages) so you can see everything at once. Each panel has a drop-down to choose what it shows – every tab is available – and a ⤢ button to make it full size. Classic is the original tabbed page; Auto uses Control room on big screens only."],
  zoom: ["Zoom", "Magnify the spectrum. On a touch screen stretch two fingers apart to zoom in and pinch to zoom out; on a laptop touchpad do the same pinch gesture; with a mouse hold Ctrl and scroll. When zoomed in, scroll the wheel (or two-finger swipe on the touchpad) up/down to slide the view left/right, or drag it. Click a signal to tune it. Follow keeps the view centred on the frequency you are tuned to; Fit shows everything. Shift+scroll still steps the tuning frequency."],
  scope: ["Spectrum & waterfall", "Live picture of the radio band around you. The top graph shows signal strength across the received bandwidth; the waterfall below shows history – transmissions appear as bright streaks. Click anywhere to tune there; pinch or Ctrl+scroll to zoom (see Zoom). The red line and shading show the channel you are listening to. 'Range' and 'Top' set the display contrast."],
  banks: ["Memory banks", "10 banks × 100 channels = 1000 memories. Click a bank to view it, right-click to include/exclude it from scanning. In the table: click a frequency or ▶ to listen, click the name to rename, change mode/decoder in the drop-downs, tick L/O to lock out, PRI to make it the priority channel, ✕ to delete."],
  import: ["RadioReference UK import", "On radioreference.com open a UK county/agency database page and use its CSV download (premium) – or select the frequency table on the page, copy it and save it as a .txt file. Choose the file, pick the bank, and Import. AndySDR recognises Frequency, Alpha Tag, Description, Mode, Tag and Tone columns, converts FMN/AM/DMR/P25/NXDN/TETRA modes, and attaches the right decoder. Talkgroup lists (Decimal/Alpha Tag) are stored as talkgroups. Only the chosen bank is filled (100 channels) unless you raise 'using … bank(s)', which carries on into the following empty banks - banks you already use are never touched. The bank then shows the database file name, the file's date and when it was imported."],
  search: ["Search", "Set From/To in MHz, the channel step, mode and sensitivity (lower dB = finds weaker signals but more false hits), then Search. UK quick ranges fill the boxes for you. Results list each active frequency with hit count and strength – click a frequency to listen, Store to save it to the selected bank."],
  decoders: ["Decoders", "Pick what to decode on the current frequency. Digital voice uses DSD-FME (choose DMR, P25, NXDN, dPMR, YSF, M17, D-STAR or Auto; if a DMR or dPMR signal shows sync but only garbage, try the Inverted entry). AndySDR also centres the signal for the decoder automatically so a dongle's tuning error doesn't break decoding; pagers use multimon-ng (POCSAG & FLEX); WEFAX, SSTV and ADS-B aircraft tracking (1090 MHz, see the ADS-B tab) are built in; Meteor-M LRPT records the satellite pass and hands it to SatDump. Decoded text appears in the log and on the LCD bottom line; pager messages are also listed in the PAGER MESSAGES tab. Memory channels can start their decoder automatically."],
  biaspanel: ["Bias-T panel", "Shows whether the bias-T (antenna power) is on and can reach your antenna (RSP2: Antenna B only), the receiver noise floor over the last two minutes, and receiver overloads. No SDR can measure the current it supplies, so a drop in noise floor is used to spot lost power. 'Run power check' switches bias-T off for 6 s then on for 8 s and tells you how many dB the powered antenna or LNA adds - if there is no change, nothing is being powered. Do not run it during a scheduled capture."],
  receiver: ["Receiver", "Choose which SDR to use: every SoapySDR device (RTL-SDR/Nooelec, SDRplay RSP1A/RSP2/RSP2pro/RSPdx/RSPduo, Airspy, HackRF, LimeSDR, Pluto…), rtl_tcp servers on other machines, or the Simulator for practice. Sample rate sets how wide the waterfall is. Antenna picks the input (RSP2: Antenna A, B or Hi-Z). Device switches (bias-T, notch filters, direct sampling) appear underneath automatically."],
  autoid: ["Auto ID (automatic signal identification)", "When ticked, AndySDR analyses whatever you are listening to and shows what it is next to the VFO: DMR (it spots the 30 ms TDMA bursts), P25, NXDN, dPMR, pagers (POCSAG/FLEX), APRS, DTMF, SSTV, WEFAX, TETRA or analogue voice (with any CTCSS tone). It then switches to the right mode and starts the matching decoder by itself, and stops it again when the channel goes back to voice. SSTV pictures are caught from their very first line. A decoder you pick yourself, or one stored on a memory channel, is never overridden. Untick to turn it off."],
  eibitab: ["Shortwave (EiBi)", "The EiBi shortwave broadcast schedule: press Get latest schedule once (or Import file…), then search by station, language or frequency. Tick On air now to see only what is being broadcast right now (UTC). Tune goes to the frequency in AM; ★ stores it in memory; ⏰ schedules an automatic tune-in every time that broadcast is on."],
  morsebox: ["Morse code decoder", "Press the MORSE key (DECODE row) to open this box. AndySDR switches to CW mode and reads Morse from the audio: it finds the pitch of the tone by itself, follows the sending speed (about 5-50 WPM) and prints the letters on the ticker tape, newest on the right; the box underneath keeps everything decoded. SPEED, TONE and SIGNAL show what it hears and the green light flashes with the keying. Click a popular frequency to tune it – most activity is near the QRP spots (7.030, 14.060 MHz), the IARU beacons (14.100, 28.200 MHz) and aviation NDB beacons (190-530 kHz). Weak or very fast signals decode with mistakes (shown as ¿); a clean tone with the signal 15 dB or more above the noise works best. ✕ hides the box but decoding carries on; ■ Stop ends it."],
  ft8tab: ["FT8 / FT4", "Weak-signal amateur digital modes. Pick a band and press ▶ FT8 (or ▶ FT4): AndySDR tunes USB to the dial frequency and decodes every 15 s UTC slot (FT4: 7.5 s). It also starts by itself when you tune or scan onto an FT8/FT4 channel from the band plan and something is on the air. The PC clock must be accurate. CQ only hides everything except stations calling CQ. From/Grid are read from the message."],
  satauto: ["Satellite auto-capture", "Tick Record on a satellite and AndySDR predicts every pass over your position for the next 4 days (listed day by day) (set it with My position or 📍), tunes at acquisition of signal, follows the Doppler shift if Track is ticked, runs the right decoder and stops when the satellite sets – then puts the radio back as it was. ISS SSTV listens on 145.800 MHz FM (limit it to the ARISS event dates with the date boxes); Meteor-M N2-3 and N2-4 are LRPT on 137.900 MHz (switch to 137.100 if the satellite moves); GOES-19/18 are 1694.1 MHz HRIT from geostationary orbit – AndySDR says honestly when the satellite is below your horizon. Tick the days of the week you want (none ticked = every day) – you never type a time, AndySDR starts each capture only when the satellite is actually above your horizon. Min elevation hides low, weak passes. Orbit data (TLEs) is downloaded from CelesTrak and refreshed automatically. A capture you add by hand always takes priority."],
  sched: ["Scheduler", "Captures that run by themselves at set times – ideal for weather fax charts, ISS SSTV events and Meteor-M satellite passes. Pick a station from Quick pick (or type a frequency), choose what to do (WEFAX, SSTV, LRPT, record…), the start time on this PC's clock and how long, and either tick days of the week or pick a single date. At the start time AndySDR stops scanning, tunes, runs the decoder; at the end it saves the pictures and puts the radio back exactly as it was. Only one capture can run at a time: if two overlap, the one with the higher Priority takes over the radio (satellite passes count as High) and an interrupted capture carries on afterwards if time is left; overlaps are flagged with ⚠ and listed above the table. ▶ Now runs one straight away. AndySDR must be running (it can run as a server in the background)."],
  bppick: ["Plan › Band › Section", "Drill down through the UK band plan: pick a Plan (amateur, aviation, marine, PMR…), then a Band in it, then a Section (sub-band or key frequency). Tune goes there in the right mode and step; Search section loads it into the Live Activity search; 'Add key frequencies to bank' stores every key frequency in that band (e.g. marine Ch 16/70/AIS, airband 121.5/123.1) into the memory bank you choose. Clicking a coloured strip on the spectrum or a band name in the list also selects it here. Tap the coloured plan buttons underneath to show or hide each plan on the spectrum."],
  bandplan: ["Band plan size", "Shows the UK band plan as coloured strips across the top of the spectrum and in the band map above it. Off hides it; Small shows main allocations only; Medium adds sub-bands (e.g. 2 m FM simplex) and markers for key frequencies (Ch 16, 121.5, APRS…); Large makes the strips bigger and names the key frequencies. Each computer remembers its own choice."],
  vfo: ["VFO bar", "The VFO is the frequency you are tuned to. This bar shows it, which UK band and sub-band it is in, and – if the band normally uses a different mode or step – a 'Use' button that sets them for you. The red line and red VFO flag on the spectrum mark the same frequency."],
  bandmap: ["Band map", "The whole radio spectrum the receiver can tune (e.g. RSP2 Pro: 1 kHz to 2 GHz) on one strip, coloured by who uses it (see the legend in the UK BAND PLAN tab). The red needle is your VFO and the white box is the part the spectrum display is showing. Hover to see a band's name; click a band to jump there in the right mode and step."],
  bptab: ["UK band plan list", "Every UK allocation, amateur sub-band and key frequency the receiver can tune (e.g. RSP2 Pro: 1 kHz to 2 GHz). Type a name or a frequency (e.g. 156.8) to find it, pick a category or click the coloured legend, untick sub-bands for the big picture. Tune goes straight there in the right mode; Search loads the band into the Live Activity search so you can find everything active in it. 'Where am I?' lists the bands your VFO is in."],
  mesh: ["MeshCore messaging", "Text chat over the MeshCore LoRa mesh. Your SDRs can only receive, so AndySDR sends and receives through a MeshCore companion board (Heltec V3/V4, RAK4631, T-Deck, T-Beam, Xiao…) flashed with Companion firmware from flasher.meshcore.co.uk – USB Serial firmware for a cable, Bluetooth or Wi-Fi firmware otherwise. Plug it in, pick its COM port (★ = looks like a radio board) and press Connect; AndySDR reconnects by itself next time. Channels on the left: Public and any #hashtag channel anyone can join (+ Join, e.g. #uk), or private channels with a shared key. Contacts are nodes heard advertising – click one for a private, end-to-end encrypted message (✓ = sent, ✓✓ = delivered). Send advert announces you to nearby nodes. UK users normally use the UK/EU narrow preset (869.618 MHz, 62.5 kHz, SF8, CR8) – licence-exempt, the board keeps to the power and duty-cycle limits. Watch on SDR tunes your receiver to the mesh frequency so you can see the packets on the waterfall. Messages are kept in %APPDATA%\\AndySDR\\meshcore_messages.jsonl and work from remote PCs and phones too."],
  adsb: ["ADS-B aircraft (1090 MHz)", "Tracks aircraft that broadcast ADS-B (nearly every airliner and many smaller aircraft). Press ▶ Start ADS-B: AndySDR tunes to 1090 MHz, centres the receiver exactly on it and decodes the aircraft messages itself – no other program is needed. The table shows callsign, hex address (ICAO), airline and country (from the callsign/address), altitude, speed, heading, climb rate, squawk, distance from you and how long ago it was heard; click a heading to sort. The map shows each aircraft as a plane pointing the way it is flying, coloured by height (orange = low, green = middle, blue = high) with a trail behind it. Squawks 7500, 7600 and 7700 flash red and pop up a warning. You need a receiver running at 2 MS/s or more (any SDRplay RSP, RTL-SDR, Airspy, HackRF) and an antenna that works at 1090 MHz – a short 6.9 cm whip or a proper 1090 MHz collinear, as high and clear of buildings as you can get it. Use the RSP's Antenna A/B (50 Ω) socket, not Hi-Z. Turn RF GAIN up fairly high; if you see lots of messages but few aircraft, turn it down a little. Press Stop (or tune somewhere else) to go back to normal scanning. With the Simulator receiver you get ten practice aircraft."],
  adsbopts: ["ADS-B options", "My position: type your latitude/longitude (decimal degrees, e.g. 51.5074 and -0.1278) or press 📍 Use my location. With it set the map shows range rings, the table shows each aircraft's distance, and positions come out faster and more reliably. Units: aviation (feet, knots, nautical miles) or metric. Street map downloads OpenStreetMap tiles from the internet (the radar-style map works offline). Trails and Labels switch the track lines and text on the map; 'Only with position' hides aircraft that have not sent a position yet; Find dims the others on the map."],
  adsbmap: ["ADS-B map", "Drag to move, mouse wheel or +/− to zoom, double-click to zoom in on a spot, ⌖ Fit to show everything. Click an aircraft for its details; click it again (or ✕) to let go. Rings are 'distance from you' (set My position first)."],
  topstatus: ["Status strip", "A live read-out across the top of the radio: LOCAL and UTC clocks (UTC is what weather-fax and satellite schedules use), the receiver in use (RX), the width of spectrum it is receiving (SPAN), the audio filter width for the current mode (FILTER), what the scanner is doing (STATE – turns amber while scanning, searching or holding), the running decoder, and how many screens are connected (LINKS)."],
  leds: ["Indicator lights", "RX (green) – a signal is open on the squelch and you are hearing it. SCAN (amber) – scanning or searching; it flashes while sweeping and stays lit when stopped on a signal. DEC (blue) – a decoder is running. REC (red) – recording to a WAV file. LINK (green) – this screen is connected to the radio."],
  qbw: ["Bandwidth (WIDTH)", "How wide the receive filter is for the current mode. Narrower cuts out neighbouring signals and noise; wider lets the whole signal through. The list shows the usual widths for the mode. AUTO follows the ★ suggestion, which AndySDR works out from the decoder that is running, what Auto ID has measured or recognised, and the channel spacing of the band you are tuned to (the reason is shown underneath). Pick a width yourself and it is remembered for that mode; pick Auto again, or press ★, to go back to the suggestion. Broadcast FM (WFM) is always 200 kHz. The red band on the spectrum and the FILTER read-out at the top show the width in use."],
  qmode: ["Mode buttons", "One click to choose how the signal is demodulated – no need to press MODE repeatedly. NFM/FM for most two-way radio, AM for airband, WFM for FM broadcast, USB/LSB for HF voice and weather fax, CW for Morse, DIG and DIG25 for digital signals (DIG25 is the wider one for TETRA and satellites). The current mode is lit."],
  qband: ["Band buttons", "Jump straight to a popular UK band with the right mode and tuning step already set: AIR (121.500 MHz), MARINE (channel 16), PMR446, 2M, 70CM, CB, FM BC, PAGER and weather SATS. Then step with ▲ ▼, press SCAN, or use SRCH to find what is active. The button for the band you are in stays lit."],
  qdec: ["Decoder buttons", "Start a decoder with one click: DMR, P25, NXDN, dPMR, M17 (digital voice), TETRA (network, call and message signalling), PAGER (POCSAG/FLEX), APRS (also DTMF/ZVEI/EAS), WEFAX, SSTV, LRPT (Meteor-M) and ADS-B (aircraft). AUTO (Auto ID) lets AndySDR identify signals and pick the decoder itself. OFF stops the decoder. A dimmed button means its program is not installed yet – see the DECODERS tab."],
  qlock: ["Skip / lock out", "Stops the scanner dwelling on something you don't want to hear. While it is stopped on a transmission (or searching) press SKIP 10 MIN, SKIP 1 HOUR or SKIP TODAY and it carries straight on; the channel is scanned again when the time is up (or when AndySDR is restarted). LOCK OUT is the permanent version (the L/O key): a memory channel stays locked out until you untick L/O in the MEMORY tab. UNSKIP clears all temporary skips and search lockouts at once – the number shows how many are being ignored and hovering lists them. Keyboard: I = skip for 10 minutes."],
  replay: ["Instant replay", "AndySDR keeps the last two minutes of everything you heard (silences left out). Press ⏪ REPLAY under the display, or Replay here, to hear the last 15 s, 30 s, 60 s or 2 minutes again – handy when the scanner passes a conversation you wanted. Keep as recording saves it as a normal sound file in the list below. Only what came through the speaker is remembered, so turn SQUELCH up just enough to hear stations, not noise."],
  qfiles: ["Files buttons", "IMAGES – shows the WEATHER IMAGES tab: every received picture (WEFAX charts, SSTV, satellite images) with the satellite or station name, frequency and time, and a Download button that saves it to the device you are using. RECORDINGS – shows the RECORDINGS tab: your sound recordings with Play and Download buttons. Both work the same on the radio PC and on remote PCs, tablets and phones."],
  meshsdr: ["MeshCore with just the SDR", "Receive-only MeshCore using your SDR – no LoRa board. Press Start listening (UK/EU narrow is 869.618 MHz, 62.5 kHz, SF8). NODES lists every repeater, room server, companion and sensor heard advertising, with its position (click it for a map). MESSAGES shows channel messages for the Public channel and any #hashtag channel you add (or a 32-digit key). PACKET LOG lists everything heard including encrypted direct messages (which cannot be read) and damaged frames. Needs a decent 869 MHz antenna; the SDR must run at 250 kS/s or more."],
  qtog: ["Audio & step buttons", "AGC – automatic RF gain. MUTE – silences this screen without touching the VOLUME knob. MONITOR – holds the squelch open so you can hear the background noise and set up the antenna or gain; press again to return to your squelch setting. STEP − / STEP + – smaller or larger tuning step (shown on the LCD)."],
  updates: ["Updates", "AndySDR looks for a newer version on its GitHub releases page when it starts and every 12 hours. When one is found a pop-up shows what is new and asks what to do: Download & install, Remind me later, or Skip this version. After the download (checked against GitHub's SHA-256) AndySDR asks again before it installs and restarts itself. 'Check for updates now' looks straight away, even for a version you skipped. Tick 'Download new versions in the background' to have the file ready so installing takes seconds. If AndySDR was installed with the Setup program the installer runs quietly in the background; a portable (unzipped) copy copies the new files over the old ones; a copy run from Python source just opens the releases page. Your settings, memories and pictures in %APPDATA%\\AndySDR are never touched. Updating restarts the radio PC's program, so remote screens reconnect by themselves a few seconds later."],
  adsbshare: ["Share aircraft feed", "Tick to let other programs on your network use AndySDR as an ADS-B receiver: SBS/BaseStation messages on TCP port 30003 and raw AVR (*8D…;) on port 30002. Point Virtual Radar Server, tar1090/readsb, PlanePlotter or a flight-tracking-site feeder at this PC and those ports. The second box turns the daily CSV position log on or off."],
  adsbtable: ["Aircraft list", "One row per aircraft heard in the last two minutes (faded = not heard for 30 s). Click a column title to sort, click a row to highlight it on the map. Callsign and airline come from the aircraft's own identification message, so a new aircraft may show only its hex address for a few seconds. Share the live feed with other programs (Virtual Radar Server, tar1090, PlanePlotter …) from REMOTE & SETTINGS; a dump1090-style aircraft.json is at /api/aircraft.json?token=<password>."],
  pager: ["Pager messages", "Every POCSAG and FLEX page decoded by the pager decoder appears here as a row, newest at the top: time, frequency, protocol and speed, capcode (the pager's address, with the POCSAG function number), your name for it, the type (Alpha text, Numeric or Tone only) and the message. Click ☆ to highlight a capcode – its rows are coloured and a notice pops up whenever it is paged. Click + name to give a capcode a name, click a capcode to show only that one, click a frequency to tune there. Search and the filters narrow the list. Export CSV saves the list; every message is also logged to a CSV file per day in %APPDATA%\\AndySDR\\pager. The same page repeated within a minute is shown once. The tab shows a count of new messages while you are on another tab."],
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
  <h3>KNOBS &amp; DISPLAY</h3>${ctl(["volume", "squelch", "gain", "power", "leds", "lcd", "topstatus"])}
  <h3>QUICK BUTTONS (under the display)</h3>${ctl(["qmode", "qbw", "qband", "qdec", "qlock", "qtog", "qfiles", "replay"])}
  <h3>KEYPAD &amp; FUNCTION KEYS</h3>${ctl(["k_scan", "k_man", "k_srch", "k_pgm", "k_lo", "k_pri", "k_mode", "k_step", "k_bank", "k_dly", "k_rec", "k_dec", "k_dot", "k_e", "k_arrows", "k_clr"])}
  <p class="small">Keyboard: digits and <code>.</code> type, <code>Enter</code> tunes, <code>↑ ↓</code> step, <code>S</code> scan, <code>M</code> manual, <code>P</code> program, <code>L</code> lockout, <code>I</code> skip 10 min, <code>H</code> help, <code>Esc</code> clear.</p>
  <h3>SCREENS</h3>${ctl(["scope", "zoom", "vfo", "autoid", "bandmap", "bandplan", "bptab", "bppick", "sched", "satauto", "eibitab", "ft8tab", "morsebox", "banks", "import", "search", "decoders", "pager", "adsb", "adsbopts", "adsbmap", "adsbtable", "adsbshare", "updates", "receiver", "remote", "theme", "conn"])}
  <h3>DECODER PROGRAMS</h3>
  <p>AndySDR has WEFAX and SSTV built in (SSTV: Martin, Scottie, Robot and PD modes – ISS 145.800 NFM, 2 m 144.500 NFM, 20 m 14.230 USB). DSD-FME, multimon-ng and SatDump now come bundled in the <code>decoders</code> folder next to <code>AndySDR.exe</code>. If one shows ✖ in the DECODERS tab, press <b>⬇ Download missing decoders</b> and AndySDR fetches it from GitHub into <code>%APPDATA%\\AndySDR\\decoders</code>. You can also unzip your own copy into either folder (any sub-folder is fine – AndySDR finds the .exe):</p>
  <dl><dt>DSD-FME</dt><dd>github.com/lwvmobile/dsd-fme – DMR, P25 1/2, NXDN, dPMR, YSF, M17. AndySDR feeds it like SDR++'s network sink (TCP 127.0.0.1:7355, 48 kHz); DSD-FME plays decoded voice on your speakers itself (AndySDR starts the PulseAudio sound server that comes with it).</dd>
  <dt>multimon-ng</dt><dd>github.com/EliasOenal/multimon-ng – POCSAG 512/1200/2400, FLEX, APRS, DTMF, ZVEI, EAS.</dd>
  <dt>SatDump</dt><dd>satdump.org – Meteor-M N2-3 / N2-4 LRPT (137.9 or 137.1 MHz, 72k). Use DIG25 mode, start the LRPT decoder as the satellite rises, stop it when it sets – pictures appear in WEATHER IMAGES. SatDump needs the Microsoft Visual C++ 2015-2022 x64 runtime (the AndySDR installer adds it).</dd>
  <dt>TETRA</dt><dd>AndySDR demodulates the 25 kHz π/4-DQPSK carrier itself and hands the symbols to the osmo-tetra decoder (tetra-rx, bundled). Tune to the exact centre of a TETRA carrier in DIG25 mode (the control channel is the strongest, always-on one), start TETRA from the DECODE buttons or let Auto ID do it. The log shows the network (MCC/MNC and colour code) as soon as it locks, then cell broadcasts, call set-up and text messages. If it says 'no sync yet' it tells you the symbol quality and frequency error: re-centre the frequency, check the gain (AGC off, just below overload works best) and that you are on a TETRA downlink. Encrypted networks (including UK Airwave) show signalling only – AndySDR does not and must not decode encrypted speech.</dd></dl>
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
setTimeout(crAuto, 0);

/* pinch / Ctrl+wheel must zoom the SPECTRUM, never the whole browser window */
window.addEventListener("wheel", (e) => { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
["gesturestart", "gesturechange", "gestureend"].forEach((g) => document.addEventListener(g, (e) => e.preventDefault()));
window.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && ["+", "=", "-", "_", "0"].includes(e.key)) { e.preventDefault(); if (typeof setZoom === "function" && spec) { if (e.key === "0") $("zoomFit").click(); else (e.key === "-" || e.key === "_" ? $("zoomOut") : $("zoomIn")).click(); } } });

/* ======================================================= Morse code decoder box */
const MORSE_FREQS = [
  ["80 m CW QRP", 3560000, "Low-power calling area"], ["40 m CW QRP", 7030000, "Busiest QRP spot"], ["40 m CW", 7020000, "Everyday CW activity"],
  ["30 m CW", 10116000, "CW only band (10.106–10.130)"], ["20 m CW QRP", 14060000, "Busiest QRP spot"], ["20 m CW", 14030000, "Everyday CW activity"],
  ["15 m CW QRP", 21060000, "QRP calling"], ["10 m CW QRP", 28060000, "QRP calling"],
  ["20 m beacons", 14100000, "IARU / NCDXF beacons send callsigns in Morse"], ["10 m beacons", 28200000, "IARU / NCDXF beacons"],
  ["6 m CW calling", 50090000, "Morse calling frequency"], ["4 m CW (UK)", 70090000, "UK 70 MHz CW"], ["2 m CW calling", 144050000, "Morse calling frequency"],
  ["70 cm CW calling", 432050000, "Morse calling frequency"],
  ["NDB beacons", 375000, "Aviation non-directional beacons (190–530 kHz) repeat their ID in Morse"],
];
let MORSE_TXT = "";
const morseOpen = (start) => {
  $("morseBox").hidden = false;
  if (start) morseStart();
  renderMorse();
};
function morseStart() {
  if (S.mode !== "CW") send({ cmd: "mode", mode: "CW" });
  setTimeout(() => send({ cmd: "decoder", id: "morse", sub: "auto" }), 200);
}
function renderMorse() {
  const t = MORSE_TXT.slice(-3000);
  $("mPage").textContent = t; $("mPage").scrollTop = $("mPage").scrollHeight;
  $("mTape").textContent = t.slice(-26).replace(/\n/g, " ");
  $("mFreq").textContent = S.freq ? (S.freq / 1e6).toFixed(S.freq < 1e6 ? 4 : 3) + " MHz" : "--";
  $("mFreqs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", Math.abs(+b.dataset.f - (S.freq || 0)) < 600));
  $("mStart").disabled = S.decoder === "morse";
}
function morseStatus(st) {
  $("mWpm").textContent = st.ok ? Math.round(st.wpm) : "--";
  $("mTone").textContent = st.ok ? st.tone : "--";
  $("mSnr").textContent = Math.round(st.snr);
  $("mKey").classList.toggle("on", !!st.key && st.ok);
}
function onMorse(d) {
  if (d.text) { MORSE_TXT = (MORSE_TXT + d.text).slice(-4000); if (!$("morseBox").hidden) renderMorse(); }
  if (d.status) { morseStatus(d.status); }
}
$("mFreqs").innerHTML = MORSE_FREQS.map((f) => `<button data-f="${f[1]}" title="${f[2]}"><b>${(f[1] / 1e6).toFixed(f[1] < 1e6 ? 3 : 3)} MHz</b>${f[0]}</button>`).join("");
$("mFreqs").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  send({ cmd: "tune", freq: +b.dataset.f, mode: "CW" });
  if (S.decoder !== "morse") setTimeout(morseStart, 150);
};
$("mClose").onclick = () => { $("morseBox").hidden = true; };
$("mStart").onclick = morseStart;
$("mStop").onclick = () => send({ cmd: "decoder", id: "" });
$("mClear").onclick = () => { MORSE_TXT = ""; renderMorse(); };
$("mCopy").onclick = () => { try { navigator.clipboard.writeText(MORSE_TXT); } catch (e) { /* ignore */ } };
