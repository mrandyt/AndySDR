import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pg = await (await b.new_context(viewport={"width":1500,"height":900})).new_page()
        await pg.goto("http://127.0.0.1:8077/"); await pg.wait_for_timeout(2500)
        # login may be needed
        if await pg.locator("#loginModal:not([hidden])").count():
            await pg.fill("#loginPass","ANDY-75674D"); await pg.click("#loginGo"); await pg.wait_for_timeout(1500)
        await pg.evaluate("send({cmd:'tune',freq:168000000,mode:'DIG'}); send({cmd:'stepsize',hz:12500})"); await pg.wait_for_timeout(800)
        f=lambda: pg.evaluate("S.freq")
        print("start", await f())
        # pointer well below the radio (tabs area)
        await pg.mouse.move(700, 880); await pg.evaluate("window.scrollTo(0,1200)"); await pg.wait_for_timeout(300)
        y0=await pg.evaluate("scrollY")
        for k in ["ArrowDown","ArrowDown","ArrowUp","s","m"]: await pg.keyboard.press(k)
        await pg.wait_for_timeout(600)
        print("away from radio: freq", await f(), "scan", await pg.evaluate("S.scan"), "scrolled", y0, "->", await pg.evaluate("scrollY"))
        # over the radio
        await pg.evaluate("window.scrollTo(0,0)"); await pg.wait_for_timeout(300)
        box=await pg.locator("#lcd").bounding_box(); await pg.mouse.move(box["x"]+50, box["y"]+30); await pg.wait_for_timeout(200)
        await pg.keyboard.press("ArrowUp"); await pg.wait_for_timeout(600)
        print("over radio after ArrowUp:", await f())
        await pg.keyboard.press("ArrowDown"); await pg.wait_for_timeout(600)
        print("over radio after ArrowDown:", await f())
        # Ctrl+S must do nothing
        await pg.keyboard.press("Control+s"); await pg.wait_for_timeout(500)
        print("after Ctrl+S scan:", await pg.evaluate("S.scan"))
        await b.close()
asyncio.run(main())
