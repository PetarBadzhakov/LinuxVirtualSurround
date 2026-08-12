# Windows Sonic / Dolby Atmos for Headphones, on Linux

Games and movies mix for **speakers** - 5.1 or 7.1, arranged around you. On headphones you get none of that. The driver folds all those channels into a flat stereo pair, so a helicopter behind you and a helicopter in front of you sound *identical*. Every channel that isn't front-left or front-right just gets smeared into the middle of your skull.

Windows solves this with Windows Sonic, Dolby Atmos for Headphones and DTS Headphone:X. Linux ships no such thing.

**This tool is a "one-and-done" fix that gives you:**
* **A real 7.1 sound card, as far as games are concerned.** Steam, Proton and native games detect 8 channels and mix for them properly.
* **True 3D spatial audio:** each virtual speaker is convolved through a measured HRTF, so behind actually sounds behind.
* **Stereo left alone:** music and browser audio stay a normal front pair instead of being fake-upmixed into your rear channels.

# deploy_surround

Creates a virtual 7.1 (or 5.1) PipeWire sink that binaurally downmixes to your headphones using the **SADIE KU100** HRTF dataset - the same dataset [HRTF_installer](https://github.com/PetarBadzhakov/HRTF_installer) uses for OpenAL.

Headphones only. On speakers this would sound wrong, since it is already simulating speakers.

Nothing is compiled and nothing is patched. It uses PipeWire's built-in `sofa` spatializer and the stock `filter-chain.service`, so it survives PipeWire upgrades and can't take your audio down if it fails - the filter chain runs as a separate service from the main daemon.

**Not tied to any specific headphones.** The generated config has no hardcoded device name in it. The binaural downmix follows your system's normal default output - the same mechanism PipeWire already uses to re-route your desktop audio when you plug in something different. Switch from Bluetooth to wired, swap one headset for another, doesn't matter: it plays through whatever your default device currently is. Confirmed by yanking the device name out of the config and pw-link still routing correctly, even while the surround sink itself is the system default. Pin it to one specific sink with `--target <name>` if you'd rather it never move (`pactl list sinks short` for names).

## Don't stack it with another HRTF pass

If you already use **OpenAL Soft HRTF** or **DSOAL** for a game (as `HRTF_installer` sets up), that game is *already* rendering binaural audio. Sending it through this as well means two HRTF passes stacked on top of each other, which smears imaging and wrecks positional cues - exactly the same mistake as leaving Windows Sonic on under OpenAL.

For those games, either:

```
deploy_surround.py default off        # temporarily go back to plain stereo
```

or leave the surround sink as default and pin just that game to the raw device (see *Per-game routing* below).

Rule of thumb: **the game does HRTF, or this does. Never both.**

## Requirements

PipeWire built with libmysofa, which is the norm on Arch/CachyOS. Check first:

```
python deploy_surround.py doctor
```

Everything should report `OK`. If the SOFA plugin is missing you can still use the tool with HeSuVi impulse files instead (see below).

## Usage

```
python deploy_surround.py install --set-default
```

That's the whole install. It downloads the HRTF dataset (~12 MB, checksum-verified), writes a PipeWire config, starts the service and makes the surround sink your default output.

Then confirm it actually works:

```
python deploy_surround.py test
```

You'll hear a noise burst from each channel in turn. **FL** front-left, **SL** hard left, **RL** behind-left, and so on. If a channel comes from the wrong place, something is wrong - it should be obvious, not subtle.

Other commands:

```
python deploy_surround.py status            # what's configured and running
python deploy_surround.py list              # available datasets and layouts
python deploy_surround.py default off       # back to plain stereo
python deploy_surround.py default on        # back to surround
python deploy_surround.py uninstall         # remove config, keep downloads
python deploy_surround.py uninstall --purge # remove downloads too
```

Uninstall stops the service, deletes the generated configs and restores your previous default output. It touches nothing else.

Stdlib only, no `pip install` needed.

## Steam and Proton

**No Proton flags. No env vars. No winecfg.** Proton reports whatever the default sink says it is, so once the surround sink is default, Wine sees a 7.1 card and games offer 7.1 in their audio menus.

Two things you do need to do:

1. **Set the game's audio output to 7.1 / Surround.** Most engines default to stereo and will *stay* stereo even on a 7.1 device. This is the step people miss. If a game only offers 5.1, use `--layout 5.1` or just let it send 5.1 into the 7.1 sink - PipeWire maps the channels correctly either way.
2. **Check Steam → Settings → Audio.** If it's pinned to a specific device, set it to `Virtual Surround (HRTF) 7.1` or "Default". Steam voice chat uses its own device setting and is unaffected.

### Per-game routing

To send one game to surround without making it the system default - or to keep one game *out* of surround - use a Steam launch option:

```
PULSE_SINK=pw_surround %command%
```

or to force a game back to the raw headphone device:

```
PULSE_SINK=bluez_output.XX_XX_XX_XX_XX_XX.1 %command%
```

Get the exact name from `pactl list sinks short`. This works because Proton talks to PipeWire through its PulseAudio layer, which honours `PULSE_SINK`.

### Movies

Media players also default to stereo. To make them use the surround sink:

* **mpv** - add `audio-channels=7.1` to `~/.config/mpv/mpv.conf`
* **VLC** - Preferences → Audio → Output, set channels to 7.1

Without this a 5.1 movie is downmixed to stereo *before* it reaches the sink, and you gain nothing.

## Choosing a different HRTF

There is no universally correct HRTF - these are measured from dummy heads, not from your ears, so externalisation is subjective. If the default feels flat or "in the middle of your head", try another:

```
python deploy_surround.py list
python deploy_surround.py use --dataset sadie2-ku100
```

| dataset | what it is |
|---|---|
| `sadie-ku100-dfc` | SADIE/Google KU100, diffuse-field corrected. Default. Sharpest imaging, least coloration. |
| `sadie2-ku100` | SADIE II D1, 8802 measurement points. Same data behind the OpenAL `.mhr`. More coloured. |
| `sadie2-kemar` | SADIE II D2, KEMAR head. Different geometry - try if KU100 doesn't sit right. |

### HeSuVi impulse files

HeSuVi is Windows-only (it's an Equalizer APO front-end), but its impulse responses are just 14-channel WAVs and work fine here. If you want Dolby Headphone or DTS Headphone:X specifically, grab the HeSuVi `hrir` folder and point at one:

```
python deploy_surround.py install --dataset hesuvi:~/hrir/dolby.wav
```

These add simulated room reflections. That makes them externalise more convincingly than the dry SADIE sets, at the cost of positional precision. Worth trying if SADIE sounds too "close".

### Layouts

```
python deploy_surround.py use --layout 5.1
```

`7.1` uses ITU/SMPTE speaker angles - the positions games actually pan for, so it's the accurate choice. `7.1-wide` exaggerates the separation; it sounds more dramatic but is less faithful. `5.1` is there for games that misbehave when offered 8 channels.

## Volume

The graph ends in a hard limiter, so gain (`0.35` by default) is sized for realistic content rather than for eight channels of simultaneous full-scale noise - that pathological case doesn't happen in real mixes, and padding every sound to survive it was making the volume slider top out quiet no matter how far you turned it up. Full range is:

```
python deploy_surround.py use --gain 0.5   # louder
python deploy_surround.py use --gain 0.25  # quieter
```

The limiter itself holds to about -1 dBTP, not exactly 0 dBFS - Bluetooth headphones resample this 48kHz graph to their own rate (usually 44.1kHz for AAC) and that reconstruction can overshoot a hard ceiling, so a small margin avoids audible clipping on the far side of that resample.

## What it does

1. Downloads the SADIE KU100 SOFA dataset to `~/.local/share/pw-surround/hrtf/` and verifies its SHA-256.
2. Generates a PipeWire filter-chain graph into `~/.config/pipewire/filter-chain.conf.d/50-pw-surround.conf`: one `sofa` spatializer per channel at a fixed azimuth, feeding two 8-input mixers (one per ear).
3. Routes **LFE dry, with no HRTF**. Below ~120 Hz an HRTF contributes no directional information, so convolving it only adds delay and comb filtering.
4. Writes `~/.config/pipewire/client-rt.conf.d/50-pw-surround-no-upmix.conf` so stereo content isn't synthesised into surround channels.
5. Enables the stock `filter-chain.service` user unit and restarts it.

## How we know it works

Not by ear - by measurement. An impulse was played into each channel and the signal *actually arriving at the headphone sink* was captured and analysed:

| channel | ITD | ILD | result |
|---|---|---|---|
| FL / FR | ∓271 / +250 µs | ±7.6 dB | correct |
| FC | 0 µs | −0.1 dB | dead centre |
| LFE | 0 µs | 0 dB | dry, as designed |
| RL / RR | ∓292 / +271 µs | ±7.1 dB | correct |
| SL / SR | ∓812 / +792 µs | ±11 dB | correct |

ITD is the arrival-time difference between your ears, ILD the level difference - the two cues the brain actually uses to localise sound. **±812 µs at hard left/right is the human physical maximum** (sound takes about that long to travel around a head), and the values are cleanly antisymmetric, which is what proves left and right aren't mirrored.

The clamp/gain balance was tuned the same way, by measuring rather than guessing. Predicting gain from HRIR energy alone suggested `0.35` with no limiter, which measured a true peak of **6.3x full scale** - about 16 dB of clipping, hidden only because the test headphones sat below 50% volume. Adding a hard limiter (PipeWire's builtin `clamp`) after the mixer fixes that without sacrificing loudness: measured directly at the filter graph's own output, it holds to exactly its configured bound (0.89, ~-1 dBTP) for both a single channel and all eight firing at once. That bound sits slightly under 0 dBFS rather than at it because Bluetooth headphones resample this 48kHz graph to their own rate, and that reconstruction can overshoot a hard ceiling - confirmed by measuring the same content on the real headphone output, which peaked at 1.496 with the limiter's ceiling set to exactly 1.0.

## Caveat - Bluetooth latency

If you're on Bluetooth headphones, AAC on Linux adds roughly 200 ms of latency. This tool doesn't change that in either direction (HRTF convolution itself costs about 5 ms), but it also can't fix it. For competitive shooters, wired is wired.

---

## Credits & Acknowledgments

Credits to the authors of the software and data referenced:
* **[Wim Taymans and the PipeWire project](https://pipewire.org/)** - the `sofa` spatializer and filter-chain infrastructure this is built on. The heavy lifting is theirs; this tool only generates configuration.
* **[Christian Hoene](https://github.com/hoene/libmysofa)** - for **libmysofa**, which does the SOFA parsing and HRIR interpolation.
* **SADIE** - for the [SADIE II database](https://www.york.ac.uk/sadie-project/database.html), measured at the University of York and released under Apache 2.0. If you use the data in published work, cite [doi:10.3390/app8112029](https://doi.org/10.3390/app8112029). All measurements are Copyright University of York.
