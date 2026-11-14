# AndySDR

A desktop SDR scanner for Windows 10/11 with a classic hardware-scanner front panel, a live
spectrum/waterfall, 1000 memory channels, a band search that finds every live transmission,
RadioReference UK CSV import, digital/analogue decoding, weather pictures, and a built-in
network server so any other PC, laptop, tablet or phone in the house can use the radio.

**Latest: 1.5.0** – AndySDR now **updates itself**: it checks GitHub for new releases and pops up a prompt before downloading and installing. (1.4.0 added the ADS-B tab, 1.3.1 the PAGER MESSAGES tab, 1.3.0 MeshCore messaging.)

![logo](andysdr/web/logo.png)

## What's in it

| Area | Details |
|---|---|
| Front panel | Three switchable styles: **PRO-2045 style** (amber 7-segment LCD), **UBCD260DN style** (black base/mobile, white-blue dot-matrix), **TRX-2 style** (rugged gunmetal, grey-green dot-matrix). VOLUME, SQUELCH and RF GAIN knobs (drag, mouse wheel, arrow keys, double-click to reset), full keypad, SCAN / MAN / SRCH / PGM / L/O / PRI / MODE / STEP / BANK / DLY / REC / DEC keys, S-meter with squelch marker, status flags. |
| Receivers | **SDRplay RSP1, RSP1A, RSP1B, RSP2, RSP2pro, RSPduo, RSPdx, RSPdx-R2 through SDRplay's own API** (antenna A/B/Hi-Z, bias-T, FM/DAB notch, LNA+IF gain, AGC). RTL-SDR (Nooelec etc.), Airspy, HackRF, LimeSDR, PlutoSDR and others via SoapySDR. rtl_tcp network dongles. Built-in **Simulator** for practice. |
| Modes | NFM, FM, AM, WFM (50 µs UK de-emphasis), USB, LSB, CW, DIG (12.5 kHz digital), DIG25 (25 kHz digital). |
| Scanning | Memory scan across selected banks with lockouts, resume delay, priority channel. Uses the spectrum to skip silent channels instantly. |
| Search | Sweeps any range (UK quick ranges included) and logs every live signal with hit count and strength; *stop on signal* or *sweep & log only*. Search lockouts. |
| UK band plan | Every UK allocation, amateur sub-band and key frequency from 9 kHz to 6.5 GHz (Ofcom UK FAT + RSGB band plans), drawn in colour across the top of the spectrum. Size selectable: Off / Small / Medium / Large. A full-range **band map** shows the whole spectrum with a red **VFO needle** and the part the waterfall is showing; the **VFO bar** names the band you are in and offers the usual mode/step. **UK BAND PLAN** tab: drill down **Plan › Band › Section** (e.g. Marine › Marine VHF › Ch 16), Tune or Search any section, add a band's key frequencies to a memory bank in one go, and show/hide each plan on the spectrum. |
| Auto ID | **Automatic signal identification**: AndySDR analyses whatever you're tuned to and works out what it is – DMR (spots the 30 ms TDMA bursts), P25, NXDN, dPMR, POCSAG/FLEX pagers, APRS, DTMF, SSTV, WEFAX, TETRA or analogue voice (with CTCSS tone) – then switches mode and starts the right decoder by itself. SSTV pictures are caught from their header. A decoder you choose yourself always wins. |
| Scheduler | Timed captures for **WEFAX charts, SSTV (ISS events), Meteor-M passes** or plain recordings: pick a station, start time, length, days of the week or a single date. AndySDR tunes, decodes, then puts the radio back how it was. Quick picks for Northwood WEFAX, ISS/2 m/HF SSTV and Meteor-M. |
| Memory | 10 banks × 100 channels. RadioReference CSV / copied-table import (UK or any country), talkgroup lists, export to CSV. |
| Decoders | DMR, P25 Phase 1/2, NXDN, dPMR, YSF, M17 (via DSD-FME) · POCSAG & FLEX pagers, APRS, DTMF, ZVEI, EAS (via multimon-ng) · **ADS-B aircraft (1090 MHz), WEFAX weather charts and SSTV pictures (Martin, Scottie, Robot, PD modes) built in** · Meteor-M LRPT satellite pictures (via SatDump) · TETRA baseband out over UDP. |
| ADS-B aircraft | **ADS-B** tab: press **▶ Start ADS-B** and AndySDR tunes to 1090 MHz and decodes Mode S / ADS-B itself (any receiver running at 2 MS/s or more – RSP2pro, RTL-SDR, Airspy, HackRF…). Live **aircraft table** (callsign, hex, airline, country, altitude, speed, heading, climb, squawk, distance, messages, last seen – click a heading to sort, filter by text) and a **map** with plane icons pointing the way they fly, height colours, trails, range rings and click-for-details; optional street map. Emergency squawks (7500/7600/7700) flash red. Enter your position for distance/bearing and faster fixes. Daily CSV log, CSV export, and an optional feed to other programs (SBS port 30003, raw port 30002, dump1090-style `aircraft.json`). Works from remote PCs and phones too. |
| MeshCore messaging | **MESHCORE** tab: text chat over the MeshCore LoRa mesh through a MeshCore companion board (Heltec V3/V4, RAK4631, T-Deck, T-Beam, Xiao…) on USB, Wi-Fi or Bluetooth. Public and #hashtag channels, private channels, direct messages with delivery ticks, node list with last-heard and hops, adverts, UK/EU preset, unread badges, message history, and **Watch on SDR** to see the packets on the waterfall. Works from remote PCs and phones too. |
| Pager messages | **PAGER MESSAGES** tab lists every POCSAG (512/1200/2400) and FLEX page as a table: time, frequency, protocol, capcode, your name for the capcode, type (text / numeric / tone only) and the message, newest first. Search, filter by protocol or type, highlight capcodes (with a pop-up notice when they are paged), export to CSV. Every page is also logged to a daily CSV file. Repeats within a minute are shown once. A badge on the tab counts new messages. Works the same from remote PCs, tablets and phones. |
| Remote | Every copy of AndySDR is also a server (port 8045). Open `http://<pc-name-or-ip>:8045` in a browser on any device on the network, enter the password, and you get the full radio with audio and waterfall. |
| Updates | **Self-update from GitHub Releases**: checks on start-up and every 12 h (or press *Check for updates now*), shows a pop-up with the release notes – **Download & install / Remind me later / Skip this version** – checks the download against GitHub's SHA-256, asks again before installing, then restarts itself. Works for the installed and the portable copy. |
| Help | **? HELP** explains every control; *Explain mode* lets you click any knob or key for a description. Hover tooltips everywhere. |

