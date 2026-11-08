"""End-to-end test against simulated Bluetooth bulbs (no hardware):  python tests/test_govee.py

Uses a temporary data folder (GOVEE_HOME), so the real bulbs.json and scenes.json are not touched,
and runs the CLI in-process with the simulated bulbs, so no real Bluetooth connection is made.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="govee-test-"))
os.environ["GOVEE_HOME"] = str(HOME)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import fake_ble  # noqa: E402
import govee  # noqa: E402
import lights as L  # noqa: E402

(HOME / "bulbs.json").write_text(json.dumps({"settings": {}}), encoding="utf-8")
passed = 0


def check(cond, what):
    global passed
    if not cond:
        raise AssertionError(what)
    passed += 1
    print("  ok  ", what)


fake = fake_ble.FakeTransport(5)
F = list(fake.bulbs.values())
lights = L.Lights(transport=fake)

print("discovery")
r = lights.discover(seconds=1)
check(r["found"] == 5 and len(r["new"]) == 5, f"5 new bulbs found ({r['new']})")
check(r["new"][0] == "H600D 0001", "default name = model + last 4 hex digits of the address")
saved = json.loads((HOME / "bulbs.json").read_text(encoding="utf-8"))
check(saved["settings"]["groups"] == ["down", "up"] and len(saved["bulbs"]) == 5, "bulbs.json written with settings")
check(lights.discover(seconds=1)["new"] == [], "second discovery adds nothing")

print("names and groups")
for i, (name, group) in enumerate([("Konyha 1", "down"), ("Konyha 2", "down"), ("Nappali", "down"),
                                   ("Hálószoba", "up"), ("Fürdő", "up")]):
    lights.rename(bulb=r["new"][i], name=name)
    lights.set_group(bulb=name, group=group)
st = lights.state()
check([v["name"] for v in st["bulbs"] if v["group"] == "down"] == ["Konyha 1", "Konyha 2", "Nappali"], "3 bulbs in down")
check([v["name"] for v in st["bulbs"] if v["group"] == "up"] == ["Hálószoba", "Fürdő"], "2 bulbs in up")
check(all(v["online"] and v["rssi"] is not None for v in st["bulbs"]), "state shows online + signal strength")
for bad, why in [("all", "reserved word"), ("down", "group name"), ("Nappali", "duplicate"), ("a,b", "comma")]:
    try:
        lights.rename(bulb="Fürdő", name=bad)
        check(False, f"rename to '{bad}' rejected ({why})")
    except L.LightsError:
        check(True, f"rename to '{bad}' rejected ({why})")
check(lights.state(target="0003")["bulbs"][0]["name"] == "Nappali", "address suffix selects a bulb")

print("control")
down, up = F[:3], F[3:]
r = lights.control(target="down", on=True)
check(all(b.state["on"] for b in down) and not any(b.state["on"] for b in up), "on down: only down is on")
check(r["warnings"] == [] and len(r["bulbs"]) == 3 and all(v["on"] for v in r["bulbs"]), "verified, no warnings")
lights.control(target="up", color="orange")
check(all(b.state["on"] and b.state["mode"] == "color" and b.state["rgb"] == [255, 127, 0] for b in up),
      "color up orange (switches them on)")
lights.control(target="all", kelvin="2200", brightness=40)
check(all(b.state["mode"] == "white" and b.state["kelvin"] == 2700 and b.state["brightness"] == 40 for b in F),
      "white 2200 clamps to 2700, brightness 40")
lights.control(target="halo", kelvin="cool")
check(F[3].state["kelvin"] == 5000 and F[4].state["kelvin"] == 2700, "'halo' = Hálószoba only; cool = 5000K")
lights.control(target="Konyha 1, furdo", color="10,20,30")
check(F[0].state["rgb"] == [10, 20, 30] and F[4].state["rgb"] == [10, 20, 30], "comma list, accents optional")
for bad in [dict(target="cellar", on=True), dict(target="all", color="blurple"), dict(target="all", kelvin="hot"),
            dict(target="all", brightness="lots"), dict(target="all", color="red", kelvin=3000), dict(target="all")]:
    try:
        lights.control(**bad)
        check(False, f"rejected {bad}")
    except L.LightsError as e:
        check(True, f"rejected {bad}: {e}")

print("unreliable bulbs")
F[1].ignore = 1
r = lights.control(target="down", brightness=77)
check(F[1].state["brightness"] == 77 and r["warnings"] == [], "ignored command is noticed by read-back and re-sent")
F[4].reachable = False
r = lights.control(target="all", on=False)
check(not any(b.state["on"] for b in F[:4]), "reachable bulbs switched off")
check(r["warnings"] and "Fürdő" in r["warnings"][0] and "not reachable" in r["warnings"][0], f"warning: {r['warnings']}")
check([v["online"] for v in lights.state()["bulbs"]] == [True, True, True, True, False], "state marks it offline")
F[4].reachable = True

print("scenes")
check(set(lights.scenes()["scenes"]) >= {"bright", "relax", "night", "movie", "all off"}, "default scenes created")
lights.scene_apply(name="night")
check(all(b.state["on"] and b.state["brightness"] == 8 and b.state["kelvin"] == 2700 for b in down)
      and not any(b.state["on"] for b in up), "scene night: down 8% warm, up off")
lights.control(target="Nappali", color="#00ff00", brightness=55)
r = lights.scene_save(name="Esti mozi")
check(r["saved"] == ["Konyha 1", "Konyha 2", "Nappali", "Hálószoba", "Fürdő"] and not r["skipped"], "scene saved from live state")
sc = lights.scenes()["scenes"]["Esti mozi"]
check(sc["Nappali"] == {"on": True, "brightness": 55, "color": "#00ff00"} and sc["Fürdő"] == {"on": False}, "saved values")
lights.scene_apply(name="all off")
check(not any(b.state["on"] for b in F), "scene all off")
lights.scene_apply(name="esti")
check(F[2].state["rgb"] == [0, 255, 0] and F[0].state["on"] and not F[4].state["on"], "saved scene restored (prefix match)")
lights.rename(bulb="Nappali", name="Nappali lámpa")
check("Nappali lámpa" in lights.scenes()["scenes"]["Esti mozi"], "rename updates scene keys")
(HOME / "scenes.json").write_text(json.dumps({**lights.scenes()["scenes"],
                                              "hand": {"all": {"brightness": 30, "kelvin": 3000}, "up": {"color": "pink"},
                                                       "cellar": {"on": True}}}, ensure_ascii=False), encoding="utf-8")
r = lights.scene_apply(name="hand")
check(all(b.state["brightness"] == 30 for b in F) and all(b.state["mode"] == "color" for b in up)
      and all(b.state["kelvin"] == 3000 for b in down), "hand-written scene: all + group override")
check(any("cellar" in w for w in r["warnings"]), "unknown scene target reported, rest applied")
lights.scene_delete(name="hand")
check("hand" not in lights.scenes()["scenes"], "scene deleted")

print("effects")
cats = lights.effects()["categories"]
check(list(cats)[:5] == ["Cyberpunk", "Nature", "Cozy", "Calm", "Soft party"] and cats["Phone"][0] == "Last phone scene"
      and len(cats["Cyberpunk"]) >= 6, f"own scenes are offered ({cats})")
import govee_ble as G
fx = L.load_effects()
check(all(e["frames"] and e["code"] == 8217 for e in fx.values() if e["name"] != "Last phone scene"),
      "every scene with data is uploaded and started as 8217")
check([f.hex() for f in fx[L.norm("Neon Dusk")]["frames"]] == [
      "a3000102018301ffd066ff00036000ff2800c0e1", "a3ffb000b000000000000000000000000000005c"],
      "encoder: the frames verified on the bulbs")
check(len(G.scene_frames(bytes(11))) == 2 and len(G.scene_frames(bytes(65))) == 5 and len(G.scene_frames(bytes(66))) == 5,
      "frame count like the phone app: at least 2, an empty last frame when the data fills the others")
(HOME / "effects_local.json").write_text(json.dumps({"categories": {"Phone": [
    {"name": "My capture", "frames": ["a3000102018301ffebff960001ff000000000000"]}]}}), encoding="utf-8")
check(L.find_effect("my capture")["frames"][0].hex().startswith("a30001020183"), "effects_local.json adds your own scenes")
check(G.gradient_data([(0x60, 0, 0xFF)], 40, 255, 0xD0).hex() == "8301ffd066ff00016000ff", "gradient layout")
r = lights.control(target="down", effect="neon dusk")
check(all(b.state["effect"] == "Neon Dusk" for b in down) and r["warnings"] == []
      and all(v["effect"] == "Neon Dusk" for v in r["bulbs"]), "own scene on down, shown by name although it reports 8217")
lights.control(target="Konyha 1", effect="lullaby")
check(F[0].state["effect"] == "Lullaby" and F[0].state["brightness"] == 1, "Lullaby starts the bulb at 1%")
lights.control(target="Konyha 1", effect="lullaby", brightness=20)
check(F[0].state["brightness"] == 20, "a brightness given with it wins")
F[1].state["effect"] = None  # e.g. after a restart: we no longer know which upload it was
check(lights.state("Konyha 2")["bulbs"][0]["effect"] == "Last phone scene", "unknown upload shows as Last phone scene")
r = lights.control(target="down", effect="last phone")
check(all(b.state["mode"] == "scene" and b.state["effect"] == "Last phone scene" and b.state["on"] for b in down)
      and r["warnings"] == [], "replaying the last phone scene on down (prefix match), verified")
lights.control(target="Konyha 1", color="red")
check(F[0].state["effect"] is None and F[0].state["mode"] == "color", "a colour ends the effect")
for bad in [dict(target="all", effect="Volcano"), dict(target="all", effect="last phone", color="red")]:
    try:
        lights.control(**bad)
        check(False, f"rejected {bad}")
    except L.LightsError as e:
        check(True, f"rejected {bad}: {e}")
r = lights.scene_save(name="tuz", target="down")
check(lights.scenes()["scenes"]["tuz"]["Konyha 2"] == {"on": True, "brightness": F[1].state["brightness"],
                                                      "effect": "Last phone scene"}, "saving a scene keeps the effect")
lights.scene_delete(name="tuz")

print("blink")
lights.control(target="Konyha 2", on=False)
n = len(F[1].log)
lights.blink(target="Konyha 2", times=2)
seq = F[1].log[n:]
check(seq[0] == {"on": True, "brightness": 100} and [w.get("on") for w in seq[1:5]] == [False, True, False, True]
      and seq[-1] == {"on": False} and not F[1].state["on"], f"blink flashes and restores off ({seq})")

print("live mode (desktop app)")
live = L.Lights(transport=fake, live=True)
check(fake.watch == set(fake.bulbs), "live mode keeps every known bulb connected")
check(live.state()["bulbs"][0]["online"], "live state reads the kept-alive bulbs")

print("CLI (in-process, simulated bulbs)")
govee.connect = lambda: L.Lights(transport=fake)


def cli(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = govee.main(list(args))
    return code, buf.getvalue()


code, out = cli("off", "down")
check(code == 0 and not any(b.state["on"] for b in down), "CLI 'off down'")
code, out = cli("white", "up", "3500", "20")
check(code == 0 and all(b.state["kelvin"] == 3500 and b.state["brightness"] == 20 for b in up), "CLI 'white up 3500 20'")
code, out = cli("set", "Konyha 1", "-c", "pink", "-b", "70")
check(code == 0 and F[0].state["rgb"] == [255, 20, 147] and F[0].state["brightness"] == 70, "CLI 'set ... -c pink -b 70'")
code, out = cli("status")
check(code == 0 and "DOWN (3)" in out and "UP (2)" in out and "Hálószoba" in out, "CLI status:\n" + out)
code, out = cli("scene", "save", "only up", "-t", "up")
check(code == 0 and set(lights.scenes()["scenes"]["only up"]) == {"Hálószoba", "Fürdő"}, "CLI scene save -t up")
code, out = cli("scene")
check(code == 0 and "Esti mozi" in out and "only up" in out, "CLI scene list")
code, out = cli("scene", "only up")
check(code == 0 and "Scene 'only up'" in out, "CLI scene apply")
code, out = cli("effect", "down", "last")
check(code == 0 and all(b.state["effect"] == "Last phone scene" for b in down) and "effect Last phone scene" in out,
      "CLI 'effect down last'")
code, out = cli("effects")
check(code == 0 and out.startswith("Cyberpunk: Neon Dusk") and "Phone: Last phone scene" in out, "CLI 'effects' lists it")
code, out = cli("color", "nowhere", "red")
check(code == 1 and "no bulb or group called 'nowhere'" in out, "CLI error message, exit code 1")
check(not fake.connected, "the CLI lets go of the bulbs when it is done")

print(f"\nALL {passed} CHECKS PASSED  (data folder: {HOME})")
