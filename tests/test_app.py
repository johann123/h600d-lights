"""Drive the desktop window against simulated bulbs (no hardware):  python tests/test_app.py
A window opens for a few seconds while the test clicks through it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="govee-apptest-"))
os.environ["GOVEE_HOME"] = str(HOME)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tkinter import TclError, ttk  # noqa: E402

import app as A  # noqa: E402  (app.pyw)
import fake_ble  # noqa: E402
import lights as L  # noqa: E402

(HOME / "bulbs.json").write_text(json.dumps({"settings": {}}), encoding="utf-8")
fake = fake_ble.FakeTransport(5)
F = list(fake.bulbs.values())
down, up = F[:3], F[3:]
setup = L.Lights(transport=fake)
for old, new, g in zip(setup.discover(seconds=0)["new"], ["Down left", "Down middle", "Down right", "Up left", "Up right"],
                       ["down"] * 3 + ["up"] * 2):
    setup.rename(bulb=old, name=new)
    setup.set_group(bulb=new, group=g)
passed = 0


def check(cond, what):
    global passed
    if not cond:
        raise AssertionError(what)
    passed += 1
    print("  ok  ", what)


root = A.tk.Tk()
ui = A.App(root, L.Lights(transport=fake, live=True))
root.geometry(f"{ui.px(1120)}x{ui.px(900)}")


def pump(seconds=0.8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            root.update()
        except TclError:  # window closed
            return
        time.sleep(0.01)


def buttons(widget, text):
    found = []
    for w in widget.winfo_children():
        if isinstance(w, ttk.Button) and str(w.cget("text")) == text:
            found.append(w)
        found += buttons(w, text)
    return found


def click_swatch(card, index):
    step, px = ui.px(24), ui.px
    x, y = px(2) + index * step + px(10), px(12)
    card.swatches.event_generate("<Motion>", x=x, y=y)
    card.swatches.event_generate("<Button-1>", x=x, y=y)


pump(1.2)
print("window")
check(set(ui.cards) == {"group:all", "group:down", "group:up", *fake.bulbs}, "cards for All, Down, Up and the 5 bulbs")
check(ui.summary.cget("text") == "5 of 5 bulbs connected", "header shows 5 of 5 connected")

print("group controls")
buttons(ui.cards["group:down"].frame, "On")[0].invoke()
pump()
check(all(b.state["on"] for b in down) and not any(b.state["on"] for b in up), "Down group On button: only down is on")
buttons(ui.cards["group:up"].frame, "On")[0].invoke()
pump()
check(all(b.state["on"] for b in up), "Up group On button")
card = ui.cards["group:up"]
card._moved("brightness", 30)  # what dragging the slider does
pump(0.5)
check(all(b.state["brightness"] == 30 for b in up), "dragging Up's brightness slider changes the bulbs live")
card.bright.set(45)
card._released("brightness")
pump()
check(all(b.state["brightness"] == 45 for b in up) and not any(b.state["brightness"] == 45 for b in down),
      "releasing the slider sets 45% on Up only")
card = ui.cards["group:down"]
card.white.set(3020)
card._released("kelvin")
pump()
check(all(b.state["mode"] == "white" and b.state["kelvin"] == 3000 for b in down), "Down white slider -> 3000 K (50 K steps)")
check(card.white_val.cget("text") == "3000 K", "the card shows the new temperature")
click_swatch(ui.cards["group:all"], 0)
pump()
check(all(b.state["mode"] == "color" and b.state["rgb"] == [255, 0, 0] for b in F), "red swatch on All colours every bulb")

print("bulb controls")
card = ui.cards[F[0].address]
card.switch.toggle()
pump()
check(not F[0].state["on"] and card.switch.value is False, "Down left's switch turns it off")
click_swatch(ui.cards[F[4].address], 5)
pump()
check(F[4].state["rgb"] == [0, 0, 255] and F[3].state["rgb"] == [255, 0, 0], "blue swatch on Up right only")
n = len(F[1].log)
buttons(ui.cards[F[1].address].frame, "Identify")[0].invoke()
pump(4.5)
check([w.get("on") for w in F[1].log[n:]][1:5] == [False, True, False, True], "Identify flashes Down middle")
card = ui.cards[F[4].address]
card.name.delete(0, "end")
card.name.insert(0, "Galéria jobb")
card._renamed()
pump()
check(ui.cards[F[4].address].view["name"] == "Galéria jobb", "renaming in the card")
check(any(b["name"] == "Galéria jobb" for b in json.loads((HOME / "bulbs.json").read_text(encoding="utf-8"))["bulbs"]),
      "the new name is saved to bulbs.json")
card = ui.cards[F[2].address]
card.group.set("up")
card._group_picked()
pump()
check(ui.cards["group:up"].count.cget("text").startswith("3 bulbs"), "moving Down right to up: Up shows 3 bulbs")
ui.cards[F[2].address].group.set("down")
ui.cards[F[2].address]._group_picked()
pump()

print("effects")
card = ui.cards["group:up"]
card.effect.set(ui.effect_label["Last phone scene"])
card._effect_picked()
pump()
check(all(b.state["effect"] == "Last phone scene" for b in up), "Effect list on the Up card replays the phone scene on both")
check(ui.cards[F[3].address].effect.get() == "Phone · Last phone scene", "the bulb cards show the running effect")

print("scenes and header")
buttons(ui.scene_box, "night")[0].invoke()
pump()
check(all(b.state["on"] and b.state["brightness"] == 8 for b in down) and not any(b.state["on"] for b in up),
      "night scene button")
ui.scene_name.insert(0, "teszt")
ui.save_scene()
pump()
check(buttons(ui.scene_box, "teszt") and "teszt" in json.loads((HOME / "scenes.json").read_text(encoding="utf-8")),
      "Save current adds a scene button and saves it")
buttons(root, "All off")[0].invoke()
pump()
check(not any(b.state["on"] for b in F), "All off")

print("unreachable bulb")
F[3].reachable = False
ui.refresh(force=True)
pump()
ld = ui.cards[F[3].address].loader
check(ld.frame.winfo_ismapped() and "switched off" in ld.label.cget("text") and not ld.spinning
      and ld.button.winfo_ismapped(), "a switched-off bulb gets a still 'switched off or out of range' cover with Try now")
ld.button.invoke()
pump()
check(getattr(fake, "retried", []) == [F[3].address], "Try now asks the transport to retry that bulb at once")
F[3].reachable = True
ui.refresh(force=True)
pump()
check(not ld.frame.winfo_ismapped(), "the spinner disappears once the bulb is back")
F[3].reachable = False
ui.refresh(force=True)
pump()
check(ui.summary.cget("text") == "4 of 5 bulbs connected", "header shows 4 of 5")
gc = ui.cards["group:up"]
check(gc.count.cget("text").endswith(" · 1 off") and "connected" not in gc.count.cget("text") and not gc.mini_shown,
      "the Up group card shows '1 off' and no spinner for the switched-off bulb")
F[3].reachable = True

print("closing")
ui.close()
pump(1.5)
try:
    root.winfo_exists()
    closed = False
except TclError:
    closed = True
check(closed and not fake.connected, "closing the window lets go of every bulb")
print(f"\nALL {passed} CHECKS PASSED")
