# Omarchy Speaker Calibrator

An Omarchy bar-panel plugin that measures speakers through PipeWire using either
an internal microphone or an external/USB calibration microphone. Its native
panel handles device selection, measurement quality, filter review, install,
status, and disabling without opening a terminal. It generates a subtractive-first
EQ, a protective 55 Hz high-pass, automatic headroom, and a -1 dBFS
look-ahead limiter.

Phase 1 uses three repeated exponential sine sweeps per speaker. Left and right
are measured independently. The analyzer deconvolves the response with the
band outside the sweep rolled off, finds the direct sound, and reads the
magnitude through a frequency-dependent window of 15 cycles: 150 ms at 100 Hz,
15 ms at 1 kHz, 1.5 ms at 10 kHz. That keeps the desk reflection a listener at
the laptop also hears, while dropping later room reflections and the distortion
products a sine sweep folds into negative time, so the filters are fitted to
the sound of the speaker rather than to the room around the microphone. The
room sound recorded between sweeps is put through the identical deconvolution
and window to measure the noise floor at every frequency; the resulting
signal-to-noise ratio widens the optimizer's uncertainty wherever the noise
floor is close, which on small speakers is mostly the bass. The analyzer also
estimates and corrects playback/recording clock drift, and checks clipping,
sweep prominence, alignment, repeatability, microphone gain stability, and
harmonic residuals. Measurements that fail a quality gate cannot be installed.

When a built-in microphone source exposes two or more input channels, the panel
recommends **All built-in microphones**. Every channel is analyzed independently;
their raw recordings are never mixed. The analyzer aligns broad microphone gain,
combines the magnitude responses with a robust median, and adds disagreement
between microphones to the uncertainty curve. A failed microphone channel is
ignored when another is reliable. With three or more microphones, a clear broad
response outlier is also rejected. This makes the optimizer more cautious where
the laptop's microphones disagree instead of creating artificial phase
cancellation.

The quality model compares perceptually broad response rather than treating raw
narrow-bin room variance as failure. When all three sweeps agree they are kept;
when one capture is contaminated, the closest repeatable pair may be retained and
the outlier is reported. The panel plots left/right response with a shaded
repeat-uncertainty band. Accepted repeat-response curves are also retained (never
the raw microphone waveforms) for independent optimizer validation.

Before the sweeps, a short level search plays a brief left/right probe at a
quiet level, measures the microphone peak, and moves the sweep level toward a
-6 dBFS microphone peak, backing off by 12 dB whenever a probe clips. It
usually needs two probes and never leaves a window of 24 dB below to 6 dB above
the output's default sweep level, so a hot microphone is caught before the long
sweeps and a quiet one is raised instead of measuring into the room noise. The
search stops with guidance instead of measuring when the room is already loud
before the probe starts, when the microphone peak does not follow the sweep
level (automatic gain control, an overloaded microphone, or other audio
playing), or when nothing is heard at all; the error names any application that
is playing audio at the time. The level check on the finished measurement is
based on the accepted sweep peaks rather than a fixed laptop volume percentage. It reports when the microphone signal was too low or came too
close to clipping. An isolated clipped sweep is discarded when two clean repeats
for that speaker remain; widespread clipping still rejects the measurement.
Because built-in calibration is relative, scalar built-in-microphone gain movement
is reported as a warning when the response shape still repeats. External
measurement microphones retain the stricter stable-gain requirement.

Phase 2 uses a fully adaptive parametric EQ against a smooth, pleasant in-room
loudspeaker target: a gentle bass rise, flat midband, and gradual treble decline.
It chooses the number of filters and optimizes every filter's frequency, gain, and
Q (width). Built-in microphones are limited to six sections with Q 0.5–2.0;
an uncalibrated external microphone may use eight, and a calibrated external
microphone may use ten with narrower Q up to 4.0. These are upper bounds, not
targets: a broad problem that needs one filter gets one filter rather than a
dense fixed bank. A residual that stays high all the way to the bass or treble
end of the band is offered a cut-only low or high shelf, so a tilt costs one
section instead of several overlapping peaking filters. Depth is limited on
the whole correction rather than per filter: one section may cut up to 12 dB
and the sum of all sections never goes below -15 dB at any frequency, with
tighter limits toward the band edges for built-in microphones, so stacked
shallow cuts can no longer add up past what one deep cut is allowed to do.

