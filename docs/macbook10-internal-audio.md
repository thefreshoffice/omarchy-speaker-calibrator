# MacBook10,1 internal-audio setup

The 12-inch MacBook (2017, `MacBook10,1`) can calibrate its built-in speakers
with both built-in microphone channels. No external microphone is required.

## When the devices appear but calibration fails

On the tested system, the stock CS4208 driver selected a MacBook Air pin
layout through the shared PCI subsystem ID `8086:7270`. It selected microphone
pin `0x1c` instead of `0x19` and an analog speaker path instead of the digital
speaker path at `0x1d`. PipeWire listed the devices, but test tones were silent
and the microphone recording contained no matching tone frequencies.

## Set up audio before measuring

1. Install the [macbook12-audio-driver](https://github.com/leifliddy/macbook12-audio-driver)
   using its DKMS instructions and headers that match the running kernel.
   Reboot after installing the replacement driver. Replacing only the codec
   module in a running system caused a kernel fault during testing.
2. Apply the driver's [required software-volume setup](https://github.com/leifliddy/macbook12-audio-driver#3-speaker-volume-required).
   The digital speaker path has no usable hardware volume control. With
   WirePlumber 0.5+, the configuration can live in
   `~/.config/wireplumber/wireplumber.conf.d/51-macbook-cs4208-softvol.conf`:

   ```text
   monitor.alsa.rules = [
     {
       matches = [ { device.name = "alsa_card.pci-0000_00_1f.3" } ]
       actions = { update-props = { api.alsa.soft-mixer = true } }
     }
   ]
   ```

   Follow the driver's instructions to set and save the hardware mixer values
   and restart WirePlumber. Start with a low software playback volume.
3. Confirm that you can hear audio through the built-in speakers. Select the
   internal microphone and confirm that a short recording captures speech.
   Device names and moving playback meters alone do not prove either path.
4. Run **Calibrate speakers** with the internal microphone array selected, then
   **Check the calibration**. Keep the normal measurement quality checks enabled.

The successful measurement used hardware Capture at 0 dB and Internal Mic Boost
at 0 dB. Built-in microphone measurements remain relative estimates because
the microphone response and chassis coupling are unknown.

## Tested configuration — 2026-09-18

| Component | Version or ID |
| --- | --- |
| Hardware | MacBook10,1; CS4208 `1013:4208`, codec subsystem `106b:6600` |
| System | Omarchy `4.0.4-1`, kernel `7.2.5-3-omarchy` |
| Driver | [`4cdfcdb`](https://github.com/leifliddy/macbook12-audio-driver/commit/4cdfcdbac2db3f300cc45d9679cfd21df6590a8a), installed through DKMS |
| Plugin | `08be2c3` with the two-line bytecode fix from [PR #9](https://github.com/thefreshoffice/omarchy-speaker-calibrator/pull/9) |

The internal microphone captured the scheduled 800, 1600, and 2400 Hz speaker
tones. Calibration accepted both channels with zero clipped samples. After a
full audio-controller teardown and reload of the installed driver, calibration
verification passed again: 0.17 dB worst repeatability, 1.15 dB RMS model error,
and measured target error reduced from 9.82 to 3.96 dB. The user also confirmed
working audio.

The installed-driver reload and audio-service restart were tested. A full
post-install reboot, suspend/resume, and later kernel upgrades were not tested.
