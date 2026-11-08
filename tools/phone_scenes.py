"""Copy the scenes you played from the Govee phone app into effects_local.json.

1. Android: Developer options -> "Enable Bluetooth HCI snoop log" (full), then restart the phone.
2. Close this program, open the phone app, tap the scenes you like (a few seconds each), close the app.
3. adb bugreport bugreport.zip   (USB debugging on)
4. python tools/phone_scenes.py bugreport.zip            (or a btsnoop_hci.log file)

Each scene upload the phone sent to a bulb (a3 frames followed by 33 05 04 19 20) becomes an entry
"Phone 1", "Phone 2", ... in the order you tapped them; rename them in effects_local.json.
Uploads that were identical for several bulbs are stored once.
"""
from __future__ import annotations

import json
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_IN_ZIP = "FS/data/log/bt/btsnoop_hci.log"


def read_log(path: Path) -> bytes:
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.endswith("btsnoop_hci.log")]
        if not names:
            sys.exit("no btsnoop_hci.log in that bug report - is the HCI snoop log on, and was the phone restarted?")
        return z.read(LOG_IN_ZIP if LOG_IN_ZIP in names else names[0])
    return path.read_bytes()


def att_writes(raw: bytes):
    """(connection handle, value) for every ATT write the phone sent, in order."""
    if raw[:8] != b"btsnoop\0":
        sys.exit("not a btsnoop log")
    off, frag = 16, {}
    while off + 24 <= len(raw):
        _olen, ilen, flags, _drops, _ts = struct.unpack(">IIIIq", raw[off:off + 24])
        pkt = raw[off + 24: off + 24 + ilen]
        off += 24 + ilen
        if len(pkt) < 5 or pkt[0] != 0x02 or flags & 1:  # outgoing ACL data only
            continue
        hflags = struct.unpack_from("<H", pkt, 1)[0]
        handle, pb = hflags & 0x0FFF, (hflags >> 12) & 0x3
        frag[handle] = pkt[5:] if pb in (0, 2) else frag.get(handle, b"") + pkt[5:]
        buf = frag[handle]
        if len(buf) < 4:
            continue
        l2len, cid = struct.unpack_from("<HH", buf, 0)
        if len(buf) < 4 + l2len:
            continue
        att = frag.pop(handle)[4:4 + l2len]
        if cid == 4 and att and att[0] in (0x12, 0x52) and len(att) == 23:  # write (command), 20-byte value
            yield handle, att[3:].hex()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    pending: dict[int, list[str]] = {}
    scenes: list[list[str]] = []
    for handle, value in att_writes(read_log(Path(argv[0]))):
        if value.startswith("a3"):
            if value.startswith("a300"):
                pending[handle] = []
            pending.setdefault(handle, []).append(value)
        elif value.startswith("3305041920") and pending.get(handle):
            frames = pending.pop(handle)
            if frames[-1].startswith("a3ff") and frames not in scenes:
                scenes.append(frames)
    if not scenes:
        print("no scene uploads found")
        return 1
    path = ROOT / "effects_local.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    phone = data.setdefault("categories", {}).setdefault("Phone", [])
    known = {tuple(e.get("frames", [])) for e in phone}
    n = len(phone)
    for frames in scenes:
        if tuple(frames) not in known:
            n += 1
            phone.append({"name": f"Phone {n}", "frames": frames})
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(scenes)} scene(s) found; effects_local.json now has {len(phone)} phone scene(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