Before anything is fitted, the response is smoothed by the ear's own
resolution rather than by a fixed fraction of an octave. The window follows the
critical band, which is a wide fraction of an octave in the bass and about a
sixth of an octave from 1 kHz upward, so the unreliable low end is averaged
broadly while the midrange keeps its detail. The average is taken over cubed
amplitudes, which lets peaks survive and largely fills narrow dips: a resonance
is audible and worth removing, while a cancellation of the same depth mostly is
not, and filling one with gain achieves nothing. Curves that are differences
rather than responses, such as what the check feeds back, use a plain mean
instead.

Filter selection is cross-validated. The optimizer fits using earlier accepted
repeat groups and adds a candidate only when a separate held-out repeat also
improves without making the worst repeat worse. After the filter structure has
passed that test, its parameters are refit to the robust aggregate and kept only
if held-out behavior remains safe. The graph shows measured channels,
uncertainty, predicted corrected response, filter-center ticks, and the aligned
target as a dotted line. This is a loudspeaker/room transfer target, not a
headphone HRTF target.

The optimizer is deliberately asymmetric. It aligns the target low enough that
cuts are the normal solution and penalizes boosts more than ten times as strongly.
A positive band is considered only when a deficit is broad, repeatable, locally
high-confidence, above the measured bass roll-off, below 8 kHz, and beneficial on
the held-out repeat. Built-in mic measurements can boost at most +1.5 dB; an
uncalibrated external mic at most +2 dB; a calibrated external mic at most +3 dB.
The actual full-filter response is calculated from exact biquad transfer
functions, and input trim reserves its positive peak plus 1 dB before the limiter.

## Usage

Install the required system packages, then add the plugin from its Git repository:

```bash
omarchy pkg add python-numpy python-scipy lsp-plugins-lv2
omarchy plugin add https://github.com/michaeldeby/omarchy-speaker-calibrator.git --enable
```

Omarchy clones the repository into `~/.config/omarchy/plugins/local.speaker-calibrator/`,
validates `manifest.json`, and asks where to place the bar widget. Review third-party
plugin code before enabling it because shell plugins run unsandboxed inside the
long-lived Omarchy shell process.

- Click the speaker icon to open or close the calibration panel.
- Middle-click refreshes the detected audio devices.
- The panel is built for someone with no audio knowledge: pick the speakers
  and microphone (the built-in ones are preselected), press **Calibrate
  speakers**, and the result installs itself when the measurement passes.
  Two toggles change the sound at once from the last measurement, without new
  sweeps: **Loudness** for a fuller sound with more bass, like the loudness
  button on a stereo, and **Make it louder** to give back the volume the
  correction takes away. A **Calibration** toggle switches the filters off and
  on live, so anyone can hear before and after. An equalizer view shows each
  filter as a coloured band with a node at its frequency and gain, and the
  combined curve on top.
- **Advanced** reveals the voicing, the three-level loudness setting, the
  microphone channel and calibration file, refit and install controls, the
  measurement quality and response graph, the list of sections, the switch
  between the two stored profiles, and the stop button.
- For a multi-microphone laptop, keep **All built-in microphones — recommended**
  selected. Individual channels remain available for troubleshooting.
- Measure first, review the proposed filters and graph, then explicitly install
  the profile.
- After a second install, **Hear the previous profile** plays the profile you
  were listening to before, and pressing it again returns to the new one, so two
  measurements or two plugin versions can be compared by ear on the same music.
  The switch updates the running filter in place, so playback continues; the
  tuning is only restarted once, when the running graph still has an older
  shape.
  The command-line equivalent is `compare-toggle`; `status` says which one is
  playing.

The wizard asks you to select a physical speaker sink, microphone, microphone
channel, and flat or warm voicing. For an external microphone, an optional
serial-number calibration text file can be supplied. Keep the room quiet and do
not move the computer or microphone during the roughly 30-second level check and
sweep sequence.

### Loudness compensation

Quiet music sounds thin because the ear loses bass as the level drops, not
because the speakers do. **Loudness compensation** in Advanced puts back what
hearing drops, using the ISO 226:2023 equal-loudness curves, and follows the
volume so the amount is right at every setting.

It is the only part of this that needs something running in the background: the
compensator has to be told the listening level, and only the output device
knows it, so a small service watches the volume and passes it on. That service
writes nothing but the compensator's own controls, and switches the
compensation off when it stops, because a quiet-level curve left applied at
high volume would be heard as far too much bass.

