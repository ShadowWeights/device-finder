"""
device_finder.py  -  Device Finder  (version 3)

Start it by double-clicking  "Device Finder.bat"  in the project folder.

Three ways of measuring, all through Windows' own Bluetooth:

  1. LISTEN (Bluetooth LE): hears every LE "advertisement" around the laptop.
     Phones are mostly silent here unless something wakes them up.

  2. LOOK (visible Classic devices): when switched on, Windows keeps searching
     for devices that are "visible" (discoverable). A phone is only visible
     while its Bluetooth settings screen is open, so this is mainly for tests.

  3. FOLLOW (new in version 3): the laptop "calls" your phone's Bluetooth
     address about once a second. A phone answers the call whenever its
     Bluetooth is ON - screen off, in a pocket, connected to earbuds: all
     fine. It does NOT need to be visible. The moment the call connects,
     Windows measures the signal of that connection; we read it right then,
     hang up at once and call again, so the number keeps moving.
     (The LOOK search pauses while following: it would write its own,
     differently-scaled readings into the same place and confuse the meter.)

The connection signal has its own scale, and the meter shows it as it is:
above 0 = right next to the laptop, 0 = within a few metres (inside the
radio's "golden range"), below 0 = that many steps weaker, so farther away.
Normal readings (LE and visible devices) are shown in dBm, as before.

Options (for testing):  --demo        fake devices, no Bluetooth needed
                        --no-browser  don't open the browser
                        --seconds N   stop after N seconds
                        --port N      use another port (default 8765)
"""

