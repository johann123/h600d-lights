# H600D lights: Bluetooth control for Govee H600D bulbs

A small Windows desktop window and command line for **Govee H600D** RGBWW bulbs, over Bluetooth only:
on/off, brightness, colour, white temperature, groups, saved scenes and slow, smooth moving effects
that start in sync on every bulb of a group. No Govee account, no cloud, no Wi-Fi, no server, no
Home Assistant.

> **Unofficial.** Not affiliated with, endorsed by or supported by Govee. "Govee" is a trademark of its
> owner and is used here only to say which bulbs this works with. The protocol was worked out from
> the author's own bulbs and from the Bluetooth traffic of the author's own phone, for interoperability.

## Compatibility

- **Tested:** Govee H600D, firmware 1.00.14, Windows 11, Python 3.13, `bleak` 2.1.
- **Probably works** (discovery, connection, on/off, brightness): other Govee H6xxx Bluetooth bulbs.
- **Not checked:** colour/white on other models (some older ones use a different colour command),
  and the effects. Reports from other models are welcome.
- The code is Windows-oriented (desktop window, launcher, taskbar pinning). The Bluetooth and CLI parts
  use `bleak` and could run elsewhere, but that is untested.

## Install

```
pip install bleak
python govee.py discover          finds the bulbs nearby and writes bulbs.json
python govee.py app               opens the window
```

Optional: `powershell -ExecutionPolicy Bypass -File make_shortcut.ps1` builds a small launcher
(`Govee lights.exe`) and puts **Govee lights** shortcuts on the desktop and in the Start menu, so the
window can be pinned to the taskbar with its own icon.

Close the Govee phone app while using this: a bulb accepts only one Bluetooth connection at a time.

## Window

Groups sit at the top, every bulb below. Click a bulb's name to rename it, pick its group, or use
Identify to flash it. Right-click a scene button to delete it. Bulbs appear as they connect (a weak
bulb can take up to ~25 s). A bulb that cannot be reached twice in a row (switched off at the wall,
out of range) shows "switched off or out of range" with a **Try now** button; it reconnects by itself
when it is back. Closing the window lets go of the bulbs. If the window is already open, starting it
again brings it to the screen you are on.

## Command line

```
govee                         status of all bulbs
govee on down                 on/off: all, a group, a bulb name, or "Down left,Up right"
govee off all
govee bright up 40            brightness 1..100
govee color down orange       #ff8800, "255,136,0" or a name (red, blue, pink, ...)
govee white all 2700 60       white 2700 (warm) .. 6500 (cool), optional brightness
govee set down -b 30 -k 3000  several changes at once (--on/--off, -b, -c, -k, -e)
govee effects                 list effects;  govee effect down "neon dusk"  plays one
govee scene                   list scenes;  govee scene relax  applies one
govee scene save evening      save how every bulb looks right now (-t down: only those)
govee scene delete evening
govee blink "Down left"       flash a bulb to see which one it is
govee name "Down left" Sofa   rename;  govee group Sofa down  (or: none)
govee discover                look for new bulbs
```

Each command connects, does its job, checks the result by reading the bulbs back, and lets go
(about 3–25 s). While the window is open, commands take about 1.5 s, because Windows lets them reuse
the window's connections. A `!` line means a bulb did not do it or could not be reached.

## Effects

The Effect list (window) or `govee effect <target> <name>` plays a moving scene. On a group, every
bulb gets the scene data first and then the start signal at the same moment, so they run in sync.

Included: 29 slow colour cycles without flashing or sudden jumps, in the categories Cyberpunk, Nature,
Cozy, Calm (with **Lullaby**, a barely visible night light that sets the bulb to 1%) and Soft party.
**Last phone scene** restarts whatever scene data the bulb received last.

Brightness works during an effect too (slider or `govee bright`). On its own, a brightness change
freezes an H600D scene on its current colour, so the program starts the scene again right after it,
on every bulb of the group at the same moment.

**Make your own** in `effects.json` (shipped) or `effects_local.json` (yours), under any category:

```json
{"categories": {"Mine": [
  {"name": "My scene", "colors": ["#6000ff", "#2800c0", "#b000b0"], "brightness": 40, "tempo": 255, "pace": 208}
]}}
```

