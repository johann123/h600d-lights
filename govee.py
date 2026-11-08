"""govee - control Govee Bluetooth bulbs (H600D) from this PC. No server: each command connects
straight to the bulbs it needs over Bluetooth (a few seconds), does its job and lets go.

  govee                         status of all bulbs
  govee on down                 on/off: all, a group (down, up), a bulb, or "Down left,Up right"
  govee off all
  govee bright up 40            brightness 1..100
  govee color down orange       #ff8800, "255,136,0" or a name (red, blue, pink, ...)
  govee white all 2700 [60]     white 2700 (warm) .. 6500 (cool), optional brightness
  govee set down -b 30 -k 3000  several changes at once (--on/--off, -b, -c, -k, -e)
  govee effect down "neon dusk" play a moving effect (in sync on a group; see govee effects)
  govee effects                 list all effects
  govee scene                   list scenes            govee scene relax     apply one
  govee scene save evening      save the current state of all bulbs (-t down: only some)
  govee scene delete evening
  govee blink "Down left"       flash a bulb to see which one it is
  govee name "Down left" Sofa   rename          govee group Sofa down      set its group
  govee discover                look for new bulbs over Bluetooth
  govee app                     open the desktop window
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from lights import Lights, LightsError

NO_BULBS_HELP = """No bulb answered. Check that the bulbs:
  - are switched on at the wall (they need power to be reachable),
  - are within Bluetooth range of this PC,
  - are not connected to the Govee app on a phone (that blocks the PC)."""


# ---- output ---------------------------------------------------------------------
def bulb_line(v: dict, width: int) -> str:
    if not v.get("online"):
        state = "-- not reachable"
    elif not v["on"]:
        state = "off"
    else:
        what = {"white": f"white {v['kelvin']}K", "color": f"color {v['color']}"}.get(
            v["mode"], f"effect {v['effect']}" if v.get("effect") else "app scene")
        state = f"on {v['brightness']:>3}%  {what}"
    return f"{v['name']:<{width}}  {state:<26}  {v['address']}"


def print_bulbs(views: list[dict], groups: list[str]) -> None:
    width = max([len(v["name"]) for v in views] + [6])
    for g in [*groups, ""]:
        items = [v for v in views if v["group"] == g]
        if items:
            print(f"{g.upper() if g else 'NO GROUP'} ({len(items)})")
            for v in items:
                print("  " + bulb_line(v, width))


def report(res: dict) -> None:
    views = res.get("bulbs") or []
    width = max([len(v["name"]) for v in views] + [6])
    for v in views:
        print("  " + bulb_line(v, width))
    for w in res.get("warnings") or []:
        print("  ! " + w)


def describe_entry(e) -> str:
    if not isinstance(e, dict):
        return "?"
    if e.get("on") is False:
        return "off"
    parts = ["on"]
    if "brightness" in e:
        parts.append(f"{e['brightness']}%")
    if "kelvin" in e:
        parts.append(f"{e['kelvin']}K")
    if "color" in e:
        parts.append(str(e["color"]))
    if "effect" in e:
        parts.append(f"effect {e['effect']}")
    return " ".join(parts)


# ---- commands ----------------------------------------------------------------------
def cmd_status(lights, a):
    st = lights.state(target=a.target)
    if not st["bulbs"]:
        print("No bulbs known yet. Run:  govee discover")
        return 1
    print_bulbs(st["bulbs"], st["groups"])
    if not any(v["online"] for v in st["bulbs"]):
        print("\n" + NO_BULBS_HELP)


def cmd_discover(lights, a):
    r = lights.discover(seconds=a.seconds)
    print(f"Found {r['found']} bulb(s)." + (f"  New: {', '.join(r['new'])}." if r["new"] else ""))
    if r["missing"]:
        print(f"Not advertising right now: {', '.join(r['missing'])}")
    if r["new"]:
        print('\nTo tell them apart:  govee blink "<name>"   then   govee name "<name>" "Kitchen"'
              '\nand put them in groups:  govee group Kitchen down   (or use the app: govee app)')
    if not r["found"]:
        print("\n" + NO_BULBS_HELP)
        return 1


def cmd_on(lights, a):
    report(lights.control(target=a.target, on=True))


def cmd_off(lights, a):
    report(lights.control(target=a.target, on=False))


def cmd_bright(lights, a):
    report(lights.control(target=a.target, brightness=a.value))


def cmd_color(lights, a):
    report(lights.control(target=a.target, color=" ".join(a.color)))


def cmd_white(lights, a):
    report(lights.control(target=a.target, kelvin=a.kelvin, brightness=a.brightness))


def cmd_set(lights, a):
    on = True if a.on else False if a.off else None
    report(lights.control(target=a.target, on=on, brightness=a.brightness, color=a.color, kelvin=a.kelvin,
                          effect=a.effect))


def cmd_effect(lights, a):
    report(lights.control(target=a.target, effect=" ".join(a.name), brightness=a.brightness))


def cmd_effects(lights, a):
    for cat, names in lights.effects()["categories"].items():
        print(f"{cat}: {', '.join(names)}")


def cmd_blink(lights, a):
    r = lights.blink(target=a.target)
    print(f"Blinked: {', '.join(r['bulbs'])}" + (f"  (not reachable: {', '.join(r['unreachable'])})"
                                                 if r["unreachable"] else ""))


def cmd_name(lights, a):
    r = lights.rename(bulb=a.bulb, name=" ".join(a.name))
    print(f"Renamed '{r['old']}' -> '{r['name']}'")


def cmd_group(lights, a):
    r = lights.set_group(bulb=a.bulb, group=a.group)
    print(f"{', '.join(r['bulbs'])} -> {r['group'] or 'no group'}")


def cmd_scene(lights, a):
    words = ([a.action] if a.action else []) + a.rest
    verb = words[0].lower() if words else "list"
    if verb == "list":
        scenes = lights.scenes()["scenes"]
        if not scenes:
            print("No scenes yet. Save one:  govee scene save <name>")
        width = max([len(n) for n in scenes] + [4])
        for n, sc in scenes.items():
            entries = list(sc.items()) if isinstance(sc, dict) else []
            text = ", ".join(f"{t}: {describe_entry(e)}" for t, e in entries[:3])
            print(f"  {n:<{width}}  {text}{' ...' if len(entries) > 3 else ''}")
    elif verb == "save":
        r = lights.scene_save(name=" ".join(words[1:]), target=a.target)
        print(f"Saved scene '{r['scene']}' ({len(r['saved'])} bulbs)"
              + (f"; not saved (unreachable): {', '.join(r['skipped'])}" if r["skipped"] else ""))
    elif verb in ("delete", "del", "remove", "rm"):
        print(f"Deleted scene '{lights.scene_delete(name=' '.join(words[1:]))['deleted']}'")
    else:
        name = " ".join(words[1:] if verb == "apply" else words)
        r = lights.scene_apply(name=name)
        print(f"Scene '{r['scene']}':")
        report(r)


def open_app() -> int:
    exe = Path(sys.executable)
    exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    subprocess.Popen([str(exe), str(Path(__file__).resolve().with_name("app.pyw"))],
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    print("Opening the Govee window...")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="govee", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_, aliases=()):
        sp = sub.add_parser(name, help=help_, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "show bulbs and their state", ["st", "list", "ls"]).add_argument("target", nargs="?", default="all")
    add("discover", cmd_discover, "look for bulbs over Bluetooth", ["scan"]).add_argument("--seconds", type=float, default=8.0)
    add("on", cmd_on, "switch on").add_argument("target", nargs="?", default="all")
    add("off", cmd_off, "switch off").add_argument("target", nargs="?", default="all")
    sp = add("bright", cmd_bright, "brightness 1..100", ["brightness", "dim", "b"])
    sp.add_argument("target")
    sp.add_argument("value")
    sp = add("color", cmd_color, "RGB color", ["colour", "c"])
    sp.add_argument("target")
    sp.add_argument("color", nargs="+")
    sp = add("white", cmd_white, "white temperature in Kelvin", ["temp", "ct", "w"])
    sp.add_argument("target")
    sp.add_argument("kelvin")
    sp.add_argument("brightness", nargs="?")
    sp = add("set", cmd_set, "several changes at once")
    sp.add_argument("target")
    sp.add_argument("--on", action="store_true")
    sp.add_argument("--off", action="store_true")
    sp.add_argument("-b", "--brightness")
    sp.add_argument("-c", "--color")
    sp.add_argument("-k", "--kelvin")
    sp.add_argument("-e", "--effect")
    sp = add("effect", cmd_effect, "play a dynamic effect", ["fx"])
    sp.add_argument("target")
    sp.add_argument("name", nargs="+")
    sp.add_argument("-b", "--brightness")
    add("effects", cmd_effects, "list the dynamic effects")
    add("blink", cmd_blink, "flash bulbs to identify them", ["identify"]).add_argument("target")
    sp = add("name", cmd_name, "rename a bulb", ["rename"])
    sp.add_argument("bulb")
    sp.add_argument("name", nargs="+")
    sp = add("group", cmd_group, "put a bulb in a group (down, up, none)")
    sp.add_argument("bulb")
    sp.add_argument("group")
    sp = add("scene", cmd_scene, "list / apply / save / delete scenes", ["scenes"])
    sp.add_argument("action", nargs="?")
    sp.add_argument("rest", nargs="*")
    sp.add_argument("-t", "--target", default="all", help="scene save: only these bulbs/groups")
    add("app", None, "open the desktop window", ["window", "gui"])
    return p


def connect():
    """The bulb controller for one command (tests replace this)."""
    return Lights()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    a = build_parser().parse_args(argv)
    if a.cmd in ("app", "window", "gui"):
        return open_app()
    fn = getattr(a, "fn", None) or cmd_status
    if fn is cmd_status and not hasattr(a, "target"):
        a.target = "all"
    lights = None
    try:
        lights = connect()
        return fn(lights, a) or 0
    except LightsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if lights is not None:
            lights.close()  # let go of the bulbs


if __name__ == "__main__":
    sys.exit(main())
