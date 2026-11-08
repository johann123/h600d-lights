"""Bluetooth LE transport for Govee bulbs (tested with H600D), built on bleak.

Protocol (plain Govee BLE frames: 20 bytes, last byte = XOR of the first 19), verified on H600D:
  write to 00010203-0405-0607-0809-0a0b0c0d2b11, answers arrive as notifications on ...2b10
  33 01 <0|1>                    power                    ack: 33 01 00 ...
  33 04 <1..100>                 brightness (>100 ignored) ack: 33 04 00 ...
  33 05 0d R G B 00 00           colour mode
  33 05 0d FF FF FF Kh Kl        white mode, Kelvin big-endian (RGB bytes ignored)
  aa 01 / aa 04 / aa 05 01       read power / brightness / "0d R G B Kh Kl" (Kelvin 0 = colour mode)
Known bulbs are connected straight by address (Windows finds them itself), which is faster and far
more reliable for weak bulbs than scanning first. A bulb drops an idle link after ~12 s, so the
desktop app keeps its links alive with status reads. Several programs on this PC can use a bulb at
the same time (Windows shares the link), but a phone connected to a bulb blocks the PC.

All Bluetooth work runs on one asyncio loop in a background thread; the public methods are plain
blocking calls.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import namedtuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
NAME_RE = re.compile(r"^(?:GV|ihoment_|Govee_)(H6\w{3})_?([0-9A-F]{4})$", re.I)  # GVH600D386E -> H600D, 386E
NOT_FOUND = "not reachable - switched off at the wall, out of Bluetooth range, or connected to a phone"

# bleak connects without scanning when it gets a device that already carries the address
_Raw = namedtuple("_Raw", "adv scan")
_Adv = namedtuple("_Adv", "bluetooth_address")


def frame(*data: int) -> bytes:
    p = bytearray(20)
    p[: len(data)] = bytes(data)
    x = 0
    for v in p[:19]:
        x ^= v
    p[19] = x
    return bytes(p)


def frames_for(w: dict) -> list[bytes]:
    """Frames for a wanted state {on, brightness, rgb, kelvin} (missing keys = leave unchanged)."""
    if w.get("on") is False:
        return [frame(0x33, 0x01, 0x00)]
    out = []
    if w.get("on"):
        out.append(frame(0x33, 0x01, 0x01))
    if "brightness" in w:
        out.append(frame(0x33, 0x04, max(1, min(100, int(w["brightness"])))))
    if "rgb" in w:
        r, g, b = (max(0, min(255, int(c))) for c in w["rgb"])
        out.append(frame(0x33, 0x05, 0x0D, r, g, b, 0x00, 0x00))
    elif "kelvin" in w:
        k = int(w["kelvin"])
        out.append(frame(0x33, 0x05, 0x0D, 0xFF, 0xFF, 0xFF, (k >> 8) & 0xFF, k & 0xFF))
    return out


def effect_frames(code: int, param: bytes) -> list[bytes]:
    """A Govee library scene: its parameter blob as a3 frames (the first carries the frame count, the
    last is indexed ff), then 33 05 04 <code, little-endian> starts it. Same encoding the Govee app uses."""
    out = []
    if param:
        stream, count, last = bytearray(b"\xa3\x00\x01\x00\x02"), 0, 1
        for byte in param:
            if len(stream) % 19 == 0:
                count += 1
                stream.append(0xA3)
                last = len(stream)
                stream.append(count)
            stream.append(byte)
        stream[last] = 0xFF
        stream[3] = count + 1
        out += [bytes(stream[i:i + 19]) for i in range(0, len(stream), 19)]
    out.append(b"\x33\x05\x04" + int(code).to_bytes(2, "little"))
    return [frame(*f) for f in out]


PHONE_SCENE = 8217  # "the scene data uploaded last" (by the phone app or by us)


def scene_frames(data: bytes) -> list[bytes]:
    """Scene data as the Govee phone app sends it to an H600D (captured from the phone's Bluetooth log):
    a3 00 01 <frame count> 01 <data...>, a3 01 <data...>, ..., the last one indexed a3 ff; at least
    two frames, and an empty a3 ff when the data fills the others exactly. Start it with
    33 05 04 19 20 (code 8217)."""
    body = bytes([0x01, 0x00, 0x01]) + bytes(data)
    n = max(2, len(body) // 17 + 1)
    out = []
    for i in range(n):
        chunk = body[i * 17:(i + 1) * 17]
        if i == 0:
            chunk = bytes([0x01, n, 0x01]) + chunk[3:]
        out.append(frame(0xA3, 0xFF if i == n - 1 else i, *chunk))
    return out


def gradient_data(colors, brightness: int = 100, tempo: int = 255, pace: int = 0xEB) -> bytes:
    """A smooth colour-cycle scene (mode 83), worked out by testing on the bulbs:
    83 01 | ff (on) | pace (lower = much slower; 0xEB ~17 s a round, 0xB0 minutes) | brightness 0-255
    | tempo (1 ~5 s .. 255 ~16 s a round) | 00 | number of colours | R G B ..."""
    head = [0x83, 0x01, 0xFF, int(pace) & 0xFF, round(max(1, min(100, int(brightness))) * 2.55),
            max(1, min(255, int(tempo))), 0x00, len(colors)]
    return bytes(head) + b"".join(bytes(c) for c in colors)


class _Link:
    """One bulb: its connection, request/answer matching and last known state."""

    def __init__(self, address: str):
        self.address = address
        self.client: BleakClient | None = None
        self.lock = asyncio.Lock()  # one request at a time per bulb
        self.connect_lock = asyncio.Lock()
        self.waiters: list[tuple[bytes, asyncio.Future]] = []
        self.state: dict | None = None
        self.state_t = 0.0
        self.effect: str | None = None  # name of the library scene we started (bulbs barely answer while it runs)
        self.switching = False  # an effect is being started: the keep-alive leaves this bulb alone
        self.sent_param = False  # this connection already carried one scene-data upload
        self.error = ""
        self.fails = 0
        self.next_try = 0.0

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def on_notify(self, _char, data: bytearray) -> None:
        data = bytes(data)
        for i, (prefix, fut) in enumerate(self.waiters):
            if data.startswith(prefix) and not fut.done():
                fut.set_result(data)
                del self.waiters[i]
                return

    def lost(self) -> None:
        self.client = None
        for _, fut in self.waiters:
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))
        self.waiters.clear()

    async def request(self, data: bytes, timeout: float = 2.0) -> bytes:
        """Write a frame and wait for the notification that answers it (same first two bytes)."""
        async with self.lock:
            if not self.connected:
                raise ConnectionError("not connected")
            fut = asyncio.get_running_loop().create_future()
            entry = (data[:2], fut)
            self.waiters.append(entry)
            try:
                await self.client.write_gatt_char(WRITE_UUID, data, response=False)
                return await asyncio.wait_for(fut, timeout)
            finally:
                if entry in self.waiters:
                    self.waiters.remove(entry)


class BleTransport:
    OFFLINE_AFTER = 2  # failed connection attempts in a row: show the bulb as off / out of range
    KEEPALIVE_EVERY = 2.5  # seconds; bulbs drop idle links after ~12 s
    CONNECT_TIMEOUT = 25.0  # alone ~8 s for the weakest bulb; Windows queues parallel connects (up to ~25 s)

    def __init__(self, keepalive: bool = False):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="bluetooth", daemon=True).start()
        self.links: dict[str, _Link] = {}
        self.seen: dict[str, dict] = {}  # address -> {"name", "rssi", "t"} from discovery scans
        self.watch: set[str] = set()  # addresses kept connected (desktop app)
        if keepalive:
            self._call_soon(self._keepalive_loop())
            self._call_soon(self._reconnect_loop())

    # ---- plumbing ------------------------------------------------------------
    def _call(self, coro, timeout: float = 90.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _call_soon(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _link(self, address: str) -> _Link:
        address = address.upper()
        if address not in self.links:
            self.links[address] = _Link(address)
        return self.links[address]

    # ---- connecting -------------------------------------------------------------
    async def _connect(self, link: _Link) -> bool:
        async with link.connect_lock:
            if link.connected:
                return True
            dev = BLEDevice(link.address, None, _Raw(_Adv(int(link.address.replace(":", ""), 16)), None))
            client = BleakClient(dev, timeout=self.CONNECT_TIMEOUT, winrt={"use_cached_services": True},
                                 disconnected_callback=lambda _c: link.lost())
            try:
                await client.connect()
                await client.start_notify(NOTIFY_UUID, link.on_notify)
            except Exception as e:  # noqa: BLE001 - any Bluetooth failure means "not reachable now"
                link.error = NOT_FOUND if isinstance(e, (TimeoutError, asyncio.TimeoutError)) \
                    else f"connection failed ({type(e).__name__}: {e})"
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                return False
            link.client, link.error, link.fails, link.sent_param = client, "", 0, False
            return True

    async def _ready(self, link: _Link, connect: bool) -> bool:
        return link.connected or (connect and await self._connect(link))

    # ---- reading and writing ------------------------------------------------------
    async def _read_state(self, link: _Link) -> dict:
        last = link.state or {}
        p = await link.request(frame(0xAA, 0x01))
        try:
            b = (await link.request(frame(0xAA, 0x04)))[2]
            c = await link.request(frame(0xAA, 0x05, 0x01))
        except asyncio.TimeoutError:  # answers on/off but not the rest: busy starting a scene
            b, c = last.get("brightness") or 100, None
        if c is not None and c[2] == 0x0D:
            k = (c[6] << 8) | c[7]
            st = {"on": p[2] == 1, "brightness": b, "mode": "white" if k else "color",
                  "rgb": [c[3], c[4], c[5]], "kelvin": k, "effect": None}
            link.effect = None
        else:  # a library scene is playing: "aa 05 04 <code lo> <code hi>" says which one
            code = c[3] | (c[4] << 8) if c is not None and c[2] == 0x04 else None
            st = {"on": p[2] == 1, "brightness": b, "mode": "scene", "rgb": None, "kelvin": 0,
                  "effect": link.effect, "effect_code": code}
        link.state, link.state_t = st, time.monotonic()
        return st

    async def _play_effect(self, link: _Link, effect: dict) -> bool:
        link.switching = effect["name"]
        try:
            return await self._play_effect_inner(link, effect)
        finally:
            link.switching = False

    async def _upload(self, link: _Link, effect: dict) -> bool:
        """Fresh link, a plain command, the scene data, ~3 s of silence, then check the reported code."""
        if link.connected:
            try:
                await link.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            link.lost()
        await asyncio.sleep(1.0)
        if not await self._connect(link) or not await self._command(link, frame(0x33, 0x01, 0x01)):
            return False
        data = effect_frames(effect["code"], effect["param"])
        async with link.lock:
            try:
                for f in data[:-1]:
                    await link.client.write_gatt_char(WRITE_UUID, f, response=False)
                    await asyncio.sleep(0.05)
                for _ in range(2):
                    await link.client.write_gatt_char(WRITE_UUID, data[-1], response=False)
                    await asyncio.sleep(0.15)
            except Exception:  # noqa: BLE001 - dropped link
                return False
            await asyncio.sleep(3.0)  # quiet: questions now make the bulb choke
        return await self._wait_for(link, lambda c: c[2] == 0x04 and (c[3] | (c[4] << 8)) == int(effect["code"]), 6)

    async def _wait_for(self, link: _Link, ok, tries: int) -> bool:
        for _ in range(tries):
            if not link.connected and not await self._connect(link):
                await asyncio.sleep(1.0)
                continue
            try:
                if ok(await link.request(frame(0xAA, 0x05, 0x01), timeout=1.5)):
                    return True
            except Exception:  # noqa: BLE001 - busy
                pass
            await asyncio.sleep(0.8)
        return False

    async def _play_effect_inner(self, link: _Link, effect: dict) -> bool:
        """Start a library scene. Measured on H600D:
        - a scene without data (Reading, Study, ...) starts at once from any state;
        - a scene with data starts reliably only from a plain colour, on a fresh link, followed by
          ~3 s of silence; the first upload right after leaving another scene is usually rejected
          (the bulb drops the link and stays in colour), the second one then works."""
        want = int(effect["code"])
        try:
            c = await link.request(frame(0xAA, 0x05, 0x01))
            if c[2] == 0x04 and (c[3] | (c[4] << 8)) == want:
                link.effect = effect["name"]
                return True
            playing = c[2] != 0x0D
        except Exception:  # noqa: BLE001 - too busy to answer: most likely playing a scene
            playing = True
        if not effect["param"]:
            async with link.lock:
                for _ in range(2):
                    await link.client.write_gatt_char(WRITE_UUID, effect_frames(want, b"")[-1], response=False)
                    await asyncio.sleep(0.15)
            ok = await self._wait_for(link, lambda c: c[2] == 0x04 and (c[3] | (c[4] << 8)) == want, 6)
        else:
            if playing:  # leave the old scene: off, on, a colour; then wait for colour mode
                for f in (frame(0x33, 0x01, 0x00), frame(0x33, 0x01, 0x01),
                          frame(0x33, 0x05, 0x0D, 0xFF, 0xFF, 0xFF, 0x0A, 0x8C)):
                    await self._command(link, f)
                    await asyncio.sleep(0.4)
                await self._wait_for(link, lambda c: c[2] == 0x0D, 6)
            ok = await self._upload(link, effect) or await self._upload(link, effect)  # at most one retry
        if ok:
            link.effect = effect["name"]
        return ok

    async def _read_many(self, addresses: list[str], max_age: float, connect: bool) -> dict:
        async def one(a):  # each bulb is read as soon as it is connected: idle links drop after ~12 s
            link = self._link(a)
            if link.connected and link.state and time.monotonic() - link.state_t <= max_age:
                return link.state
            if not await self._ready(link, connect):
                return None
            try:
                return await self._read_state(link)
            except Exception as e:  # noqa: BLE001
                link.error = f"no answer ({type(e).__name__})"
                return None

        results = await asyncio.gather(*(one(a) for a in addresses))
        return {a.upper(): st for a, st in zip(addresses, results)}

    async def _command(self, link: _Link, data: bytes) -> bool:
        """Send one frame and wait for the bulb's acknowledgement; reconnect and retry if needed."""
        for _ in range(3):
            if not link.connected and not await self._connect(link):
                return False
            try:
                await link.request(data)
                return True
            except Exception:  # noqa: BLE001 - timeout or dropped link: retry
                await asyncio.sleep(0.15)
        return False

    async def _start_together(self, plan: dict[str, dict], connect: bool) -> dict:
        """Several bulbs, the same data-less scene (Alarm, Reading, ...): stop the old scenes with a
        colour, then send the start frame to all of them at the same moment, so they run in sync."""
        effect = next(iter(plan.values()))["effect"]
        links = [self._link(a) for a in plan]
        ok = await asyncio.gather(*(self._ready(link, connect) for link in links))
        live = [link for link, up in zip(links, ok) if up]
        stop = frame(0x33, 0x05, 0x0D, 0xFF, 0xFF, 0xFF, 0x0A, 0x8C)
        for link in live:
            link.switching = effect["name"]
        try:
            await asyncio.gather(*(self._command(link, frame(0x33, 0x01, 0x01)) for link in live))
            await asyncio.gather(*(self._command(link, stop) for link in live))
            await asyncio.sleep(1.0)
            start = effect_frames(effect["code"], b"")[-1]
            for _ in range(2):
                await asyncio.gather(*(link.client.write_gatt_char(WRITE_UUID, start, response=False)
                                       for link in live if link.connected), return_exceptions=True)
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.0)
            want = int(effect["code"])
            done = await asyncio.gather(*(self._wait_for(
                link, lambda c: c[2] == 0x04 and (c[3] | (c[4] << 8)) == want, 4) for link in live))
        finally:
            for link in live:
                link.switching = False
        out = {}
        for link in links:
            if link not in live:
                out[link.address] = link.error or NOT_FOUND
            elif done[live.index(link)]:
                link.effect, out[link.address] = effect["name"], None
            else:
                out[link.address] = "did not confirm the effect"
        return out

    async def _start_uploaded(self, plan: dict[str, dict], connect: bool) -> dict:
        """Scenes with data (the phone's and our own): every bulb gets its data, then all of them the
        start frame at the same moment, so a group runs in sync. Tested on H600D: works from any
        state (colour or another scene) on the open link, no need to stop the old scene first."""
        links = {a: self._link(a) for a in plan}
        up = await asyncio.gather(*(self._ready(link, connect) for link in links.values()))
        out = {a: (None if ok else (links[a].error or NOT_FOUND)) for a, ok in zip(links, up)}

        async def prepare(a):
            link, w = links[a], plan[a]
            link.switching = w["effect"]["name"]
            for data in frames_for(w):  # on, brightness
                if not await self._command(link, data):
                    return "did not confirm the command"
            try:
                async with link.lock:
                    for f in w["effect"]["frames"]:
                        await link.client.write_gatt_char(WRITE_UUID, f, response=False)
                        await asyncio.sleep(0.01)
            except Exception:  # noqa: BLE001 - dropped link
                return "lost the connection while sending the scene"
            return None

        live = [a for a in plan if out[a] is None]
        try:
            for a, err in zip(live, await asyncio.gather(*(prepare(a) for a in live))):
                out[a] = err
            ready = [links[a] for a in live if out[a] is None and links[a].connected]
            start = frame(0x33, 0x05, 0x04, PHONE_SCENE & 0xFF, PHONE_SCENE >> 8)
            await asyncio.gather(*(link.client.write_gatt_char(WRITE_UUID, start, response=False)
                                   for link in ready), return_exceptions=True)
            await asyncio.sleep(1.0)
            done = await asyncio.gather(*(self._wait_for(
                link, lambda c: c[2] == 0x04 and (c[3] | (c[4] << 8)) == PHONE_SCENE, 4) for link in ready))
            for link, ok in zip(ready, done):
                if ok:
                    link.effect = plan[link.address]["effect"]["name"]
                else:
                    out[link.address] = "did not confirm the effect"
        finally:
            for link in links.values():
                link.switching = False
        return out

    async def _write_many(self, plan: dict[str, dict], connect: bool) -> dict:
        if any((w.get("effect") or {}).get("frames") for w in plan.values()):
            uploads = {a: w for a, w in plan.items()
                       if (w.get("effect") or {}).get("frames") and w.get("on") is not False}
            rest = {a: w for a, w in plan.items() if a not in uploads}
            out = await self._start_uploaded(uploads, connect) if uploads else {}
            if rest:
                out.update(await self._write_many(rest, connect))
            return out
        effects = {(w.get("effect") or {}).get("code") for w in plan.values()}
        if (len(plan) > 1 and len(effects) == 1 and None not in effects
                and not next(iter(plan.values()))["effect"]["param"]
                and all(set(w) <= {"effect", "on"} for w in plan.values())):
            return await self._start_together(plan, connect)
        restart: dict[_Link, int] = {}  # bulbs whose scene must be restarted after a brightness change

        async def one(a, w):
            link = self._link(a)
            if not await self._ready(link, connect):
                return link.error or NOT_FOUND
            if w.get("on") is False or "rgb" in w or "kelvin" in w:
                link.effect = None  # these end a running scene
            elif "brightness" in w and not w.get("effect"):
                # measured on H600D: a brightness change freezes a running scene on its current colour;
                # starting the scene again (no new data) keeps it moving at the new brightness
                st = link.state
                if st is None:
                    try:
                        st = await self._read_state(link)
                    except Exception:  # noqa: BLE001
                        st = None
                if st and st.get("mode") == "scene" and st.get("on"):
                    restart[link] = st.get("effect_code") or PHONE_SCENE
            for data in frames_for(w):
                if not await self._command(link, data):
                    restart.pop(link, None)
                    return "did not confirm the command"
            if w.get("effect") and w.get("on") is not False:
                # sent once: retrying a bulb that choked on a scene made it flicker between scenes
                if not (link.connected or await self._connect(link)) or not await self._play_effect(link, w["effect"]):
                    return ("did not confirm the effect - if it keeps playing an old one, "
                            "switch it off and on at the wall")
            return None

        errors = await asyncio.gather(*(one(a, w) for a, w in plan.items()))
        if restart:  # all at the same moment, so a group stays in sync
            await asyncio.sleep(0.3)
            await asyncio.gather(*(link.client.write_gatt_char(
                WRITE_UUID, frame(0x33, 0x05, 0x04, code & 0xFF, code >> 8), response=False)
                for link, code in restart.items() if link.connected), return_exceptions=True)
        return {a.upper(): e for a, e in zip(plan, errors)}

    # ---- keeping links alive (desktop app) ------------------------------------------
    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.KEEPALIVE_EVERY)
            live = [self._link(a) for a in self.watch if self._link(a).connected and not self._link(a).switching]
            await asyncio.gather(*(self._read_state(link) for link in live), return_exceptions=True)

    async def _reconnect_loop(self) -> None:
        async def retry(link: _Link):
            if await self._connect(link):
                await self._read_many([link.address], 0, False)
            else:
                link.fails += 1
                link.next_try = time.monotonic() + min(30, 3 * link.fails)

        while True:
            now = time.monotonic()
            due = [self._link(a) for a in self.watch if not self._link(a).connected and self._link(a).next_try <= now]
            await asyncio.gather(*(retry(link) for link in due))
            await asyncio.sleep(1)

    # ---- discovery ----------------------------------------------------------------------
    async def _scan(self, seconds: float) -> None:
        def seen(dev, adv):
            self.seen[dev.address.upper()] = {"name": adv.local_name or dev.name or "", "rssi": adv.rssi,
                                              "t": time.monotonic()}

        async with BleakScanner(detection_callback=seen):
            await asyncio.sleep(seconds)

    # ---- public, blocking API ---------------------------------------------------------
    def scan(self, seconds: float = 8.0) -> list[dict]:
        """Govee bulbs advertising nearby: [{address, name, sku, rssi}] (connected bulbs do not advertise)."""
        t0 = time.monotonic()
        self._call(self._scan(seconds), timeout=seconds + 30)
        out = []
        for a, s in self.seen.items():
            m = NAME_RE.match(s["name"])
            if s["t"] >= t0 and m:
                out.append({"address": a, "name": s["name"], "sku": m.group(1).upper(), "rssi": s["rssi"]})
        return sorted(out, key=lambda d: -d["rssi"])

    def read(self, addresses: list[str], max_age: float = 0.0, connect: bool = True) -> dict:
        """{address: {on, brightness, mode, rgb, kelvin} or None if unreachable}."""
        return self._call(self._read_many(list(addresses), max_age, connect))

    def write(self, plan: dict[str, dict], connect: bool = True) -> dict:
        """Apply {address: want}; returns {address: None (confirmed) or an error text}."""
        return self._call(self._write_many(plan, connect))

    def info(self, address: str) -> dict:
        link = self._link(address)
        s = self.seen.get(link.address)
        return {"connected": link.connected, "rssi": s["rssi"] if s else None,
                "connecting": not link.connected and link.connect_lock.locked(),
                "offline": not link.connected and link.fails >= self.OFFLINE_AFTER,
                "switching": link.switching or None,
                "error": "" if link.connected else (link.error or NOT_FOUND)}

    def retry_now(self, address: str) -> None:
        """Try this bulb again right away instead of waiting for the background back-off."""
        self._link(address).next_try = 0.0

    def keep_connected(self, addresses) -> None:
        self.watch = {a.upper() for a in addresses}

    def close(self) -> None:
        self.watch = set()  # stop keep-alive and reconnects first

        async def disconnect_all():
            await asyncio.gather(*(link.client.disconnect() for link in self.links.values() if link.connected),
                                 return_exceptions=True)

        try:
            self._call(disconnect_all(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
