"""The desktop window with 5 simulated bulbs, to try the UI without hardware:  python tests/demo_app.py
Shows a red DEMO banner, uses a throw-away data folder and makes no Bluetooth connection.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="govee-demo-"))
os.environ["GOVEE_HOME"] = str(HOME)
os.environ["GOVEE_DEMO"] = "1"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import app  # noqa: E402  (app.pyw)
import fake_ble  # noqa: E402
import lights  # noqa: E402

(HOME / "bulbs.json").write_text(json.dumps({"settings": {}}), encoding="utf-8")
fake = fake_ble.FakeTransport(5)
setup = lights.Lights(transport=fake)
names = setup.discover(seconds=0)["new"]
for old, new, group in zip(names, ["Down left", "Down middle", "Down right", "Up left", "Up right"],
                           ["down", "down", "down", "up", "up"]):
    setup.rename(bulb=old, name=new)
    setup.set_group(bulb=new, group=group)
setup.control(target="down", color="#8b00ff", brightness=27)
setup.control(target="Up right", kelvin=2700, brightness=60)
setup.control(target="Up left", on=False)
if "--offline" in sys.argv:
    list(fake.bulbs.values())[3].reachable = False  # show how an out-of-range bulb looks
if "--all-offline" in sys.argv:
    for b in fake.bulbs.values():
        b.reachable = False
app.main(lights.Lights(transport=fake, live=True))