- `colors`: the colours, cycled in order and back to the first (tested with 1–6). Neighbouring
  shades keep it gentle.
- `brightness`: 1–100, the effect's own brightness, on top of the bulb's brightness.
- `tempo`: 1 (about 5 s a round) to 255 (about 16 s).
- `pace`: an extra slowdown: 235 normal, 208 slow, 176 a few minutes a round, below ~128 nearly still.
- `bulb_brightness` (optional): sets the bulb to this brightness when the effect starts (e.g. 1).
  A brightness given at the same time wins.

The bulb changes colour in small steps, not perfectly smoothly. At very low brightness the steps
are easier to see; very close shades hide them best.

**Scenes from the Govee phone app** can be copied too (Android):

1. Developer options → *Enable Bluetooth HCI snoop log* (full), then restart the phone.
2. Close this program, open the phone app, tap the scenes you like a few seconds apart, close the app.
3. `adb bugreport bugreport.zip`
4. `python tools/phone_scenes.py bugreport.zip` adds them to `effects_local.json` as "Phone 1",
   "Phone 2", ... in the order you tapped them. Rename them there.

The bulbs report every uploaded scene with the same code (8217), so after reopening the window a
running effect shows as "Last phone scene" until you pick one again.

## Files

- `bulbs.json` (created by `discover`): names, groups and Bluetooth addresses; also editable from
  the window or the CLI. `"settings": {"groups": [...]}` holds the group names.
- `scenes.json` (created on first run): scenes. Keys are `all`, a group or a bulb name; later
  entries override earlier ones:

```json
{
  "movie": {"down": {"on": true, "brightness": 12, "color": "#ff5a1e"}, "up": {"on": false}},
  "evening": {"all": {"brightness": 40, "kelvin": 2700}, "Down middle": {"color": "purple"}}
}
```

- `effects.json`: the shipped effects. `effects_local.json`: your own.
- `app_error.log`: appears only if the window ever hits an error.
- Set `GOVEE_HOME` to keep `bulbs.json`, `scenes.json` and `effects_local.json` in another folder.

## Tests (no bulbs needed)

- `python tests/test_govee.py`: checks against simulated bulbs (names, groups, control, retries,
  effects, scenes, CLI).
- `python tests/test_app.py`: opens the window for ~15 s and clicks through it.
- `python tests/demo_app.py`: the window with simulated bulbs and a red DEMO banner
  (`--offline` shows a switched-off bulb).

## Protocol (as observed on H600D, firmware 1.00.14)

Write 20-byte frames to `00010203-0405-0607-0809-0a0b0c0d2b11` (write without response); the last
byte is the XOR of the first 19. Answers arrive as notifications on `...2b10`.

| Frame | Meaning |
|---|---|
| `33 01 01` / `33 01 00` | on / off |
| `33 04 <1-100>` | brightness (freezes a running scene; start it again afterwards) |
| `33 05 0d R G B 00 00` | colour |
| `33 05 0d FF FF FF Kh Kl` | white, Kelvin (2700–6500, clamped by the bulb) |
| `aa 01` / `aa 04` / `aa 05 01` | read power / brightness / mode: `aa 05 0d R G B Kh Kl` (Kelvin 0 = colour) or `aa 05 04 <code lo hi>` (scene) |
| `a3 00 01 <n> 01 <data>`, `a3 01 <data>`, …, `a3 ff <data>` | scene data in n frames, as the phone app sends it (at least 2 frames; an empty `a3 ff` when the data fills the others) |
| `33 05 04 19 20` | start the uploaded scene (code 8217) |

Smooth colour-cycle scene data, worked out by testing:
`83 01 ff <pace> <brightness 0-255> <tempo> 00 <number of colours> <R G B>...`
(the `ff` switches it on; `00` gives a dark bulb). Other phone scenes start with `85` or `93` and
are layered (per layer roughly `ff <pace> <brightness> <tempo> 00 <R G B>`); they are replayed as captured.

Other observations: an idle connection drops after about 12 s, so the window reads the bulbs every
2.5 s; Windows connects most reliably by address with cached services, without scanning first.

## License

MIT, see `LICENSE`.
