"""Bulb list (bulbs.json), scenes (scenes.json) and the controller shared by the CLI and the
desktop window. Every public method of `Lights` returns a plain dict the callers print or show.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

HOME = Path(os.environ.get("GOVEE_HOME") or Path(__file__).resolve().parent)
BULBS_FILE = HOME / "bulbs.json"
SCENES_FILE = HOME / "scenes.json"
EFFECTS_FILE = Path(__file__).resolve().parent / "effects.json"  # scenes shipped with the program
LOCAL_EFFECTS_FILE = HOME / "effects_local.json"  # your own (e.g. captured from the phone app); not published

DEFAULT_SETTINGS = {
    "groups": ["down", "up"],
    "kelvin_min": 2700,  # H600D white range
    "kelvin_max": 6500,
}

DEFAULT_SCENES = {
    "bright": {"all": {"on": True, "brightness": 100, "kelvin": 4000}},
    "relax": {"all": {"on": True, "brightness": 45, "kelvin": 2700}},
    "night": {"down": {"on": True, "brightness": 8, "kelvin": 2700}, "up": {"on": False}},
    "movie": {"down": {"on": True, "brightness": 12, "color": "#ff5a1e"}, "up": {"on": False}},
    "all off": {"all": {"on": False}},
}

COLORS = {
    "red": "#ff0000", "orange": "#ff7f00", "amber": "#ffbf00", "yellow": "#ffff00",
    "lime": "#bfff00", "green": "#00ff00", "mint": "#00ff7f", "cyan": "#00ffff",
    "sky": "#0080ff", "blue": "#0000ff", "purple": "#8000ff", "violet": "#bf00ff",
    "magenta": "#ff00ff", "pink": "#ff1493", "white": "#ffffff",
    # magyarul
    "piros": "#ff0000", "narancs": "#ff7f00", "sarga": "#ffff00", "zold": "#00ff00",
    "turkiz": "#00ffff", "kek": "#0000ff", "lila": "#8000ff", "rozsaszin": "#ff1493", "feher": "#ffffff",
}
KELVIN_NAMES = {"warm": 2700, "soft": 3000, "neutral": 4000, "cool": 5000, "daylight": 6500,
                "meleg": 2700, "semleges": 4000, "hideg": 6500}
ALL_WORDS = {"all", "*", "mind", "minden", "osszes"}


class LightsError(Exception):
    """A problem the user can fix (bad name, unknown bulb, invalid JSON, ...)."""


# ---- small helpers -----------------------------------------------------------
def norm(s) -> str:
    """Case-, accent- and whitespace-insensitive key: 'Hálószoba  1' -> 'haloszoba 1'."""
    s = unicodedata.normalize("NFKD", str(s)).casefold()
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def parse_color(value) -> tuple[int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(v))) for v in value)
    s = norm(value)
    s = COLORS.get(s, s)
    if m := re.fullmatch(r"#?([0-9a-f]{6})", s):
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    if m := re.fullmatch(r"#([0-9a-f]{3})", s):
        return tuple(int(c * 2, 16) for c in m.group(1))
    parts = [p for p in re.split(r"[,;\s]+", s) if p]
    if len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return tuple(int(p) for p in parts)
    names = ", ".join(k for k in COLORS if k.isascii())
    raise LightsError(f"unknown color '{value}' - use #rrggbb, r,g,b or a name ({names})")


def parse_kelvin(value, lo: int, hi: int) -> int:
    s = norm(value)
    if s in KELVIN_NAMES:
        k = KELVIN_NAMES[s]
    elif re.fullmatch(r"\d{4,5}\s*k?", s):
        k = int(s.rstrip("k "))
    else:
        raise LightsError(f"unknown white temperature '{value}' - use {lo}..{hi} (Kelvin) or warm/neutral/cool")
    return max(lo, min(hi, k))


def parse_brightness(value) -> int:
    s = str(value).strip().rstrip("%").strip()
    if not s.lstrip("-").isdigit():
        raise LightsError(f"brightness must be a number 1..100, not '{value}'")
    return max(1, min(100, int(s)))


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = norm(value)
    if s in ("1", "true", "on", "yes", "be"):
        return True
    if s in ("0", "false", "off", "no", "ki"):
        return False
    raise LightsError(f"'on' must be true/false, not '{value}'")


def describe(st: dict) -> str:
    """Short text for a bulb state."""
    if not st.get("on"):
        return "off"
    if st.get("mode") == "white":
        what = f"white {st['kelvin']}K"
    elif st.get("mode") == "color":
        what = "color " + to_hex(st["rgb"])
    else:
        what = f"effect {st['effect']}" if st.get("effect") else "app scene"
    return f"on {st.get('brightness')}% {what}"


def _matches(st: dict, w: dict) -> bool:
    """Does a bulb's reported state show the wanted state?"""
    if "on" in w and bool(st.get("on")) != w["on"]:
        return False
    if w.get("on") is False:
        return True
    if "brightness" in w and abs(int(st.get("brightness") or 0) - w["brightness"]) > 1:
        return False
    if "kelvin" in w and (st.get("mode") != "white" or abs(int(st.get("kelvin") or 0) - w["kelvin"]) > 60):
        return False
    if "rgb" in w and (st.get("mode") != "color" or any(abs(int(a) - b) > 3 for a, b in zip(st["rgb"], w["rgb"]))):
        return False
    if "effect" in w and (st.get("mode") != "scene" or (
            st.get("effect_code") is not None and st["effect_code"] != w["effect"]["code"])):
        return False  # in a scene, but not the one we asked for
    return True


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for key, others in (("rgb", ("kelvin", "effect")), ("kelvin", ("rgb", "effect")), ("effect", ("rgb", "kelvin"))):
        if key in b:
            for o in others:
                out.pop(o, None)
    out.update(b)
    return out