The service stays registered with systemd between switches rather than being
installed and removed each time, because registering it costs longer than
switching the sound does and the toggle should be quick enough to hear the
difference. It is only ever running while the compensation is on: started with
the compensation off, it reads that and stops again.

Two honest caveats. The compensator's volume control attenuates as well as
selecting the curve, so the input gain cancels that and leaves the curve alone;
the output device keeps doing the actual attenuating, which means this never
fights the volume keys. And how much compensation a given volume earns depends
on what full volume is in real decibels, which no uncalibrated measurement can
tell us; the assumption is that full volume is a normal listening level. The
shape of the correction is right regardless, only the amount depends on that.
Whatever it lifts below the speaker's knee is removed again by the high-pass,
so on a small laptop speaker most of its effect lands between the knee and the
midrange.

### Channel balance

A broadband level difference between the two speakers pulls the stereo image
off centre, and trimming it is the one per-channel correction worth making. A
difference in *shape* between the channels would need a second set of filters
and is far too easy to get wrong on an uncertain measurement, so it is not
attempted.

The trim is off by default and refuses to act on a built-in microphone array at
all. Those microphones sit centimetres from the speakers and closer to one than
the other, so each mostly hears its own side. Measured here, the two built-in
microphones disagreed about which speaker was louder, by more than the
difference they were trying to report, while their combination put the real
difference near a tenth of a decibel. There is nothing to correct and the
measurement cannot see it anyway.

With an external microphone at the listening position the difference is real
and is what you hear. Set **Channel balance** to automatic and it is trimmed,
but only when the difference stands at least twice clear of the spread the
measurement itself shows, only by turning the louder side down so no headroom
is spent, and never by more than 3 dB. A larger difference is a wiring or
placement fault that the panel reports rather than hides. Even when it is
switched off, the advanced view shows the difference it measured, so it is
visible without acting.

### Flat or Warm?

- **Flat** is the default. It keeps the sound balanced, preserves the most
  clarity, and may sound a little brighter. Flat does not mean the measured
  graph must become a perfectly straight line.
- **Warm** makes sharp voices, cymbals, and hiss gentler. It sounds softer and
  can be easier to enjoy for a long time.

### Bass: Normal or Full?

- **Normal** applies the measured correction only.
- **Full** adds a +3 dB low shelf placed above the protective high-pass, at
  the measured knee or two and a half times the high-pass corner, whichever is
  higher, clamped to 150–600 Hz. A low shelf reaches its full lift below its
  corner, so putting the corner on the high-pass would drop the whole boost
  into the band the high-pass has just removed. Placed clear of it, the lift
  lands where the driver still turns voltage into sound. Like any boost it is
  paid for by input trim, so pair it with Balanced or Matched loudness to keep
  the level.

### Loudness: Protected, Balanced, or Matched?

Every correction is a cut, and the deepest cuts land where the speaker was
loudest, so a corrected speaker plays quieter at the same volume setting. The
optimizer estimates that loss as the A-weighted level of pink noise through the
response before and after correction.

- **Protected** adds nothing back. It is the cleanest choice and keeps the full
  1 dB limiter margin plus any boost headroom.
- **Balanced** adds back half of the lost loudness as input gain.
- **Matched** adds back all of it, up to 6 dB. The -1 dBFS limiter absorbs the
  peaks, so at high volume it works harder and loud passages are held down.

Both choices can be changed after a measurement: **Refit saved measurement**
re-runs the analysis and the fit on the recorded sweeps without playing
anything. A refit is only a proposal; nothing changes in the sound until
**Install and play** is pressed. Installing switches live, and the compare
button then names the other stored profile it would switch to, so the
variants can be compared by ear.

The built-in microphone is useful for a rough first profile. A measurement mic
placed on-axis at normal listening distance is recommended for final tuning.

### Always measured raw

A calibration measures the bare speakers. The sweeps are played straight at the
physical output, which bypasses the filter chain, and the running filter is
flattened for the duration as well, so the measurement is of the raw speakers
even if the stream were routed through the correction. The calibrated sink is
never offered as a measurement target, and a saved capture records whether it
was taken raw; one taken through the correction cannot be refitted. Only the
check below deliberately measures through the correction.

### Checking the result

