# Omarchy Speaker Calibrator

**Measure your speakers with a microphone. Get them to sound the way they
should. In about thirty seconds, from the Omarchy bar.**

![Speaker Calibrator: make every laptop sound great, like a MacBook](preview.png)

Laptop speakers have peaks and dips of ten decibels or more, and no two
machines are wrong in the same way. This measures yours with a microphone you
already own, works out what to subtract, and installs a PipeWire filter that
does it. Everything happens in the panel. Nothing needs a terminal.

## Install

```bash
omarchy plugin add https://github.com/thefreshoffice/omarchy-speaker-calibrator.git --enable
```

That is the whole installation. Omarchy asks where to put the bar widget.

The filter chain uses `lsp-plugins-lv2`, which Omarchy pacstraps from its own
package list, so it is already there. Measuring additionally needs
`python-numpy` and `python-scipy`, which Omarchy does not ship; the panel
notices they are absent and installs them in one press, again from Omarchy's
own packages rather than the AUR. Nothing here needs a terminal.

### Apple Silicon (Asahi)

asahi-audio plays the speakers through a software DSP and puts the raw
microphone array behind one of its own. The speaker side is handled by this
plugin. To calibrate from the built-in array, expose it once:

```bash
cp /usr/share/wireplumber/wireplumber.conf.d/99-asahi.conf \
   ~/.config/wireplumber/wireplumber.conf.d/99-asahi.conf
```

In the copy, find the `node.software-dsp.rules` entry for
`~hw:AppleJ[0-9][0-9][0-9]HPAI,0`, which is the microphone, and set
`hide-parent = false`. Leave the speaker entry hidden. Then apply it:

```bash
systemctl --user restart wireplumber
```

Until then the panel lists the MacBook microphone as unsupported and says why:
that source is the array behind an adaptive beamformer, which re-estimates its
weights while a sweep runs and suppresses sound that does not come from the
user, the speakers included. A correction fitted through it would describe the
beamformer. WirePlumber withdraws every client's access to the raw array, so
the plugin cannot reach it by itself.

The array then appears as `alsa_input.platform-sound.RawMics`, and the panel
offers it as the built-in microphone with one channel per microphone. The copy
shadows Asahi's packaged file, so repeat the one-line change after an
asahi-audio update replaces it.

## Using it

1. **Click the speaker icon** in the bar.
2. **Press Calibrate speakers.** Your built-in speakers and microphones are
   already selected. Pick another microphone and the panel keeps that choice;
   once a calibration is installed it starts from the devices that one was
   made with, whenever they are connected.
3. **Be quiet for about thirty seconds.** You will hear six sweeps: three per
   speaker.
4. **Listen, then decide.** If the measurement passes its quality checks it
   plays right away, and when a calibration was already installed the panel
   asks: apply the new one, or keep the previous? A switch lets you hear both
   before you answer, level matched, and either answer takes effect at once.
   The first calibration has nothing to compare against and simply installs.

![The panel: four switches, the equalizer, and the calibrate button](screenshot.png)

Middle-click the bar icon to re-scan for devices. If a measurement fails a
quality gate it is kept for diagnosis but never installed, so a bad
measurement cannot make your speakers worse.

