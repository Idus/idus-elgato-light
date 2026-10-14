#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
#  Idus Elgato Light  ·  v1.1  ·  build 2026-10-14
#  (bump APP_VERSION + BUILD below and this line together on every update)
# ─────────────────────────────────────────────────────────────────────────────
"""
Idus Elgato Light — control Elgato Wi-Fi lights on Linux (KDE Plasma 6). GTK4.

Works with Key Light, Key Light Air, Key Light Mini, Ring Light (on / brightness /
colour temperature) and Light Strip (on / brightness / hue / saturation) over their
built-in HTTP API on port 9123 — no Elgato Control Center needed.

 • Finds the lights on your network (mDNS + a quick scan), or add one by IP.
 • Presets that remember the lights you pick — lights left out are not touched.
 • Every action as a command for OpenDeck (Starterpack › Run Command), with a
   144×144 key image to match.

GUI launches with no arguments. Same actions via CLI (OpenDeck / KDE shortcuts):
  idus-elgato-light --on | --off | --toggle      [--light NAME …]
  idus-elgato-light --preset NAME
  idus-elgato-light --brightness=+10 | =-10 | =60   [--light NAME …]
  idus-elgato-light --kelvin=+300 | =-300 | =4500   [--light NAME …]
  idus-elgato-light --list                        lights + presets
  idus-elgato-light --scan [--add]                find lights on the network
  idus-elgato-light --identify NAME               make a light blink

Dependencies (CachyOS / Arch):  sudo pacman -S --needed python-gobject gtk4
"""
import argparse
import colorsys
import concurrent.futures as cf
import ipaddress
import json
import math
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- configuration
APP_NAME = "Idus Elgato Light"
APP_SLUG = "idus-elgato-light"
APP_ID = "se.wolfpack.iduselgatolight"
APP_VERSION = "1.1"
BUILD = "2026-10-14"  # keep in sync with the header comment at the top of this file
REPO_URL = "https://github.com/Idus/idus-elgato-light"
KOFI_URL = "https://ko-fi.com/N4N2QZHKK"

# Self-contained folder: app + icon + settings live together, so one folder backup captures everything
APP_DIR = Path(__file__).resolve().parent
ICON_FILE = APP_DIR / f"{APP_SLUG}.png"
SETTINGS_FILE = APP_DIR / f"{APP_SLUG}-settings.json"
WRAPPER = Path.home() / ".local/bin" / APP_SLUG

PORT = int(os.environ.get("IEL_PORT", "9123"))
TIMEOUT = 3.0
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 100
KELVIN_MIN, KELVIN_MAX = 2900, 7000
MIRED_MIN, MIRED_MAX = 143, 344
MDNS_SERVICE = "_elg._tcp.local"

DEFAULT_SETTINGS = {
    "lights": [],          # [{id, ip, name, model, color, firmware}] — shown in this order, side by side
    "presets": [],         # [{name, lights: {light id: {on, brightness, kelvin | hue, saturation}}}]
    "first_run_done": False,
    "extra_networks": "",  # e.g. "10.0.20.0/24" — scanned in addition to this computer's own network
    "poll_seconds": 15,
    "bri_step": 10,
    "kelvin_step": 300,
    "key_dir": "",         # where key images go; empty = ~/Pictures/Idus Elgato Light
}

_lock = threading.RLock()


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    return s


def save_settings():
    with _lock:
        try:
            tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
            tmp.write_text(json.dumps(CFG, indent=2, ensure_ascii=False))
            os.replace(tmp, SETTINGS_FILE)
        except Exception:
            pass


CFG = load_settings()


def key_dir() -> Path:
    if CFG.get("key_dir"):
        return Path(os.path.expanduser(CFG["key_dir"]))
    pics = None
    if shutil.which("xdg-user-dir"):
        try:
            pics = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            pics = None
    base = Path(pics) if pics and pics != str(Path.home()) else Path.home() / "Pictures"
    return base / APP_NAME


# ---------------------------------------------------------------- colour maths
def kelvin_to_mired(k):
    k = max(KELVIN_MIN, min(KELVIN_MAX, k))
    return max(MIRED_MIN, min(MIRED_MAX, round(1_000_000 / k)))


def mired_to_kelvin(m):
    m = max(MIRED_MIN, min(MIRED_MAX, m))
    return max(KELVIN_MIN, min(KELVIN_MAX, round(1_000_000 / m)))


def kelvin_rgb(k):
    """Approximate colour of a black body at k kelvin → (r, g, b) 0..1."""
    t = k / 100.0
    if t <= 66:
        r = 255
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255
    return tuple(max(0.0, min(1.0, c / 255)) for c in (r, g, b))


def hue_rgb(hue, sat):
    return colorsys.hsv_to_rgb((hue % 360) / 360, max(0, min(100, sat)) / 100, 1.0)


def value_rgb(v: dict):
    """The colour a light value shows (for key images and preset swatches)."""
    if "hue" in v:
        return hue_rgb(v.get("hue", 0), v.get("saturation", 0))
    return kelvin_rgb(v.get("kelvin", 4500))


# ---------------------------------------------------------------- light API (port 9123)
def _http(ip, path, method="GET", body=None, timeout=TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{ip}:{PORT}{path}", data=data, method=method,
                                 headers={"Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode()) if raw.strip() else {}


def accessory_info(ip, timeout=TIMEOUT):
    try:
        return _http(ip, "/elgato/accessory-info", timeout=timeout)
    except Exception:
        return None


def get_state(ip, timeout=TIMEOUT):
    """→ {on, brightness, kelvin} or {on, brightness, hue, saturation}; None if unreachable."""
    try:
        light = _http(ip, "/elgato/lights", timeout=timeout)["lights"][0]
    except Exception:
        return None
    st = {"on": int(light.get("on", 0)), "brightness": int(light.get("brightness", 0))}
    if "hue" in light:
        st.update(hue=float(light.get("hue", 0)), saturation=float(light.get("saturation", 0)))
    else:
        st["kelvin"] = mired_to_kelvin(int(light.get("temperature", MIRED_MAX)))
    return st


def set_state(ip, on=None, brightness=None, kelvin=None, hue=None, saturation=None) -> bool:
    light = {}
    if on is not None:
        light["on"] = int(bool(on))
    if brightness is not None:
        light["brightness"] = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, int(round(brightness))))
    if kelvin is not None:
        light["temperature"] = kelvin_to_mired(int(kelvin))
    if hue is not None:
        light["hue"] = round(float(hue) % 360, 1)
    if saturation is not None:
        light["saturation"] = round(max(0.0, min(100.0, float(saturation))), 1)
    try:
        _http(ip, "/elgato/lights", "PUT", {"numberOfLights": 1, "lights": [light]})
        return True
    except Exception:
        return False


def identify(ip) -> bool:
    try:
        _http(ip, "/elgato/identify", "POST", None)
        return True
    except Exception:
        return False


def battery(ip):
    """Key Light Mini: battery % (None for mains-powered lights)."""
    try:
        b = _http(ip, "/elgato/battery-info", timeout=2)
        return round(float(b.get("level")))
    except Exception:
        return None


# ---------------------------------------------------------------- discovery
def _local_networks() -> list:
    """This computer's IPv4 networks (each limited to its /24) + the extra ones from Settings."""
    nets = []
    if shutil.which("ip"):
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                                 capture_output=True, text=True, timeout=5).stdout
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out):
                ip, pfx = m.group(1), int(m.group(2))
                nets.append(ipaddress.ip_network(f"{ip}/{max(pfx, 24)}", strict=False))
        except Exception:
            pass
    if not nets:  # no iproute2: ask the kernel which address it would use to go out
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                nets.append(ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False))
        except Exception:
            pass
    nets = [n for n in nets if not n.is_loopback]
    for part in re.split(r"[,\s]+", CFG.get("extra_networks") or ""):
        if part:
            try:
                n = ipaddress.ip_network(part, strict=False)
                if n.num_addresses <= 1024:  # a /22 at most — a scan should stay a quick one
                    nets.append(n)
            except ValueError:
                pass
    seen, out = set(), []
    for n in nets:
        if str(n) not in seen:
            seen.add(str(n))
            out.append(n)
    return out


