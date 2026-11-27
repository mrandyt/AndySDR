"""AUDIO key + MUTE quick button: one mute state, and the sound really stops / starts (level measured at the output)."""
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pg = await (await b.new_context(viewport={"width":1456,"height":819})).new_page()
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.add_init_script("""window.__an=null; const oc=AudioNode.prototype.connect;
          AudioNode.prototype.connect=function(d,...r){ if(d instanceof AudioDestinationNode){ const a=this.context.createAnalyser(); a.fftSize=2048; oc.call(this,a); window.__an=a; } return oc.call(this,d,...r); };""")
        await pg.goto("http://127.0.0.1:8077/"); await pg.wait_for_timeout(2500)
        await pg.evaluate("send({cmd:'tune',freq:145500000,mode:'NFM'}); send({cmd:'squelch',value:-120})"); await pg.wait_for_timeout(500)
        async def level():
            m = 0
            for _ in range(12):
                v = await pg.evaluate("(()=>{const a=window.__an; if(!a) return 0; const d=new Float32Array(a.fftSize); a.getFloatTimeDomainData(d); let s=0; for(const x of d)s+=x*x; return Math.sqrt(s/d.length)})()")
                m = max(m, v); await pg.wait_for_timeout(100)
            return m
        label = lambda: pg.evaluate("$('pwrTxt').textContent")
        fails = []
        async def step(name, action, want_on):
            await action(); await pg.wait_for_timeout(900)
            lv, lb = await level(), await label()
            ok = (lv > 0.01 and lb == "AUDIO ON") if want_on else (lv < 0.0005 and lb == "MUTED")
            print(("PASS " if ok else "FAIL ") + f"{name}: key '{lb}', level {lv:.4f}")
            if not ok: fails.append(name)
        key = lambda: pg.click("#pwrBtn")
        quick = lambda: pg.evaluate("document.querySelector('button[data-t=mute]').click()")
        await step("AUDIO key starts the sound", key, True)
        await step("AUDIO key mutes", key, False)
        await step("AUDIO key un-mutes", key, True)
        await step("quick MUTE button mutes", quick, False)
        await step("AUDIO key un-mutes after the quick mute (this used to stay silent)", key, True)
        await step("quick MUTE button mutes", quick, False)
        await step("quick MUTE button un-mutes", quick, True)
        await step("AUDIO key mutes", key, False)
        await step("quick MUTE button un-mutes", quick, True)
        srv = await pg.evaluate("player.live()")
        print("errors", errs); print("OK" if not fails and not errs else "FAIL", fails)
        await b.close()
asyncio.run(main())