def load_effects() -> dict:
    """{normalized name: {name, category, code, param, frames}} from effects.json.
    An entry is one of: {"code"} (start a scene the bulb already has), {"frames": [hex, ...]} (scene
    data captured from the phone app) or {"colors": [...], "brightness", "tempo", "pace"} (our own
    smooth colour cycle). The last two are uploaded and then started as code 8217."""
    import base64
    from govee_ble import PHONE_SCENE, gradient_data, scene_frames
    cats: dict[str, list] = {}
    for path in (EFFECTS_FILE, LOCAL_EFFECTS_FILE):
        for cat, items in ((_read_json(path, {}) or {}).get("categories") or {}).items():
            cats.setdefault(cat, []).extend(items)
    out = {}
    for cat, items in cats.items():
        for e in items:
            frames = []
            if e.get("frames"):
                frames = [bytes.fromhex(f) for f in e["frames"]]
            elif e.get("colors"):
                frames = scene_frames(gradient_data(
                    [parse_color(c) for c in e["colors"]], e.get("brightness", 100),
                    e.get("tempo", 255), e.get("pace", 0xEB)))
            out[norm(e["name"])] = {"name": e["name"], "category": cat,
                                    "code": PHONE_SCENE if frames else int(e["code"]),
                                    "param": base64.b64decode(e.get("param") or ""), "frames": frames,
                                    "bulb_brightness": e.get("bulb_brightness")}
    return out


def effect_for_code(code) -> str | None:
    """Name for a scene code the bulb reports. Uploaded scenes all report 8217, so for that code it is
    the entry without data ("Last phone scene"), unless we know what we uploaded."""
    hits = sorted((e for e in load_effects().values() if e["code"] == code), key=lambda e: bool(e["frames"]))
    return hits[0]["name"] if hits else None


def find_effect(name) -> dict:
    effects = load_effects()
    key = norm(name)
    hits = [e for k, e in effects.items() if k == key] or [e for k, e in effects.items() if k.startswith(key)]
    if len(hits) != 1:
        raise LightsError(f"no effect called '{name}'" if not hits else
                          f"'{name}' matches several effects: {', '.join(e['name'] for e in hits)}")
    return hits[0]


def _read_json(path: Path, default):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LightsError(f"{path.name} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}") from None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # file briefly locked (editor, antivirus)
            if attempt == 19:
                raise
            time.sleep(0.05)


def load_settings() -> dict:
    data = _read_json(BULBS_FILE, {})
    return {**DEFAULT_SETTINGS, **((data.get("settings") if isinstance(data, dict) else None) or {})}


