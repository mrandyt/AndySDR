/* AndySDR phone layout.
   On a phone-sized screen this puts a bottom bar (Radio, Memory, Weather, More) under the page, adds big tuning buttons and
   sliders that drive the same controls as the knobs, and shows one page at a time.  It only adds a class to <body> on a small
   screen; on a PC or a large tablet nothing here changes anything.  Every control it adds calls the existing one, so there is
   no second copy of the radio logic to keep in step. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const PREF = "andysdr_phone";                       // auto | on | off
  const getPref = () => { try { return localStorage.getItem(PREF) || "auto"; } catch (e) { return "auto"; } };
  const setPref = (v) => { try { localStorage.setItem(PREF, v); } catch (e) { /* private mode: it just will not be remembered */ } };

  const isPhone = () => {
    const p = getPref();
    if (p === "on") return true;
    if (p === "off") return false;
    if (window.innerWidth <= 700) return true;        // phone, portrait (the keyboard never changes the width)
    // phone, landscape: judged by the size of the screen itself, so a tablet with its keyboard open is not mistaken for a phone
    const small = Math.min(window.screen.width || 9999, window.screen.height || 9999) <= 500;
    return small && window.innerWidth <= 1000 && window.matchMedia("(pointer: coarse)").matches;
  };
  const isLandscape = () => {
    const o = window.screen && window.screen.orientation && window.screen.orientation.type;
    return o ? /landscape/.test(o) : window.innerWidth > window.innerHeight;
  };

  const ICON = {
    radio: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h16v10H4z M7 9l10-5 M8 14h4 M16 14h.01" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    memory: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14 M5 12h14 M5 18h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    weather: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 9l6 6 M13 5l6 6-3 3-6-6z M5 13l6 6-3 1-4-4z M16 19a4 4 0 0 0 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    more: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/></svg>',
  };

  let built = false, internal = false, quiet = false, page = "radio", seg = "img", subTab = "", subName = "";
  let tabNames = {};

  function tabButtons() { return [...document.querySelectorAll("nav.tabbar .tab")]; }
  function labelOf(btn) { return btn.textContent.replace(/\s+\d*\s*$/, "").trim(); }

  /* ---------- building the pieces (once) ---------- */
  function build() {
    if (built) return;
    const lcd = $("lcd"), wrap = document.querySelector("main.wrap"), tabs = document.querySelector("section.tabs");
    if (!lcd || !wrap || !tabs) return;
    built = true;
    tabButtons().forEach((b) => (tabNames[b.dataset.tab] = labelOf(b)));

    // big controls under the display
    const ctl = document.createElement("div");
    ctl.id = "phCtl";
    ctl.innerHTML = `
      <div class="ph-tune">
        <button type="button" class="ph-big" data-hold="down" aria-label="Tune down"><b>&#9660;</b><span>Down</span></button>
        <button type="button" class="ph-big" data-hold="up" aria-label="Tune up"><b>&#9650;</b><span>Up</span></button>
      </div>
      <div class="ph-act">
        <button type="button" class="ph-btn" id="phScan">Scan</button>
        <button type="button" class="ph-btn" id="phSrch">Search</button>
        <button type="button" class="ph-btn" id="phSkip">Skip</button>
      </div>
      <div class="ph-sliders">
        <label class="ph-sl"><span>Volume</span><input type="range" id="phVol"><output id="phVolO"></output></label>
        <label class="ph-sl"><span>Squelch</span><input type="range" id="phSq"><output id="phSqO"></output></label>
        <label class="ph-sl"><span>RF gain</span><input type="range" id="phGain"><output id="phGainO"></output></label>
      </div>
      <div class="ph-act">
        <button type="button" class="ph-btn" id="phAudio">Audio</button>
        <button type="button" class="ph-btn" id="phAgc">AGC</button>
        <button type="button" class="ph-btn" id="phKeys" aria-expanded="false">Scanner keys</button>
      </div>`;
    lcd.insertAdjacentElement("afterend", ctl);

    // mode / width / band / digital / tools buttons: tucked into one folding section so the spectrum is not a long scroll away
    const qj = document.querySelector(".radio .q-jump"), quick = document.querySelector(".radio .quick");
    if (qj && quick && qj.parentNode === quick.parentNode) {
      const d = document.createElement("details");
      d.className = "ph-quick";
      let open = false; try { open = localStorage.getItem("andysdr_phq") === "1"; } catch (e) {}
      d.open = open;
      d.innerHTML = "<summary><span>Mode, band and digital tools</span><i>&#9662;</i></summary>";
      qj.parentNode.insertBefore(d, qj);
      d.appendChild(qj); d.appendChild(quick);
      d.addEventListener("toggle", () => { try { localStorage.setItem("andysdr_phq", d.open ? "1" : "0"); } catch (e) {} });
    }

    // memory page: the channels come first; renaming, clearing and importing fold away
    const mem = $("tab-mem");
    if (mem && $("bankName")) {
      const t = document.createElement("details");
      t.className = "ph-tools";
      t.innerHTML = "<summary><span>Bank tools, rename and import</span><i>&#9662;</i></summary>";
      const holder = document.createElement("div");
      holder.className = "ph-tools-body";
      [$("bankName"), $("bankRename"), $("bankClear"), $("bankSource"), mem.querySelector(".import-box")].forEach((e) => { if (e) holder.appendChild(e); });
      t.appendChild(holder);
      const pager = $("memPagerTop");
      if (pager) pager.parentNode.insertBefore(t, pager);
    }

    // title / back bar for the pages other than the radio
    const bar = document.createElement("div");
    bar.id = "phBar";
    bar.innerHTML = `<button type="button" id="phBack" aria-label="Back">&#8249; More</button><b id="phTitle"></b>
      <div id="phSeg" role="tablist"><button type="button" data-seg="img">Pictures</button><button type="button" data-seg="sch">Passes and schedule</button></div>`;
    wrap.insertBefore(bar, tabs);

    // the More page
    const more = document.createElement("div");
    more.id = "phMore";
    more.innerHTML = '<div id="phMoreList"></div><div class="ph-layout"><label>Screen layout <select id="phPref"><option value="auto">Automatic</option><option value="on">Phone</option><option value="off">Full (PC style)</option></select></label><p class="muted small">Automatic uses the phone layout on small screens and the full layout on tablets and PCs.</p></div>';
    wrap.insertBefore(more, tabs.nextSibling);

    // spectrum options are tucked away on the phone
    const sb = document.querySelector(".scope-bar");
    if (sb) {
      const t = document.createElement("button");
      t.type = "button"; t.id = "phScopeOpt"; t.textContent = "Spectrum options";
      t.onclick = () => body.classList.toggle("ph-scope-opts");
      sb.parentNode.insertBefore(t, sb);
    }

    // bottom bar
    const nav = document.createElement("nav");
    nav.id = "phNav";
    nav.setAttribute("aria-label", "AndySDR pages");
    nav.innerHTML = [["radio", "Radio"], ["memory", "Memory"], ["weather", "Weather"], ["more", "More"]]
      .map(([k, t]) => `<button type="button" data-go="${k}">${ICON[k]}<span>${t}</span></button>`).join("");
    document.body.appendChild(nav);

    // a way back to the phone layout when someone chose "Full" on a small screen
    const back = document.createElement("button");
    back.type = "button"; back.id = "phToPhone"; back.textContent = "Phone view";
    back.onclick = () => { setPref("auto"); apply(); };
    document.body.appendChild(back);

    wire();
    buildMore();
  }

  function buildMore() {
    const list = $("phMoreList");
    if (!list) return;
    const skip = new Set(["mem", "img"]);
    list.innerHTML = tabButtons().filter((b) => !skip.has(b.dataset.tab))
      .map((b) => `<button type="button" class="ph-item" data-tab="${b.dataset.tab}"><span>${labelOf(b)}</span><em data-badge="${b.dataset.tab}"></em><i>&#8250;</i></button>`).join("");
    list.querySelectorAll(".ph-item").forEach((el) => (el.onclick = () => go("sub", el.dataset.tab)));
    refreshBadges();
    const sel = $("phPref");
    if (sel) { sel.value = getPref(); sel.onchange = () => { setPref(sel.value); apply(); }; }
  }

  function refreshBadges() {
    tabButtons().forEach((b) => {
      const t = b.querySelector(".tab-badge");
      const em = document.querySelector(`#phMoreList [data-badge="${b.dataset.tab}"]`);
      if (em) { const on = t && !t.hidden && t.textContent.trim() && t.textContent.trim() !== "0"; em.textContent = on ? t.textContent.trim() : ""; em.hidden = !on; }
    });
  }

  /* ---------- navigation ---------- */
  function nativeShowTab(name) {
    internal = true;
    try { if (typeof window.showTab === "function") window.showTab(name); } finally { internal = false; }
  }

  function go(p, tab) {
    if (!built) return;
    page = p;
    if (p === "memory") nativeShowTab("mem");
    else if (p === "weather") { seg = tab === "sch" ? "sch" : tab === "img" ? "img" : seg; nativeShowTab(seg); }
    else if (p === "sub") { subTab = tab; subName = tabNames[tab] || tab; nativeShowTab(tab); }
    body.dataset.pp = p;
    $("phTitle").textContent = p === "memory" ? "Memory" : p === "more" ? "More" : p === "sub" ? subName : "Weather";
    document.querySelectorAll("#phSeg [data-seg]").forEach((b) => b.classList.toggle("on", b.dataset.seg === seg));
    document.querySelectorAll("#phNav [data-go]").forEach((b) => {
      const cur = p === "sub" ? "more" : p;
      b.classList.toggle("on", b.dataset.go === cur);
      b.setAttribute("aria-current", b.dataset.go === cur ? "page" : "false");
    });
    if (p === "more") refreshBadges();
    window.scrollTo(0, 0);
    quiet = true;
    window.dispatchEvent(new Event("resize"));          // canvases measure themselves when they become visible
    quiet = false;
  }

  // something else in the app opened a tab (for example the IMAGES key): show the matching phone page
  function route(name) {
    if (!body.classList.contains("phone") || internal || !built) return;
    if (name === "mem") go("memory");
    else if (name === "img" || name === "sch") go("weather", name);
    else go("sub", name);
  }

  function wire() {
    document.querySelectorAll("#phNav [data-go]").forEach((b) => (b.onclick = () => go(b.dataset.go)));
    $("phBack").onclick = () => go("more");
    document.querySelectorAll("#phSeg [data-seg]").forEach((b) => (b.onclick = () => go("weather", b.dataset.seg)));

    const press = (k) => {
      if (typeof window.key === "function") window.key(k);
      else { const el = document.querySelector(`[data-k="${k}"]`); if (el) el.click(); }
    };
    // up / down: one tap = one step, hold = keep stepping
    document.querySelectorAll("#phCtl [data-hold]").forEach((b) => {
      let t0 = 0, t1 = 0, held = false;
      const stop = () => { clearTimeout(t0); clearInterval(t1); t0 = t1 = 0; };
      b.addEventListener("pointerdown", (e) => {
        e.preventDefault(); held = true; press(b.dataset.hold);
        t0 = setTimeout(() => { t1 = setInterval(() => press(b.dataset.hold), 140); }, 500);
      });
      ["pointerup", "pointercancel", "pointerleave"].forEach((ev) => b.addEventListener(ev, () => { held = false; stop(); }));
      b.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); press(b.dataset.hold); } });
      b.addEventListener("contextmenu", (e) => e.preventDefault());
    });
    $("phScan").onclick = () => press("scan");
    $("phSrch").onclick = () => press("srch");
    $("phSkip").onclick = () => press("skip");
    $("phAudio").onclick = () => { const e = $("pwrBtn"); if (e) e.click(); };
    $("phAgc").onclick = () => { const e = $("gainAgc"); if (e) e.click(); };
    $("phKeys").onclick = () => {
      const on = body.classList.toggle("ph-keys-open");
      $("phKeys").setAttribute("aria-expanded", on ? "true" : "false");
    };

    // sliders drive the knobs, so every other part of the app sees the same value
    const bind = (id, outId, name) => {
      const el = $(id);
      el.addEventListener("input", () => {
        if (typeof knobs !== "undefined" && knobs[name]) knobs[name].set(+el.value, true);
      });
    };
    bind("phVol", "phVolO", "vol"); bind("phSq", "phSqO", "sq"); bind("phGain", "phGainO", "gain");

    // tapping a memory channel tunes it (app.js does that); on a phone, also show the radio so you can hear/see it
    document.addEventListener("click", (e) => {
      if (!body.classList.contains("phone") || !built) return;
      const t = e.target.closest && e.target.closest('#memTable [data-act="go"]');
      if (t) setTimeout(() => go("radio"), 60);
    });

    // the bottom bar would sit on top of the keyboard: hide it while typing
    document.addEventListener("focusin", (e) => { if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== "range" && e.target.type !== "checkbox") body.classList.add("ph-typing"); });
    document.addEventListener("focusout", () => body.classList.remove("ph-typing"));
  }

  /* ---------- keeping the extra controls in step with the app ---------- */
  function mirror() {
    if (!built || !body.classList.contains("phone")) return;
    const k = typeof knobs !== "undefined" ? knobs : null;
    if (k) {
      [["phVol", "phVolO", "vol"], ["phSq", "phSqO", "sq"], ["phGain", "phGainO", "gain"]].forEach(([id, oid, n]) => {
        const el = $(id), kn = k[n];
        if (!el || !kn || document.activeElement === el) return;
        if (el.min !== String(kn.min)) el.min = kn.min;
        if (el.max !== String(kn.max)) el.max = kn.max;
        if (el.step !== String(kn.step)) el.step = kn.step;
        if (el.value !== String(kn.value)) el.value = kn.value;
      });
      [["phVolO", "oVol"], ["phSqO", "oSq"], ["phGainO", "oGain"]].forEach(([a, b]) => { if ($(a) && $(b)) $(a).textContent = $(b).textContent; });
    }
    const on = (id, el) => { const e = $(id); if (e) e.classList.toggle("on", !!(el && el.classList.contains("on"))); };
    on("phScan", $("fScan")); on("phSrch", $("fSrch"));
    on("phAudio", $("pwrLed")); on("phAgc", $("gainAgc"));
    if ($("phAudio")) $("phAudio").textContent = $("pwrLed") && $("pwrLed").classList.contains("on") ? "Audio on" : "Audio off";
    if ($("phAgc") && $("gainAgc")) $("phAgc").textContent = $("gainAgc").textContent.trim() || "AGC";
    if (page === "more") refreshBadges();
  }

  /* ---------- switching the layout on and off ---------- */
  function apply() {
    const on = isPhone();
    const land = isLandscape();
    if (on) build();
    body.classList.toggle("phone", on && built);
    body.classList.toggle("phone-land", on && built && land);
    body.classList.toggle("phone-off", !on && getPref() === "off" && Math.min(window.screen.width || 9999, window.screen.height || 9999) <= 700);
    if (!(on && built)) document.querySelectorAll("details.ph-quick, details.ph-tools").forEach((d) => { d.open = true; });   // the full layout never hides them
    if (on && built) {
      if (!body.dataset.pp) go("radio");              // only the first time: a resize (keyboard, rotation) must not change the page
    } else {
      delete body.dataset.pp;
    }
  }

  // other code calls showTab(): follow it
  (function wrapShowTab() {
    const orig = window.showTab;
    if (typeof orig !== "function" || orig.__ph) return;
    const w = function (name) { const r = orig.apply(this, arguments); route(name); return r; };
    w.__ph = true;
    window.showTab = w;
  })();

  let rt = 0;
  const later = () => { clearTimeout(rt); rt = setTimeout(apply, 120); };
  window.addEventListener("resize", () => { if (!internal && !quiet) later(); });
  window.addEventListener("orientationchange", later);
  setInterval(mirror, 250);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply); else apply();
})();