import json
import math
import random
import re
import sys
import threading
import time
import urllib.request
import webbrowser
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import winrt.system as wrt
from winrt.windows.devices.bluetooth import (
    BluetoothAddressType,
    BluetoothCacheMode,
    BluetoothConnectionStatus,
    BluetoothDevice,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.advertisement import (
    BluetoothLEAdvertisementScanParameters,
    BluetoothLEAdvertisementWatcher,
    BluetoothLEAdvertisementWatcherStatus as WatcherStatus,
    BluetoothLEScanningMode,
)
from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind, DeviceWatcherStatus
from winrt.windows.foundation import AsyncStatus

# ------------------------------------------------------------------ settings
PORT = 8765               # the Device Finder page lives at http://127.0.0.1:8765
FAST_SECONDS = 0.35       # smoothing for the "Fast" meter, LE readings (smaller = quicker but jumpier)
STEADY_SECONDS = 1.5      # smoothing for the "Steady" meter, LE readings
CLASSIC_FAST = 1.5        # the same two, for visible Classic devices (a reading every 10-20 s)
CLASSIC_STEADY = 6.0
LINK_FAST = 1.0           # ...and for a followed phone's connection signal (a reading every ~1 s)
LINK_STEADY = 4.0
TREND_SECONDS = 4.0       # LE: look at the last 4 s to decide "stronger" or "weaker"
TREND_CLASSIC = 15.0      # visible devices: fewer readings, so look at a longer stretch
TREND_LINK = 6.0          # a followed phone
MAX_PUSHES = 20           # send at most 20 updates per second to the page
FULL_EVERY = 0.5          # ...and the complete device list every half second
SHOW_FOR = 120            # don't show devices that were silent for 2 minutes
FORGET_AFTER = 300        # forget them completely after 5 minutes
RESTART_IF_SILENT = 30    # restart Windows' LE scanner if it hears nothing for 30 s
RETRY_EVERY = 3           # if Bluetooth is off or broken, try again every 3 s
FRESH_SEEN = 20           # a search result older than this is Windows' MEMORY, not a measurement
CALL_TIMEOUT = 15         # an unanswered call gives up by itself after ~5 s; this is the safety net
PAUSE_ANSWERED = 0.1      # wait between calls when the phone answers (one reading about every second)
PAUSE_NO_ANSWER = 1.0     # wait between calls when it doesn't (each try already takes ~5 s)
LINK_LOW, LINK_HIGH = -63, -43   # the radio's "golden range" in dBm (measured on this laptop:
                                 # the TV reads -80 in a search and -17 on a connection)
PAGE = Path(__file__).with_name("device_finder.html")
PHONES = Path(__file__).with_name("phones.json")   # phones this laptop has seen (for the Follow button)

# --------------------------------------------- what the signals can tell us
# Who made a device: the 2-byte company ID inside its Bluetooth message.
COMPANIES = {
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
    0x038F: "Xiaomi", 0x01DA: "Logitech", 0x012D: "Sony", 0x0157: "Huami",
    0x009E: "Bose", 0x0087: "Garmin", 0x00C4: "LG", 0x0171: "Amazon",
    0x027D: "Huawei",
}
# Short (16-bit) service IDs that also hint at the maker.
SERVICES = {"fe2c": "Google", "fef3": "Google", "fe9f": "Google", "feaa": "Google",
            "fcf1": "Google", "fe95": "Xiaomi", "fd5a": "Samsung", "fd69": "Samsung",
            "fe03": "Amazon"}
BASE_UUID = "-0000-1000-8000-00805f9b34fb"   # short IDs live inside this template

# Makers that can NOT be your POCO phone, in plain words.
NOT_YOUR_PHONE = {
    "Apple": "an Apple device", "Microsoft": "a Windows computer or accessory",
    "Samsung": "a Samsung device", "Logitech": "a mouse or keyboard",
    "Sony": "Sony headphones or TV", "Huami": "a fitness band",
    "Bose": "Bose headphones", "Garmin": "a Garmin watch", "LG": "an LG TV",
    "Amazon": "an Amazon device", "Huawei": "a Huawei device",
}
# Microsoft's "Connected Devices" signal says what kind of device sent it.
# Number 8 is special: an ANDROID phone running Microsoft's Phone Link app.
MS_KINDS = {1: "an Xbox", 6: "an iPhone", 7: "an iPad", 8: "an Android phone",
            9: "a Windows computer", 11: "a Windows phone", 12: "a Linux computer",
            13: "a Windows device", 14: "a Surface Hub", 15: "a Windows laptop",
            16: "a Windows tablet"}
# Classic Bluetooth devices announce what they are ("major device class").
CLASSIC_KINDS = {0: "device", 1: "computer", 2: "PHONE", 3: "network device",
                 4: "TV, speaker or headphones", 5: "mouse or keyboard",
                 6: "printer or camera", 7: "watch or wearable", 8: "toy",
                 9: "health device"}
PHONE_NAME = re.compile(r"poco|redmi|xiaomi|\bmi\b", re.IGNORECASE)
# Checked FIRST, so "Mi Band 6" or "Redmi Buds3" is not mistaken for your phone.
NOT_PHONE_NAME = re.compile(
    r"\b(tv|buds|airpods|earbuds|earphone|headphone|headset|speaker|soundbar|watch|band|"
    r"mouse|keyboard|printer|remote|tag)|stb|wh-1000|mx master|mx keys", re.IGNORECASE)
ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", re.IGNORECASE)    # like AA:BB:CC:11:22:33
GENERIC_NAME = re.compile(r"^bluetooth [0-9a-f:]{17}$", re.IGNORECASE)   # Windows' stand-in name

# ------------------------------------------------------------- shared state
# The Bluetooth callbacks write here; the web server reads from here.
lock = threading.Lock()
changed = threading.Condition(lock)  # lets the page-pusher sleep until there is news
version = 0                          # goes up by 1 every time something changes
devices = {}                         # device id -> record (see new_record)
status = {"le": "starting", "le_error": None, "restarts": 0,
          "search": "off", "search_error": None,          # "Look for visible devices"
          "follow": None,                                  # who we are calling (see Follower)
          "remembered": [],                                # phones we know the address of
          "started": time.time()}
memory = searcher = follower = None  # created in main()


def bump():
    """Something changed: wake up the page-pusher. (Call with the lock held.)"""
    global version
    version += 1
    changed.notify_all()


def touch(d):
    """This device's row changed: wake the page-pusher and mark the row. (Lock held.)"""
    bump()
    d["v"] = version            # lets the page-pusher send only what changed


# ------------------------------------------------------- understanding a signal
def understand(company, services, service_data):
    """Turn company IDs, service IDs and special signals into makers + clues."""
    makers, clues = [], set()
    for cid, data in company.items():
        if cid in COMPANIES:
            makers.append(COMPANIES[cid])
        if cid == 0x0006 and len(data) >= 2 and data[0] == 0x01:
            clues.add(f"ms{data[1] & 0x3F}")       # Microsoft: what kind of device
        elif cid == 0x0006 and data[:1] == b"\x03":
            clues.add("swiftpair")                  # an accessory that wants to pair
    for uuid in [*services, *service_data]:
        if uuid in SERVICES:
            makers.append(SERVICES[uuid])
    if "fef3" in services or "fef3" in service_data:
        clues.add("nearby")                         # Google Nearby / Quick Share
    if "fe2c" in service_data:                      # Google Fast Pair...
        clues.add("quickshare" if service_data["fe2c"][:3] == b"\xfc\x12\x8e" else "fastpair")
    if "feaa" in service_data:                      # Google Find Hub, or an old beacon
        clues.add("findhub" if service_data["feaa"][:1] in (b"\x40", b"\x41") else "eddystone")
    return list(dict.fromkeys(makers)), clues


def message_info(company, services, service_data, name, public, scan_response):
    """Everything we keep from one Bluetooth LE message."""
    makers, clues = understand(company, services, service_data)
    # The "shape" of the message. Android changes its Bluetooth address about
    # every 15 minutes, but the shape usually stays the same, so the page can
    # keep following the same device.
    shape = "|".join([",".join(f"{cid:04x}:{len(data)}" for cid, data in sorted(company.items())),
                      ",".join(sorted(services)), ",".join(sorted(service_data))])
    return {"kind": "le", "name": name or "", "makers": makers, "clues": clues,
            "shape": "" if shape == "||" else shape, "public": public,
            "scan_response": scan_response}


def read_le_message(e):
    """Pull the useful parts out of one message that Windows gives us."""
    adv = e.advertisement
    company = {m.company_id: bytes(m.data) for m in adv.manufacturer_data}
    services = {str(u)[4:8] for u in adv.service_uuids if str(u).endswith(BASE_UUID)}
    service_data = {}
    for section in adv.data_sections:
        if section.data_type == 0x16:               # 0x16 = "service data" section
            raw = bytes(section.data)
            if len(raw) >= 2:
                service_data[f"{raw[1]:02x}{raw[0]:02x}"] = raw[2:]
    return message_info(company, services, service_data, adv.local_name,
                        e.bluetooth_address_type == BluetoothAddressType.PUBLIC,
                        e.is_scan_response)


def describe(d):
    """How likely is this device your phone? Returns (score, reason in plain words)."""
    name, makers, clues = d["name"], d["makers"], d["clues"]
    if d["kind"] == "classic":
        kind = CLASSIC_KINDS.get(d["cls"], "device")
        phone_name = bool(name and PHONE_NAME.search(name))
        if d["followed"]:
            return (10 if kind == "PHONE" or phone_name else 8), \
                "You are following this " + ("PHONE" if kind == "PHONE" else kind)
        if kind == "PHONE" and phone_name:
            return 10, "A visible PHONE with a POCO/Xiaomi name - this looks like YOURS"
        if kind == "PHONE":
            return 7, "A visible PHONE - is this name yours?"
        return -3, f"Not a phone (Classic Bluetooth {kind})"
    if name and NOT_PHONE_NAME.search(name):
        return -4, "Not a phone (its name says what it is)"
    if name and PHONE_NAME.search(name):
        return 10, "This looks like YOUR PHONE (its name matches)"
    if "ms8" in clues:
        return 7, "An Android phone (Microsoft Phone Link signal) - could be yours"
    if "nearby" in clues or "quickshare" in clues:
        return 7, "Google Nearby / Quick Share signal - usually an Android phone"
    if "findhub" in clues:
        return 6, "Google Find Hub signal - an Android device or a tracker tag"
    if "swiftpair" in clues:
        return -4, "Not a phone (an accessory waiting to be paired)"
    if "fastpair" in clues:
        return -3, "Earbuds or another accessory (Google Fast Pair)"
    if "eddystone" in clues:
        return -3, "A beacon, not a phone"
    for clue in clues:
        if clue.startswith("ms"):
            return -5, f"Not your phone ({MS_KINDS.get(int(clue[2:]), 'a Microsoft device')})"
    for maker in makers:
        if maker in NOT_YOUR_PHONE:
            return -5, f"Not your phone ({NOT_YOUR_PHONE[maker]})"
    if "Xiaomi" in makers:
        return 6, "Xiaomi signal - could be your POCO (or a Xiaomi gadget)"
    if "Google" in makers:
        return 5, "Google signal - could be an Android phone"
    if d["public"]:
        return -2, "A fixed device (TV box, speaker...) - phones hide their real address"
    if name:
        return 0, "Has a name, but not a phone name"
    return 1, "Unknown device - could be anything (even your phone)"


def label_of(d):
    if d["name"]:
        return d["name"]
    if d["kind"] == "classic":
        return f"Classic {CLASSIC_KINDS.get(d['cls'], 'device')}"
    if "ms8" in d["clues"]:
        return "Android phone (Phone Link)"
    if "nearby" in d["clues"]:
        return "Google Nearby device"
    if d["makers"]:
        return f"{d['makers'][0]} device"
    return "Unknown device"


# ------------------------------------------------------------ storing readings
def new_record(kind, now):
    return {"kind": kind, "first": now, "last": now, "raw": None, "fast": None, "steady": None,
            "last3": deque(maxlen=3), "history": deque(maxlen=400), "times": deque(maxlen=400),
            "name": "", "makers": [], "clues": set(), "shape": "", "fp": "", "public": False,
            "cls": None, "address": "",
            "via": kind,          # where the newest reading came from: "le", "visible" or "link"
            "scale": "dbm",       # "dbm", or "link" for connection signals (a different scale)
            "link": None,         # the newest connection signal (0 = very close), if "link"
            "visible": None,      # True/False once the search has seen it / lost it
            "followed": False, "reach": deque(maxlen=12), "tried": None, "v": 0}


def ensure_device(key, kind, name="", cls=None, address=""):
    """Find a device record, or create an empty one. (Call with the lock held.)"""
    d = devices.get(key)
    if d is None:
        d = devices[key] = new_record(kind, time.time())
    if name and not GENERIC_NAME.match(name):
        d["name"] = name.strip()
    if cls:                       # 0 means "Windows doesn't know yet", so never overwrite with it
        d["cls"] = cls
    if address:
        d["address"] = address
    return d


def add_reading(key, rssi, now, info, smooth=True):
    """Store one signal-strength reading for one device. (Call with the lock held.)
    smooth=False for rare readings (one every 10 s), where waiting for 3 of them
    would make the meter very slow."""
    d = ensure_device(key, info["kind"], info.get("name", ""), info.get("cls"), info.get("address", ""))
    scale = "link" if info.get("via") == "link" else "dbm"
    if scale != d["scale"]:
        # A connection signal and a dBm reading can't be mixed (0 means "very
        # close" in one and "impossibly strong" in the other): start fresh.
        d["fast"] = d["steady"] = None
        d["last3"].clear()
        d["history"].clear()
        d["scale"] = scale
    if d["fast"] is not None:
        now = max(now, d["last"])       # readings must stay in time order
    # 1. Ignore single wild jumps: use the middle one of the last 3 readings.
    if smooth:
        d["last3"].append(rssi)
        value = sorted(d["last3"])[len(d["last3"]) // 2]
    else:
        d["last3"].clear()
        value = rssi
    # 2. Smooth it, two ways. The longer since the last reading, the more we
    #    trust the new one - so the meter never lags far behind.
    gap = max(0.0, now - d["last"])
    if scale == "link":
        speeds = (("fast", LINK_FAST), ("steady", LINK_STEADY))
    elif d["kind"] == "classic":
        speeds = (("fast", CLASSIC_FAST), ("steady", CLASSIC_STEADY))
    else:
        speeds = (("fast", FAST_SECONDS), ("steady", STEADY_SECONDS))
    for field, seconds in speeds:
        if d[field] is None:
            d[field] = value
        else:
            d[field] += (1 - math.exp(-gap / seconds)) * (value - d[field])
    d["raw"], d["last"] = rssi, now
    d["history"].append((now, value))
    d["times"].append(now)
    d["via"] = info.get("via", d["kind"])
    d["link"] = info.get("link")
    # 3. Remember what the device told us about itself.
    for maker in info.get("makers", ()):
        if maker not in d["makers"]:
            d["makers"].append(maker)
    d["clues"].update(info.get("clues", ()))
    if info.get("shape") and not info.get("scan_response"):
        d["shape"] = info["shape"]
    if "public" in info:
        d["public"] = info["public"]
    text = d["shape"] + "|" + d["name"]
    d["fp"] = f"{zlib.crc32(text.encode()):08x}" if text != "|" else ""
    touch(d)


def link_to_dbm(link):
    """Connection signal -> approximate dBm. Only used to sort the device list
    (the meter itself shows the connection signal as it is).
    0 = inside the radio's "golden range" (very close). Below 0: that many dB
    under the range. Above 0: that many dB over it (right next to the laptop)."""
    if link <= -45:
        return link                     # this already looks like dBm: use it as it is
    if link < 0:
        return LINK_LOW + link
    if link == 0:
        return (LINK_LOW + LINK_HIGH) / 2
    return LINK_HIGH + link


def last_seconds(points, now, seconds):
    """The newest part of a list of (time, value) points."""
    out = []
    for t, v in reversed(points):
        if now - t > seconds:
            break
        out.append((t, v))
    out.reverse()
    return out


def count_since(times, start):
    """How many readings arrived after the time 'start' (newest are at the end)."""
    n = 0
    for t in reversed(times):
        if t < start:
            break
        n += 1
    return n


def trend_of(points):
    """Fit a straight line through (time, dBm) points.
    Returns (slope, sure): slope = dB per second (+ means getting stronger),
    sure = True only if the change is clearly bigger than the random jumping."""
    n = len(points)
    if n < 4 or points[-1][0] - points[0][0] < 1.5:
        return 0.0, False
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / spread
    misses = sum((v - mean_v - slope * (t - mean_t)) ** 2 for t, v in points)
    unsure = math.sqrt(misses / (n - 2) / spread)    # the "standard error" of the slope
    return slope, abs(slope) > 2 * unsure


def row_of(key, d, now):
    window = TREND_LINK if d["scale"] == "link" else TREND_CLASSIC if d["kind"] == "classic" else TREND_SECONDS
    slope, sure = trend_of(last_seconds(d["history"], now, window))
    score, guess = describe(d)
    reach = None
    if d["reach"]:                                   # the last few calls: answered or not
        at, ok, ms = d["reach"][-1]
        reach = {"ok": ok, "ms": ms, "at": round(at, 2),
                 "hist": "".join("1" if r[1] else "0" for r in d["reach"])}
    return {"id": key, "kind": d["kind"], "label": label_of(d), "guess": guess, "score": score,
            "fast": None if d["fast"] is None else round(d["fast"], 1),
            "steady": None if d["steady"] is None else round(d["steady"], 1),
            "raw": d["raw"], "trend": round(slope, 2), "sure": sure,
            "pps": round(count_since(d["times"], now - 5) / 5, 1),
            "first": round(d["first"], 2), "last": round(d["last"], 2), "fp": d["fp"],
            "address": d["address"], "via": d["via"], "scale": d["scale"], "link": d["link"],
            "visible": d["visible"],
            "followed": d["followed"], "reach": reach,
            "tried": None if d["tried"] is None else round(d["tried"], 2)}


def snapshot(since=None):
    """What the page needs. since=None: everything. since=N: only devices
    that changed after version N (much smaller, so it can be sent often)."""
    now = time.time()
    with lock:
        if since is None:
            for key in [k for k, d in devices.items() if now - d["last"] > FORGET_AFTER and not d["followed"]]:
                del devices[key]
        rows = [row_of(key, d, now) for key, d in devices.items()
                if (now - d["last"] <= SHOW_FOR or d["followed"]) and (since is None or d["v"] > since)]
        state, seen = dict(status), version
    def strength(r):                        # strongest first; connection signals compared in dBm
        if r["fast"] is None:
            return -999
        return link_to_dbm(r["fast"]) if r["scale"] == "link" else r["fast"]
    rows.sort(key=strength, reverse=True)
    message = {"full": since is None, "v": seen, "now": round(now, 3),
               "status": state, "devices": rows[:80]}
    if since is None:
        message["total_pps"] = round(sum(r["pps"] for r in rows if r["kind"] == "le"), 1)
    return message


def wait_op(operation, timeout):
    """Wait for one of Windows' background operations to finish.
    Returns its result, or None if it failed or took longer than 'timeout'."""
    deadline = time.time() + timeout
    while operation.status == AsyncStatus.STARTED:
        if time.time() > deadline:
            try:
                operation.cancel()
            except Exception:
                pass
            return None
        time.sleep(0.02)
    try:
        return operation.get_results()
    except Exception:
        return None


# ---------------------------------------------------- listener 1: Bluetooth LE
class Listener:
    """Keeps Windows' Bluetooth LE scanner running - and restarts it if it stops."""

    def __init__(self):
        self.watcher = None
        self.generation = 0          # which scanner is the current one
        self.started_at = 0.0
        self.last_signal = 0.0
        self.problems = 0

    def start(self):
        self.generation += 1
        gen = self.generation
        watcher = BluetoothLEAdvertisementWatcher()
        watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE   # also ask for names
        try:
            watcher.allow_extended_advertisements = True          # hear newer, longer messages
        except Exception:
            pass
        try:   # ask Windows for its fastest scanning
            watcher.scan_parameters = BluetoothLEAdvertisementScanParameters.low_latency()
        except Exception:
            pass
        watcher.add_received(self.on_signal)
        watcher.add_stopped(lambda sender, e: self.on_stopped(gen, e))
        self.watcher = watcher
        self.started_at = time.time()
        watcher.start()

    def stop(self):
        old, self.watcher = self.watcher, None
        self.generation += 1          # anything the old scanner says is now ignored
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass

    def on_signal(self, sender, e):
        """CALLBACK: Windows calls this for every Bluetooth LE message it hears."""
        try:
            rssi = e.raw_signal_strength_in_dbm
            if not -127 < rssi < 20:
                return
            key = e.bluetooth_address.to_bytes(6, "big").hex(":").upper()
            info = read_le_message(e)
        except Exception as problem:  # never let one strange message stop the meter
            self.problems += 1
            if self.problems <= 3:
                print("Skipped a strange Bluetooth message:", problem)
            return
        now = time.time()
        self.last_signal = now
        with changed:
            add_reading(key, rssi, now, info)

    def on_stopped(self, gen, e):
        """CALLBACK: Windows stopped the scanner. Find out why."""
        if gen != self.generation:
            return                    # an old scanner we replaced on purpose
        error = e.error
        if error in (BluetoothError.RADIO_NOT_AVAILABLE, BluetoothError.DISABLED_BY_USER):
            set_le_state("off", "Bluetooth is turned off")
        elif error != BluetoothError.SUCCESS:
            set_le_state("error", error.name.replace("_", " ").lower())

    def run_forever(self):
        """Start the scanner, then check on it every second."""
        while True:
            watcher = self.watcher
            state = watcher.status if watcher is not None else None
            now = time.time()
            if state == WatcherStatus.STARTED:
                set_le_state("listening", None)
                if now - max(self.last_signal, self.started_at) > RESTART_IF_SILENT:
                    self.restart()            # quiet for long: give the scanner a fresh start
            elif now - self.started_at >= RETRY_EVERY:
                if state in (WatcherStatus.STOPPED, WatcherStatus.ABORTED) and status["le"] == "listening":
                    set_le_state("error", "Windows stopped the scanner")
                self.restart()                # not running (or stuck): start a new one
            time.sleep(1)

    def restart(self):
        if self.watcher is not None:
            with changed:
                status["restarts"] += 1
        self.stop()
        try:
            self.start()
        except Exception as problem:
            self.watcher = None
            self.started_at = time.time()
            set_le_state("error", str(problem) or type(problem).__name__)


def set_le_state(state, error):
    with changed:
        if (status["le"], status["le_error"]) == (state, error):
            return
        status.update(le=state, le_error=error)
        bump()
    words = {"listening": "Bluetooth: listening.",
             "off": "Bluetooth is OFF on this laptop - turn it on in Windows Settings.",
             "error": f"Bluetooth problem: {error} - trying again..."}
    print(words.get(state, state))


# --------------------------------------------------------- remembered phones
class Memory:
    """Phones this laptop has seen, kept in phones.json - so the Follow button
    is there the next time, even if Windows has forgotten the phone."""

    def __init__(self, path, readonly=False):
        self.path = path
        self.readonly = readonly   # the demo must not write into the real file
        self.items = {}            # address -> {"name", "cls", "seen", "how", "keep"}
        self.saved_at = 0.0
        try:
            for address, e in json.loads(path.read_text("utf-8")).items():
                if ADDRESS.match(address):
                    self.items[address.upper()] = {
                        "name": str(e.get("name", "")), "cls": e.get("cls"), "seen": e.get("seen"),
                        "how": e.get("how", "windows"), "keep": True}
        except (OSError, ValueError, AttributeError):
            pass                   # no file yet, or not readable: start empty
        status["remembered"] = self.listing()

    def note(self, address, name, cls, seen, how, keep=None):
        """We saw (or Windows remembers) a device. Phones are saved to the file;
        other devices are only listed for this run. (Call with the lock held.)"""
        e = self.items.setdefault(address, {"name": "", "cls": None, "seen": None, "how": how, "keep": False})
        if name and not GENERIC_NAME.match(name):
            e["name"] = name.strip()
        if cls:                    # 0 = "unknown", never overwrite a known class with it
            e["cls"] = cls
        if seen is not None and (e["seen"] is None or seen > e["seen"]):
            e["seen"] = seen
        e["how"] = how
        if keep is None:
            keep = e["cls"] == 2 or how == "followed"
        e["keep"] = e["keep"] or keep
        status["remembered"] = self.listing()
        bump()
        if e["keep"] and time.time() - self.saved_at > 5:
            self.save()

    def listing(self):
        rows = [{"address": a, "name": e["name"], "cls": e["cls"],
                 "kind": CLASSIC_KINDS.get(e["cls"], "device"), "seen": e["seen"], "how": e["how"]}
                for a, e in self.items.items()]
        rows.sort(key=lambda r: (r["cls"] == 2, r["seen"] or 0), reverse=True)   # phones first
        return rows

    def save(self):
        if self.readonly:
            return
        data = {a: {k: e[k] for k in ("name", "cls", "seen", "how")} for a, e in self.items.items() if e["keep"]}
        try:
            self.path.write_text(json.dumps(data, indent=2), "utf-8")
            self.saved_at = time.time()
        except OSError as problem:
            print("Could not save phones.json:", problem)


# --------------------------------------- listener 2: visible Classic devices
SEARCH_PROPERTIES = ["System.Devices.Aep.DeviceAddress", "System.Devices.Aep.SignalStrength",
                     "System.Devices.Aep.IsPresent", "System.Devices.Aep.IsConnected",
                     "System.Devices.Aep.Bluetooth.Cod.Major", "System.Devices.Aep.Bluetooth.LastSeenTime"]


def read_property(props, key, *unboxers):
    """Windows hands us 'boxed' values; try each way of unpacking them."""
    try:
        value = props[key] if key in props else None
    except Exception:
        return None
    if value is None:
        return None
    for unbox in unboxers:
        try:
            return unbox(value)
        except Exception:
            pass
    return None


NUMBER = (wrt.unbox_int32, wrt.unbox_int16, wrt.unbox_uint8, wrt.unbox_uint16, wrt.unbox_uint32)


def local_epoch(when):
    """Windows' "last seen" times reach us as the local clock time, labelled UTC.
    Turn them into seconds-since-1970, like time.time(). (If one ever really is
    UTC, the local reading would be in the future - then use the UTC reading.)"""
    if when is None:
        return None
    local = when.replace(tzinfo=None).timestamp()
    return local if local <= time.time() + 60 else when.timestamp()


class Searcher:
    """'Look for visible devices': keeps Windows' Classic Bluetooth search running.
    Windows refreshes each visible device's signal every 10-20 s - slow, but it
    never stops until we switch it off."""

    def __init__(self, demo=False):
        self.demo = demo
        self.watcher = None
        self.wanted = False        # the switch on the page
        self.running = False       # whether a search is going on right now
        self.generation = 0
        self.known = {}            # Windows device id -> {"name", "cls", "address"}
        self.started_at = 0.0

    def set(self, on):
        """The switch on the page was flipped."""
        self.wanted = on
        self.update()

    def update(self):
        """Run the search only if it is wanted AND nobody is being followed. While
        we follow a phone, a search would write its own readings (in dBm) into the
        same place as the connection readings, and the meter would mix them up."""
        should_run = self.wanted and follower.address is None
        with changed:
            state = "on" if should_run else "paused" if self.wanted else "off"
            if status["search"] != state:
                status.update(search=state, search_error=None)
                bump()
        if should_run and not self.running:
            self.start()
        elif not should_run and self.running:
            self.stop()

    def start(self):
        self.stop()
        self.generation += 1
        gen = self.generation
        self.started_at = time.time()
        self.running = True
        if self.demo:
            threading.Thread(target=self.demo_forever, args=(gen,), daemon=True).start()
            return
        try:
            watcher = DeviceInformation.create_watcher(
                BluetoothDevice.get_device_selector_from_pairing_state(False),
                SEARCH_PROPERTIES, DeviceInformationKind.ASSOCIATION_ENDPOINT)
            watcher.add_added(lambda s, info: self.seen(gen, info.id, info.name, info.properties))
            watcher.add_updated(lambda s, update: self.seen(gen, update.id, None, update.properties))
            watcher.add_removed(lambda s, update: self.gone(gen, update.id))
            self.watcher = watcher
            watcher.start()
        except Exception as problem:
            self.watcher = None
            with changed:
                status.update(search="error", search_error=str(problem) or type(problem).__name__)
                bump()

    def stop(self):
        old, self.watcher = self.watcher, None
        self.running = False
        self.generation += 1
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass

    def run_forever(self):
        """Once a second: if the search should be running but Windows stopped it, start it again."""
        while True:
            time.sleep(1)
            if not self.running or self.demo:
                continue
            w = self.watcher
            broken = w is None or w.status in (DeviceWatcherStatus.STOPPED, DeviceWatcherStatus.ABORTED)
            if broken and time.time() - self.started_at >= RETRY_EVERY:
                self.start()

    def seen(self, gen, device_id, name, props):
        """CALLBACK: Windows found or updated a Classic Bluetooth device."""
        if gen != self.generation:
            return
        address = read_property(props, "System.Devices.Aep.DeviceAddress", wrt.unbox_string)
        rssi = read_property(props, "System.Devices.Aep.SignalStrength", *NUMBER)
        major = read_property(props, "System.Devices.Aep.Bluetooth.Cod.Major", *NUMBER)
        when = read_property(props, "System.Devices.Aep.Bluetooth.LastSeenTime", wrt.unbox_date_time)
        connected = read_property(props, "System.Devices.Aep.IsConnected", wrt.unbox_boolean)
        now = time.time()
        with changed:
            k = self.known.setdefault(device_id, {"name": "", "cls": None, "address": ""})
            if name and name.strip():
                k["name"] = name.strip()
            if major:                 # 0 = "unknown"
                k["cls"] = major
            if address:
                k["address"] = address.upper()
            if not k["address"]:
                return
            seen_at = local_epoch(when)
            if seen_at is None or now - seen_at > FRESH_SEEN:
                # Windows' MEMORY of an earlier time - not a measurement. Just remember the device.
                memory.note(k["address"], k["name"], k["cls"], seen_at, "windows")
                return
            key = "Classic " + k["address"]
            d = ensure_device(key, "classic", k["name"], k["cls"], k["address"])
            if follower.address == k["address"] or connected:
                # Our own call refreshed this entry. That is not "visible", and the
                # follower measures this device itself (on a different scale).
                touch(d)
                return
            d["visible"] = True
            memory.note(k["address"], k["name"], k["cls"], seen_at, "visible")
            t = min(seen_at, now)
            info = {"kind": "classic", "name": k["name"], "cls": k["cls"], "address": k["address"], "via": "visible"}
            if rssi is not None and -127 < rssi < 0:
                add_reading(key, rssi, t, info, smooth=False)
            elif d["raw"] is not None and d["via"] == "visible" and t > d["last"] + 1:
                add_reading(key, d["raw"], t, info, smooth=False)    # heard again, same strength
            else:
                touch(d)

    def gone(self, gen, device_id):
        """CALLBACK: Windows no longer sees this device."""
        if gen != self.generation:
            return
        with changed:
            k = self.known.get(device_id)
            d = devices.get("Classic " + k["address"]) if k and k["address"] else None
            if d is not None and d["visible"]:
                d["visible"] = False
                touch(d)

    def demo_forever(self, gen):
        with changed:
            memory.note("D0:00:00:00:00:99", "Demo phone", 2, time.time() - 3600, "windows", keep=False)
        key, address = "Classic D0:00:00:00:00:10", "D0:00:00:00:00:10"
        while gen == self.generation:
            with changed:
                add_reading(key, round(-79 + random.uniform(-4, 4)), time.time(),
                            {"kind": "classic", "name": "[TV] Samsung 43 TV", "cls": 4,
                             "address": address, "via": "visible"}, smooth=False)
                devices[key]["visible"] = True
            time.sleep(10)


# ------------------------------------------------- listener 3: follow a phone
class Radio:
    """A handle to the laptop's Bluetooth radio - used only to hang up a call at
    once. (Windows would hang up by itself, but only about 3 seconds later.)"""

    DISCONNECT = (0x41 << 16) | (0x03 << 2)      # Windows' IOCTL_BTH_DISCONNECT_DEVICE request

    def __init__(self):
        self.handle = None
        self.kernel32 = self.find_radio = self.close_find = None
        try:
            import ctypes
            import ctypes.wintypes as wt
            self.ctypes, self.wt = ctypes, wt

            class Params(ctypes.Structure):
                _fields_ = [("dwSize", wt.DWORD)]

            self.Params = Params
            bth = ctypes.WinDLL("bthprops.cpl", use_last_error=True)
            bth.BluetoothFindFirstRadio.restype = ctypes.c_void_p
            bth.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(Params), ctypes.POINTER(wt.HANDLE)]
            bth.BluetoothFindRadioClose.argtypes = [ctypes.c_void_p]
            self.find_radio, self.close_find = bth.BluetoothFindFirstRadio, bth.BluetoothFindRadioClose
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                                      ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                                      ctypes.c_void_p]
            self.kernel32.DeviceIoControl.restype = wt.BOOL
            self.open()
        except Exception as problem:
            print("Quick hang-up is not available (each call will take ~3 s longer):", problem)

    def open(self):
        if self.find_radio is None:
            return
        handle = self.wt.HANDLE()
        params = self.Params(self.ctypes.sizeof(self.Params))
        found = self.find_radio(self.ctypes.byref(params), self.ctypes.byref(handle))
        if found:
            self.close_find(found)
            self.handle = handle

    def hang_up(self, address_int):
        """Ask Windows to drop the connection to this address. True if it accepted."""
        if self.handle is None:
            self.open()                   # Bluetooth may have been off when we started
            if self.handle is None:
                return False
        address = self.ctypes.c_ulonglong(address_int)
        returned = self.wt.DWORD()
        return bool(self.kernel32.DeviceIoControl(self.handle, self.DISCONNECT, self.ctypes.byref(address), 8,
                                                  None, 0, self.ctypes.byref(returned), None))