# ---- the controller -----------------------------------------------------------
class Lights:
    def __init__(self, transport=None, live: bool = False):
        """live=True (desktop window): keep every bulb connected and answer from the kept-alive state.
        live=False (CLI): connect on demand and disconnect in close()."""
        self._lock = threading.RLock()
        self.live = live
        if transport is None:
            from govee_ble import BleTransport  # imported here so tests can run with a fake transport
            transport = BleTransport(keepalive=live)
        self.ble = transport
        cfg = self._cfg()
        with self._lock:
            if not SCENES_FILE.exists():
                _write_json(SCENES_FILE, DEFAULT_SCENES)
        self._watch(cfg)

    def close(self) -> None:
        self.ble.close()

    # ---- files ----
    def _load(self) -> dict:
        data = _read_json(BULBS_FILE, {})
        if not isinstance(data, dict):
            raise LightsError(f"{BULBS_FILE.name} must contain a JSON object")
        settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
        bulbs = []
        for b in data.get("bulbs") or []:
            if isinstance(b, dict) and b.get("address"):
                a = str(b["address"]).upper()
                bulbs.append({"name": str(b.get("name") or a), "group": str(b.get("group") or ""),
                              "address": a, "sku": str(b.get("sku") or "")})
        return {"settings": settings, "bulbs": bulbs}

    def _cfg(self) -> dict:
        with self._lock:
            return self._load()

    def _save(self, cfg: dict) -> None:
        _write_json(BULBS_FILE, {"settings": cfg["settings"], "bulbs": cfg["bulbs"]})
        self._watch(cfg)

    def _watch(self, cfg: dict) -> None:
        if self.live:
            self.ble.keep_connected([b["address"] for b in cfg["bulbs"]])

    def _scenes(self) -> dict:
        data = _read_json(SCENES_FILE, None)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise LightsError(f"{SCENES_FILE.name} must be an object: {{\"scene name\": {{...}}}}")
        return data

    # ---- lookup ----
    @staticmethod
    def _groups(cfg: dict) -> list[str]:
        groups = [str(g) for g in cfg["settings"].get("groups") or []]
        for b in cfg["bulbs"]:
            if b["group"] and all(norm(g) != norm(b["group"]) for g in groups):
                groups.append(b["group"])
        return groups

    def _resolve(self, cfg: dict, target) -> list[dict]:
        """'all', a group, a bulb name or address (or its last 4 hex digits), or several, comma-separated."""
        bulbs = cfg["bulbs"]
        if not bulbs:
            raise LightsError("no bulbs known yet - run discovery first (govee discover)")
        out: list[dict] = []
        for part in str("all" if target in (None, "") else target).split(","):
            key = norm(part)
            if not key:
                continue
            if key in ALL_WORDS:
                hits = bulbs
            else:
                bare = key.replace(":", "").upper()
                hits = ([b for b in bulbs if norm(b["group"]) == key]
                        or [b for b in bulbs if norm(b["name"]) == key]
                        or [b for b in bulbs if b["address"] == key.upper()]
                        or [b for b in bulbs if len(bare) >= 4 and b["address"].replace(":", "").endswith(bare)]
                        or [b for b in bulbs if norm(b["name"]).startswith(key)])
            if not hits:
                raise LightsError(f"no bulb or group called '{part.strip()}' (groups: "
                                  f"{', '.join(self._groups(cfg)) or '-'}; bulbs: {', '.join(b['name'] for b in bulbs)})")
            out += [b for b in hits if b not in out]
        if not out:
            raise LightsError("no bulb selected")
        return out

    def _check_name(self, cfg: dict, name: str, me: dict | None) -> None:
        if not name:
            raise LightsError("the name is empty")
        if "," in name:
            raise LightsError("names cannot contain commas")
        if norm(name) in ALL_WORDS or any(norm(g) == norm(name) for g in self._groups(cfg)):
            raise LightsError(f"'{name}' is already used as a group name")
        if any(b is not me and norm(b["name"]) == norm(name) for b in cfg["bulbs"]):
            raise LightsError(f"another bulb is already called '{name}'")

    def _want(self, cfg: dict, e: dict) -> dict:
        """{on, brightness, color, kelvin} (None = leave unchanged) -> internal 'want' dict."""
        s = cfg["settings"]
        w: dict = {}
        if e.get("on") is not None:
            w["on"] = parse_bool(e["on"])
        if e.get("brightness") not in (None, ""):
            w["brightness"] = parse_brightness(e["brightness"])
        has_color, has_kelvin = e.get("color") not in (None, ""), e.get("kelvin") not in (None, "")
        has_effect = e.get("effect") not in (None, "")
        if has_color + has_kelvin + has_effect > 1:
            raise LightsError("give only one of: a color, a white temperature, an effect")
        if has_effect:
            w["effect"] = find_effect(e["effect"])
            if w["effect"].get("bulb_brightness") and "brightness" not in w:  # e.g. a night scene starts at 1%
                w["brightness"] = parse_brightness(w["effect"]["bulb_brightness"])
        if has_color:
            w["rgb"] = parse_color(e["color"])
        if has_kelvin:
            w["kelvin"] = parse_kelvin(e["kelvin"], int(s["kelvin_min"]), int(s["kelvin_max"]))
        if "on" not in w and any(k in w for k in ("brightness", "rgb", "kelvin", "effect")):
            w["on"] = True  # changing the light implies switching it on
        return w

    def _view(self, b: dict, st: dict | None) -> dict:
        info = self.ble.info(b["address"])
        v = {"id": b["address"], "name": b["name"], "group": b["group"], "address": b["address"],
             "sku": b["sku"], "online": st is not None, "rssi": info.get("rssi"),
             "offline": st is None and bool(info.get("offline")),  # several tries failed: off at the wall?
             "connecting": st is None and bool(info.get("connecting")) and not info.get("offline"),
             "error": "" if st is not None else info.get("error", "")}
        if st is not None:
            v.update(on=bool(st["on"]), brightness=int(st["brightness"]), mode=st["mode"],
                     kelvin=st["kelvin"] or None, color=to_hex(st["rgb"]) if st.get("rgb") else None,
                     effect=st.get("effect"))
            if st.get("effect_code") is not None:  # the bulb told us which scene it is playing
                effects = load_effects()
                known = effects.get(norm(v["effect"] or ""))
                if not known or known["code"] != st["effect_code"]:  # every uploaded scene reports 8217
                    v["effect"] = effect_for_code(st["effect_code"]) or v["effect"]
        return v

    def _read(self, bulbs: list[dict], fresh: bool = False) -> dict:
        addrs = [b["address"] for b in bulbs]
        if self.live:  # the window keeps links alive in the background: never block on connecting here
            return self.ble.read(addrs, max_age=0 if fresh else 6.0, connect=False)
        return self.ble.read(addrs)

    def _apply(self, pairs: list[tuple[dict, dict]], verify: bool) -> dict:
        plan = {b["address"]: w for b, w in pairs}
        # the window never waits for a missing bulb here; its background loop keeps reconnecting it
        errors = self.ble.write(plan, connect=not self.live)
        if not verify:
            return {"bulbs": [], "warnings": []}
        bulbs = [b for b, _ in pairs]
        st = self.ble.read(list(plan), connect=False)
        again = {a: w for a, w in plan.items() if st.get(a) is not None and not _matches(st[a], w)}
        if again:  # resend once to bulbs that report something else
            errors.update(self.ble.write(again, connect=False))
            st.update(self.ble.read(list(again), connect=False))
        warnings = []
        for b, w in pairs:
            s = st.get(b["address"])
            if s is None:
                warnings.append(f"{b['name']}: {errors.get(b['address']) or 'not reachable'}")
            elif not _matches(s, w):
                warnings.append(f"{b['name']}: reports {describe(s)}")
        return {"bulbs": [self._view(b, st.get(b["address"])) for b in bulbs], "warnings": warnings}

    # ---- public API (CLI + desktop window) ------------------------------------
    def ping(self) -> dict:
        return {"ok": True}

    def state(self, target: str = "all") -> dict:
        cfg = self._cfg()
        bulbs = self._resolve(cfg, target) if cfg["bulbs"] and target not in (None, "", "all") else cfg["bulbs"]
        st = self._read(bulbs) if bulbs else {}
        s = cfg["settings"]
        return {"bulbs": [self._view(b, st.get(b["address"])) for b in bulbs], "groups": self._groups(cfg),
                "scenes": list(self._scenes()), "kelvin": [int(s["kelvin_min"]), int(s["kelvin_max"])],
                "live": self.live, "demo": bool(os.environ.get("GOVEE_DEMO"))}

    def discover(self, seconds: float = 8.0) -> dict:
        found = {d["address"]: d for d in self.ble.scan(float(seconds))}
        new = []
        with self._lock:
            cfg = self._load()
            known = {b["address"] for b in cfg["bulbs"]}
            for a, d in found.items():
                if a not in known:
                    name, n = f"{d['sku']} {d['name'][-4:].upper()}", 2
                    while any(norm(x["name"]) == norm(name) for x in cfg["bulbs"]):
                        name, n = f"{d['sku']} {d['name'][-4:].upper()}-{n}", n + 1
                    cfg["bulbs"].append({"name": name, "group": "", "address": a, "sku": d["sku"]})
                    new.append(name)
            if new or not BULBS_FILE.exists():
                self._save(cfg)
        # connected bulbs stop advertising, so they count as found too
        here = set(found) | {b["address"] for b in cfg["bulbs"] if self.ble.info(b["address"])["connected"]}
        missing = [b["name"] for b in cfg["bulbs"] if b["address"] not in here]
        return {"found": len(here), "new": new, "missing": missing, **self.state()}

    def effects(self) -> dict:
        """The library scenes (dynamic effects) the bulbs can play, by category."""
        cats: dict[str, list[str]] = {}
        for e in load_effects().values():
            cats.setdefault(e["category"], []).append(e["name"])
        return {"categories": cats}

    def control(self, target: str = "all", on=None, brightness=None, color=None, kelvin=None,
                effect=None, verify: bool = True) -> dict:
        cfg = self._cfg()
        bulbs = self._resolve(cfg, target)
        w = self._want(cfg, {"on": on, "brightness": brightness, "color": color, "kelvin": kelvin, "effect": effect})
        if not w:
            raise LightsError("nothing to change - give on/off, brightness, color, kelvin or an effect")
        return self._apply([(b, dict(w)) for b in bulbs], bool(verify))

    def reconnect(self, target: str) -> dict:
        """Window: try unreachable bulbs again now (they are retried in the background anyway)."""
        bulbs = self._resolve(self._cfg(), target)
        for b in bulbs:
            self.ble.retry_now(b["address"])
        return {"bulbs": [b["name"] for b in bulbs]}

    def blink(self, target: str, times: int = 3) -> dict:
        """Flash bulbs on/off so you can see which physical bulb has which name."""
        cfg = self._cfg()
        bulbs = self._resolve(cfg, target)
        before = self._read(bulbs, fresh=True)
        addrs = [b["address"] for b in bulbs if before.get(b["address"]) is not None]
        if not addrs:
            raise LightsError("none of these bulbs is reachable right now")
        self.ble.write({a: {"on": True, "brightness": 100} for a in addrs}, connect=False)
        for _ in range(max(1, min(10, int(times)))):
            time.sleep(0.5)
            self.ble.write({a: {"on": False} for a in addrs}, connect=False)
            time.sleep(0.5)
            self.ble.write({a: {"on": True} for a in addrs}, connect=False)
        restore = {a: ({"on": False} if not before[a]["on"] else {"brightness": before[a]["brightness"]})
                   for a in addrs}
        self.ble.write(restore, connect=False)
        return {"bulbs": [b["name"] for b in bulbs if b["address"] in addrs],
                "unreachable": [b["name"] for b in bulbs if b["address"] not in addrs]}

    def rename(self, bulb: str, name: str) -> dict:
        name = " ".join(str(name).split())
        with self._lock:
            cfg = self._load()
            hits = self._resolve(cfg, bulb)
            if len(hits) != 1:
                raise LightsError(f"'{bulb}' matches {len(hits)} bulbs - name exactly one")
            b = hits[0]
            self._check_name(cfg, name, b)
            old, b["name"] = b["name"], name
            self._save(cfg)
            scenes, changed = self._scenes(), False
            for sname, sc in list(scenes.items()):  # keep scenes pointing at the renamed bulb
                if isinstance(sc, dict) and any(norm(k) == norm(old) for k in sc):
                    scenes[sname] = {(name if norm(k) == norm(old) else k): v for k, v in sc.items()}
                    changed = True
            if changed:
                _write_json(SCENES_FILE, scenes)
        return {"old": old, "name": name}

    def set_group(self, bulb: str, group: str) -> dict:
        g = " ".join(str(group or "").split())
        if norm(g) in ("", "none", "-", "nincs"):
            g = ""
        with self._lock:
            cfg = self._load()
            hits = self._resolve(cfg, bulb)
            if g:
                if "," in g or norm(g) in ALL_WORDS or any(norm(b["name"]) == norm(g) for b in cfg["bulbs"]):
                    raise LightsError(f"'{g}' cannot be used as a group name")
                same = [x for x in self._groups(cfg) if norm(x) == norm(g)]
                if same:
                    g = same[0]
                else:
                    cfg["settings"]["groups"] = list(cfg["settings"].get("groups") or []) + [g]
            for b in hits:
                b["group"] = g
            self._save(cfg)
        return {"bulbs": [b["name"] for b in hits], "group": g}

    def scenes(self) -> dict:
        with self._lock:
            return {"scenes": self._scenes()}

    @staticmethod
    def _find_scene(scenes: dict, name: str) -> str:
        key = norm(name)
        hits = [k for k in scenes if norm(k) == key] or [k for k in scenes if norm(k).startswith(key)]
        if not hits:
            raise LightsError(f"no scene '{name}' (scenes: {', '.join(scenes) or 'none yet'})")
        if len(hits) > 1:
            raise LightsError(f"'{name}' matches several scenes: {', '.join(hits)}")
        return hits[0]

    def scene_apply(self, name: str, verify: bool = True) -> dict:
        with self._lock:
            cfg, scenes = self._load(), self._scenes()
        sname = self._find_scene(scenes, name)
        scene = scenes[sname]
        if not isinstance(scene, dict) or not scene:
            raise LightsError(f"scene '{sname}' is empty or not an object")
        wants: dict[str, tuple[dict, dict]] = {}
        warnings = []
        for target, entry in scene.items():
            try:
                if not isinstance(entry, dict):
                    raise LightsError(f"'{target}' must map to an object like {{\"on\": true}}")
                hits = self._resolve(cfg, target)
                w = self._want(cfg, entry)
            except LightsError as e:
                warnings.append(f"scene entry '{target}': {e}")
                continue
            for b in hits:
                wants[b["address"]] = (b, _merge(wants.get(b["address"], (b, {}))[1], w))
        if not wants:
            raise LightsError(f"scene '{sname}' does not match any known bulb" +
                              (f" ({'; '.join(warnings)})" if warnings else ""))
        res = self._apply(list(wants.values()), bool(verify))
        return {"scene": sname, "bulbs": res["bulbs"], "warnings": warnings + res["warnings"]}

    def scene_save(self, name: str, target: str = "all") -> dict:
        name = " ".join(str(name).split())
        if not name:
            raise LightsError("give the scene a name")
        cfg = self._cfg()
        bulbs = self._resolve(cfg, target)
        st = self._read(bulbs, fresh=True)
        scene, skipped = {}, []
        for b in bulbs:
            s = st.get(b["address"])
            if s is not None and s["mode"] == "scene" and not s.get("effect") and s.get("effect_code") is not None:
                s = dict(s, effect=effect_for_code(s["effect_code"]))
            if s is None or (s["mode"] == "scene" and not s.get("effect")):
                skipped.append(b["name"])
                continue
            e: dict = {"on": bool(s["on"])}
            if e["on"]:
                e["brightness"] = int(s["brightness"])
                if s["mode"] == "scene":
                    e["effect"] = s["effect"]
                elif s["mode"] == "white":
                    e["kelvin"] = int(s["kelvin"])
                else:
                    e["color"] = to_hex(s["rgb"])
            scene[b["name"]] = e
        if not scene:
            raise LightsError("no bulb answered, so there is no state to save")
        with self._lock:
            scenes = self._scenes()
            name = next((k for k in scenes if norm(k) == norm(name)), name)  # overwrite same name
            scenes[name] = scene
            _write_json(SCENES_FILE, scenes)
        return {"scene": name, "saved": list(scene), "skipped": skipped}

    def scene_delete(self, name: str) -> dict:
        with self._lock:
            scenes = self._scenes()
            sname = next((k for k in scenes if norm(k) == norm(name)), None)
            if sname is None:
                raise LightsError(f"no scene '{name}'")
            del scenes[sname]
            _write_json(SCENES_FILE, scenes)
        return {"deleted": sname}
