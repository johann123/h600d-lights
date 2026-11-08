"""In-memory stand-in for govee_ble.BleTransport: simulated H600D bulbs, no Bluetooth needed."""
from __future__ import annotations

import threading

KELVIN = (2700, 6500)


class FakeBulb:
    def __init__(self, address: str, rssi: int = -70):
        self.address = address.upper()
        self.rssi = rssi
        self.reachable = True
        self.ignore = 0  # acknowledge but ignore the next N commands (tests the verify + resend path)
        self.state = {"on": False, "brightness": 100, "mode": "white", "rgb": [255, 255, 255], "kelvin": 4000,
                      "effect": None, "effect_code": None}
        self.log: list[dict] = []

    def apply(self, w: dict) -> None:
        self.log.append(dict(w))
        if self.ignore > 0:
            self.ignore -= 1
            return
        if w.get("on") is False:
            self.state["on"] = False
            return
        if w.get("on"):
            self.state["on"] = True
        if "brightness" in w and 1 <= int(w["brightness"]) <= 100:
            self.state["brightness"] = int(w["brightness"])
        if "rgb" in w:
            self.state.update(mode="color", rgb=[int(c) for c in w["rgb"]], kelvin=0, effect=None, effect_code=None)
        elif "kelvin" in w:
            self.state.update(mode="white", kelvin=max(KELVIN[0], min(KELVIN[1], int(w["kelvin"]))), effect=None, effect_code=None)
        elif "effect" in w:
            self.state.update(mode="scene", rgb=None, kelvin=0, effect=w["effect"]["name"],
                              effect_code=w["effect"]["code"])  # every uploaded scene reports 8217


class FakeTransport:
    def __init__(self, n: int = 5):
        self.bulbs = {b.address: b for b in (FakeBulb(f"AA:BB:CC:DD:00:{i + 1:02X}", -60 - 4 * i) for i in range(n))}
        self.connected: set[str] = set()
        self.watch: set[str] = set()
        self.lock = threading.Lock()

    def _reach(self, addresses, connect: bool) -> list[str]:
        out = []
        for a in addresses:
            a = a.upper()
            b = self.bulbs.get(a)
            if b is None or not b.reachable:
                self.connected.discard(a)
                continue
            if a in self.watch or connect:
                self.connected.add(a)
            if a in self.connected:
                out.append(a)
        return out

    def scan(self, seconds: float = 8.0) -> list[dict]:
        return [{"address": a, "name": f"GVH600D{a.replace(':', '')[-4:]}", "sku": "H600D", "rssi": b.rssi}
                for a, b in self.bulbs.items() if b.reachable and a not in self.connected]

    def read(self, addresses, max_age: float = 0.0, connect: bool = True) -> dict:
        with self.lock:
            ok = set(self._reach(addresses, connect))
            return {a.upper(): (dict(self.bulbs[a.upper()].state, rgb=list(self.bulbs[a.upper()].state["rgb"] or []) or None)
                                if a.upper() in ok else None) for a in addresses}

    def write(self, plan: dict, connect: bool = True) -> dict:
        with self.lock:
            ok = set(self._reach(plan, connect))
            out = {}
            for a, w in plan.items():
                if a.upper() in ok:
                    self.bulbs[a.upper()].apply(w)
                    out[a.upper()] = None
                else:
                    out[a.upper()] = "not reachable - out of Bluetooth range, switched off at the wall, or held by the phone app"
            return out

    def info(self, address: str) -> dict:
        b = self.bulbs.get(address.upper())
        up = address.upper() in self.connected
        return {"connected": up, "rssi": b.rssi if b else None, "connecting": False,
                "offline": not up and b is not None and not b.reachable,
                "error": "" if up else "not reachable"}

    def retry_now(self, address: str) -> None:
        self.retried = getattr(self, "retried", []) + [address.upper()]

    def keep_connected(self, addresses) -> None:
        self.watch = {a.upper() for a in addresses}
        self._reach(self.watch, False)

    def close(self) -> None:
        self.connected.clear()