class Follower:
    """'Follow this phone': calls one Bluetooth address again and again.
    Each answered call gives one connection-signal reading."""

    def __init__(self, radio, demo=False):
        self.radio = radio
        self.demo = demo
        self.address = None
        self.key = None
        self.generation = 0
        self.device = None         # Windows' object for the followed address, reused call after call
        self.call_no = 0
        self.connect = None        # (call number, time, connection signal) - set by on_status
        self.total = [0, 0]        # calls made, calls answered (for the summary at the end)

    def follow(self, address):
        """Start following 'address' (like AA:BB:CC:11:22:33), or stop with None."""
        with changed:
            self.generation += 1
            gen = self.generation
            for d in devices.values():
                if d["followed"]:
                    d["followed"] = False
                    touch(d)
            self.address, self.key = address, ("Classic " + address if address else None)
            if address:
                known = memory.items.get(address, {})
                d = ensure_device(self.key, "classic", known.get("name", ""), known.get("cls"), address)
                d["followed"] = True
                d["reach"].clear()
                status["follow"] = {"address": address, "key": self.key, "state": "calling", "error": None,
                                    "calls": 0, "answered": 0, "ms": None, "since": time.time()}
                memory.note(address, d["name"], d["cls"], None, "followed")
                touch(d)
            else:
                status["follow"] = None
                bump()
        self.drop_device()
        if address:
            threading.Thread(target=self.run, args=(gen, address), daemon=True).start()
        searcher.update()          # the search pauses while we follow, and resumes after

    def drop_device(self):
        device, self.device = self.device, None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass

    def run(self, gen, address):
        """The calling loop: call, note the answer, pause, repeat."""
        address_int = int(address.replace(":", ""), 16)
        while gen == self.generation:
            try:
                answered, ms, link, name, cls = (self.demo_call if self.demo else self.call)(address_int)
            except Exception as problem:
                self.drop_device()         # start the next try with a fresh Windows object
                with changed:
                    if gen == self.generation and status["follow"]:
                        status["follow"].update(state="error", error=str(problem) or type(problem).__name__)
                        bump()
                time.sleep(RETRY_EVERY)
                continue
            now = time.time()
            self.total[0] += 1
            self.total[1] += int(answered)
            with changed:
                if gen != self.generation:
                    break
                d = ensure_device(self.key, "classic", name or "", cls, address)
                d["reach"].append((now, answered, ms))
                d["tried"] = now
                f = status["follow"]
                f.update(state="in" if answered else "out", error=None, calls=f["calls"] + 1,
                         answered=f["answered"] + int(answered), ms=ms)
                if answered:
                    memory.note(address, d["name"], d["cls"], now, "followed")
                if answered and link is not None:
                    add_reading(self.key, link, now,
                                {"kind": "classic", "address": address, "via": "link", "link": link})
                else:
                    touch(d)
            time.sleep(PAUSE_ANSWERED if answered else PAUSE_NO_ANSWER)

    def get_device(self, address_int):
        """Windows' object for the followed address (made once, then reused)."""
        if self.device is not None:
            return self.device
        for attempt in range(6):          # Windows sometimes says "no device" right after a hang-up
            device = wait_op(BluetoothDevice.from_bluetooth_address_async(address_int), 10)
            if device is not None:
                device.add_connection_status_changed(self.on_status)
                self.device = device
                return device
            time.sleep(0.3)
        raise RuntimeError("Windows could not prepare the call - is Bluetooth on?")

    def on_status(self, device, _):
        """CALLBACK: the connection came up (or went down). Windows measures the
        signal the moment the connection is made - so read it right now, before
        anything else can overwrite it."""
        try:
            if device.connection_status == BluetoothConnectionStatus.CONNECTED:
                self.connect = (self.call_no, time.time(), self.peek(device.device_id))
        except Exception:
            pass

    def call(self, address_int):
        """One call: connect, read the connection signal, hang up.
        Returns (answered, milliseconds, connection signal or None, name, device class)."""
        device = self.get_device(address_int)
        self.call_no += 1
        call_no = self.call_no
        self.connect = None
        started = time.time()
        # Asking for the device's service list is the "call": Windows must connect to answer it.
        operation = device.get_rfcomm_services_async(BluetoothCacheMode.UNCACHED)
        # Wait until the phone picks up (on_status fires) or the call gives up by itself (~5 s).
        while operation.status == AsyncStatus.STARTED and time.time() - started < CALL_TIMEOUT:
            if self.connect is not None and self.connect[0] == call_no:
                break
            time.sleep(0.005)
        ms = int((time.time() - started) * 1000)
        connect = self.connect if self.connect is not None and self.connect[0] == call_no else None
        answered = connect is not None or device.connection_status == BluetoothConnectionStatus.CONNECTED
        if operation.status == AsyncStatus.STARTED:
            try:
                operation.cancel()         # we only needed the connection, not the service list
            except Exception:
                pass
        else:
            try:
                result = operation.get_results()
                answered = answered or (result is not None and result.services.size > 0)
            except Exception:
                pass
        link = connect[2] if connect is not None else (self.peek(device.device_id) if answered else None)
        if link is not None and link <= -45:
            link = None                    # that is a search reading in dBm, not a connection reading
        name = cls = None
        if answered:
            name = device.name
            try:
                cls = int(device.class_of_device.major_class) or None     # 0 = "unknown"
            except Exception:
                pass
            self.hang_up(address_int, device)
        return answered, ms, link, name, cls

    def peek(self, device_id):
        """Ask Windows for the signal strength it measured when the call connected."""
        info = wait_op(DeviceInformation.create_from_id_async(
            device_id, ["System.Devices.Aep.SignalStrength"], DeviceInformationKind.ASSOCIATION_ENDPOINT), 5)
        if info is None:
            return None
        value = read_property(info.properties, "System.Devices.Aep.SignalStrength", *NUMBER)
        return value if value is not None and -127 < value < 40 else None

    def hang_up(self, address_int, device):
        """End the call, then wait until Windows agrees it has ended. (Otherwise
        the next call would reuse this connection, and Windows measures the
        signal only when a connection is made.)"""
        quick = self.radio.hang_up(address_int)
        deadline = time.time() + (2 if quick else 5)
        while time.time() < deadline:
            try:
                if device.connection_status != BluetoothConnectionStatus.CONNECTED:
                    return
            except Exception:
                return
            time.sleep(0.05)

    def demo_call(self, address_int):
        t = time.time() - status["started"]
        answered = (t % 45) < 32                   # "out of reach" for 13 s of every 45
        time.sleep(0.8 if answered else 5.1)
        link = None
        if answered:   # walks from about -30 (far away) up to +12 (next to the laptop) and back
            link = round(-9 + 19 * math.sin(2 * math.pi * t / 70) + random.uniform(-2, 2))
        return answered, int((0.8 if answered else 5.1) * 1000), link, None, 2


