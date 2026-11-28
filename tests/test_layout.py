"""The page layout: every panel has somewhere to stand, in every layout.

This exists because the same fault shipped twice. The second receiver's panel is placed by a named grid area. A
CSS grid puts an item with no named area into an *implicit* row after everything else, silently - so a layout whose
`grid-template-areas` forgets `rx2p` does not break visibly, it drops the panel thousands of pixels below the rest
of the page where nobody will ever scroll. The first time, the wide layout was missed. The second time, the wide
one was fixed and the narrow one - a media query a hundred lines further down - was not, so a window under 1100px
wide still lost it.

Reading the stylesheet is the only way to catch that: it cannot be seen in one browser at one window size, which is
exactly how it survived two attempts. Every `.wrap` layout must name every area its children ask for.
"""
import os
import re
import sys

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "andysdr", "web")

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


css = open(os.path.join(WEB, "style.css"), encoding="utf-8").read()
phone_css = open(os.path.join(WEB, "phone.css"), encoding="utf-8").read()
html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()

# --- what the children of .wrap ask to be placed in --------------------------------------------------------
wanted = set(re.findall(r"\.wrap\s*>\s*\.([\w-]+)\s*\{[^}]*grid-area:\s*([\w-]+)", css))
areas_wanted = {area for _cls, area in wanted}
check("the layout places its panels by name", len(areas_wanted) >= 3, sorted(areas_wanted))
check("...the second receiver's panel among them", "rx2p" in areas_wanted, sorted(areas_wanted))

# --- every .wrap layout must name every one of them -----------------------------------------------------------
layouts = []
for m in re.finditer(r"\.wrap\s*\{([^}]*)\}", css + "\n" + phone_css):
    body = m.group(1)
    g = re.search(r"grid-template-areas:\s*([^;]+);", body)
    if g:
        line = css[:m.start()].count("\n") + 1
        named = set(re.findall(r"[\w-]+", g.group(1)))
        layouts.append((line, g.group(1).strip(), named))

check("there is more than one layout, so one of them is easy to miss", len(layouts) >= 2, len(layouts))
for line, text, named in layouts:
    missing = sorted(areas_wanted - named)
    check(f"the layout at style.css:{line} has a square for every panel", not missing,
          f"{text}  is missing {missing}")

# --- and the panel has to be in the page in the first place -------------------------------------------------
check("the panel is in the page", 'id="rx2Panel"' in html)
check("...with the class the layout places", re.search(r'class="[^"]*\brx2p\b[^"]*"[^>]*id="rx2Panel"', html)
      or re.search(r'id="rx2Panel"[^>]*class="[^"]*\brx2p\b', html), "rx2Panel has no rx2p class")

# --- it sits between the scope and the tabs in the page, which is where the layouts put it ---------------------
i_scope = html.find('class="panel scope')
i_panel = html.find('id="rx2Panel"')
i_tabs = html.find('class="panel tabs"')
check("it comes after the spectrum and before the tabs in the page itself",
      -1 < i_scope < i_panel < i_tabs, (i_scope, i_panel, i_tabs))

# --- the compact layout deliberately hides it; that is a choice, not an accident -------------------------------
check("the compact layout hides it on purpose rather than mislaying it",
      re.search(r"body\.cr\s+\.rx2p\s*\{[^}]*display:\s*none", css) is not None)

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
