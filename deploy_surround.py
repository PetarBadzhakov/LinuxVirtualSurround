#!/usr/bin/env python3
"""
pw-surround - HRTF virtual surround for PipeWire.

Creates a virtual 7.1 (or 5.1) sink that games, Proton and media players see as a
real surround card, and binaurally downmixes it to stereo headphones using a
measured HRTF dataset.

This is the Linux equivalent of Windows Sonic / Dolby Atmos for Headphones /
DTS Headphone:X, built on PipeWire's built-in `sofa` spatializer -- no external
DSP host, no LADSPA/LV2 plugins, no patched PipeWire.

Requires: PipeWire >= 0.3.60 with the SOFA filter-graph plugin
          (/usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-sofa.so)

Standard library only. Run `deploy_surround.py doctor` to check your system.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

APP = "pw-surround"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP
HRTF_DIR = DATA_DIR / "hrtf"
STATE_FILE = DATA_DIR / "state.json"

CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "pipewire/filter-chain.conf.d"
)
CONFIG_FILE = CONFIG_DIR / "50-pw-surround.conf"

# Global PipeWire client tweak used by --no-upmix; kept in its own file so
# `uninstall` can remove it again without touching anything else.
CLIENT_CONF_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pipewire/client-rt.conf.d"
)
UPMIX_FILE = CLIENT_CONF_DIR / "50-pw-surround-no-upmix.conf"

SERVICE = "filter-chain.service"
SOFA_PLUGIN = Path("/usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-sofa.so")

SINK_NODE_NAME = "pw_surround"
SINK_DESCRIPTION = "Virtual Surround (HRTF)"

# --------------------------------------------------------------------------
# HRTF datasets
#
# sha256 values below were verified against a real download. `None` means the
# upstream file is not pinned -- it will be fetched but not integrity-checked.
# --------------------------------------------------------------------------

DATASETS = {
    "sadie-ku100-dfc": {
        "desc": "SADIE/Google Neumann KU100, diffuse-field corrected, 1550 pts, 48 kHz",
        "note": "Least spectral coloration. Best default: sharp imaging, stays neutral on music.",
        "url": "https://www.york.ac.uk/sadie-project/Resources/Binaural/SOFA/"
               "SADIE_KU100_DFC_256_order_fir_48000.sofa",
        "sha256": "6822b4318ce80ab6b8421a4b55b1edcedde1b276426aa5416526a57cccbe7e58",
        "filename": "sadie_ku100_dfc_48000.sofa",
    },
    "sadie2-ku100": {
        "desc": "SADIE II subject D1, Neumann KU100, 8802 pts, 48 kHz",
        "note": "Highest spatial resolution (same dataset as the OpenAL Soft SADIE .mhr). "
                "Not diffuse-field corrected, so slightly more coloured than the DFC set.",
        "url": "https://zenodo.org/records/12092466/files/D1_HRIR_SOFA.zip?download=1",
        "sha256": "366321fa78f211bacc0ec6bea96701625b196a5db54ec16748b0eab0b9705f75",
        "archive_member": "D1_HRIR_SOFA/D1_48K_24bit_256tap_FIR_SOFA.sofa",
        "member_sha256": "9af7cb19531e52fb7ae8ec92621e6ab62b1d5fe584b3742be36699a0ddb0ccd4",
        "filename": "D1_48K_24bit_256tap_FIR_SOFA.sofa",
    },
    "sadie2-kemar": {
        "desc": "SADIE II subject D2, KEMAR dummy head, 48 kHz",
        "note": "Different head/pinna geometry. Try it if KU100 imaging feels off for you.",
        "url": "https://zenodo.org/records/12092466/files/D2_HRIR_SOFA.zip?download=1",
        "sha256": None,
        "archive_member": "D2_HRIR_SOFA/D2_48K_24bit_256tap_FIR_SOFA.sofa",
        "member_sha256": None,
        "filename": "D2_48K_24bit_256tap_FIR_SOFA.sofa",
    },
}

DEFAULT_DATASET = "sadie-ku100-dfc"

# --------------------------------------------------------------------------
# Speaker layouts
#
# Azimuth follows PipeWire's SOFA convention, verified empirically against the
# HRIR data: 0 = straight ahead, 90 = LEFT, 180 = behind, 270 = RIGHT.
# (At 90 the measured ITD is -812 us with +15.3 dB in the left ear.)
#
# Angles match the ITU-R BS.775 / SMPTE positions that games actually pan for.
# Widening them does not add accuracy, it just decorrelates the mix further.
# --------------------------------------------------------------------------

LAYOUTS = {
    "7.1": {
        "desc": "8 channels, ITU/SMPTE angles - what Proton and most engines expect",
        "channels": ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"],
        "angles": {"FL": 30, "FR": 330, "FC": 0, "RL": 150, "RR": 210, "SL": 90, "SR": 270},
    },
    "7.1-wide": {
        "desc": "8 channels, exaggerated separation - more dramatic, less faithful",
        "channels": ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"],
        "angles": {"FL": 40, "FR": 320, "FC": 0, "RL": 145, "RR": 215, "SL": 100, "SR": 260},
    },
    "5.1": {
        "desc": "6 channels - use if a game misbehaves when offered 7.1",
        "channels": ["FL", "FR", "FC", "LFE", "SL", "SR"],
        "angles": {"FL": 30, "FR": 330, "FC": 0, "SL": 110, "SR": 250},
    },
}

DEFAULT_LAYOUT = "7.1"

# HeSuVi 14-channel WAV channel order (matches PipeWire's upstream example).
HESUVI_MAP = {
    "FL": (0, 1), "SL": (2, 3), "RL": (4, 5), "FC": (6, 13),
    "FR": (8, 7), "SR": (10, 9), "RR": (12, 11),
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class Fail(Exception):
    """User-facing error."""


def run(cmd, check=False, quiet=True):
    return subprocess.run(
        cmd,
        capture_output=quiet,
        text=True,
        check=check,
    )


def have(binary):
    return shutil.which(binary) is not None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def download(url, dest):
    """Download with a progress indicator, atomically."""
    print(f"  fetching {url.split('/')[-1].split('?')[0]}")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/1.0"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    tty = sys.stdout.isatty()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = last = 0
            with open(tmp, "wb") as out:
                while True:
                    buf = resp.read(1 << 16)
                    if not buf:
                        break
                    out.write(buf)
                    done += len(buf)
                    # Redrawing every chunk is fine on a terminal but turns into
                    # thousands of lines when the output is piped or logged.
                    if total and tty:
                        pct = done * 100 // total
                        print(f"\r  {pct:3d}%  {done >> 20:4d}/{total >> 20} MiB", end="")
                    elif total and done - last > (8 << 20):
                        last = done
                        print(f"  {done * 100 // total:3d}%")
        if tty:
            print()
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise Fail(f"download failed: {exc}") from exc
    tmp.replace(dest)


def ensure_dataset(key):
    """Download + unpack a dataset if needed. Returns the .sofa path."""
    if key.startswith("hesuvi:"):
        path = Path(key.split(":", 1)[1]).expanduser().resolve()
        if not path.is_file():
            raise Fail(f"HeSuVi file not found: {path}")
        return path

    if key not in DATASETS:
        raise Fail(f"unknown dataset '{key}'. Try: {', '.join(DATASETS)}")

    spec = DATASETS[key]
    HRTF_DIR.mkdir(parents=True, exist_ok=True)
    final = HRTF_DIR / spec["filename"]
    if final.is_file():
        return final

    print(f"installing dataset '{key}'")
    is_zip = "archive_member" in spec
    staged = HRTF_DIR / (spec["filename"] + (".zip" if is_zip else ".tmp"))
    download(spec["url"], staged)

    if spec.get("sha256"):
        got = sha256_of(staged)
        if got != spec["sha256"]:
            staged.unlink(missing_ok=True)
            raise Fail(f"checksum mismatch for {key}\n  expected {spec['sha256']}\n  got      {got}")
        print("  checksum OK")
    else:
        print("  (no pinned checksum for this dataset; skipping verification)")

    if is_zip:
        print(f"  extracting {spec['archive_member']}")
        with zipfile.ZipFile(staged) as zf:
            with zf.open(spec["archive_member"]) as src, open(final, "wb") as dst:
                shutil.copyfileobj(src, dst)
        staged.unlink(missing_ok=True)
        if spec.get("member_sha256"):
            got = sha256_of(final)
            if got != spec["member_sha256"]:
                final.unlink(missing_ok=True)
                raise Fail(f"extracted file checksum mismatch for {key}")
            print("  extracted checksum OK")
    else:
        staged.replace(final)

    print(f"  installed -> {final}")
    return final


# --------------------------------------------------------------------------
# PipeWire introspection
# --------------------------------------------------------------------------

def list_sinks():
    """[(id, name, description)] for all real sinks, excluding ours."""
    out = run(["pactl", "list", "sinks", "short"]).stdout or ""
    sinks = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            sinks.append((parts[0], parts[1]))
    return sinks


def default_sink():
    out = run(["pactl", "info"]).stdout or ""
    for line in out.splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip()
    return None


def sink_id(name):
    for sid, sname in list_sinks():
        if sname == name:
            return sid
    return None


def set_default_sink(name):
    """Set the default output by node name.

    Deliberately uses `pactl set-default-sink`, which takes a name: `wpctl
    set-default` wants a PipeWire node id, which is *not* the same number as
    the pactl sink index.
    """
    res = run(["pactl", "set-default-sink", name])
    if res.returncode != 0:
        raise Fail(f"could not set default sink to {name}: {res.stderr.strip()}")
    return True


def pick_hardware_sink():
    """Best guess at the physical output to feed, never our own virtual sink.

    Only consulted when we have no prior known-good target (see cmd_install).
    HDMI audio devices almost always exist in ALSA even when nothing is
    plugged into the port, so a bare "first sink in the list" guess tends to
    land on a dead output instead of real speakers/headphones. Prefer
    Bluetooth and USB sinks over HDMI/PCI ones when guessing blind.
    """
    cur = default_sink()
    if cur and not cur.startswith(SINK_NODE_NAME):
        return cur
    candidates = [name for _, name in list_sinks() if not name.startswith(SINK_NODE_NAME)]
    for name in candidates:
        if "hdmi" not in name.lower():
            return name
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# Config generation
# --------------------------------------------------------------------------

def build_graph(hrtf_path, layout_name, gain, lfe_gain):
    """Return the filter.graph block for the given dataset + layout."""
    layout = LAYOUTS[layout_name]
    channels = layout["channels"]
    angles = layout["angles"]
    hesuvi = hrtf_path.suffix.lower() == ".wav"

    if hesuvi:
        missing = [c for c in channels if c not in HESUVI_MAP and c != "LFE"]
        if missing:
            raise Fail(f"HeSuVi files have no impulse for {missing} - use layout 7.1 or 5.1")

    nodes, links, inputs = [], [], []
    gains = []

    for slot, ch in enumerate(channels, start=1):
        if ch == "LFE":
            # LFE is non-directional; HRTF convolution below ~120 Hz only adds
            # delay and comb filtering. Route it dry into both ears instead.
            nodes.append(f'{{ type = builtin label = copy name = copy{ch} }}')
            inputs.append(f'"copy{ch}:In"')
            links.append(f'{{ output = "copy{ch}:Out" input = "mixL:In {slot}" }}')
            links.append(f'{{ output = "copy{ch}:Out" input = "mixR:In {slot}" }}')
            gains.append(gain * lfe_gain)
            continue

        if hesuvi:
            left_ch, right_ch = HESUVI_MAP[ch]
            nodes.append(
                f'{{ type = builtin label = convolver name = conv{ch}_L '
                f'config = {{ filename = "{hrtf_path}" channel = {left_ch} }} }}'
            )
            nodes.append(
                f'{{ type = builtin label = convolver name = conv{ch}_R '
                f'config = {{ filename = "{hrtf_path}" channel = {right_ch} }} }}'
            )
            nodes.append(f'{{ type = builtin label = copy name = copy{ch} }}')
            inputs.append(f'"copy{ch}:In"')
            links.append(f'{{ output = "copy{ch}:Out" input = "conv{ch}_L:In" }}')
            links.append(f'{{ output = "copy{ch}:Out" input = "conv{ch}_R:In" }}')
            links.append(f'{{ output = "conv{ch}_L:Out" input = "mixL:In {slot}" }}')
            links.append(f'{{ output = "conv{ch}_R:Out" input = "mixR:In {slot}" }}')
        else:
            az = angles[ch]
            nodes.append(
                f'{{ type = sofa label = spatializer name = sp{ch} '
                f'config = {{ filename = "{hrtf_path}" }} '
                f'control = {{ "Azimuth" = {float(az):.1f} "Elevation" = 0.0 "Radius" = 1.0 }} }}'
            )
            inputs.append(f'"sp{ch}:In"')
            links.append(f'{{ output = "sp{ch}:Out L" input = "mixL:In {slot}" }}')
            links.append(f'{{ output = "sp{ch}:Out R" input = "mixR:In {slot}" }}')
        gains.append(gain)

    gain_ctl = " ".join(f'"Gain {i}" = {g:.4f}' for i, g in enumerate(gains, start=1))
    nodes.append(f'{{ type = builtin label = mixer name = mixL control = {{ {gain_ctl} }} }}')
    nodes.append(f'{{ type = builtin label = mixer name = mixR control = {{ {gain_ctl} }} }}')

    # A hard limiter on each ear, not a bigger safety margin on gain. Sizing
    # `gain` itself to survive 8 fully independent full-scale channels firing
    # at once (an unrealistic worst case - real mixes are correlated and sit
    # well under full scale) costs ~26 dB of headroom on every normal sound,
    # which is what made the volume slider top out quiet. The clamp only
    # engages for genuine peaks, so normal content keeps its full range.
    #
    # Bounded at 0.89 (~-1 dBTP), not 1.0: measured directly at this node's
    # output the clamp holds exactly to its bound, but Bluetooth headphones
    # resample this 48kHz graph down (44.1kHz AAC) and that reconstruction
    # can overshoot a hard 0 dBFS ceiling - a well-known "inter-sample peak"
    # effect, which is why streaming/broadcast delivery standardly targets
    # -1 dBTP instead of exactly 0. Confirmed the clamp itself is not a
    # no-op: measured directly at this node's output it holds exactly to
    # its bound for both a single channel and all 8 at once.
    nodes.append('{ type = builtin label = clamp name = clampL control = { "Min" = -0.89 "Max" = 0.89 } }')
    nodes.append('{ type = builtin label = clamp name = clampR control = { "Min" = -0.89 "Max" = 0.89 } }')
    links.append('{ output = "mixL:Out" input = "clampL:In" }')
    links.append('{ output = "mixR:Out" input = "clampR:In" }')

    pad = "\n                    "
    return (
        "                nodes = [" + pad + pad.join(nodes) + "\n                ]\n"
        "                links = [" + pad + pad.join(links) + "\n                ]\n"
        "                inputs  = [ " + " ".join(inputs) + " ]\n"
        '                outputs = [ "clampL:Out" "clampR:Out" ]\n'
    )


def build_config(hrtf_path, layout_name, gain, lfe_gain, target, dataset_key):
    layout = LAYOUTS[layout_name]
    channels = layout["channels"]
    graph = build_graph(hrtf_path, layout_name, gain, lfe_gain)
    target_line = f'                target.object  = "{target}"\n' if target else ""
    desc = f"{SINK_DESCRIPTION} {layout_name}"

    return f"""# Generated by {APP}. Do not edit by hand - re-run the tool instead.