# ------------------------------------------------------------- demo (testing)
def demo_forever():
    """Pretend LE devices, so the page can be tested without real Bluetooth."""
    fakes = [  # address, company data, services, service data, name, public?, dBm, wave s, appears after s
        ("D0:00:00:00:00:01", {0x004C: b"\x10\x05\x01\x18\x44\x00"}, set(), {}, "", False, -62, 23, 0),
        ("D0:00:00:00:00:02", {0x0006: b"\x01\x09\x20\x02" + bytes(20)}, set(), {}, "", False, -82, 31, 0),
        ("D0:00:00:00:00:03", {}, set(), {"fe2c": b"\x0b\x1e\x22"}, "OnePlus Nord Buds 3r", False, -74, 19, 0),
        ("D0:00:00:00:00:04", {0xFFFF: b"\x01\x02"}, {"f798"}, {}, "Demo STB (TV box)", True, -92, 17, 0),
        ("D0:00:00:00:00:05", {}, set(), {}, "", False, -88, 29, 0),
        ("D0:00:00:00:00:06", {}, {"fef3"}, {"fef3": bytes(12)}, "", False, -66, 13, 25),  # "phone" wakes up
    ]
    set_le_state("listening", None)
    start = time.time()
    while True:
        t = time.time() - start
        for address, company, services, sdata, name, public, base, period, after in fakes:
            if t < after:
                continue
            rssi = round(base + 14 * math.sin(2 * math.pi * t / period) + random.uniform(-4, 4))
            info = message_info(company, services, sdata, name, public, False)
            with changed:
                add_reading(address, rssi, time.time(), info)
        time.sleep(0.2)