**Check the calibration** in Advanced measures a second time, this time through
the corrected output, and compares what came out with what the fit predicted.
It answers two separate questions. Did the filters do what the fit said they
would, which tests the model rather than the taste? And is the result closer to
the target than the raw speaker was, which tests whether the correction was
worth applying at all?

The deep-bass add-on is muted for the duration. It invents harmonics that no
linear model predicts, and it puts them just above the high-pass corner, so
leaving it running would be measured as several decibels of error exactly
there and feeding that back would have the optimizer cut away the bass the
add-on had just added.

Both curves are level-aligned before comparison, because the sweep level and
the input trim differ between the two measurements and only shape matters. The
high-passed region and any band where the check itself sank into the room noise
are left out, since neither says anything about the filters. A result more than
4 dB from the plan is reported as a failure, which usually means the wrong
output was measured, the microphone moved, or something else was playing.

The check writes its own capture files, never the calibration capture, so a
later refit can never fit an already-corrected recording.

### Deep bass, an optional add-on

Small speakers cannot move enough air to make a low note at all. Rather than
asking them to try, the **Deep bass** switch plays the harmonics of those notes,
which the speakers can produce, and the ear supplies the fundamental it never
heard. This is what laptop and phone makers do to get bass out of hardware that
has none.

#### Why this works

A note is not only its fundamental. A bass guitar playing a 55 Hz note also
radiates energy at 110, 165, 220 Hz and beyond, and the ear works out the pitch
from the *spacing* of that series rather than from the presence of the lowest
tone. Remove the fundamental entirely and the pitch does not change: the brain
still reports 55 Hz, because only a 55 Hz note produces harmonics spaced 55 Hz
apart. This is the missing fundamental, described by Seebeck in 1841, and it is
why a telephone limited to 300 Hz and above still carries a voice whose
fundamental is near 100 Hz.

Your speakers stop somewhere between 150 and 250 Hz, so a bass line's
fundamentals are simply absent. The add-on takes what lies below that corner,
generates its second and third harmonics, and plays those instead. They land
where the speaker is efficient, so they are actually heard, and the ear
reconstructs the pitch that was never reproduced. The bass line becomes
audible, in tune, and follows the music.

Compare that with the obvious alternative of turning the bass up. Below the
corner the cone still travels its full distance while radiating almost nothing,
because a small driver cannot move enough air at long wavelengths. Boosting
there buys distortion and stolen headroom and no more sound. Playing the
harmonics asks the speaker only for frequencies it is good at.

What it cannot do is give you the physical weight of real low frequencies,
because nothing is moving that much air. It supplies pitch and line, not
impact. Pushed hard it also turns honky, as the added harmonics start to
compete with the music's own midrange, which is why the amount here is modest
and the upper limit follows the measured knee rather than a fixed frequency.
The same trick, under various names, is what laptop and phone makers use to get
bass out of hardware that has none.

It needs one free package, `bankstown`. Nothing else here depends on it:
without it the filter chain is built exactly as before and the switch offers to
install it. Pressing the switch the first time opens a terminal running the
install, so it is visible and asks for the password itself; pressing it
afterwards switches the effect on and off live, with no new measurement and no
refit.

Which command is used is decided when the switch is pressed. A configured
repository is preferred, so `omarchy pkg add bankstown` is used if any
repository carries it, including Omarchy's own; otherwise it falls back to
`omarchy pkg aur add bankstown`, which builds it from the Arch User Repository.
Nothing needs changing here if the package later appears in a repository.

While no repository carries it, the panel warns before installing that the
package is not one of Omarchy's curated ones, that anyone can publish to the
AUR, that it is built from source on the machine, and where to read it first.
The warning disappears once a repository carries it or once it is installed.

The two frequency limits follow the measurement rather than being fixed: the
harmonics are made from what lies below the measured knee and kept above it,
which is the only place the speaker can reproduce them. The generated graph
includes the add-on only when an installed copy has every port this expects, so
a partial or different build can never stop the filter chain from loading.

### Improving from the check

Because the check measures the speaker through a correction whose response is
known exactly, whatever the result differs from the prediction by is what the
original measurement got wrong. **Improve from the check** adds that difference
back into the raw estimate and fits again from scratch, so the filter count and
the depth limits stay where they were rather than accumulating a second
correction on top of the first.