#
#   dataset : {dataset_key}
#   hrtf    : {hrtf_path}
#   layout  : {layout_name}
#   gain    : {gain}  (lfe x{lfe_gain})
#   target  : {target or "(follow default sink)"}
#
# Applied by:  systemctl --user restart {SERVICE}

context.modules = [
    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "{desc}"
            media.name       = "{desc}"
            filter.graph = {{
{graph}            }}
            capture.props = {{
                node.name      = "{SINK_NODE_NAME}"
                node.description = "{desc}"
                media.class    = Audio/Sink
                audio.channels = {len(channels)}
                audio.position = [ {" ".join(channels)} ]
                priority.driver = 1000
                priority.session = 1000
            }}
            playback.props = {{
                node.name      = "{SINK_NODE_NAME}.output"
                node.passive   = true
                audio.channels = 2
                audio.position = [ FL FR ]
                stream.dont-remix = true
{target_line}            }}
        }}
    }}
]
"""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_doctor(args):
    ok = True

    def check(label, good, detail=""):
        nonlocal ok
        mark = "OK  " if good else "FAIL"
        if not good:
            ok = False
        print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
        return good

    print("checking prerequisites")
    for tool in ("pipewire", "pactl", "systemctl"):
        check(f"{tool} present", have(tool))

    ver = (run(["pipewire", "--version"]).stdout or "").strip().splitlines()
    ver = next((l.split()[-1] for l in ver if "Compiled" in l), "unknown")
    check("pipewire version", ver != "unknown", ver)

    check("SOFA filter-graph plugin", SOFA_PLUGIN.is_file(), str(SOFA_PLUGIN))

    if have("ldd") and SOFA_PLUGIN.is_file():
        ldd = run(["ldd", str(SOFA_PLUGIN)]).stdout or ""
        check("libmysofa linked", "libmysofa" in ldd)

    builtin = Path("/usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so")
    if builtin.is_file() and have("ldd"):
        ldd = run(["ldd", str(builtin)]).stdout or ""
        check("libsndfile linked (needed for HeSuVi .wav)", "libsndfile" in ldd)

    unit = run(["systemctl", "--user", "list-unit-files", SERVICE]).stdout or ""
    check(f"{SERVICE} available", SERVICE in unit)

    print()
    print("all good" if ok else "some checks failed (see above)")
    return 0 if ok else 1


def cmd_list(args):
    state = load_state()
    active = state.get("dataset")
    print("HRTF datasets:")
    for key, spec in DATASETS.items():
        installed = (HRTF_DIR / spec["filename"]).is_file()
        flag = "*" if key == active else " "
        tags = []
        if installed:
            tags.append("downloaded")
        if key == DEFAULT_DATASET:
            tags.append("default")
        tag = f"  [{', '.join(tags)}]" if tags else ""
        print(f" {flag} {key:<18} {spec['desc']}{tag}")
        print(f"   {'':<18} {spec['note']}")
    print()
    print("Speaker layouts:")
    for key, spec in LAYOUTS.items():
        flag = "*" if key == state.get("layout") else " "
        print(f" {flag} {key:<18} {spec['desc']}")
    print()
    print("HeSuVi impulse files are also supported:")
    print("   --dataset hesuvi:/path/to/dolby.wav   (14-channel HeSuVi WAV)")
    return 0


def cmd_install(args):
    if not SOFA_PLUGIN.is_file() and not args.dataset.startswith("hesuvi:"):
        raise Fail(
            f"SOFA plugin missing: {SOFA_PLUGIN}\n"
            "Install a PipeWire build with libmysofa support, or use --dataset hesuvi:<file>."
        )
    if args.layout not in LAYOUTS:
        raise Fail(f"unknown layout '{args.layout}'. Try: {', '.join(LAYOUTS)}")

    hrtf = ensure_dataset(args.dataset)

    # No target.object is written unless the user pins one explicitly. With
    # none set, WirePlumber links the binaural output to whatever your
    # system's real default sink is right now - the same mechanism that
    # already re-routes normal desktop audio when you plug in a different
    # headset. Verified: it correctly skips pw_surround itself even while
    # pw_surround IS the system default, so there's no self-link loop.
    # This is what makes the tool portable across AirPods / Sony BT / wired
    # 3.5mm / USB DAC without editing anything - swap devices, it follows.
    target = args.target if args.target not in ("", "auto") else None
    if target:
        print(f"routing binaural output to: {target}  (pinned)")
    else:
        print("routing binaural output to: your system's current default device (auto)")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        build_config(hrtf, args.layout, args.gain, args.lfe_gain, target, args.dataset)
    )
    print(f"wrote {CONFIG_FILE}")

    if args.no_upmix:
        CLIENT_CONF_DIR.mkdir(parents=True, exist_ok=True)
        UPMIX_FILE.write_text(
            f"# Generated by {APP}.\n"
            "# Keeps stereo content as stereo instead of synthesising surround channels\n"
            "# for it, so music/browser audio is only placed at the front pair.\n"
            "stream.properties = {\n"
            "    channelmix.upmix = false\n"
            "}\n"
        )
        print(f"wrote {UPMIX_FILE}  (stereo will not be upmixed)")
    else:
        UPMIX_FILE.unlink(missing_ok=True)

    save_state({
        "dataset": args.dataset,
        "layout": args.layout,
        "gain": args.gain,
        "lfe_gain": args.lfe_gain,
        "target": target,
        "hrtf": str(hrtf),
        "no_upmix": bool(args.no_upmix),
    })

    run(["systemctl", "--user", "enable", SERVICE])
    res = run(["systemctl", "--user", "restart", SERVICE])
    if res.returncode != 0:
        raise Fail(f"could not start {SERVICE}:\n{res.stderr}")
    print(f"{SERVICE} restarted")

    _wait_for_sink()

    if args.set_default:
        if sink_id(SINK_NODE_NAME):
            set_default_sink(SINK_NODE_NAME)
            print(f"default sink -> {SINK_DESCRIPTION}")
        else:
            print("surround sink did not come up; leaving default output alone", file=sys.stderr)

    print()
    print("done. Verify placement with:  deploy_surround.py test")
    return 0


def _wait_for_sink(tries=20):
    import time
    for _ in range(tries):
        if sink_id(SINK_NODE_NAME):
            print(f"sink is up: {SINK_NODE_NAME}")
            return True
        time.sleep(0.25)
    print(
        f"warning: sink '{SINK_NODE_NAME}' did not appear.\n"
        f"         check: systemctl --user status {SERVICE}",
        file=sys.stderr,
    )
    return False


def cmd_status(args):
    state = load_state()
    if not state:
        print("not installed (run: deploy_surround.py install)")
        return 1

    print("configuration")
    for key in ("dataset", "layout", "gain", "lfe_gain", "target", "no_upmix"):
        print(f"  {key:<10} {state.get(key)}")
    print(f"  config     {CONFIG_FILE}")

    active = run(["systemctl", "--user", "is-active", SERVICE]).stdout.strip()
    print(f"\nservice      {SERVICE}: {active}")

    sid = sink_id(SINK_NODE_NAME)
    print(f"sink         {'up (id ' + sid + ')' if sid else 'NOT PRESENT'}")

    cur = default_sink()
    print(f"default sink {cur}")
    if sid and cur != SINK_NODE_NAME:
        print(f"\n  note: surround sink exists but is not the default output.")
        print(f"        games will only see 7.1 if they play into it:")
        print(f"        deploy_surround.py default on")
    return 0


def cmd_use(args):
    state = load_state()
    if not state:
        raise Fail("not installed yet (run: deploy_surround.py install)")
    ns = argparse.Namespace(
        dataset=args.dataset or state.get("dataset", DEFAULT_DATASET),
        layout=args.layout or state.get("layout", DEFAULT_LAYOUT),
        gain=state.get("gain", 0.35) if args.gain is None else args.gain,
        lfe_gain=state.get("lfe_gain", 0.7) if args.lfe_gain is None else args.lfe_gain,
        target=state.get("target") or "auto",
        set_default=False,
        no_upmix=state.get("no_upmix", True),
    )
    return cmd_install(ns)


def cmd_default(args):
    if args.mode == "on":
        if not sink_id(SINK_NODE_NAME):
            raise Fail("surround sink is not running")
        set_default_sink(SINK_NODE_NAME)
        print(f"default sink -> {SINK_DESCRIPTION}")
    else:
        target = load_state().get("target") or pick_hardware_sink()
        if not target:
            raise Fail("could not find a hardware sink to fall back to")
        set_default_sink(target)
        print(f"default sink -> {target}")
    return 0


def cmd_uninstall(args):
    run(["systemctl", "--user", "stop", SERVICE])
    run(["systemctl", "--user", "disable", SERVICE])
    for path in (CONFIG_FILE, UPMIX_FILE):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    # restore a sane default output
    target = load_state().get("target") or pick_hardware_sink()
    if target:
        run(["pactl", "set-default-sink", target])
        print(f"default sink -> {target}")
    if args.purge:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        print(f"removed {DATA_DIR}")
    else:
        STATE_FILE.unlink(missing_ok=True)
        print(f"kept downloaded HRTF data in {HRTF_DIR} (use --purge to delete)")
    print("uninstalled")
    return 0


def cmd_test(args):
    """Play a noise burst from each channel in turn so you can hear the placement."""
    state = load_state()
    layout_name = args.layout or state.get("layout", DEFAULT_LAYOUT)
    if layout_name not in LAYOUTS:
        raise Fail(f"unknown layout '{layout_name}'")
    channels = LAYOUTS[layout_name]["channels"]

    if not sink_id(SINK_NODE_NAME):
        raise Fail("surround sink is not running (run: deploy_surround.py install)")
    if not have("pw-play"):
        raise Fail("pw-play not found (install pipewire-audio / pipewire-tools)")

    rate = 48000
    burst = int(rate * 0.7)
    gap = int(rate * 0.25)
    nch = len(channels)
    rnd = random.Random(1234)

    # Pink-ish noise reads as more natural and localises better than white.
    def noise_burst(n):
        out, b0 = [], 0.0
        for i in range(n):
            white = rnd.uniform(-1.0, 1.0)
            b0 = 0.99 * b0 + white * 0.1
            v = (white * 0.3 + b0)
            # 20 ms raised-cosine fade in/out to avoid clicks
            fade = int(rate * 0.02)
            if i < fade:
                v *= 0.5 - 0.5 * math.cos(math.pi * i / fade)
            elif i > n - fade:
                v *= 0.5 - 0.5 * math.cos(math.pi * (n - i) / fade)
            out.append(v)
        return out

    print(f"playing a burst from each channel of the {layout_name} layout:")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "surround-test.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(nch)
            w.setsampwidth(2)
            w.setframerate(rate)
            for idx, ch in enumerate(channels):
                sig = noise_burst(burst)
                frames = bytearray()
                for v in sig:
                    # kept well below full scale so the test itself never clips
                    s = int(max(-1.0, min(1.0, v * 0.35)) * 32767)
                    for c in range(nch):
                        frames += struct.pack("<h", s if c == idx else 0)
                frames += b"\x00" * (gap * nch * 2)
                w.writeframes(bytes(frames))

        expect = {
            "FL": "front left", "FR": "front right", "FC": "dead ahead",
            "LFE": "low rumble, no direction", "RL": "behind left", "RR": "behind right",
            "SL": "hard left", "SR": "hard right",
        }
        for ch in channels:
            print(f"   {ch:<4} -> {expect.get(ch, '')}")
        print()
        res = run(["pw-play", "--target", SINK_NODE_NAME, str(path)], quiet=False)
        if res.returncode != 0:
            raise Fail("playback failed")
    print("\nif any channel came from the wrong place, try a different --dataset")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="deploy_surround.py",
        description="HRTF virtual surround sink for PipeWire.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  deploy_surround.py doctor\n"
            "  deploy_surround.py install --set-default\n"
            "  deploy_surround.py install --dataset sadie2-ku100 --layout 7.1\n"
            "  deploy_surround.py install --dataset hesuvi:~/hrir/dolby.wav\n"
            "  deploy_surround.py test\n"
            "  deploy_surround.py default off\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="check that this system can run it")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("list", help="show available datasets and layouts")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("install", help="download HRTF, generate config, start the sink")
    sp.add_argument("--dataset", default=DEFAULT_DATASET,
                    help=f"HRTF dataset or hesuvi:<file> (default: {DEFAULT_DATASET})")
    sp.add_argument("--layout", default=DEFAULT_LAYOUT,
                    help=f"speaker layout (default: {DEFAULT_LAYOUT})")
    # The graph ends in a hard limiter (see build_graph), so gain only needs
    # to be sized for realistic content, not for the pathological case of 8
    # fully independent full-scale channels at once. 0.35 roughly compensates
    # for the spatializer's own gain (measured HRIR energy ~0.94/channel) so
    # normal single/dual-channel sounds land near their natural loudness;
    # the limiter catches genuine peaks instead of every sound being padded
    # ~26 dB quiet to survive a case that real mixes don't produce.
    sp.add_argument("--gain", type=float, default=0.35,
                    help="master gain for the downmix, 8 channels sum into 2 (default: 0.35)")
    sp.add_argument("--lfe-gain", type=float, default=0.7, dest="lfe_gain",
                    help="extra gain applied to LFE (default: 0.7)")
    sp.add_argument("--target", default="",
                    help="pin the downmix to one specific sink by name; "
                         "default follows your current default output, "
                         "which is what makes this portable across devices")
    sp.add_argument("--set-default", action="store_true",
                    help="make the surround sink the default output")
    sp.add_argument("--no-upmix", action="store_true", default=True,
                    help="keep stereo as stereo instead of synthesising surround (default)")
    sp.add_argument("--upmix", action="store_false", dest="no_upmix",
                    help="allow PipeWire to upmix stereo into all channels")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("use", help="switch dataset/layout, keeping other settings")
    sp.add_argument("--dataset")
    sp.add_argument("--layout")
    sp.add_argument("--gain", type=float)
    sp.add_argument("--lfe-gain", type=float, dest="lfe_gain")
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser("status", help="show current state")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("default", help="point the default output at/away from the surround sink")
    sp.add_argument("mode", choices=["on", "off"])
    sp.set_defaults(func=cmd_default)

    sp = sub.add_parser("test", help="play a positional test through each channel")
    sp.add_argument("--layout")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("uninstall", help="remove config and stop the sink")
    sp.add_argument("--purge", action="store_true", help="also delete downloaded HRTF data")
    sp.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