In Omarchy's sound menu and output switcher the calibration is the output
called **Calibrated Speakers**; keep that one selected. The raw speaker device
stays listed next to it, because Omarchy hides the hardware only behind its own
laptop tunings ([omacom/omarchy#12191](https://github.com/omacom/omarchy/issues/12191)
asks for the same treatment of this sink). Selecting the raw device plays
around the calibration, and so does an application that switches the default
output on its own.

### The four switches

| Switch | What it does |
| --- | --- |
| **Loudness** | Fuller sound with more bass, like the loudness button on a stereo. |
| **Make it louder** | Gives back the volume the correction takes away. The limiter works harder at full volume. |
| **Deep bass** | Suggests low notes the speakers cannot physically play, using their harmonics. |
| **Calibration** | Switch it off to hear the plain speakers, level-matched so only the tone changes. |

Everything else lives under **Advanced**: voicing, the three loudness levels,
channel balance, loudness compensation, the measurement details, and a
comparison of what each microphone measured.

## Deep bass, built in

Your speakers are too small to make low notes at all. **Deep bass** plays
their harmonics instead, and your ear fills in the note that is missing: what
lies below the measured knee is saturated and its harmonics between the knee
and three times the knee are added back ahead of the EQ, where the drivers can
play them. The recipe is bankstown's, the bass enhancer James Calligeros wrote
for Asahi Linux (MIT), written out in PipeWire's own built-in nodes, so
nothing has to be installed and it works on every Omarchy machine out of the
box. It is on by default; the switch turns it off.

## How it works

### Measuring

Three exponential sine sweeps per speaker, left and right measured
independently. The analyzer deconvolves the response, finds the direct sound,
and reads the magnitude through a window of fifteen cycles: 150 ms at 100 Hz,
15 ms at 1 kHz, 1.5 ms at 10 kHz. That keeps the desk reflection you also
hear while dropping later room reflections, so the filters are fitted to the
speaker rather than to the room around the microphone.

The room between sweeps is put through the identical deconvolution to measure
the noise floor at every frequency. The resulting signal-to-noise ratio widens
the optimizer's uncertainty wherever the floor is close, which on small
speakers is mostly the bass. Clock drift between playback and recording is
estimated and corrected. Clipping, sweep prominence, alignment, repeatability,
microphone gain stability and harmonic residuals are all checked.

Before the first probe the plugin listens to what the speakers are playing,
at their output, as samples, for a good second. Anything there, however quiet,
stops the measurement with a message before a sound is made: music under the
sweeps would be measured as if it were the speakers. It does not go by the list
of open audio streams, because a player that is paused without closing its
stream looks the same as one that plays, and it does not leave it to the
microphone, which cannot tell quiet music from a fan.

Setting levels is the plugin's job, the microphone's as much as the speakers'.
A built-in microphone is often left at full input gain, where the
preamplifier's own hiss and the rumble below the measured band read as a loud
room and the quietest probe already clips. When the level search stops for a
reason that points at that, it lowers the microphone's input level a step
(12 dB, three steps at most) and searches again, which also lets the sweeps
play louder and stand further above the room. The level is put back exactly as
it was when the measurement ends, pass or fail, and it is written down before
it is touched, so a measurement that is killed halfway is repaired the next
time the helper starts. The result says when this happened. A room that really
is loud still fails as one: turned down, the probe has to stand clear of what
is left.

When a built-in microphone array offers two or more channels, every channel is
analyzed independently and their raw recordings are never mixed. The responses
are combined with a robust median, and disagreement between microphones is
added to the uncertainty curve, which suppresses risky corrections.

### Fitting

Cuts are the normal solution. The optimizer penalises boosts more than ten
times as strongly, and every filter is cross-validated against a held-out
repeat of the measurement, so a correction has to improve a sweep it was not
fitted to before it is kept.

Smoothing follows the ear's own resolution rather than a fixed fraction of an
octave, and weights peaks over dips: a resonance is audible and worth
removing, while a cancellation of the same depth is usually a null at the
microphone that moves when your head does.

A protective high-pass goes where the measurement says the speaker gives up,
and everything is fitted above that.

### Checking the result

**Check the calibration** replays the sweeps through the installed filter and
measures what actually comes out. It reports how closely the sound follows the
plan and whether it sits closer to the target than the plain speakers did. A
check is always made with the microphone the calibration was made with, on the
same channels: another microphone would measure the difference between two
microphones, not between the speakers and the plan. If that microphone is not
connected, the button says so and waits. The deep-bass add-on is muted during a
check, because it invents harmonics no linear model predicts and would
otherwise read as error.

**Improve from the check** feeds that residual back in and fits again, using
the same gates. It is level-neutral, so iterating never drifts the overall
loudness.

## Advanced

**Flat or Warm.** Flat is balanced with more clarity. Warm is softer and less
sharp for long listening. Both are voicings of the same measurement.

**Loudness: Protected, Balanced, Matched.** How much of the loudness the cuts
removed is added back as input gain: none, half, or all of it up to 6 dB.
Matched is the loudest and makes the limiter work hardest.

**Bass: Normal or Full.** Full adds a +3 dB low shelf placed clear of the
high-pass corner, paid for by input trim like any boost.

**Channel balance** trims a broadband level difference between the two
speakers, which pulls the stereo image off centre. It only ever acts on a
measurement made with an external microphone at the listening position, only
when the difference stands clear of what the measurement itself varies by, and
only by turning the louder side down. A built-in array sits closer to one
speaker than the other, so what it measures is where it is rather than what
reaches you.

**Loudness compensation** follows the volume and puts back what hearing drops
as the level falls, using the ISO 226:2023 equal-loudness curves. Full volume
is the reference: it does nothing there and more the further down you play.
The loudness stays the same either way; only the tone moves. It is the one
part of this that needs something running in the background, because only the
output device knows the listening level. It is on by default for a new calibration; the switch under
Advanced turns it off, and the setting is kept across refits.

### Which microphones can measure

The list offers real capture devices and nothing else. A Bluetooth headset
appears on the system as a microphone, but its input runs over HFP or HSP:
mono, eight to sixteen kilohertz, with automatic gain and noise suppression
applied inside the headset. It cannot describe a loudspeaker, and a
calibration fitted to one would be correcting for the headset. Those are named
in the panel as connected but unusable rather than quietly dropped.

A laptop also lists its headset jack as a microphone whether or not anything
is in it, and on some machines as a built-in one next to the real array.
PipeWire reports when a jack is empty, so such a source is marked **nothing
plugged in** and is never the automatic choice. Where the hardware cannot
tell, the measurement finds out: a built-in microphone that hears nothing of
the level probe hands over to the laptop's other built-in microphone, and the
result says that it did. It never hands over to an external microphone, and an
external microphone you picked is never replaced.

A microphone that is muted or turned down to zero in the sound settings is
marked as such in the list, and a measurement with it, or through muted
speakers, stops before the first sound with a message that says which device
and why. Nothing is played louder to find out what the settings already say.

An external microphone you picked is never swapped for another one. When it
hears nothing at all and the laptop has a microphone of its own, the failure
offers that one, and one press measures with it for that measurement only;
your pick stays what it was.

The speaker and microphone you pick by hand are remembered across restarts, in
`selection.json` in the plugin's data folder: device names only, and a name is
only honoured while a device of that name is connected.

### When the microphone is processed

Laptops with a digital microphone array often run it through a pipeline inside
the sound firmware: dynamic range compression, automatic gain, noise
suppression. A sweep cannot be measured through that. A compressor turns the
level down exactly where the speakers are loud and up where they are weak, so
the measured curve comes out flatter than the speakers are and the correction
too weak. PipeWire only sees what comes out of the firmware, and recording from
the ALSA device directly reads the same point, so there is no raw stream to ask
for; the mixer switches the firmware exposes are the only handle.

So every measurement, a calibration and a check alike, runs with the
processing switches of the measuring microphone's own sound card switched off,
and puts them back exactly as they were when it ends, pass or fail. Only
switches whose name says they change with the signal are touched (DRC, AGC,
automatic gain, noise suppression, echo cancelling), only on that one card, and
only for the seconds a measurement takes; calls and dictation before and after
are as they were. The switches are recorded before the first one is touched, so
a run that is killed halfway is repaired the next time the helper starts, which
the panel does within seconds. The result says which switches were suspended.
On a Dell XPS 16 this is the difference between a calibration and none: with
its `Microphone Capture DRC switch` on, the level search stops because the
recording will not follow the sweep level.

Not every machine exposes its processing as a switch. The level probes
therefore also check that the recorded level follows the played level
one-for-one. When it does not, the result carries a warning, and names any
processing switch that is still on for that microphone together with the
`amixer` command that turns it off.

To check a machine without calibrating anything, a few quiet chirps and no
sweeps:

```console
python3 speaker-calibrate.py devices-json          # the speaker and microphone names
python3 speaker-calibrate.py microphone-linearity-json --sink <speaker> --mic <microphone>
```

It probes with the switches as you have them, which a measurement never does,
so it shows what the processing does to the level. With `--bypass` it probes a
second time with those switches off, the way a measurement runs, and puts them
back afterwards.

### Comparing the two microphones

Advanced keeps the last measurement from each kind of microphone and draws
them on one set of axes, levelled on the 250 Hz to 1 kHz band so you see the
difference in shape rather than in sensitivity, with the difference read out
by band underneath.

This is the honest answer to whether an external microphone is worth it. A
built-in array sits inside the case, inches from one driver and behind
whatever the lid is made of. A measuring microphone sits where your head is.
Where the two curves disagree, the built-in one is describing its own position
rather than the sound that reaches you.

Only the curve is kept, a few kilobytes, never the recording.

## Safety model

The whole point is that a bad measurement cannot damage anything or make the
sound worse than it started.

- One filter section may cut at most 12 dB, and the whole correction is held
  to -15 dB at any frequency. With an uncalibrated built-in microphone the
  limit tightens by frequency: -6 dB at 160 Hz, -8 dB at 10 kHz, the full
  -15 dB only in the reliable midrange.
- Boosts are spent from a budget, not merely capped. Every decibel is
  electrical headroom the limiter must be given back, which is the same
  headroom **Make it louder** would otherwise return, and it is also cone
  travel: for the same pressure a driver moves four times as far an octave
  lower. Each decibel is priced by the inverse square of frequency, measured
  from where *this* speaker gives up. The same 4 dB dip at 260 Hz earns
  0.31 dB on a laptop whose knee is at 196 Hz and the full 1.5 dB on speakers
  measured down to 55 Hz. What it cost is recorded in the profile.
- The protective high-pass goes at the highest frequency below 400 Hz where
  the speaker falls more than 15 dB short of its target, clamped to between 50
  and 200 Hz. Below that the cone still travels as far as ever while producing
  almost nothing, so the content is removed rather than amplified. The slope
  doubles only when the corner is at or below 100 Hz, where a steep filter
  cannot be heard. The high-pass is added to the target too, so the optimizer
  never spends filters boosting back what was deliberately removed.
- Every positive correction is matched by automatic input trim plus 1 dB of
  margin, so no band is ever driven more than 5 dB harder than the uncorrected
  speaker at the same volume setting. The limiter ceiling stays at -1 dBFS,
  with auto-level and boost disabled.
- The limiter looks 15 ms ahead and attacks over 15 ms. On the plugin's default
  of 5 ms its gain follows each cycle of a bass note and modulates everything
  played with it, which is heard as clipping the moment it has to work: a
  player turned up to 120 % is enough, because that uses up the headroom the
  loudness contour's bass lift has left. At 15 ms the distortion it adds,
  measured with a microphone in front of the speakers for bass notes from 60
  to 150 Hz, is back at what the speakers do with the limiter idle. It costs
  10 ms of latency.
- Switching the calibration off level-matches the plain speakers to the
  loudness the correction plays at, so the comparison is about tone rather
  than volume. It only ever turns the plain sound down, never up.
- A measurement is always taken with the correction flattened, so the plugin
  can never fit a correction on top of itself.
- The filter graph always has the same shape, so installing or comparing
  profiles updates its controls rather than restarting the audio client.
- Existing tuning files are backed up before replacement, and failed or
  clipped measurements are saved for diagnosis but cannot be installed.

## Sharing a calibration

A calibration is specific to one model's speakers, so it can be handed to
someone with the same machine. Under Advanced, **Export this calibration**
writes the calibration that is playing to your Downloads folder as one file,
named after the machine, the microphone and the date, for example
`slimbook-executive-external-mic-2026-09-16.speaker-calibration.json`. The
file carries the machine it was made on (the vendor, product, SKU and board
from the firmware, the same fields Omarchy keys its own speaker tunings on)
and the speaker device, and nothing about you: no user name, no paths, and no
microphone device name, since a USB device's name carries its serial number.
What kind of microphone it was travels, and whether it had a correction file,
never where that file lives.

A shared file dropped into your Downloads folder appears in the same section.
**Load** makes it the last measurement, ready to install, exactly like a fresh
measurement: **Install last measurement** applies it, your own calibration is
kept, and **Switch profile** brings it back. When the file was made on
different hardware the panel says so before you install it, because speakers
differ between models and a calibration for another laptop may sound wrong.
When the exporting machine's speaker device does not exist here, the
calibration is pointed at this machine's speakers instead.

**Export as an Omarchy tuning** renders the same calibration in the layout
Omarchy ships its own laptop tunings in, `tuning.conf` and `filter-chain.conf`
under `default/audio/tunings/<vendor>-<model>/`, into your Downloads folder.
The chain holds only what Omarchy ships: the high-pass, the fitted sections,
the shelves and the limiter, with loudness compensation and volume following
left out. Deep bass travels with it when the switch is on: bankstown's recipe
(the bass below the knee, saturated, its harmonics between the knee and three
times the knee added back ahead of the EQ) is written out in PipeWire's
built-in nodes, so the tuning needs no add-on. `tuning.conf` matches on the DMI product SKU and
the speaker sink name, records where the tuning came from, and reports the
four figures Omarchy asks for: the fit's RMS deviation from its target, the
group delay swing over 30 to 300 Hz from the biquad coefficients, and the
limiter headroom and loudness-range change measured by running a hot master
through the chain (pink noise unless you pass a track:
`speaker-calibrate.py vendor-tuning-json --reference song.flac`). Listen on the
hardware, fill in `validated_by`, and offer it as a pull request to Omarchy.

**Hear the exported tuning** plays it beside the calibration: the rendered
chain starts as a second output, your music is moved onto it, and a PipeWire
stream survives that move without a break, so nothing stops and nothing
restarts. What plays is the plain chain with its built-in deep bass, no
loudness compensation, no volume following. **Back to the calibration** moves
the music back and drops the second output. The faithful check of the files,
through Omarchy's own installer with its matching and verification, is
`speaker-calibrate.py vendor-try-json --installer`; that one restarts the
tuning host, so playback pauses for a second, and `vendor-restore-json` puts
the calibration back.

A shared file never chooses the speaker output. The exporter's device is used
only when this machine's own list has one of exactly that name, and otherwise
the calibration is pointed at this machine's speakers; whatever name a file
carries is never written into a configuration. A file built to upset the
reader is listed as unreadable and nothing more.

Every value a shared file could feed into the filter chain is checked against
the same limits the optimizer works under before anything is loaded: no cut
deeper than 18 dB, no boost above 6 dB, no high-pass above 400 Hz, no trim
beyond 6 dB, at most twelve filters, and a measurement that passed its own
quality checks. A file outside those limits is refused, not repaired. From a
terminal, `speaker-calibrate.py export-json` and
`speaker-calibrate.py import-json --path FILE` do the same.

## What leaves your machine

Nothing. There is no API, no telemetry, no update check and no account. The
plugin never opens a socket.

An exported calibration is a file in your Downloads folder and goes nowhere
unless you send it; loading one reads a file from that folder and nothing else.

The microphone is opened only while a measurement is running. The recordings
stay on disk under `~/.local/share/omarchy-speaker-calibrator/` and are never
uploaded.

## Removing it

```bash
omarchy plugin remove thefreshoffice.speaker-calibrator
```

Press **Disable** in the panel first. That restores the default output, stops
the services, ends an exported tuning that is on trial and removes its files,
and takes the filter out of the audio path. If the plugin is
removed while a calibration is still active, the PipeWire filter keeps running
from the files below until they are deleted or the machine restarts, and the
panel is no longer there to switch it off.

Removing the plugin deletes its own directory and nothing else. These are
created outside it and stay behind:

| Path | What it is |
| --- | --- |
| `~/.config/systemd/user/omarchy-speaker-tuning.service` | runs the filter graph |
| `~/.config/systemd/user/omarchy-speaker-loudness.service` | follows the volume for loudness compensation |
| `~/.config/pipewire/omarchy-speaker-tuning.conf` | the filter sink |
| `~/.config/pipewire/omarchy-speaker-tuning.conf.d/90-tuning.conf` | the measured filters |
| `~/.config/systemd/user/omarchy-speaker-trial.service` | only while an exported tuning is on trial; Disable removes it |
| `~/.config/pipewire/omarchy-speaker-trial.conf` | the trial's sink, same lifetime |
| `~/.config/pipewire/omarchy-speaker-trial.conf.d/90-trial.conf` | the trial's filters, same lifetime |
| `~/.local/share/omarchy-speaker-calibrator/` | profiles, checks, and the recorded sweeps |
| `~/.local/share/omarchy-speaker-calibrator/microphone-processing-restore.json` | exists only during a measurement: which microphone switches to put back |
| `~/.local/share/omarchy-speaker-calibrator/microphone-volume-restore.json` | exists only during a measurement that lowered the microphone's input level: what to put back |
| `~/Downloads/*.speaker-calibration.json` | calibrations you exported, or that someone gave you |
| `~/Downloads/omarchy-tuning-*/` | vendor tunings you exported |

The files in Downloads are yours: the plugin wrote them because you pressed
Export and never deletes them. On Apple Silicon, the copy of
`99-asahi.conf` under `~/.config/wireplumber/wireplumber.conf.d/` is one you
made by hand following the setup above; delete it to hide the raw microphone
array again.

Those recordings are audio captured in your room by your microphone. Nothing
is ever sent anywhere, but they survive removal until deleted.

To remove all of it after disabling:

```bash
systemctl --user disable --now omarchy-speaker-tuning.service omarchy-speaker-loudness.service
systemctl --user stop omarchy-speaker-trial.service
rm -f ~/.config/systemd/user/omarchy-speaker-tuning.service \
      ~/.config/systemd/user/omarchy-speaker-loudness.service \
      ~/.config/systemd/user/omarchy-speaker-trial.service \
      ~/.config/pipewire/omarchy-speaker-tuning.conf \
      ~/.config/pipewire/omarchy-speaker-tuning.conf.d/90-tuning.conf \
      ~/.config/pipewire/omarchy-speaker-trial.conf \
      ~/.config/pipewire/omarchy-speaker-trial.conf.d/90-trial.conf
systemctl --user daemon-reload
rm -rf ~/.local/share/omarchy-speaker-calibrator
```


## Runtime dependencies

| Package | Ships with Omarchy | Needed for |
| --- | --- | --- |
| `pipewire` | yes | everything |
| `lsp-plugins-lv2` | yes | the filter chain, the limiter, loudness compensation |
| `python-numpy` | no | measuring |
| `python-scipy` | no | measuring |

Only measuring waits on the two Omarchy does not ship. The panel checks all
three and offers to install whichever are missing, so removing one by hand is
recoverable without a terminal. The plugin uses Arch's system Python so its DSP
environment is deterministic even when another Python is first on `PATH`.

## Research basis

The target curve follows the in-room response listeners prefer in controlled
tests (Olive, Welti and McMullin), which slopes gently down rather than being
flat. Fixing peaks while leaving dips largely alone follows Toole and
Välimäki: a resonance is a property of the speaker, while a deep null is
usually interference that moves with your head. Smoothing follows the ear's
critical bandwidth rather than a fixed fraction of an octave. Loudness
compensation uses the ISO 226:2023 equal-loudness contours, and Deep bass
relies on the missing fundamental, first described by Seebeck in 1841.

## Licence

MIT.