def _mdns_responders(timeout=2.5) -> set:
    """IPs answering an mDNS query for Elgato's service (_elg._tcp). Only the answer's sender is used;
    every candidate is confirmed over HTTP afterwards."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in MDNS_SERVICE.split(".")) + b"\0"
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + qname + struct.pack("!HH", 12, 0x8001)  # PTR, QU
    found = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        sock.settimeout(0.3)
    except Exception:
        return found
    end = time.time() + timeout
    sent = 0
    with sock:
        while time.time() < end:
            if sent < 2 and time.time() > end - timeout + sent * 0.8:
                try:
                    sock.sendto(query, ("224.0.0.251", 5353))
                except Exception:
                    pass
                sent += 1
            try:
                data, (src, _port) = sock.recvfrom(9000)
                if len(data) > 12 and b"_elg" in data:
                    found.add(src)
            except socket.timeout:
                continue
            except Exception:
                break
    return found


def _port_open(ip, timeout=0.5):
    try:
        with socket.create_connection((str(ip), PORT), timeout=timeout):
            return True
    except Exception:
        return False


def discover(log=print) -> list:
    """→ [{id, ip, model, name, color, firmware}] of every Elgato light found (mDNS + port scan)."""
    candidates = set(_mdns_responders())
    nets = _local_networks()
    hosts = [str(h) for n in nets for h in n.hosts()]
    log(f"🔍 searching {', '.join(map(str, nets)) or 'nothing'} (mDNS + port {PORT}) …")
    with cf.ThreadPoolExecutor(max_workers=128) as ex:
        for ip, ok in zip(hosts, ex.map(_port_open, hosts)):
            if ok:
                candidates.add(ip)
    found = []

    def probe(ip):
        info = accessory_info(ip, timeout=2.5)
        if not info:
            return None
        st = get_state(ip, timeout=2.5)
        model = info.get("productName") or "Elgato light"
        return {"id": info.get("serialNumber") or ip, "ip": ip, "model": model,
                "name": (info.get("displayName") or "").strip(),
                "color": bool(st and "hue" in st), "firmware": info.get("firmwareVersion", "")}

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(probe, sorted(candidates, key=lambda a: tuple(int(x) for x in a.split(".")))):
            if r:
                found.append(r)
    return found


def merge_found(found: list, log=print) -> int:
    """Add new lights; update the IP of known ones (DHCP). → number added."""
    added = 0
    known = {lt["id"]: lt for lt in CFG["lights"]}
    for f in found:
        lt = known.get(f["id"]) or next((x for x in CFG["lights"] if x["ip"] == f["ip"]), None)
        if lt:
            if lt["ip"] != f["ip"]:
                log(f"· {lt['name']}: new address {lt['ip']} → {f['ip']}")
                lt["ip"] = f["ip"]
            lt.update(model=f["model"], color=f["color"], firmware=f["firmware"], id=f["id"])
            continue
        f["name"] = unique_name(f["name"] or short_model(f["model"]))
        CFG["lights"].append(f)
        log(f"➕ {f['name']} — {f['model']} at {f['ip']}")
        added += 1
    if added and not CFG["presets"]:
        CFG["presets"] = default_presets()
        log(f"· made {len(CFG['presets'])} starter presets — change them to your liking")
    save_settings()
    return added


def short_model(model: str) -> str:
    return re.sub(r"^Elgato\s+", "", model or "Light").strip() or "Light"


def unique_name(base: str) -> str:
    names = {lt["name"].lower() for lt in CFG["lights"]}
    if base.lower() not in names:
        return base
    n = 2
    while f"{base} {n}".lower() in names:
        n += 1
    return f"{base} {n}"


def add_by_ip(ip: str, log=print):
    info = accessory_info(ip)
    if not info:
        log(f"❌ no Elgato light answers at {ip}:{PORT}")
        return None
    st = get_state(ip)
    found = [{"id": info.get("serialNumber") or ip, "ip": ip, "model": info.get("productName") or "Elgato light",
              "name": (info.get("displayName") or "").strip(), "color": bool(st and "hue" in st),
              "firmware": info.get("firmwareVersion", "")}]
    merge_found(found, log)
    return found[0]


def remove_light(light_id: str):
    CFG["lights"] = [lt for lt in CFG["lights"] if lt["id"] != light_id]
    for p in CFG["presets"]:
        p["lights"].pop(light_id, None)
    CFG["presets"] = [p for p in CFG["presets"] if p["lights"]]
    save_settings()


def find_light(query: str):
    q = (query or "").strip().lower()
    return next((lt for lt in CFG["lights"] if q in (lt["name"].lower(), lt["id"].lower(), lt["ip"])), None)


def light_by_id(light_id):
    return next((lt for lt in CFG["lights"] if lt["id"] == light_id), None)


# ---------------------------------------------------------------- presets
def default_presets() -> list:
    white = [lt for lt in CFG["lights"] if not lt.get("color")]
    if not white:
        return []

    def every(bri, k):
        return {lt["id"]: {"on": 1, "brightness": bri, "kelvin": k} for lt in white}
    out = [{"name": "Meeting", "lights": every(60, 4500)},
           {"name": "Video", "lights": every(100, 5600)},
           {"name": "Warm evening", "lights": every(35, 3000)}]
    if len(white) >= 2:
        kf = {lt["id"]: {"on": 1, "brightness": 45, "kelvin": 5000} for lt in white}
        kf[white[0]["id"]]["brightness"] = 100
        out.append({"name": "Key + Fill", "lights": kf})
    return out


def find_preset(name: str):
    q = (name or "").strip().lower()
    return next((p for p in CFG["presets"] if p["name"].lower() == q), None)


def apply_values(light, v: dict) -> bool:
    if "hue" in v and light.get("color"):
        return set_state(light["ip"], on=v.get("on", 1), brightness=v.get("brightness"),
                         hue=v.get("hue"), saturation=v.get("saturation"))
    if "kelvin" in v and not light.get("color"):
        return set_state(light["ip"], on=v.get("on", 1), brightness=v.get("brightness"), kelvin=v.get("kelvin"))
    return set_state(light["ip"], on=v.get("on", 1), brightness=v.get("brightness"))


def parallel(fn, items):
    """Run fn on every item at once (lights answer slowly one by one). → list of results."""
    items = list(items)
    if not items:
        return []
    with cf.ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        return list(ex.map(fn, items))


def apply_preset(p: dict) -> tuple:
    """→ (lights ok, lights in preset)"""
    pairs = [(light_by_id(lid), v) for lid, v in p["lights"].items()]
    pairs = [(lt, v) for lt, v in pairs if lt]
    ok = parallel(lambda lv: apply_values(lv[0], lv[1]), pairs)
    return sum(ok), len(pairs)


def preset_summary(p: dict) -> str:
    parts = []
    for lid, v in p["lights"].items():
        lt = light_by_id(lid)
        if not lt:
            continue
        if not v.get("on", 1):
            parts.append(f"{lt['name']} off")
        elif "hue" in v:
            parts.append(f"{lt['name']} {v['brightness']}%·{int(v['hue'])}°")
        else:
            parts.append(f"{lt['name']} {v['brightness']}%·{v['kelvin']}K")
    return " · ".join(parts)


# ---------------------------------------------------------------- actions (shared by GUI, CLI, OpenDeck)
def _targets(names):
    if not names:
        return list(CFG["lights"]), []
    picked, unknown = [], []
    for n in names:
        lt = find_light(n)
        (picked.append(lt) if lt else unknown.append(n))
    return picked, unknown


def act_power(on, names=None) -> tuple:
    """on: True / False / None (= toggle: anything on → all off, else all on). → (ok, total, verb)"""
    lights, _ = _targets(names)
    if on is None:
        states = parallel(lambda lt: get_state(lt["ip"]), lights)
        on = not any(s and s["on"] for s in states)
    ok = parallel(lambda lt: set_state(lt["ip"], on=on), lights)
    return sum(ok), len(lights), "on" if on else "off"


def _relative(spec: str, current: float):
    spec = str(spec).strip()
    if spec[:1] in "+-":
        return current + float(spec)
    return float(spec)


def act_brightness(spec, names=None) -> tuple:
    lights, _ = _targets(names)

    def one(lt):
        st = get_state(lt["ip"])
        if not st:
            return False
        return set_state(lt["ip"], on=1, brightness=_relative(spec, st["brightness"]))
    ok = parallel(one, lights)
    return sum(ok), len(lights)


def act_kelvin(spec, names=None) -> tuple:
    lights = [lt for lt in _targets(names)[0] if not lt.get("color")]

    def one(lt):
        st = get_state(lt["ip"])
        if not st or "kelvin" not in st:
            return False
        return set_state(lt["ip"], kelvin=_relative(spec, st["kelvin"]))
    ok = parallel(one, lights)
    return sum(ok), len(lights)


def command_prefix() -> str:
    """Absolute path, so OpenDeck finds it whatever its PATH is."""
    if WRAPPER.exists():
        return shlex.quote(str(WRAPPER))
    return f"python3 {shlex.quote(str(APP_DIR / (APP_SLUG + '.py')))}"


def shortcuts() -> list:
    """Every action as a command. → [{key, label, args, kind, rgb}] — kind picks the key-image style."""
    bs, ks = int(CFG.get("bri_step") or 10), int(CFG.get("kelvin_step") or 300)
    out = [
        {"key": "all-on", "label": "All on", "args": "--on", "kind": "on", "rgb": kelvin_rgb(4500)},
        {"key": "all-off", "label": "All off", "args": "--off", "kind": "off", "rgb": (0.35, 0.38, 0.44)},
        {"key": "toggle-all", "label": "Toggle all", "args": "--toggle", "kind": "toggle", "rgb": kelvin_rgb(4500)},
    ]
    for lt in CFG["lights"]:
        q = shlex.quote(lt["name"])
        out.append({"key": f"toggle-{lt['id']}", "label": f"Toggle {lt['name']}", "args": f"--toggle --light {q}",
                    "kind": "toggle", "rgb": (0.6, 0.45, 1.0) if lt.get("color") else kelvin_rgb(5000)})
    for p in CFG["presets"]:
        vals = list(p["lights"].values())
        rgb = value_rgb(vals[0]) if vals else kelvin_rgb(4500)
        out.append({"key": f"preset-{p['name']}", "label": p["name"], "args": f"--preset {shlex.quote(p['name'])}",
                    "kind": "preset", "rgb": rgb})
    out += [
        {"key": "bri-up", "label": f"Brighter +{bs}%", "args": f"--brightness=+{bs}", "kind": "up",
         "rgb": kelvin_rgb(5000)},
        {"key": "bri-down", "label": f"Dimmer −{bs}%", "args": f"--brightness=-{bs}", "kind": "down",
         "rgb": kelvin_rgb(5000)},
        {"key": "warmer", "label": f"Warmer −{ks} K", "args": f"--kelvin=-{ks}", "kind": "warm",
         "rgb": kelvin_rgb(2900)},
        {"key": "cooler", "label": f"Cooler +{ks} K", "args": f"--kelvin=+{ks}", "kind": "cool",
         "rgb": kelvin_rgb(7000)},
    ]
    return out


def command_for(sc) -> str:
    return f"{command_prefix()} {sc['args']}"


def key_file_name(sc) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sc["label"].lower()).strip("-") + ".png"


# ---------------------------------------------------------------- CLI
def cli():
    ap = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME} v{APP_VERSION} — CLI mode "
                                 "(no arguments opens the window)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="turn lights on")
    g.add_argument("--off", action="store_true", help="turn lights off")
    g.add_argument("--toggle", action="store_true", help="any on → all off, else all on")
    g.add_argument("--preset", metavar="NAME", help="apply a preset")
    g.add_argument("--brightness", metavar="N", help="60 = set, +10 / -10 = change (use --brightness=-10)")
    g.add_argument("--kelvin", metavar="N", help="4500 = set, +300 / -300 = change (use --kelvin=-300)")
    g.add_argument("--list", action="store_true", help="list lights and presets")
    g.add_argument("--scan", action="store_true", help="find Elgato lights on the network")
    g.add_argument("--identify", metavar="NAME", help="make a light blink")
    ap.add_argument("--light", action="append", metavar="NAME", help="only this light (repeat for more)")
    ap.add_argument("--add", action="store_true", help="with --scan: add the lights found")
    ap.add_argument("-V", "--version", action="version", version=f"{APP_NAME} v{APP_VERSION} · {BUILD}")
    a = ap.parse_args()
    if not any([a.on, a.off, a.toggle, a.preset, a.brightness, a.kelvin, a.list, a.scan, a.identify]):
        return None

    if a.light:
        _l, unknown = _targets(a.light)
        if unknown:
            print(f"unknown light: {', '.join(unknown)} — lights: {', '.join(x['name'] for x in CFG['lights'])}",
                  file=sys.stderr)
            return 2
    if not CFG["lights"] and not a.scan:
        print("no lights yet — open the app, or run: idus-elgato-light --scan --add", file=sys.stderr)
        return 2

    def done(ok, total, what):
        print(f"{what}: {ok}/{total} light(s)")
        return 0 if ok == total else 1

    if a.on or a.off or a.toggle:
        ok, total, verb = act_power(True if a.on else False if a.off else None, a.light)
        return done(ok, total, f"turned {verb}")
    if a.preset is not None:
        p = find_preset(a.preset)
        if not p:
            print(f"unknown preset: {a.preset!r} — presets: {', '.join(x['name'] for x in CFG['presets'])}",
                  file=sys.stderr)
            return 2
        ok, total = apply_preset(p)
        return done(ok, total, f"preset {p['name']}")
    if a.brightness is not None:
        ok, total = act_brightness(a.brightness, a.light)
        return done(ok, total, f"brightness {a.brightness}")
    if a.kelvin is not None:
        ok, total = act_kelvin(a.kelvin, a.light)
        return done(ok, total, f"colour temperature {a.kelvin}")
    if a.identify:
        lt = find_light(a.identify)
        if not lt:
            print(f"unknown light: {a.identify}", file=sys.stderr)
            return 2
        return 0 if identify(lt["ip"]) else 1
    if a.scan:
        found = discover()
        for f in found:
            print(f"{f['ip']:<15}  {f['model']:<22}  {f['id']}  {f['name']}")
        if not found:
            print("no Elgato lights found")
        if a.add and found:
            merge_found(found)
        return 0 if found else 1
    if a.list:
        for lt in CFG["lights"]:
            st = get_state(lt["ip"], timeout=2)
            if st is None:
                state = "unreachable"
            elif "hue" in st:
                state = f"{'on' if st['on'] else 'off'} {st['brightness']}% hue {int(st['hue'])}° sat {int(st['saturation'])}%"
            else:
                state = f"{'on' if st['on'] else 'off'} {st['brightness']}% {st['kelvin']} K"
            print(f"{lt['name']:<16} {lt['ip']:<15} {short_model(lt['model']):<16} {state}")
        for p in CFG["presets"]:
            print(f"preset  {p['name']:<16} {preset_summary(p)}")
        return 0
    return None


# ---------------------------------------------------------------- GUI (GTK4)
def gui():
    import traceback
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Gtk, Gio, GLib, Gdk, Gsk, Graphene, Pango  # noqa: E402

    S = 1.15
    V, H = Gtk.Orientation.VERTICAL, Gtk.Orientation.HORIZONTAL
    POLL_CHOICES = [0, 5, 15, 60]
    POLL_LABELS = ["Off", "every 5 s", "every 15 s", "every 60 s"]

    def sc(n):
        return int(round(n * S))

    def margins(w, top, side=None):
        side = top if side is None else side
        w.set_margin_top(top)
        w.set_margin_bottom(top)
        w.set_margin_start(side)
        w.set_margin_end(side)

    def label(text, *classes, xalign=0.0, **kw):
        lb = Gtk.Label(label=text, xalign=xalign, **kw)
        for c in classes:
            lb.add_css_class(c)
        return lb

    def rgba(rgb, a=1.0):
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = rgb[0], rgb[1], rgb[2], a
        return c

    def menu_button(items, on_pick):
        """⋯ button with a popover of flat buttons. items: [(key, text)]. → (MenuButton, {key: Button})"""
        mb = Gtk.MenuButton(label="⋯", valign=Gtk.Align.CENTER)
        mb.add_css_class("rowmenu")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=V, spacing=sc(2))
        margins(box, sc(4))
        btns = {}
        for key, text in items:
            b = Gtk.Button(label=text)
            b.add_css_class("menuitem")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, k=key: (pop.popdown(), on_pick(k)))
            box.append(b)
            btns[key] = b
        pop.set_child(box)
        mb.set_popover(pop)
        return mb, btns

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        def do_activate(self):
            win = self.get_active_window()
            (win or Win(self)).present()

    # ------------------------------------------------------------ one light
    class LightCard:
        def __init__(self, win, light):
            self.win, self.light = win, light
            self.state = None
            self._sync = False
            self._pending = {}
            self._sending = False
            self._touched = 0.0
            self._plock = threading.Lock()

            self.widget = Gtk.Box(orientation=V, spacing=sc(8), valign=Gtk.Align.START)
            self.widget.add_css_class("card")
            self.widget.set_size_request(sc(290), -1)

            head = Gtk.Box(orientation=H, spacing=sc(8))
            self.dot = label("●", "dot-unknown", valign=Gtk.Align.CENTER)
            head.append(self.dot)
            names = Gtk.Box(orientation=V, hexpand=True)
            self.title = label(light["name"], "card-title", ellipsize=Pango.EllipsizeMode.END)
            self.sub = label("", "card-sub", ellipsize=Pango.EllipsizeMode.END)
            names.append(self.title)
            names.append(self.sub)
            head.append(names)
            mb, self.mi = menu_button([("identify", "💡  Identify (blink)"), ("rename", "✏️  Rename…"),
                                       ("left", "◀  Move left"), ("right", "▶  Move right"),
                                       ("remove", "🗑  Remove…")], self.on_menu)
            head.append(mb)
            self.widget.append(head)

            row = Gtk.Box(orientation=H, spacing=sc(8))
            row.append(label("On", hexpand=True, valign=Gtk.Align.CENTER))
            self.switch = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
            self.switch.connect("state-set", self.on_switch)
            row.append(self.switch)
            self.widget.append(row)

            self.bri, self.bri_val = self._slider("Brightness", BRIGHTNESS_MIN, BRIGHTNESS_MAX, 1, "grad-brightness",
                                                  lambda v: self.queue(brightness=v), "%")
            if light.get("color"):
                self.hue, self.hue_val = self._slider("Hue", 0, 360, 1, "grad-hue",
                                                      lambda v: self.queue(hue=v), "°")
                self.sat, self.sat_val = self._slider("Saturation", 0, 100, 1, "grad-sat",
                                                      lambda v: self.queue(saturation=v), "%")
            else:
                self.tmp, self.tmp_val = self._slider("Colour temperature", KELVIN_MIN, KELVIN_MAX, 50, "grad-temp",
                                                      lambda v: self.queue(kelvin=v), " K")
            self.update_labels()
            self.set_controls(False)

        def _slider(self, text, lo, hi, step, grad, send, unit):
            box = Gtk.Box(orientation=H, spacing=sc(6))
            box.append(label(text, hexpand=True))
            val = label("—", "val-label", xalign=1.0)
            box.append(val)
            self.widget.append(box)
            scale = Gtk.Scale.new_with_range(H, lo, hi, step)
            scale.set_draw_value(False)
            scale.set_hexpand(True)

            def changed(s):
                v = int(round(s.get_value()))
                val.set_label(f"{v}{unit}")
                if not self._sync:
                    send(v)
            scale.connect("value-changed", changed)
            pair = Gtk.Box(orientation=V, spacing=0)  # slider + its colour legend, tight together
            pair.append(scale)
            strip = Gtk.Box()
            strip.add_css_class("grad")
            strip.add_css_class(grad)
            strip.set_size_request(-1, sc(6))
            strip.set_margin_start(sc(11))
            strip.set_margin_end(sc(11))
            pair.append(strip)
            self.widget.append(pair)
            return scale, val

        def update_labels(self):
            lt = self.light
            self.title.set_label(lt["name"])
            bat = f" · 🔋{self.state['battery']}%" if self.state and self.state.get("battery") is not None else ""
            self.sub.set_label(f"{short_model(lt['model'])} · {lt['ip']}{bat}")
            i = CFG["lights"].index(lt) if lt in CFG["lights"] else 0
            self.mi["left"].set_sensitive(i > 0)
            self.mi["right"].set_sensitive(i < len(CFG["lights"]) - 1)

        def set_controls(self, ok):
            for w in self._controls():
                w.set_sensitive(ok)

        def _controls(self):
            return [self.switch, self.bri] + ([self.hue, self.sat] if self.light.get("color") else [self.tmp])

        def show_state(self, st):
            """Main thread: show a state read from the light (None = unreachable)."""
            if st is not None and time.time() - self._touched < 3:
                return  # the user is moving a slider — don't fight them
            self.state = st
            for c in ("dot-ok", "dot-off", "dot-unknown"):
                self.dot.remove_css_class(c)
            self.dot.add_css_class("dot-unknown" if st is None and not self.win.polled else
                                   "dot-off" if st is None else "dot-ok")
            self.dot.set_tooltip_text("unreachable" if st is None else ("on" if st["on"] else "off"))
            self.set_controls(st is not None)
            if st:
                self._sync = True
                self.switch.set_active(bool(st["on"]))
                self.bri.set_value(st["brightness"])
                if self.light.get("color"):
                    self.hue.set_value(st.get("hue", 0))
                    self.sat.set_value(st.get("saturation", 0))
                elif "kelvin" in st:
                    self.tmp.set_value(st["kelvin"])
                self._sync = False
            self.update_labels()

        def current_values(self):
            """What the card shows now — saved into presets."""
            if self.state is None:
                return None
            v = {"on": int(self.switch.get_active()), "brightness": int(self.bri.get_value())}
            if self.light.get("color"):
                v.update(hue=int(self.hue.get_value()), saturation=int(self.sat.get_value()))
            else:
                v["kelvin"] = int(self.tmp.get_value())
            return v

        # ---- send changes: coalesce while a slider moves, one request in flight per light
        def queue(self, **vals):
            self._touched = time.time()
            with self._plock:
                self._pending.update(vals)
                if self._sending:
                    return
                self._sending = True
            threading.Thread(target=self._sender, daemon=True).start()

        def _sender(self):
            while True:
                with self._plock:
                    vals, self._pending = self._pending, {}
                    if not vals:
                        self._sending = False
                        return
                if not set_state(self.light["ip"], **vals):
                    GLib.idle_add(self.win.log, f"❌ {self.light['name']}: no answer from {self.light['ip']}")
                time.sleep(0.05)  # ≤ 20 requests/s per light — Wi-Fi lights choke on more; the last value always lands

        def on_switch(self, _sw, state):
            if not self._sync:
                self.queue(on=1 if state else 0)
            return False

        def on_menu(self, key):
            w, lt = self.win, self.light
            if key == "identify":
                threading.Thread(target=lambda: identify(lt["ip"]), daemon=True).start()
                w.log(f"💡 {lt['name']} blinks")
            elif key == "rename":
                w.ask_text(f"Rename {lt['name']}", "Name", lt["name"], self._rename)
            elif key in ("left", "right"):
                i = CFG["lights"].index(lt)
                j = i - 1 if key == "left" else i + 1
                if 0 <= j < len(CFG["lights"]):
                    CFG["lights"][i], CFG["lights"][j] = CFG["lights"][j], CFG["lights"][i]
                    save_settings()
                    w.rebuild_all()
            elif key == "remove":
                n = sum(1 for p in CFG["presets"] if lt["id"] in p["lights"])
                w.confirm(f"Remove {lt['name']} ({lt['ip']})?\n\nIt is taken out of {n} preset(s). "
                          "Find lights brings it back.", lambda: (remove_light(lt["id"]),
                                                                  w.log(f"🗑 removed {lt['name']}"),
                                                                  w.rebuild_all()), "Remove")

        def _rename(self, name):
            name = name.strip()
            if not name or any(x is not self.light and x["name"].lower() == name.lower() for x in CFG["lights"]):
                self.win.log("❌ that name is empty or already used")
                return
            old = self.light["name"]
            self.light["name"] = name
            save_settings()
            self.win.log(f"✏️ {old} → {name}  (OpenDeck commands that use the old name need updating)")
            self.win.rebuild_all()

    # ------------------------------------------------------------ main window
    class Win(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=APP_NAME)
            self.set_default_size(sc(780), sc(940))
            try:
                st = Gtk.Settings.get_default()
                if any(pr.name == "gtk-interface-color-scheme" for pr in st.list_properties()):
                    st.set_property("gtk-interface-color-scheme", Gtk.InterfaceColorScheme.DARK)
                else:
                    st.set_property("gtk-application-prefer-dark-theme", True)
                if ICON_FILE.exists():
                    self.set_icon_name(APP_SLUG)
            except Exception:
                pass
            self.busy = False
            self.polled = False
            self.cards = {}
            self._buttons = []
            self._poll_id = None

            root = Gtk.Box(orientation=V, spacing=sc(14))
            margins(root, sc(16))
            outer = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            outer.set_child(root)
            self.set_child(outer)

            hb = Gtk.Box(orientation=V, spacing=sc(4), halign=Gtk.Align.CENTER)
            if ICON_FILE.exists():
                logo = Gtk.Image.new_from_file(str(ICON_FILE))
                logo.set_pixel_size(sc(85))
                hb.append(logo)
            hb.append(label(APP_NAME, "app-title", xalign=0.5))
            hb.append(label(f"v{APP_VERSION} · {BUILD}", "app-ver", xalign=0.5))
            root.append(hb)

            top = Gtk.Box(orientation=H, spacing=sc(8))
            for text, fn, tip in (("💡  All on", lambda: self.run_job(lambda lg: self._power(lg, True)), ""),
                                  ("🌙  All off", lambda: self.run_job(lambda lg: self._power(lg, False)), ""),
                                  ("🔀  Toggle", lambda: self.run_job(lambda lg: self._power(lg, None)),
                                   "Anything on → all off, otherwise all on")):
                top.append(self._button(text, fn, tip))
            self.status = label("", "status", xalign=1.0, hexpand=True, valign=Gtk.Align.CENTER)
            top.append(self.status)
            top.append(self._button("⚙️", self.show_settings, "Settings"))
            root.append(top)

            # 1 · Lights
            g1 = Gtk.Box(orientation=V, spacing=sc(10))
            margins(g1, sc(10))
            self.light_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                          min_children_per_line=1, max_children_per_line=4,
                                          column_spacing=sc(12), row_spacing=sc(12))
            g1.append(self.light_flow)
            self.no_lights = label("No lights yet — Find lights searches your network, or add one by its IP.",
                                   "hint", wrap=True)
            g1.append(self.no_lights)
            row = Gtk.Box(orientation=H, spacing=sc(8), homogeneous=True)
            row.append(self._button("🔍  Find lights", lambda: self.run_job(self._scan_job),
                                    "Ask the network (mDNS) and scan it for lights on port 9123"))
            row.append(self._button("➕  Add by IP…", self.show_add_ip))
            row.append(self._button("🔄  Refresh", self.refresh_states, "Read every light's state again"))
            g1.append(row)
            self._frame(root, "1 · Lights", g1)

            # 2 · Presets
            g2 = Gtk.Box(orientation=V, spacing=sc(10))
            margins(g2, sc(10))
            g2.append(label("Click a preset to use it. A preset only touches the lights you chose for it.",
                            "hint", wrap=True))
            self.preset_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                           min_children_per_line=1, max_children_per_line=3,
                                           column_spacing=sc(8), row_spacing=sc(8))
            g2.append(self.preset_flow)
            g2.append(self._button("＋  New preset from the lights' current settings…",
                                   lambda: self.show_preset_dialog(None)))
            self._frame(root, "2 · Presets", g2)

            # 3 · Shortcuts for OpenDeck
            g3 = Gtk.Box(orientation=V, spacing=sc(10))
            margins(g3, sc(10))
            g3.append(label("In OpenDeck: drag Starterpack › Run Command onto a key and paste the command. "
                            "Then give the key its image from the images folder (Save key images first).",
                            "hint", wrap=True))
            self.sc_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.sc_list.add_css_class("sclist")
            g3.append(self.sc_list)
            row = Gtk.Box(orientation=H, spacing=sc(8), homogeneous=True)
            row.append(self._button("🖼  Save all key images", self.save_all_keys,
                                    "144×144 PNGs, one per shortcut, in the images folder"))
            row.append(self._button("📂  Open images folder",
                                    lambda: (key_dir().mkdir(parents=True, exist_ok=True), self._open(key_dir()))))
            g3.append(row)
            self._frame(root, "3 · Shortcuts for OpenDeck", g3)

            self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                         wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.log_view.add_css_class("logbox")
            lsw = Gtk.ScrolledWindow()
            lsw.set_min_content_height(sc(90))
            lsw.set_child(self.log_view)
            root.append(lsw)

            foot = Gtk.Label()
            foot.set_markup(f'Made by Idus · <a href="{REPO_URL}">GitHub</a>'
                            f' · <a href="{KOFI_URL}">☕ Tip me on Ko-fi</a>')
            foot.set_halign(Gtk.Align.CENTER)
            foot.add_css_class("foot")
            foot.connect("activate-link", lambda _l, uri: (self._open(uri), True)[1])
            root.append(foot)

            self._install_css()
            self.rebuild_all()
            self._set_poll()
            if not CFG["lights"] and not CFG.get("first_run_done"):
                GLib.timeout_add(500, self._first_run)

        # ---- plumbing
        def _frame(self, root, title, child):
            frame = Gtk.Frame(label=title)
            frame.add_css_class("group")
            frame.set_child(child)
            root.append(frame)

        def _button(self, text, cb, tip=""):
            b = Gtk.Button(label=text, hexpand=False)
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", lambda _w: cb())
            self._buttons.append(b)
            return b

        def _open(self, target):
            try:
                subprocess.Popen(["xdg-open", str(target)], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        def log(self, text):
            def append():
                b = self.log_view.get_buffer()
                b.insert(b.get_end_iter(), text + "\n")
                mark = b.get_mark("end") or b.create_mark("end", b.get_end_iter(), False)
                b.move_mark(mark, b.get_end_iter())
                self.log_view.scroll_mark_onscreen(mark)
                return False
            GLib.idle_add(append)
            return False

        def set_busy(self, busy):
            self.busy = busy
            for b in self._buttons:
                b.set_sensitive(not busy)

        def run_job(self, fn, after=None):
            if self.busy:
                self.log("· busy — wait a moment")
                return
            self.set_busy(True)

            def job():
                try:
                    fn(self.log)
                except Exception as e:  # noqa
                    self.log(f"❌ error: {e}")
                    traceback.print_exc()

                def done():
                    self.set_busy(False)
                    if after:
                        after()
                    return False
                GLib.idle_add(done)
            threading.Thread(target=job, daemon=True).start()

        def confirm(self, text, on_yes, yes="OK"):
            dlg = Gtk.AlertDialog()
            dlg.set_message(text)
            dlg.set_buttons(["Cancel", yes])
            dlg.set_default_button(1)
            dlg.set_cancel_button(0)

            def cb(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        on_yes()
                except Exception:
                    pass
            dlg.choose(self, None, cb)

        def _dialog(self, title, w=520):
            d = Gtk.Window(transient_for=self, modal=True, title=title)
            d.set_default_size(sc(w), -1)
            box = Gtk.Box(orientation=V, spacing=sc(10))
            margins(box, sc(16))
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
            sw.set_child(box)
            d.set_child(sw)
            box.append(label(title, "dlg-title"))
            return d, box

        def _buttons_row(self, box, buttons):
            hb = Gtk.Box(orientation=H, spacing=sc(8), halign=Gtk.Align.END, margin_top=sc(6))
            for text, cb, primary in buttons:
                b = Gtk.Button(label=text)
                if primary:
                    b.add_css_class("primary")
                b.connect("clicked", lambda _b, f=cb: f())
                hb.append(b)
            box.append(hb)

        def ask_text(self, title, field, value, on_ok):
            d, box = self._dialog(title, 420)
            e = Gtk.Entry(text=value, hexpand=True)
            row = Gtk.Box(orientation=H, spacing=sc(8))
            row.append(label(field, "field", valign=Gtk.Align.CENTER))
            row.append(e)
            box.append(row)

            def ok():
                d.close()
                on_ok(e.get_text())
            e.connect("activate", lambda _e: ok())
            self._buttons_row(box, [("Cancel", d.close, False), ("OK", ok, True)])
            d.present()

        # ---- first start + finding lights
        def _first_run(self):
            nets = ", ".join(map(str, _local_networks())) or "your network"
            dlg = Gtk.AlertDialog()
            dlg.set_message("Search your network for Elgato lights?")
            dlg.set_detail(f"{APP_NAME} asks the network (mDNS) and checks {nets} for lights on port "
                           f"{PORT}. Every light it finds is added, side by side — you can rename, "
                           "reorder or remove them afterwards.")
            dlg.set_buttons(["Not now", "Search"])
            dlg.set_default_button(1)
            dlg.set_cancel_button(0)

            def cb(d, res):
                CFG["first_run_done"] = True
                save_settings()
                try:
                    if d.choose_finish(res) == 1:
                        self.run_job(self._scan_job)
                        return
                except Exception:
                    pass
                self.log("· no search — use Find lights or Add by IP whenever you like")
            dlg.choose(self, None, cb)
            return False

        def _scan_job(self, log):
            found = discover(log)
            added = merge_found(found, log)
            if not found:
                log("⚪ no Elgato lights found — add one by IP, or add its network in Settings "
                    "(lights on another VLAN don't answer here)")
            else:
                log(f"✅ found {len(found)} light(s) · {added} new")
            GLib.idle_add(self.rebuild_all)

        def show_add_ip(self):
            def go(ip):
                ip = ip.strip()
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    self.log(f"❌ '{ip}' isn't an IP address")
                    return
                self.run_job(lambda lg: (add_by_ip(ip, lg), GLib.idle_add(self.rebuild_all)))
            self.ask_text("Add a light by IP", "IP address", "", go)

        # ---- lights
        def rebuild_all(self):
            self.rebuild_cards()
            self.rebuild_presets()
            self.rebuild_shortcuts()
            self.refresh_states()
            return False

        def rebuild_cards(self):
            while (c := self.light_flow.get_first_child()) is not None:
                self.light_flow.remove(c)
            old = self.cards
            self.cards = {}
            for lt in CFG["lights"]:
                card = LightCard(self, lt)
                if lt["id"] in old and old[lt["id"]].state is not None:
                    card.show_state(old[lt["id"]].state)
                self.cards[lt["id"]] = card
                self.light_flow.append(card.widget)
            self.no_lights.set_visible(not CFG["lights"])
            self._update_status()

        def refresh_states(self):
            cards = list(self.cards.values())

            def work():
                def read(card):
                    st = get_state(card.light["ip"])
                    if st is not None and "mini" in card.light["model"].lower():
                        st["battery"] = battery(card.light["ip"])
                    return st
                states = parallel(read, cards)

                def apply():
                    self.polled = True
                    for card, st in zip(cards, states):
                        card.show_state(st)
                    self._update_status()
                    return False
                GLib.idle_add(apply)
                lost = [c.light for c, st in zip(cards, states) if st is None]
                if lost and not getattr(self, "_relocated", False):
                    self._relocated = True  # once per session: DHCP may have moved a light
                    found = discover(lambda _t: None)
                    moved = [f for f in found if any(f["id"] == lt["id"] and f["ip"] != lt["ip"] for lt in lost)]
                    if moved:
                        merge_found(moved, self.log)
                        GLib.idle_add(self.rebuild_all)
            threading.Thread(target=work, daemon=True).start()
            return False

        def _update_status(self):
            n = len(CFG["lights"])
            online = sum(1 for c in self.cards.values() if c.state is not None)
            if not n:
                self.status.set_label("⚪ no lights yet")
            elif not self.polled:
                self.status.set_label(f"{n} light{'s' if n != 1 else ''}")
            else:
                dot = "🟢" if online == n else ("🟠" if online else "🔴")
                self.status.set_label(f"{dot} {n} light{'s' if n != 1 else ''} · {online} online")

        def _set_poll(self):
            if self._poll_id:
                GLib.source_remove(self._poll_id)
                self._poll_id = None
            secs = int(CFG.get("poll_seconds") or 0)
            if secs:
                self._poll_id = GLib.timeout_add_seconds(secs, lambda: (self.refresh_states(), True)[1])

        def _power(self, log, on):
            ok, total, verb = act_power(on)
            log(f"{'💡' if verb == 'on' else '🌙'} turned {verb}: {ok}/{total} light(s)")
            GLib.idle_add(self.refresh_states)

        # ---- presets
        def rebuild_presets(self):
            while (c := self.preset_flow.get_first_child()) is not None:
                self.preset_flow.remove(c)
            for p in CFG["presets"]:
                cell = Gtk.Box(orientation=H)
                cell.add_css_class("preset-cell")
                btn = Gtk.Button(hexpand=True)
                btn.add_css_class("preset")
                inner = Gtk.Box(orientation=H, spacing=sc(8))
                vals = list(p["lights"].values())
                r, g, b = (int(c * 255) for c in (value_rgb(vals[0]) if vals else (0.5, 0.5, 0.5)))
                sw = Gtk.Label(valign=Gtk.Align.CENTER)
                sw.set_markup(f'<span foreground="#{r:02x}{g:02x}{b:02x}" size="xx-large">●</span>')
                inner.append(sw)
                txt = Gtk.Box(orientation=V, spacing=sc(2))
                txt.append(label(p["name"], "preset-name", ellipsize=Pango.EllipsizeMode.END))
                txt.append(label(preset_summary(p), "preset-val", ellipsize=Pango.EllipsizeMode.END))
                inner.append(txt)
                btn.set_child(inner)
                btn.set_tooltip_text(preset_summary(p))
                btn.connect("clicked", lambda _b, pp=p: self.run_job(lambda lg: self._apply(lg, pp)))
                cell.append(btn)
                mb, _mi = menu_button([("edit", "✏️  Edit / save current settings…"),
                                       ("copy", "📋  Copy OpenDeck command"),
                                       ("image", "🖼  Save key image"), ("delete", "🗑  Delete…")],
                                      lambda k, pp=p: self._preset_menu(k, pp))
                mb.add_css_class("preset-menu")
                cell.append(mb)
                self.preset_flow.append(cell)

        def _apply(self, log, p):
            ok, total = apply_preset(p)
            log(f"▶ {p['name']}: {ok}/{total} light(s)")
            GLib.idle_add(self.refresh_states)

        def _preset_menu(self, key, p):
            sc_ = next((s for s in shortcuts() if s["key"] == f"preset-{p['name']}"), None)
            if key == "edit":
                self.show_preset_dialog(p)
            elif key == "copy" and sc_:
                self.copy(command_for(sc_))
            elif key == "image" and sc_:
                self.save_key(sc_)
            elif key == "delete":
                self.confirm(f"Delete the preset {p['name']}?\n\nOpenDeck keys that use it stop working.",
                             lambda: (CFG["presets"].remove(p), save_settings(), self.log(f"🗑 deleted {p['name']}"),
                                      self.rebuild_presets(), self.rebuild_shortcuts()), "Delete")

        def show_preset_dialog(self, p):
            d, box = self._dialog("Edit preset" if p else "New preset", 520)
            box.append(label("Saves what the chosen lights show right now. Lights left out are not "
                             "touched when the preset is used.", "hint", wrap=True))
            name_e = Gtk.Entry(text=p["name"] if p else "", hexpand=True, placeholder_text="e.g. Meeting")
            row = Gtk.Box(orientation=H, spacing=sc(8))
            row.append(label("Name", "field", valign=Gtk.Align.CENTER))
            row.append(name_e)
            box.append(row)
            box.append(label("Lights in this preset", "field"))
            checks = {}
            for lt in CFG["lights"]:
                card = self.cards.get(lt["id"])
                v = card.current_values() if card else None
                if v is None:
                    desc = "unreachable — can't read its settings"
                elif not v["on"]:
                    desc = "off"
                elif "hue" in v:
                    desc = f"{v['brightness']}% · hue {v['hue']}° · sat {v['saturation']}%"
                else:
                    desc = f"{v['brightness']}% · {v['kelvin']} K"
                cb = Gtk.CheckButton(label=f"{lt['name']}  —  {desc}", sensitive=v is not None,
                                     active=v is not None and (p is None or lt["id"] in p["lights"]))
                checks[lt["id"]] = (cb, v)
                box.append(cb)
            msg = label("", "warn", wrap=True)
            box.append(msg)

            def save():
                name = name_e.get_text().strip()
                chosen = {lid: v for lid, (cb, v) in checks.items() if cb.get_active() and v is not None}
                if not name:
                    msg.set_label("Give the preset a name.")
                    return
                if any(x is not p and x["name"].lower() == name.lower() for x in CFG["presets"]):
                    msg.set_label("There is already a preset with that name.")
                    return
                if not chosen:
                    msg.set_label("Pick at least one light.")
                    return
                if p:
                    if p["name"] != name:
                        self.log(f"✏️ {p['name']} → {name}  (OpenDeck commands that use the old name need updating)")
                    p.update(name=name, lights=chosen)
                else:
                    CFG["presets"].append({"name": name, "lights": chosen})
                save_settings()
                self.log(f"💾 saved preset {name}: {preset_summary({'lights': chosen})}")
                d.close()
                self.rebuild_presets()
                self.rebuild_shortcuts()
            name_e.connect("activate", lambda _e: save())
            self._buttons_row(box, [("Cancel", d.close, False), ("Save", save, True)])
            d.present()

        # ---- shortcuts + key images
        def rebuild_shortcuts(self):
            while (c := self.sc_list.get_first_child()) is not None:
                self.sc_list.remove(c)
            for s in shortcuts():
                rr = Gtk.ListBoxRow(activatable=False, selectable=False)
                rr.add_css_class("scrow")
                hb = Gtk.Box(orientation=H, spacing=sc(8))
                margins(hb, sc(4), sc(8))
                name = label(s["label"], "sc-name", valign=Gtk.Align.CENTER, width_chars=16,
                             ellipsize=Pango.EllipsizeMode.END)
                hb.append(name)
                cmd = label(command_for(s), "sc-cmd", valign=Gtk.Align.CENTER, hexpand=True, selectable=True,
                            ellipsize=Pango.EllipsizeMode.START)
                cmd.set_tooltip_text(command_for(s))
                hb.append(cmd)
                for text, fn, tip in (("📋 Copy", lambda ss=s: self.copy(command_for(ss)), "Copy the command"),
                                      ("🖼", lambda ss=s: self.save_key(ss), "Save this key's image")):
                    b = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
                    b.add_css_class("small")
                    b.set_tooltip_text(tip)
                    b.connect("clicked", lambda _b, f=fn: f())
                    hb.append(b)
                rr.set_child(hb)
                self.sc_list.append(rr)

        def copy(self, text):
            try:
                self.get_clipboard().set(text)
                self.log(f"📋 copied: {text}")
            except Exception as e:  # noqa
                self.log(f"❌ couldn't copy ({e}) — select the command and press Ctrl+C")

        def save_key(self, s, quiet=False):
            path = key_dir() / key_file_name(s)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.render_key(s, path)
                if not quiet:
                    self.log(f"🖼 saved {path}")
                return True
            except Exception as e:  # noqa
                self.log(f"❌ key image for {s['label']}: {e}")
                return False

        def save_all_keys(self):
            n = sum(self.save_key(s, quiet=True) for s in shortcuts())
            self.log(f"🖼 saved {n} key images in {key_dir()}")

        def render_key(self, s, path):
            """144×144 Stream Deck key: a glowing light in the shortcut's colour + its name."""
            size = 144.0
            snap = Gtk.Snapshot()
            full = Graphene.Rect().init(0, 0, size, size)
            snap.append_color(rgba((0.086, 0.094, 0.114)), full)
            off = s["kind"] == "off"
            col = (0.36, 0.39, 0.45) if off else s["rgb"]
            d, cx, cy = 58.0, size / 2, 54.0
            # draw the disc at the origin of a moved coordinate system (PyGObject drops a
            # RoundedRect's own offset), then move back
            snap.save()
            snap.translate(Graphene.Point().init(cx - d / 2, cy - d / 2))
            disc = Gsk.RoundedRect().init_from_rect(Graphene.Rect().init(0, 0, d, d), d / 2)
            if not off:
                snap.append_outset_shadow(disc, rgba(col, 0.55), 0, 0, 6, 22)
            snap.push_rounded_clip(disc)
            snap.append_color(rgba(col), Graphene.Rect().init(0, 0, d, d))
            snap.pop()
            snap.restore()
            glyph = {"on": "ON", "off": "OFF", "toggle": "I/O", "up": "+", "down": "−",
                     "warm": "K−", "cool": "K+"}.get(s["kind"], "")
            if glyph:
                lay = self.create_pango_layout(glyph)
                fd = Pango.FontDescription.from_string("Sans Bold")
                fd.set_absolute_size((26 if len(glyph) <= 2 else 18) * Pango.SCALE)
                lay.set_font_description(fd)
                w, h = lay.get_pixel_size()
                snap.save()
                snap.translate(Graphene.Point().init(cx - w / 2, cy - h / 2))
                snap.append_layout(lay, rgba((0.086, 0.094, 0.114)))
                snap.restore()
            text = s["label"]
            lay = self.create_pango_layout(text)
            fd = Pango.FontDescription.from_string("Sans Bold")
            fd.set_absolute_size(15 * Pango.SCALE)
            lay.set_font_description(fd)
            lay.set_width(int(132 * Pango.SCALE))
            lay.set_alignment(Pango.Alignment.CENTER)
            lay.set_wrap(Pango.WrapMode.WORD_CHAR)
            lay.set_height(-2)
            lay.set_ellipsize(Pango.EllipsizeMode.END)
            _w, h = lay.get_pixel_size()
            snap.save()
            snap.translate(Graphene.Point().init(6, size - h - 8))
            snap.append_layout(lay, rgba((0.91, 0.92, 0.93)))
            snap.restore()
            node = snap.to_node()
            tex = self.get_renderer().render_texture(node, full)
            tex.save_to_png(str(path))

        # ---- settings
        def show_settings(self):
            d, box = self._dialog("Settings", 560)
            g = Gtk.Grid(column_spacing=sc(10), row_spacing=sc(8))
            box.append(g)
            nets = Gtk.Entry(text=CFG.get("extra_networks") or "", hexpand=True,
                             placeholder_text="e.g. 10.0.20.0/24")
            g.attach(label("Also search", "field", valign=Gtk.Align.CENTER), 0, 0, 1, 1)
            g.attach(nets, 1, 0, 2, 1)
            auto = ", ".join(str(n) for n in _local_networks() if str(n) not in (CFG.get("extra_networks") or ""))
            g.attach(label(f"Networks to scan besides this computer's own ({auto or '—'}). Use it when the lights "
                           "sit on another VLAN / Wi-Fi network.", "hint", wrap=True), 1, 1, 2, 1)
            poll = Gtk.DropDown.new_from_strings(POLL_LABELS)
            cur = int(CFG.get("poll_seconds") or 0)
            poll.set_selected(POLL_CHOICES.index(cur) if cur in POLL_CHOICES else 2)
            g.attach(label("Refresh lights", "field", valign=Gtk.Align.CENTER), 0, 2, 1, 1)
            g.attach(poll, 1, 2, 1, 1)
            g.attach(label("Picks up changes made by OpenDeck keys or another computer.", "hint", wrap=True),
                     1, 3, 2, 1)
            bs = Gtk.SpinButton.new_with_range(1, 50, 1)
            bs.set_value(int(CFG.get("bri_step") or 10))
            ks = Gtk.SpinButton.new_with_range(50, 2000, 50)
            ks.set_value(int(CFG.get("kelvin_step") or 300))
            g.attach(label("Brightness step", "field", valign=Gtk.Align.CENTER), 0, 4, 1, 1)
            g.attach(bs, 1, 4, 1, 1)
            g.attach(label("%", "hint", valign=Gtk.Align.CENTER), 2, 4, 1, 1)
            g.attach(label("Temperature step", "field", valign=Gtk.Align.CENTER), 0, 5, 1, 1)
            g.attach(ks, 1, 5, 1, 1)
            g.attach(label("K — used by the Brighter / Dimmer / Warmer / Cooler shortcuts", "hint", wrap=True),
                     2, 5, 1, 1)
            kd = Gtk.Entry(text=CFG.get("key_dir") or "", hexpand=True, placeholder_text=str(key_dir()))
            kb = Gtk.Button(label="Choose…")

            def choose():
                fd = Gtk.FileDialog(title="Folder for key images")

                def cb(dlg, res):
                    try:
                        kd.set_text(dlg.select_folder_finish(res).get_path())
                    except Exception:
                        pass
                fd.select_folder(d, None, cb)
            kb.connect("clicked", lambda _b: choose())
            g.attach(label("Key images", "field", valign=Gtk.Align.CENTER), 0, 6, 1, 1)
            g.attach(kd, 1, 6, 1, 1)
            g.attach(kb, 2, 6, 1, 1)

            def save():
                CFG["extra_networks"] = nets.get_text().strip()
                CFG["poll_seconds"] = POLL_CHOICES[poll.get_selected()]
                CFG["bri_step"] = int(bs.get_value())
                CFG["kelvin_step"] = int(ks.get_value())
                CFG["key_dir"] = kd.get_text().strip()
                save_settings()
                self._set_poll()
                self.rebuild_shortcuts()
                self.log("✅ settings saved")
                d.close()
            self._buttons_row(box, [("Cancel", d.close, False), ("Save", save, True)])
            d.present()

        def _install_css(self):
            f = int(round(13 * S))
            css = f"""
            window {{ background-color: #16181d; color: #e8eaed; font-size: {f}px; }}
            .app-title {{ font-size: {int(28*S)}px; font-weight: 800; letter-spacing: 0.5px; color: #ffd9a0; }}
            .app-ver {{ font-size: {int(11*S)}px; color: #8a93a3; margin-top: -{sc(2)}px; }}
            .foot {{ font-size: {int(11*S)}px; color: #8a93a3; margin-top: {sc(4)}px; }}
            .foot a, .foot link {{ color: #ff9a3c; text-decoration: none; }}
            .status {{ color: #9fb4d4; }}
            .hint {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            .warn {{ font-size: {int(11*S)}px; color: #ffb13c; }}
            .field {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            frame.group > label {{ font-size: {int(13*S)}px; font-weight: 700; color: #8a93a3;
                                   margin-left: {sc(4)}px; }}
            frame.group {{ background-color: #1e222b; border-radius: {sc(12)}px; border: 1px solid #2a2f3a; }}
            button {{ background-color: #262b36; background-image: none; color: #e8eaed; border-radius: {sc(8)}px;
                      border: 1px solid #333a47; padding: {sc(9)}px {sc(12)}px; box-shadow: none; }}
            button:hover {{ background-color: #2f3644; }}
            button:disabled {{ color: #5a6270; background-color: #20242d; }}
            button.primary {{ background-color: #ff9a3c; color: #16181d; font-weight: 700; border-color: #ff9a3c; }}
            button.primary:hover {{ background-color: #ffb066; }}
            button.small {{ padding: {sc(4)}px {sc(10)}px; }}
            button.menuitem {{ background: transparent; border: none; padding: {sc(6)}px {sc(12)}px; }}
            button.menuitem:hover {{ background-color: #2f3644; }}
            menubutton.rowmenu > button {{ padding: {sc(3)}px {sc(12)}px; font-weight: 800; }}
            dropdown > button {{ padding: {sc(4)}px {sc(8)}px; }}
            spinbutton, entry {{ background-color: #0f1115; color: #e8eaed; border: 1px solid #333a47;
                                 border-radius: {sc(8)}px; }}
            entry {{ padding: {sc(4)}px {sc(8)}px; }}
            spinbutton > button, spinbutton > button:disabled {{ padding: {sc(2)}px {sc(7)}px; border: none;
                                 background-color: transparent; }}
            popover > contents {{ background-color: #1e222b; color: #e8eaed; border: 1px solid #2a2f3a;
                                  border-radius: {sc(10)}px; }}
            popover > arrow {{ background-color: #1e222b; border-color: #2a2f3a; }}
            popover listview, popover listview > row {{ background-color: transparent; color: #e8eaed; }}
            scrollbar {{ background-color: transparent; border: none; }}
            scrollbar slider {{ margin: 0; min-width: {sc(6)}px; min-height: {sc(6)}px; border: none;
                                border-radius: {sc(4)}px; background-color: #3a4150; }}
            flowbox, flowboxchild {{ background-color: transparent; padding: 0; }}
            .card {{ background-color: #232833; border-radius: {sc(12)}px; padding: {sc(14)}px;
                     border: 1px solid #2a2f3a; }}
            .card-title {{ font-size: {int(16*S)}px; font-weight: 700; color: #e8eaed; }}
            .card-sub {{ font-size: {int(11*S)}px; color: #8a93a3; }}
            .val-label {{ font-size: {int(12*S)}px; color: #9fb4d4; font-family: monospace; }}
            .dot-ok {{ color: #46d160; }}
            .dot-off {{ color: #e0525b; }}
            .dot-unknown {{ color: #6b7280; }}
            scale highlight {{ background-color: #ff9a3c; background-image: none; }}
            scale trough {{ background-color: #0f1115; background-image: none; }}
            scale slider {{ background-color: #e8eaed; background-image: none; }}
            .grad {{ border-radius: {sc(3)}px; border: 1px solid #2a2f3a; }}
            .grad-brightness {{ background-image: linear-gradient(to right, #14161b 0%, #2a2f3a 25%, #8a8f99 60%,
                                #f7f4ec 100%); }}
            .grad-temp {{ background-image: linear-gradient(to right, #ff9a3c 0%, #ffd9a0 25%, #ffffff 50%,
                          #bcd8ff 75%, #6fa8ff 100%); }}
            .grad-hue {{ background-image: linear-gradient(to right, #ff0000 0%, #ffff00 17%, #00ff00 33%,
                         #00ffff 50%, #0000ff 67%, #ff00ff 83%, #ff0000 100%); }}
            .grad-sat {{ background-image: linear-gradient(to right, #ffffff 0%, #ff9a3c 100%); }}
            .preset {{ border-top-right-radius: 0; border-bottom-right-radius: 0; border-right: none;
                       padding: {sc(8)}px {sc(10)}px; }}
            menubutton.preset-menu > button {{ border-top-left-radius: 0; border-bottom-left-radius: 0;
                                               padding: {sc(8)}px {sc(10)}px; }}
            .swatch {{ border-radius: {sc(3)}px; }}
            .preset-name {{ font-size: {int(13*S)}px; font-weight: 700; color: #e8eaed; }}
            .preset-val {{ font-size: {int(10*S)}px; color: #9fb4d4; font-family: monospace; }}
            list.sclist {{ background-color: transparent; }}
            list.sclist > row.scrow {{ background-color: #232833; border-radius: {sc(8)}px; margin: {sc(2)}px 0;
                                       border: 1px solid #2a2f3a; }}
            .sc-name {{ font-weight: 700; color: #e8eaed; }}
            .sc-cmd {{ font-family: monospace; font-size: {int(11*S)}px; color: #9fb4d4; }}
            .logbox, .logbox text {{ background-color: #0f1115; color: #c7d1df; }}
            .logbox {{ padding: {sc(8)}px; border-radius: {sc(8)}px; }}
            checkbutton {{ color: #9fb4d4; font-size: {int(12*S)}px; }}
            .dlg-title {{ font-size: {int(18*S)}px; font-weight: 800; color: #ffd9a0; }}
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    App().run([sys.argv[0]])


if __name__ == "__main__":
    try:
        rc = cli()
        if rc is None:
            gui()
        else:
            sys.exit(rc)
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:
        os._exit(130)