The difference is shrunk toward zero where it is comparable with what the check
itself could resolve, which two consecutive checks on a built-in microphone
array put at about 1 dB, so repeating the check cannot inject its own noise
into the next fit. One round may move the estimate by at most 8 dB at any
frequency, and only inside the band the check could judge. The adjustment is
level-neutral, so iterating never drifts the overall loudness.

Check again after improving to see whether it helped: the distance between plan
and measurement should shrink each round. What is learned is stored with the
profile, so changing the voicing or the toggles afterwards keeps it.

## Runtime dependencies

The panel intentionally uses Arch's system Python so its DSP environment is
deterministic even when a user-managed Python is first on `PATH`.

```text
python-numpy
python-scipy
lsp-plugins-lv2
pipewire
```

Install missing DSP packages with:

```bash
omarchy pkg add python-numpy python-scipy lsp-plugins-lv2
```

## Safety model

- One section may cut at most 12 dB, and the whole correction is held to
  -15 dB at any frequency inside the fit; boosts require the confidence and
  held-out-repeat gates above.
- With an uncalibrated built-in microphone the total depth is limited by
  frequency, from -6 dB at 160 Hz and -8 dB at 10 kHz to the full -15 dB in
  the more reliable midrange.
- Shelves are cut-only; the only shelf boost is the full-bass option, which
  is paid for by input trim.
- A protective high-pass is placed where the measurement says the speaker
  stops keeping up: the highest frequency below 400 Hz at which it falls more
  than 15 dB short of its target, clamped to 50-200 Hz. Below the corner the
  cone still travels as far as ever while producing almost nothing, so that
  content is removed rather than amplified. The slope doubles to 4th order
  only when the corner is at or below 100 Hz, where a steep filter cannot be
  heard. The high-pass is added to the target as well, so the optimizer never
  spends filters or headroom boosting back what was deliberately removed.
- Positive correction is matched by automatic input trim plus 1 dB margin.
- Loudness make-up is limited to 6 dB and only ever pays back loudness that
  the cuts removed; the limiter ceiling stays at -1 dBFS. Because the input
  trim already pays for every positive correction, including the full-bass
  shelf, no band is ever driven more than 5 dB harder than the uncorrected
  speaker at the same volume setting.
- The high-pass sections reduce wasteful driver excursion; an unused second
  section is parked at 10 Hz rather than removed, so the graph keeps its shape
  and profiles stay switchable without restarting the tuning.
- The limiter has auto-level and boost disabled.
- The limiter ceiling is -1 dBFS.
- The generated filter output is pinned to the selected physical sink.
- Switching the calibration off level-matches the plain speakers to the
  loudness the correction plays at, so the comparison is about tone rather
  than volume; louder wins otherwise. It only ever turns the plain sound down,
  never up, and a calibration measurement flattens the filter without that
  attenuation, since the speaker has to be measured as it is.
- The graph always has the same shape: two high-pass sections, a low shelf,
  twelve parametric slots, a high shelf, and the limiter, per channel. Unused
  sections sit at 0 dB. Installing or comparing profiles therefore updates the
  running filter's controls instead of restarting the PipeWire client.
- Existing tuning files are backed up before replacement.
- Failed or clipped measurements are saved for diagnosis but cannot be installed.
- Built-in speakers start from a -12 dBFS sweep at the requested 50-70% hardware
  level and external outputs from a quieter -27 dBFS default; the level search
  then adjusts within -24/+6 dB of that default and lowers the level whenever a
  probe clips.
- Multi-microphone disagreement increases optimizer uncertainty and therefore
  suppresses risky boosts.

## Research basis

The target uses the broad shape supported by controlled loudspeaker preference
research while keeping the exact bass/treble amount conservative for a laptop.
The confidence-weighted, regularized inverse approach follows the same safety
principle as frequency-dependent regularization: do less where the measurement
or loudspeaker is unreliable.

- [AES: A Virtual Headphone Listening Test Methodology](https://aes.org/publications/elibrary-page/?id=17042)
- [ITU-R BS.1116-3 listening-test guidance](https://www.itu.int/rec/R-REC-BS.1116-3-201502-I/en)
- [Robust room equalization using frequency-dependent regularization](https://link.springer.com/article/10.1186/s13636-022-00247-6)
- [PipeWire filter-chain documentation](https://pipewire.pages.freedesktop.org/pipewire/page_module_filter_chain.html)
