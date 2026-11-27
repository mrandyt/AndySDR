"""The Auto-ID chips must not change the layout when they appear or disappear (the Decoders log used to jump)."""
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pg = await (await b.new_context(viewport={"width":1456,"height":819})).new_page()
        await pg.goto("http://127.0.0.1:8077/"); await pg.wait_for_timeout(2500)
        await pg.evaluate("showTab('dec')"); await pg.wait_for_timeout(400)
        pos = "[Math.round($('decLog').getBoundingClientRect().top+scrollY), Math.round($('sigChip').closest('.vfo-bar').getBoundingClientRect().height), Math.round($('lcd').getBoundingClientRect().height)]"
        none = await pg.evaluate(pos)
        await pg.evaluate("S.signal={label:'DMR voice',kind:'DMR',decoder:'dsd',snr:20,bw:12,stable:true}; S.decoder='dsd'; renderSignal()"); await pg.wait_for_timeout(200)
        shown = await pg.evaluate(pos)
        await pg.evaluate("S.signal={label:'Analogue FM voice · CTCSS 233.6 Hz and a very long description that goes on and on and on and on and on and on and on and on',kind:'FM voice',stable:true}; S.decoder=''; renderSignal()"); await pg.wait_for_timeout(200)
        long_ = await pg.evaluate(pos)
        await pg.evaluate("S.signal=null; renderSignal()"); await pg.wait_for_timeout(200)
        held = await pg.evaluate("[$('sigChip').hidden, " + pos + "]")      # dropped out for a moment: label still held
        await pg.wait_for_timeout(5600)
        gone = await pg.evaluate("[$('sigChip').hidden, " + pos + "]")
        print("none", none, "\nshown", shown, "\nlong ", long_, "\nheld ", held, "\ngone ", gone)
        ok = none == shown == long_ and held[0] is False and gone[0] is True and gone[1] == none
        # log: appending lines keeps the box the same size
        await pg.evaluate("for(let i=0;i<300;i++) decLine('line '+i)"); await pg.wait_for_timeout(200)
        print("log lines", await pg.evaluate("$('decLog').textContent.split('\\n').length"), "box h", await pg.evaluate("Math.round($('decLog').getBoundingClientRect().height)"))
        print("OK" if ok else "FAIL")
        await b.close()
asyncio.run(main())
