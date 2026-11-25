/* The SETTINGS page: every setting in one place, grouped under labelled headings, with a search box.
   The controls themselves live in index.html (inside #tab-set) and keep the ids the rest of the program uses -
   this file only builds the group list, the search and the open / close behaviour. */
(() => {
  const $ = (id) => document.getElementById(id);
  const page = $("tab-set"), nav = $("setNav"), main = $("setMain"), search = $("setSearch");
  if (!page || !nav || !main) return;
  const cats = [...main.querySelectorAll(".set-cat")];
  const isPhone = () => document.body.classList.contains("phone");
  let current = "receivers", back = "mem";
  try { current = localStorage.getItem("andysdr.setcat") || current; } catch (e) { /* storage can be unavailable */ }
  if (!cats.some((c) => c.dataset.cat === current)) current = cats[0].dataset.cat;

  /* ---- the group list on the left ---- */
  nav.innerHTML = cats.map((c) =>
    `<button type="button" class="set-nav-btn" data-cat="${c.dataset.cat}"><span class="sn-i">${c.dataset.icon}</span><span class="sn-t"><b>${c.dataset.title}</b><small>${c.dataset.sub}</small></span></button>`).join("");

  function showCat(id, keepSearch) {
    current = cats.some((c) => c.dataset.cat === id) ? id : cats[0].dataset.cat;
    try { localStorage.setItem("andysdr.setcat", current); } catch (e) { /* ignore */ }
    if (!keepSearch && search.value) { search.value = ""; filter(); }
    cats.forEach((c) => c.classList.toggle("on", c.dataset.cat === current));
    nav.querySelectorAll(".set-nav-btn").forEach((b) => b.classList.toggle("on", b.dataset.cat === current));
    const on = nav.querySelector(".set-nav-btn.on");                     // narrow screens: the group list scrolls sideways - keep the chosen one in view
    if (on && nav.scrollWidth > nav.clientWidth + 2) nav.scrollTo({ left: Math.max(0, on.offsetLeft - 16), behavior: "smooth" });
  }
  nav.addEventListener("click", (e) => {
    const b = e.target.closest(".set-nav-btn"); if (!b) return;
    showCat(b.dataset.cat);
    main.scrollTop = 0; page.scrollTop = 0;
  });

  /* ---- search: every word must appear somewhere in a card (its heading, labels, hints or keywords) ---- */
  const cardOf = (c) => [...c.querySelectorAll(":scope > .set-grid > .card")];
  const haystack = new WeakMap();
  const hay = (card) => {
    if (!haystack.has(card)) haystack.set(card, (card.dataset.find + " " + card.textContent + " " + [...card.querySelectorAll("[placeholder],[title]")].map((x) => (x.getAttribute("placeholder") || "") + " " + (x.getAttribute("title") || "")).join(" ")).toLowerCase().replace(/\s+/g, " "));
    return haystack.get(card);
  };
  // a typed word matches the START of a word ("gain" finds RF gain, not "again")
  const starts = (text, w) => new RegExp("(^|[^a-z0-9])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(text);
  function filter() {
    const words = search.value.toLowerCase().split(/[\s,]+/).filter(Boolean);
    page.classList.toggle("searching", words.length > 0);
    let shown = 0;
    cats.forEach((c) => {
      let any = 0;
      cardOf(c).forEach((card) => {
        const ok = !words.length || words.every((w) => starts(hay(card), w));
        card.classList.toggle("set-miss", !ok);
        if (ok) any++;
      });
      c.classList.toggle("set-empty", words.length > 0 && !any);
      shown += any;
    });
    $("setNone").hidden = !(words.length && !shown);
  }
  search.addEventListener("input", filter);

  /* ---- open / close ---- */
  const flash = (el) => { if (!el) return; el.classList.add("set-flash"); setTimeout(() => el.classList.remove("set-flash"), 1800); };
  window.showSettings = function showSettings(cat, focusId) {
    if (!page.classList.contains("active") && typeof window.showTab === "function") window.showTab("set");
    if (search.value) { search.value = ""; filter(); }
    showCat(cat || current);
    const el = focusId ? $(focusId) : null;
    setTimeout(() => {
      if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); const card = el.closest(".card") || el; flash(card); if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(el.tagName)) { try { el.focus({ preventScroll: true }); } catch (e) { /* ignore */ } } }
      else { main.scrollTop = 0; page.scrollTop = 0; }
    }, 60);
  };
  function closeSettings() { if (typeof window.showTab === "function") window.showTab(back || "mem"); }

  // remember where we came from, and keep <body> in step (the page is a full-screen sheet on a PC)
  const orig = window.showTab;
  window.showTab = function (name) {
    if (name !== "set") back = name;
    const r = orig.apply(this, arguments);
    document.body.classList.toggle("set-open", name === "set" && !isPhone());
    if (name === "set") { const tb = document.querySelector(".topbar"); if (tb) document.documentElement.style.setProperty("--topbar-h", tb.offsetHeight + "px"); }
    if (name === "set") showCat(current, true);
    return r;
  };

  $("setBtn").addEventListener("click", () => (page.classList.contains("active") ? closeSettings() : window.showSettings()));
  $("setClose").addEventListener("click", closeSettings);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !page.classList.contains("active") || isPhone()) return;
    if (search.value) { search.value = ""; filter(); search.focus(); return; }
    closeSettings();
  });
  document.addEventListener("click", (e) => {                       // links elsewhere: <a data-settings="decoders" data-focus="id">
    const l = e.target.closest("[data-settings]"); if (!l) return;
    e.preventDefault(); window.showSettings(l.dataset.settings, l.dataset.focus);
  });

  /* ---- small buttons on the page ---- */
  $("setHelp").addEventListener("click", () => $("helpBtn").click());
  $("setCust").addEventListener("click", () => { closeSettings(); setTimeout(() => { if (!document.body.classList.contains("customise")) $("qCust").click(); window.scrollTo({ top: 0, behavior: "smooth" }); }, 80); });
  $("setRestore").addEventListener("click", () => { $("qRestore").click(); if (typeof toast === "function") toast("Every button is showing again", "info"); });

  showCat(current, true);
})();
