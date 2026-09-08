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
Q (width). Built-in microphones are limited to six broad filters with Q 0.5–2.0;
an uncalibrated external microphone may use eight, and a calibrated external
microphone may use ten with narrower Q up to 4.0. These are upper bounds, not
targets: a broad problem that needs one filter gets one filter rather than a
dense fixed bank.

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

### Warm or Flat?

- **Warm** makes sharp voices, cymbals, and hiss gentler. It sounds softer and
  can be easier to enjoy for a long time.
- **Flat** keeps the sound more balanced without adding Warm's extra softness.
  It preserves more clarity and may sound a little brighter. Flat does not mean
  the measured graph must become a perfectly straight line.

The built-in microphone is useful for a rough first profile. A measurement mic
placed on-axis at normal listening distance is recommended for final tuning.

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

- Each parametric cut is clamped to -6 dB; boosts require the confidence and
  held-out-repeat gates above.
- With an uncalibrated built-in microphone, bass and extreme-treble cuts use
  tighter frequency-dependent limits; the full -6 dB range remains available in
  the more reliable midrange.
- Positive correction is matched by automatic input trim plus 1 dB margin.
- Two 55 Hz high-pass sections reduce wasteful driver excursion.
- The limiter has auto-level and boost disabled.
- The limiter ceiling is -1 dBFS.
- The generated filter output is pinned to the selected physical sink.
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
