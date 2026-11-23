"""AndySDR entry point.

    AndySDR.exe                 desktop window + network server
    AndySDR.exe --server        headless server only (control it from another PC's browser)
    AndySDR.exe --browser       open the GUI in your normal web browser instead of a window
    AndySDR.exe --port 8045     change the network port
"""
import argparse
import asyncio
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser

from .config import APP_NAME, VERSION, Settings, data_dir
from .engine import Radio
from .server import Server, lan_addresses


_fault_file = None


def setup_logging():
    logfile = os.path.join(data_dir(), "logs", "andysdr.log")
    try:
        if os.path.getsize(logfile) > 20 * 1024 * 1024:          # a runaway log from an older version: start afresh
            os.remove(logfile)
    except OSError:
        pass
    handlers = [logging.handlers.RotatingFileHandler(logfile, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")]
    if sys.stdout and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:                                     # a hard crash in a driver DLL leaves a trace in the log folder
        import faulthandler
        global _fault_file
        _fault_file = open(os.path.join(os.path.dirname(logfile), "crash.txt"), "a", encoding="utf-8")
        faulthandler.enable(_fault_file, all_threads=True)
    except Exception:
        pass
    return logfile


def port_in_use(port, wait=0.0):
    """True if something is already listening on this PC's `port` (waits up to `wait` s for it to be released)."""
    end = time.time() + wait
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
            c.settimeout(0.5)
            busy = c.connect_ex(("127.0.0.1", int(port))) == 0
        if not busy or time.time() >= end:
            return busy
        time.sleep(0.5)


def run_server_thread(server, radio=None):
    def run():
        try:
            asyncio.run(server.serve())
        except Exception:
            logging.getLogger("andysdr").exception("server stopped")
            # no server means no way to use this copy - and its radio thread would keep the receiver locked
            # so that the copy that IS working cannot find it. Let go of the receiver and quit.
            try:
                if radio is not None:
                    radio.stop()
            finally:
                os._exit(1)
    t = threading.Thread(target=run, name="server", daemon=True)
    t.start()
    return t


def tk_control_window(url, settings, on_quit):
    """Small fallback window when the embedded browser (pywebview/WebView2) is unavailable."""
    import tkinter as tk
    root = tk.Tk()
    root.title(f"{APP_NAME} {VERSION}")
    root.configure(bg="#14171c")
    root.geometry("420x230")
    tk.Label(root, text="AndySDR", fg="#ffb000", bg="#14171c", font=("Segoe UI", 26, "bold")).pack(pady=(14, 0))
    tk.Label(root, text="Receiver server is running", fg="#cfd6df", bg="#14171c").pack()
    addrs = ", ".join(f"http://{a}:{settings['server_port']}" for a in lan_addresses()) or url
    tk.Label(root, text=f"Other PCs: {addrs}\nPassword: {settings['password']}", fg="#8fa0b3",
             bg="#14171c", justify="center").pack(pady=8)
    row = tk.Frame(root, bg="#14171c")
    row.pack(pady=6)
    tk.Button(row, text="Open AndySDR", width=16, command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(row, text="Quit", width=10, command=lambda: (on_quit(), root.destroy())).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", lambda: (on_quit(), root.destroy()))
    webbrowser.open(url)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--server", action="store_true", help="headless network server only")
    ap.add_argument("--browser", action="store_true", help="use the default web browser")
    ap.add_argument("--port", type=int, help="network port (default 8045)")
    ap.add_argument("--host", help="bind address (0.0.0.0 = whole network, 127.0.0.1 = this PC only)")
    ap.add_argument("--password", help="set the remote access password")
    args = ap.parse_args()

    logfile = setup_logging()
    log = logging.getLogger("andysdr")
    settings = Settings()
    if args.port:
        settings["server_port"] = args.port
    if args.host:
        settings["server_host"] = args.host
    if args.password:
        settings["password"] = args.password
    settings.save()
    log.info("%s %s starting. Data folder: %s", APP_NAME, VERSION, data_dir())

    if port_in_use(settings["server_port"], wait=6.0):              # a restart after an update gives the old copy a few seconds to go
        log.warning("AndySDR is already running on port %s - not starting a second copy", settings["server_port"])
        if not args.server:
            webbrowser.open(f"http://127.0.0.1:{settings['server_port']}/")
        return

    radio = Radio(settings)
    server = Server(radio, settings)
    radio.start()
    run_server_thread(server, radio)
    time.sleep(0.6)
    url = f"http://127.0.0.1:{settings['server_port']}/"

    def quit_all():
        radio.stop()

    if args.server:
        print(f"{APP_NAME} server running. Open http://<this-pc>:{settings['server_port']} "
              f"from another computer. LAN addresses: {lan_addresses()}  Password: {settings['password']}")
        print(f"Log: {logfile}.  Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        quit_all()
        return

    if not args.browser:
        try:
            import webview  # pywebview -> Microsoft Edge WebView2 on Windows 10/11
            webview.create_window(f"{APP_NAME} {VERSION}", url, width=1360, height=900,
                                  min_size=(1000, 700), background_color="#101215")
            webview.start()
            quit_all()
            return
        except Exception as e:
            log.warning("Embedded window unavailable (%s) – using browser", e)
    try:
        tk_control_window(url, settings, quit_all)
    except Exception:
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            quit_all()


if __name__ == "__main__":
    main()