## Getting the .exe

**Easiest – let GitHub build it for you (no installs on your PC):**
1. Create a free GitHub account, make a new repository, and upload this whole folder (including the hidden `.github` folder).
2. Open the repository's **Actions** tab – the "Build AndySDR for Windows" job runs automatically (about 10 minutes).
3. Download the **AndySDR-windows** artifact: it contains `AndySDR-windows.zip` (unzip and run `AndySDR.exe`) and `AndySDR-Setup.exe` (installer with Start-menu shortcut and firewall rule).

**Or build on your own PC:**
1. Install [Miniforge](https://github.com/conda-forge/miniforge).
2. Open *Miniforge Prompt*, `cd` into this folder, run `build_windows.bat`.
3. Run `dist\AndySDR\AndySDR.exe`.

**Or run straight from source:** with Python 3.10+ installed: `pip install -r requirements.txt` then `python run_andysdr.py`.

## Updating AndySDR

AndySDR looks for a newer version on this project's GitHub Releases page a few seconds after it starts and then every
12 hours. When there is one, a pop-up shows what is new:

* **Download & install** – downloads the update (progress bar), checks its size and SHA-256 against what GitHub publishes,
  then asks **Install & restart**. AndySDR closes, updates itself and starts again (with `--server` if it was running that way).
* **Remind me later** – the pop-up returns next time AndySDR starts. **Skip this version** – not offered again until a newer one appears.
* **REMOTE & SETTINGS › Updates** has **Check for updates now** (also brings back a skipped version), a switch to turn the automatic
  check off, and *Download new versions in the background, then ask before installing*.

How it installs: a copy installed with `AndySDR-Setup.exe` runs the new installer quietly (Windows may show its usual permission
prompt if AndySDR is in *Program Files*); a portable (unzipped) copy has the new files copied over the old ones; a copy running from Python
source just opens the release page. Your settings, memories and pictures in `%APPDATA%\AndySDR` are never touched. The pop-up also appears
in a remote browser and the buttons work there too, but the update happens on the radio PC (the remote screen reconnects by itself).
Downloads are kept in `%APPDATA%\AndySDR\updates` and only ever come from `github.com/mrandyt/AndySDR/releases`.

**Publishing a new version (for you):** raise `VERSION` in `andysdr/config.py`, commit and push to `main`. GitHub builds the exe and
publishes release `v<VERSION>` with `AndySDR-Setup.exe` and `AndySDR-windows.zip` - existing copies then offer it. Pre-releases and drafts are ignored.

## One-time setup for your receivers

* **SDRplay (RSP2pro etc.)** – install *SDRplay API 3.x for Windows* from sdrplay.com. AndySDR finds `C:\Program Files\SDRplay\API\x64\sdrplay_api.dll` automatically. Close SDRuno/SDR# first – only one program can use the RSP at a time.
* **RTL-SDR / Nooelec** – run [Zadig](https://zadig.akeo.ie), *Options › List all devices*, pick *Bulk-In, Interface (Interface 0)*, install **WinUSB**.
* **Airspy / HackRF** – install the manufacturer's Windows driver.
* **Decoders** – nothing to do: DSD-FME, multimon-ng and SatDump are bundled in the `decoders` folder next to `AndySDR.exe` (the installer also adds the Visual C++ runtime SatDump needs). The DECODERS tab shows a green tick for each one; if any shows a red cross, press **⬇ Download missing decoders** and AndySDR fetches it from GitHub.

## MeshCore messaging

AndySDR's receivers can only listen, so messages are sent and received by a small **MeshCore companion
board** (about £20–40: Heltec V3/V4, RAK WisBlock 4631, LilyGO T-Deck or T-Beam, Seeed Xiao…).

1. Flash it with **Companion** firmware from <https://flasher.meshcore.co.uk> – *USB Serial* if it will be
   plugged into the PC, *Bluetooth* or *Wi-Fi* otherwise (not Repeater or Room Server).
2. In AndySDR open the **MESHCORE** tab, choose *USB (COM port)*, pick the board's port (★ marks likely
   radio boards) and press **Connect**. For Wi-Fi enter `ip:port` (usually port 5000); for Bluetooth press
   **Find boards** (enter the PIN if one is set). AndySDR reconnects automatically next time.
3. If your board is new, press **Use UK preset** (869.618 MHz, 62.5 kHz, SF8, CR8 – what most UK nodes use),
   set a **Node name**, and press **Send advert** so others can see you.

Channels: *Public* is on every board; **+ Join** adds a public `#hashtag` channel (e.g. `#uk`) or a private
channel with a 32-hex-digit key. Contacts appear when nodes advertise – click one for an end-to-end encrypted
direct message (✓ sent, ✓✓ delivered). 869.4–869.65 MHz is licence-exempt in the UK; the board's firmware
keeps to the power and duty-cycle limits.

## Using it from another PC

1. On the radio PC, open the **REMOTE & SETTINGS** tab – it shows the address(es) and password.
2. Allow AndySDR through Windows Firewall when asked (*Private networks*).
3. On the other device, open the address in Chrome/Edge/Firefox/Safari and enter the password. Press **AUDIO ON/OFF** to hear it.

To run the radio PC without a window (e.g. a spare PC in the loft): `AndySDR.exe --server`.
Options: `--port 8045`, `--host 127.0.0.1` (this PC only), `--password NEWPASS`, `--browser`.
Keep AndySDR on your home network – do not port-forward it to the internet.

## RadioReference UK import

On radioreference.com open a county or agency page in the UK database and download its CSV
(premium feature), or select the frequency table on the page, copy it and paste it into a
text file. In AndySDR: **MEMORY › RadioReference UK import**, choose the file and a bank,
press **Import CSV**. Frequency, Alpha Tag, Description, Mode, Tag and Tone columns are recognised;
FMN/FM/AM/DMR/P25/NXDN/TETRA/POCSAG modes are mapped and the right decoder is attached.
`samples/radioreference_uk_example.csv` shows the format.

## ADS-B aircraft tracking

1. You need an antenna that works at 1090 MHz – a 6.9 cm whip (quarter wave) or a proper 1090 MHz collinear/ADS-B
   antenna, as high and clear as you can get it. VHF/airband antennas are poor at 1090 MHz. On an SDRplay RSP use the
   **Antenna A / B** (50 Ω) socket, not Hi-Z. A 1090 MHz filter/LNA close to the antenna helps a lot with long cables.
2. Open the **ADS-B** tab and press **▶ Start ADS-B (1090 MHz)**. AndySDR tunes to 1090 MHz, centres the receiver
   exactly on it (if the receiver was running below 2 MS/s it is switched to 2.4 MS/s) and starts the built-in decoder.
   Turn **RF GAIN** up fairly high; if you see plenty of messages but few aircraft, turn it down a little.
3. Aircraft appear within seconds. Type your **My position** (latitude/longitude in decimal degrees, e.g. `51.5074`, `-0.1278`)
   and press **Set** – you then get range rings, each aircraft's distance and bearing, and positions decode from a single
   message instead of needing a pair.
4. **Stop** (or tuning anywhere else) takes you back to normal scanning.

| Item | What it shows |
|---|---|
| Table | Callsign · hex (ICAO) address · airline (from the callsign) or country (from the address) · altitude · speed · heading · climb rate · squawk · distance from you · messages · time since last heard. Click a column title to sort, click a row to highlight it; faded = not heard for 30 s; rows disappear after 2 minutes. |
| Map | Plane icons point along their track and are coloured by height (orange low → green → blue high), with trails behind. Drag = pan, wheel / + − = zoom, **⌖ Fit** = show everything, click a plane for its details. **Street map** adds OpenStreetMap tiles (needs internet; the radar-style map works offline). |
| Alerts | Squawk 7500 (hijack), 7600 (radio failure) and 7700 (emergency) flash red on the map and table and pop up a warning. |
| Units | Aviation (ft · kt · NM) or metric (m · km/h · km) – remembered per computer. |
| Log / export | `%APPDATA%\AndySDR\adsb\adsb_YYYY-MM-DD.csv` gets one line per aircraft every 30 s; **Export CSV** saves the current list. |
| Sharing | In **REMOTE & SETTINGS** tick *Send the ADS-B feed over the network* to let other programs use AndySDR as their receiver: **SBS/BaseStation on TCP 30003**, **raw AVR on TCP 30002**. `http://<pc>:8045/api/aircraft.json?token=<password>` is a dump1090-style list. |

How it works: the radio thread passes the raw samples to a decoder thread that builds a 2 MS/s amplitude stream, finds Mode S
preambles (at two half-sample phases so no frame falls between samples), checks the 24-bit CRC (single-bit errors in
ADS-B frames from known aircraft are repaired) and decodes identification, airborne/surface position (CPR), altitude,
velocity, squawk and emergency state. Typical decode rate in the self-test is 97–98 % of simulated frames at any sample rate from 2 to 10 MS/s.
With the **Simulator** receiver you get ten practice aircraft around 51.5 N, 1.0 W (or around your own position).
Self-test: `python tests/test_adsb.py` (decoder) and `python tests/test_radio_adsb.py` (whole engine on the simulator).

## Pager messages (POCSAG / FLEX)

1. Tune a pager frequency in **DIG** mode. In the UK most pager traffic is between about 153.0 and 153.5 MHz –
   the **Pagers** quick range in *LIVE ACTIVITY / SEARCH* finds the busy channels for you.
2. Open the **PAGER MESSAGES** tab and press **▶ Start pager decoder** (or leave *Auto-identify* on and AndySDR
   starts it when it recognises a pager signal). multimon-ng decodes POCSAG 512, 1200, 2400 and FLEX at the same time.
3. Each page appears as a new row at the top:

| Column | What it shows |
|---|---|
| ★ | Click to highlight that capcode: its rows are coloured and a notice pops up whenever it is paged. *★ Highlighted only* hides the rest. |
| Time / Frequency | When it was received and the frequency (click the frequency to tune there). |
| Protocol | POCSAG 512 / 1200 / 2400 or FLEX 1600 / 3200 / 6400. |
| Capcode | The pager's address (POCSAG adds the function number, e.g. `123456/3`). Click it to show only that capcode. |
| Name | Your name for the capcode – click **+ name** to set or change it. Names are saved and used in exports. |
| Type | *Alpha* (text), *Numeric* or *Tone* (a beep with no message). |
| Message | The decoded text. FLEX group pages list the other capcodes they went to. |

* **Search** looks in the message, capcode, name and frequency. *Protocol*, *Type* and *Hide tone-only* narrow the list further.
* **Export CSV** downloads the list (with names). **Clear list** empties the screen; log files are kept.
* Every message is also saved to `%APPDATA%\AndySDR\pager\pager_YYYY-MM-DD.csv` (one file per day), and today's
  messages come back when AndySDR restarts.
* The same page heard again within a minute (repeat transmissions, or two POCSAG speeds catching it) is listed once.
* The raw multimon-ng output still appears in the DECODERS tab log.

Remember that in the UK it is an offence to act on, or pass on, the content of pager messages you are not
authorised to receive – see *Legal* below.

## Weather pictures

* **HF WEFAX** (built in): UK Met Office Northwood on 2618.5, 4610, 8040 and 11086.5 kHz. Tune the
  frequency in USB, start the WEFAX decoder – charts appear in **WEATHER IMAGES**. Needs HF reception
  (the RSP2pro covers it; RTL-SDR needs direct-sampling mode or an upconverter).
* **Meteor-M LRPT**: Meteor-M N2-3 and N2-4 transmit on 137.9 MHz (sometimes 137.1 MHz), 72k.
  (Meteor-M N2-2's LRPT is no longer working.) Use DIG25 mode, start the LRPT decoder when the
  satellite rises and stop it when it sets; SatDump then makes the pictures. A V-dipole or QFH antenna helps a lot.
* NOAA APT satellites (137.1–137.9 MHz analogue) were retired by NOAA in 2025, so there is no APT decoder.

* **SSTV** (built in): ISS events on 145.800 MHz NFM, 2 m 144.500 MHz NFM, 20 m 14.230 MHz USB, 40 m 7.165 LSB.
  Leave Auto ID on, or start the SSTV decoder – pictures appear in **Weather images**. Use the **Scheduler** to catch them while you're out.

## Where things are stored

`%APPDATA%\AndySDR\` – `settings.json` (also holds capcode names, highlights and your ADS-B position), `memory.json`, `meshcore_messages.jsonl`, `images\`, `recordings\`, `pager\` (daily pager message logs), `adsb\` (daily aircraft position logs), `logs\andysdr.log`.

## Legal (UK)

The Wireless Telegraphy Act 2006 allows listening to broadcast, amateur and CB transmissions; it is an
offence to listen to other transmissions you are not authorised to receive or to disclose their content.
AndySDR cannot decode encrypted signals (e.g. Airwave TETRA). Check the law where you live.

## Project layout

```
andysdr/main.py        start-up, desktop window, command-line options
andysdr/engine.py      radio thread: tuning, scanner, search, priority, recording
andysdr/dsp.py         filters, demodulators, squelch, spectrum
andysdr/devices.py     SoapySDR, rtl_tcp, simulator
andysdr/sdrplay.py     native SDRplay API 3.x backend
andysdr/decoders.py    DSD-FME / multimon-ng / SatDump / TETRA plumbing
andysdr/wefax.py       built-in weather fax decoder
andysdr/pager.py       POCSAG/FLEX message parser, capcode names, daily pager logs
andysdr/adsb.py        ADS-B / Mode S demodulator, aircraft tracker, SBS + raw feeds
andysdr/adsb_sim.py    ADS-B message encoder + simulated air traffic (Simulator receiver, tests)
tests/                 decoder, whole-engine and updater self-tests (python tests/test_updater.py)
andysdr/memory.py      memory banks + RadioReference import
andysdr/meshlink.py    MeshCore messaging through a companion board (USB / Wi-Fi / Bluetooth)
andysdr/server.py      network server (HTTP + WebSocket, password protected)
andysdr/updater.py     self-update: GitHub release check, download + SHA-256 check, install, restart
andysdr/web/           the front panel (HTML/CSS/JS) and logo
AndySDR.spec           PyInstaller recipe        build_windows.bat   local build
installer.iss          Inno Setup installer      .github/workflows   cloud build
```