# --------------------------------------------------------- the little web server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/events":
            return self.push_updates()
        if path in ("/", "/index.html"):
            return self.reply(PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/state":                       # everything at once (handy for testing)
            return self.reply(json.dumps(snapshot()).encode(), "application/json")
        if path == "/hello":
            return self.reply(b"device-finder-3", "text/plain")
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace").strip() if length else ""
        path = self.path.split("?")[0]
        if path == "/search":                      # body: "on" or "off"
            searcher.set(body == "on")
            return self.reply(json.dumps({"search": status["search"]}).encode(), "application/json")
        if path == "/follow":                      # body: an address, or empty to stop
            address = body.upper()
            if address and not ADDRESS.match(address):
                return self.reply(json.dumps({"error": "That is not a Bluetooth address. "
                                              "It looks like AA:BB:CC:11:22:33."}).encode(), "application/json")
            follower.follow(address or None)
            return self.reply(json.dumps({"following": address or None}).encode(), "application/json")
        self.send_error(404)

    def reply(self, body, kind):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def push_updates(self):
        """SERVER-SENT EVENTS: the page opens ONE connection and we write a new
        message into it whenever there is news. No waiting, no asking."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent, last_full = -1, 0.0
        try:
            while True:
                with changed:              # sleep until there is news (or 1 s passes)
                    changed.wait_for(lambda: version != sent, timeout=1.0)
                full = time.time() - last_full >= FULL_EVERY
                message = snapshot() if full else snapshot(since=sent)
                if full:
                    last_full = time.time()
                sent = message["v"]
                data = json.dumps(message, separators=(",", ":"))
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                time.sleep(1 / MAX_PUSHES)
        except OSError:
            pass                           # the page was closed - that's fine
        self.close_connection = True

    def log_message(self, *args):          # don't print a line for every request
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False            # on Windows, "reuse" lets 2 programs share a port
    daemon_threads = True


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/hello", timeout=1) as reply:
            return reply.read() == b"device-finder-3"
    except OSError:
        return False


def start_page_server(first_port):
    """Returns (server, port). server is None if Device Finder is already running."""
    for port in range(first_port, first_port + 10):
        try:
            server = Server(("127.0.0.1", port), Handler)
        except OSError:                    # port is busy
            if already_running(port):
                return None, port
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, port
    sys.exit("Could not find a free port.")


# ------------------------------------------------------------------- start
def main():
    global memory, searcher, follower
    args = sys.argv[1:]
    seconds = float(args[args.index("--seconds") + 1]) if "--seconds" in args else None
    first_port = int(args[args.index("--port") + 1]) if "--port" in args else PORT
    demo = "--demo" in args
    memory = Memory(PHONES, readonly=demo)
    if demo:                               # the demo follows a pretend phone, never a real one
        memory.items.clear()
        with changed:
            memory.note("D0:00:00:00:00:07", "Demo phone", 2, time.time(), "followed")
    searcher = Searcher(demo)
    follower = Follower(Radio(), demo)
    server, port = start_page_server(first_port)
    url = f"http://127.0.0.1:{port}/"
    if server is None:
        print("Device Finder is already running - opening it in your browser.")
        webbrowser.open(url)
        return
    print("Device Finder is running." + ("  (DEMO - fake devices)" if demo else ""))
    print(f"Your browser should open Device Finder. If it doesn't, open:  {url}")
    print("Keep this window open while you search. Close it to stop.")
    listener = None
    if demo:
        threading.Thread(target=demo_forever, daemon=True).start()
    else:
        listener = Listener()
        threading.Thread(target=listener.run_forever, daemon=True).start()
    threading.Thread(target=searcher.run_forever, daemon=True).start()
    phones = [r for r in status["remembered"] if r["cls"] == 2]
    if len(phones) == 1:                   # exactly one known phone: follow it right away
        follower.follow(phones[0]["address"])
        print(f"Following {phones[0]['name'] or 'the remembered phone'} ({phones[0]['address']}): "
              "calling it about once a second. Press 'Stop following' on the page to stop.")
    if "--no-browser" not in args:
        webbrowser.open(url)
    try:
        deadline = time.time() + seconds if seconds else math.inf
        while time.time() < deadline:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    follower.follow(None)
    searcher.set(False)
    if listener is not None:
        listener.stop()
    summary = snapshot()
    print(f"Summary: Bluetooth={summary['status']['le']}, devices heard={len(summary['devices'])}, "
          f"LE signals/second={summary['total_pps']}, scanner restarts={summary['status']['restarts']}, "
          f"calls made={follower.total[0]}, answered={follower.total[1]}")
    print("Stopped.")


if __name__ == "__main__":
    main()
