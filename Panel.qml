import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "thefreshoffice.speaker-calibrator"
  ipcTarget: "thefreshoffice.speaker-calibrator.panel"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  readonly property string helperPath: decodeURIComponent(
    String(Qt.resolvedUrl("speaker-calibrate.py")).replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Util.alpha(foreground, 0.62)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // The sound options, shared by the simple toggles and the advanced
  // selectors.  They follow the installed profile until the user changes them.
  property string voicingMode: "neutral"
  property string bassMode: "normal"
  property string loudnessMode: "protected"
  property string channelTrimMode: "off"
  property bool advanced: false
  property bool _optionsAdopted: false
  // Selected device rows; -1 until the device list arrives.
  property int sinkIndex: -1
  property int micIndex: -1

  function open() {
    root.controller.show()
    // Start at the top: reopening halfway down the page loses the reading of
    // the calibration the panel exists to give.
    scroller.contentY = 0
    service.refresh()
  }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? close() : open() }
  function refresh() { service.refresh() }
  function closeForPopoutSwitch() { close() }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  // ---- devices -------------------------------------------------------------
  function channelOptions() {
    var mic = service.microphones[root.micIndex]
    var count = mic ? Math.max(1, Number(mic.channels || 1)) : 1
    var result = []
    if (mic && mic.internal === true && count > 1) {
      result.push("All " + count + " built-in microphones — recommended")
      for (var index = 0; index < count; index++)
        result.push("Microphone " + (index + 1) + " only")
    } else {
      for (index = 0; index < count; index++) result.push("Channel " + (index + 1))
    }
    return result
  }
  function selectedChannelValue() {
    var mic = service.microphones[root.micIndex]
    var count = mic ? Math.max(1, Number(mic.channels || 1)) : 1
    if (mic && mic.internal === true && count > 1)
      return channelBox.currentIndex === 0 ? "all" : channelBox.currentIndex - 1
    return channelBox.currentIndex
  }
  function multiMicAvailable() {
    var mic = service.microphones[root.micIndex]
    return !!(mic && mic.internal === true && Number(mic.channels || 1) > 1)
  }
  function selectedMicIsInternal() {
    var mic = service.microphones[root.micIndex]
    return mic ? mic.internal === true : true
  }
  // Zero-knowledge default: the laptop's own speakers and microphones.
  function selectInternalDevices() {
    var sink = -1
    for (var sinkIndex = 0; sinkIndex < service.sinks.length; sinkIndex++)
      if (service.sinks[sinkIndex].internal === true) { sink = sinkIndex; break }
    root.sinkIndex = sink >= 0 ? sink : (service.sinks.length > 0 ? 0 : -1)
    var mic = -1
    for (var micIndex = 0; micIndex < service.microphones.length; micIndex++)
      if (service.microphones[micIndex].internal === true) { mic = micIndex; break }
    root.micIndex = mic >= 0 ? mic : (service.microphones.length > 0 ? 0 : -1)
  }

  // ---- options -------------------------------------------------------------
  function options() {
    return { voicing: root.voicingMode, bass: root.bassMode, loudness: root.loudnessMode,
             channelTrim: root.channelTrimMode, micCalibrationFile: micCalPath.text.trim() }
  }
  function hasMeasurement() {
    return service.proposal !== null && service.proposal !== undefined
      && service.proposal.measurement !== undefined
  }
  // A saved measurement is re-fitted and applied at once, so a toggle is heard
  // immediately.  Without one the options simply wait for the next calibration.
  function applyOptions() {
    if (root.hasMeasurement()) service.refit(root.options(), true)
  }
  function adoptOptions(profile) {
    if (!profile) return
    root.voicingMode = profile.voicing === "warm" ? "warm" : "neutral"
    root.bassMode = profile.bass === "full" ? "full" : "normal"
    root.loudnessMode = profile.loudness || "protected"
    root.channelTrimMode = profile.channel_trim === "auto" ? "auto" : "off"
  }

  // One line under the Deep bass switch: what it is, or what pressing it does.
  function deepBassDescription() {
    var addon = service.status.bassEnhancer || {}
    if (addon.installed === true && addon.usable !== true)
      return "The installed add-on is not the one this expects, so it is left out."
    if (addon.usable !== true)
      return "Your speakers are too small to make low notes at all. This plays their "
        + "harmonics instead, and your ear fills in the note that is missing. "
        + "It needs a small free add-on"
        + (addon.source && addon.source !== "AUR" ? " from the " + addon.source + " repository" : "")
        + "; press to install it."
    return service.status.deepBass === "on"
      ? "On. Low notes are suggested by their harmonics, which these speakers can play."
      : "Off. Press to hear low notes suggested by their harmonics."
  }
  // Shown before anything is installed, and only when the package would come
  // from the AUR rather than a curated repository.
  function bassWarningText() {
    var addon = service.status.bassEnhancer || {}
    if (addon.usable === true) return ""
    if (addon.source && addon.source !== "AUR") return ""
    var name = addon.package || "bankstown"
    return "This add-on is not one of Omarchy's own packages. It comes from the AUR, "
      + "where anyone can publish, and it is built from source on your machine. "
      + "Nobody has checked it for you. You can read it first at "
      + "aur.archlinux.org/packages/" + name + "."
  }
  // "Recalibrate" says nothing about which microphone did the one in use.
  // Each row now carries its own history: whether it has measured at all,
  // when, and whether that measurement is the calibration playing right now.
  function microphoneNote(entry) {
    var archive = (service.status.microphones || {})
    var record = entry.internal ? archive.internal : archive.external
    if (!record) return "Never measured"
    // One short line that fits: a date, whether this is the calibration
    // playing, and a word about its quality only when there is one to say.
    var when = Qt.formatDate(new Date(record.created_at), "d MMM yyyy")
    var active = ((service.status.profile || {}).microphone || {})
    var parts = ["Measured " + when]
    if (active.internal === entry.internal) parts.push("the calibration in use")
    if (record.verdict && record.verdict !== "pass") parts.push(record.verdict)
    return parts.join("  ·  ")
  }
  // What the two microphones say, in words rather than decibels.
  function microphoneComparisonText() {
    var comparison = service.micComparison
    if (!comparison) return "Reading the stored measurements…"
    var haveInternal = !!comparison.internal
    var haveExternal = !!comparison.external
    if (!haveInternal && !haveExternal)
      return "Nothing measured yet. Calibrate once and the measurement is kept here."
    if (!haveExternal) {
      // Whether one is plugged in right now is a different question from
      // whether one has ever measured, and confusing the two reads as the
      // microphone not being detected.
      var connected = null
      for (var i = 0; i < service.microphones.length; i++)
        if (service.microphones[i].internal === false) {
          connected = service.microphones[i].description
          break
        }
      if (connected)
        return "Only the built-in microphones have measured so far. " + connected
          + " is connected and ready: choose it under MICROPHONE, place it where you "
          + "listen, and calibrate. Both curves then appear here together."
      return "Only the built-in microphones have measured so far, and no measuring "
        + "microphone is connected. Plug a USB one in, middle-click the bar icon to "
        + "pick it up, and calibrate with it placed where you listen."
    }
    if (!haveInternal)
      return "Only the measuring microphone has measured so far. Measure again with "
        + "the built-in microphones and both curves appear here together."
    var worst = comparison.worst
    if (!worst) return "Both measurements are stored."
    var amount = Math.abs(Number(worst.difference_db)).toFixed(1)
    var direction = Number(worst.difference_db) > 0 ? "more" : "less"
    return "The built-in microphones read " + amount + " dB " + direction + " "
      + worst.band + " than the measuring microphone, and differ by "
      + Number(comparison.rms_difference_db).toFixed(1) + " dB overall. That gap is "
      + "the microphone, not the speakers: the built-in ones sit inside the case, "
      + "inches from one driver, while the measuring one sits where you listen."
  }
  // Draw both measured curves on one set of axes, shape only.
  function paintMicrophones(canvas, comparison) {
    var context = canvas.getContext("2d")
    context.clearRect(0, 0, canvas.width, canvas.height)
    if (!comparison) return
    var padLeft = 34, padRight = 8, padTop = 8, padBottom = 20
    var plotWidth = canvas.width - padLeft - padRight
    var plotHeight = canvas.height - padTop - padBottom
    var minFrequency = 80, maxFrequency = 16000, minDb = -18, maxDb = 12
    function xFor(frequency) {
      return padLeft + (Math.log(frequency / minFrequency)
        / Math.log(maxFrequency / minFrequency)) * plotWidth
    }
    function yFor(value) {
      return padTop + ((maxDb - Math.max(minDb, Math.min(maxDb, value)))
        / (maxDb - minDb)) * plotHeight
    }
    context.lineWidth = 1
    context.strokeStyle = root.dim
    context.fillStyle = root.dim
    context.font = "9px " + root.fontFamily
    var decades = [100, 200, 500, 1000, 2000, 5000, 10000]
    for (var d = 0; d < decades.length; d++) {
      var gx = xFor(decades[d])
      context.globalAlpha = 0.25
      context.beginPath(); context.moveTo(gx, padTop)
      context.lineTo(gx, padTop + plotHeight); context.stroke()
      context.globalAlpha = 1
      context.fillText(decades[d] >= 1000 ? (decades[d] / 1000) + "k" : String(decades[d]),
                       gx - 8, canvas.height - 6)
    }
    for (var level = minDb; level <= maxDb; level += 6) {
      var gy = yFor(level)
      context.globalAlpha = level === 0 ? 0.5 : 0.2
      context.beginPath(); context.moveTo(padLeft, gy)
      context.lineTo(padLeft + plotWidth, gy); context.stroke()
      context.globalAlpha = 1
      context.fillText((level > 0 ? "+" : "") + level, 4, gy + 3)
    }
    function trace(record, colour, width) {
      if (!record) return
      var frequencies = record.frequency_hz || []
      var response = record.response_db || []
      var count = Math.min(frequencies.length, response.length)
      if (count < 2) return
      context.strokeStyle = colour
      context.lineWidth = width
      context.beginPath()
      var started = false
      for (var i = 0; i < count; i++) {
        var frequency = Number(frequencies[i])
        if (frequency < minFrequency || frequency > maxFrequency) continue
        var px = xFor(frequency), py = yFor(Number(response[i]))
        if (!started) { context.moveTo(px, py); started = true } else context.lineTo(px, py)
      }
      context.stroke()
    }
    trace(comparison.internal, root.dim, 1.5)
    trace(comparison.external, bar && bar.accent ? bar.accent : Color.accent, 2)
  }
  // Switching the correction off also drops the level to the one the
  // correction plays at, so the two can be judged on tone alone.
  function bypassMatchText() {
    var match = Number(((service.status.compare || {}).level_match_db) || 0)
    if (match > -0.05) return ""
    return ", matched " + Math.abs(match).toFixed(1) + " dB quieter so only the tone changes"
  }
  // The compensator needs to know the listening level, which only the output
  // device knows, so a small service follows the volume and passes it on.
  function loudnessCompensationDescription() {
    if (service.status.loudnessCompensation !== "on")
      return "Off. Quiet music loses its bass to the ear, not to the speakers. "
        + "Switching this on follows the volume and puts back what hearing drops, "
        + "using the ISO 226 equal-loudness curves."
    return "On, following the volume"
      + (service.status.loudnessTracker === "running" ? "." : " — but the service that "
         + "watches the volume is not running, so the amount is frozen where it was.")
      + " Full volume is the reference, so it does nothing there and more the further "
      + "down you play. The loudness stays the same either way; only the tone moves."
  }
  function heroMeta() {
    if (service.busy) return service.message
    if (!service.status.enabled)
      return service.status.profile ? "Calibration stopped" : "Not calibrated yet"
    if (service.status.bypass) return "Calibration off — hearing the plain speakers"
    var summary = service.playingSummary()
    var label = summary && summary.bass !== undefined
      ? service.simpleLabel(summary) : service.simpleLabel(service.status.profile)
    return "Calibrated" + (label ? " · " + label : "")
  }

  // ---- text blocks for the advanced view -------------------------------------
  function gainsText() {
    if (!service.proposal || !service.proposal.fit) return ""
    var fit = service.proposal.fit
    var filters = fit.filters || []
    var rows = []
    var labels = { peaking: "", lowshelf: "low shelf ", highshelf: "high shelf " }
    for (var index = 0; index < filters.length; index++) {
      var item = filters[index]
      var gain = Number(item.gain_db)
      rows.push((labels[item.type] || "") + Number(item.frequency_hz).toFixed(1) + " Hz   ·   Q "
        + Number(item.q).toFixed(2) + "   ·   "
        + (gain > 0 ? "+" : "") + gain.toFixed(2) + " dB")
    }
    return rows.join("   ·   ")
  }
  function fitSummaryText() {
    if (!service.proposal || !service.proposal.fit) return ""
    var fit = service.proposal.fit
    var validation = fit.cross_validation || {}
    return Number(fit.filter_count || 0) + " adaptive sections"
      + "   ·   weighted target error " + Number(fit.weighted_rmse_before_db || 0).toFixed(2)
      + " → " + Number(fit.weighted_rmse_after_db || 0).toFixed(2) + " dB"
      + "   ·   held-out repeat " + Number(validation.rmse_before_db || 0).toFixed(2)
      + " → " + Number(validation.rmse_after_db || 0).toFixed(2) + " dB"
      + "   ·   max boost " + Number(fit.actual_maximum_boost_db || 0).toFixed(2) + " dB"
      + "   ·   protected headroom " + Number(fit.headroom_db || 1).toFixed(2) + " dB"
      + (fit.deepest_correction_db !== undefined
          ? "   ·   deepest cut " + Number(fit.deepest_correction_db).toFixed(1) + " dB"
          : "")
      + (fit.bass_shelf
          ? "   ·   bass shelf +" + Number(fit.bass_shelf.gain_db).toFixed(1) + " dB below "
            + Number(fit.bass_shelf.frequency_hz).toFixed(0) + " Hz"
          : "")
      + (fit.loudness_loss_db !== undefined
          ? "   ·   loudness lost to cuts " + Number(fit.loudness_loss_db).toFixed(1) + " dB"
            + "   ·   make-up +" + Number(fit.makeup_db || 0).toFixed(1) + " dB (" + (fit.loudness_mode || "protected") + ")"
          : "")
  }
  function qualityMetricsText() {
    if (!service.proposal || !service.proposal.quality) return ""
    var metrics = service.proposal.quality.metrics || {}
    var result = "prominence " + Number(metrics.minimum_broadband_prominence_db || 0).toFixed(1) + " dB"
      + (metrics.snr_mid_db !== undefined ? "   ·   SNR mid " + Number(metrics.snr_mid_db).toFixed(1) + " dB" : "")
      + "   ·   repeatability " + Number(metrics.worst_repeatability_db || 0).toFixed(1) + " dB"
      + "   ·   stable " + Number(metrics.minimum_stable_band_percent || 0).toFixed(0) + "%"
      + "   ·   gain drift " + Number(metrics.worst_gain_stability_db || 0).toFixed(1) + " dB"
      + "   ·   clock " + Number(metrics.clock_drift_ppm || 0).toFixed(0) + " ppm"
      + "   ·   harmonic residual " + Number(metrics.worst_harmonic_residual_db || -120).toFixed(1) + " dB"
      + "   ·   mic peak " + Number(metrics.maximum_accepted_peak_dbfs || -120).toFixed(1) + " dBFS"
    var levelSearch = (service.proposal.measurement || {}).level_search
    if (levelSearch && levelSearch.selected_level_dbfs !== undefined)
      result += "   ·   sweep level " + Number(levelSearch.selected_level_dbfs).toFixed(1) + " dBFS"
        + " (" + (levelSearch.attempts || []).length + " probe"
        + ((levelSearch.attempts || []).length === 1 ? "" : "s") + ")"
    if (Number(metrics.microphone_channels_requested || 0) > 1)
      result += "   ·   microphones " + Number(metrics.microphone_channels_used || 0)
        + "/" + Number(metrics.microphone_channels_requested)
        + "   ·   mic spread " + Number(metrics.inter_microphone_spread_db || 0).toFixed(1) + " dB"
    return result
  }
  function qualityIssuesText() {
    if (!service.proposal || !service.proposal.quality) return ""
    var quality = service.proposal.quality
    return (quality.failures || []).concat(quality.warnings || []).join("\n")
  }
  function qualityGuidanceText() {
    if (!service.proposal || !service.proposal.quality) return ""
    return (service.proposal.quality.guidance || []).join("\n")
  }
  function microphoneArrayText() {
    if (!service.proposal || !service.proposal.measurement
        || !service.proposal.measurement.microphone_array) return ""
    var array = service.proposal.measurement.microphone_array
    var used = (array.used_channels || []).length
    var requested = (array.requested_channels || []).length
    return "Combined " + used + " of " + requested
      + " built-in microphones. Each microphone was analyzed separately; "
      + "agreement is trusted, while differences reduce correction confidence."
  }

  // ---- rows for the advanced view ---------------------------------------------
  function fmt(value, digits) {
    return (value === undefined || value === null || isNaN(Number(value))) ? "–" : Number(value).toFixed(digits)
  }
  function measurementRows() {
    if (!service.proposal || !service.proposal.quality) return []
    var m = service.proposal.quality.metrics || {}
    var rows = []
    var level = (service.proposal.measurement || {}).level_search
    if (level && level.selected_level_dbfs !== undefined) {
      var probes = (level.attempts || []).length
      rows.push({ key: "Sweep level", value: root.fmt(level.selected_level_dbfs, 1) + " dBFS after "
        + probes + (probes === 1 ? " probe" : " probes") + "  ·  " + String(level.status || "") })
    }
    rows.push({ key: "Mic peak", value: root.fmt(m.maximum_accepted_peak_dbfs, 1) + " dBFS  ·  " + String(m.measurement_level || "") })
    rows.push({ key: "Background", value: root.fmt(m.background_dbfs, 1) + " dBFS  ·  prominence " + root.fmt(m.minimum_broadband_prominence_db, 1) + " dB" })
    if (m.snr_mid_db !== undefined)
      rows.push({ key: "Signal to noise", value: "low " + root.fmt(m.snr_low_db, 0) + "  ·  mid " + root.fmt(m.snr_mid_db, 0)
        + (m.snr_high_db !== undefined ? "  ·  high " + root.fmt(m.snr_high_db, 0) : "") + " dB" })
    rows.push({ key: "Repeatability", value: root.fmt(m.worst_repeatability_db, 2) + " dB  ·  " + root.fmt(m.minimum_stable_band_percent, 0) + "% of the band stable" })
    rows.push({ key: "Gain drift", value: root.fmt(m.worst_gain_stability_db, 2) + " dB between sweeps" })
    rows.push({ key: "Clock drift", value: root.fmt(m.clock_drift_ppm, 0) + " ppm, corrected" })
    rows.push({ key: "Harmonic residual", value: root.fmt(m.worst_harmonic_residual_db, 1) + " dB" })
    if (Number(m.excluded_sweeps || 0) > 0)
      rows.push({ key: "Discarded sweeps", value: String(m.excluded_sweeps) })
    if (Number(m.microphone_channels_requested || 0) > 1)
      rows.push({ key: "Microphones", value: String(m.microphone_channels_used) + " of " + String(m.microphone_channels_requested)
        + " used  ·  spread " + root.fmt(m.inter_microphone_spread_db, 1) + " dB" })
    return rows
  }
  function issueLines() {
    if (!service.proposal || !service.proposal.quality) return []
    var quality = service.proposal.quality
    return (quality.failures || []).concat(quality.warnings || [])
  }
  function guidanceLines() {
    if (!service.proposal || !service.proposal.quality) return []
    return service.proposal.quality.guidance || []
  }
  function sectionRows() {
    if (!service.proposal || !service.proposal.fit) return []
    var fit = service.proposal.fit
    var names = { peaking: "peak", lowshelf: "low shelf", highshelf: "high shelf" }
    var rows = []
    var filters = fit.filters || []
    for (var index = 0; index < filters.length; index++) {
      var item = filters[index]
      var gain = Number(item.gain_db)
      rows.push([String(index + 1), names[item.type] || String(item.type),
                 root.fmt(item.frequency_hz, 0) + " Hz", root.fmt(item.q, 2),
                 (gain > 0 ? "+" : "") + gain.toFixed(2) + " dB"])
    }
    if (fit.bass_shelf)
      rows.push(["B", "bass shelf", root.fmt(fit.bass_shelf.frequency_hz, 0) + " Hz",
                 root.fmt(fit.bass_shelf.q, 2), "+" + root.fmt(fit.bass_shelf.gain_db, 2) + " dB"])
    var highpass = fit.highpass || {}
    var stages = Number(highpass.stages || fit.highpass_stages || 2)
    var trim = fit.channel_trim
    if (trim && trim.applied)
      rows.push(["BAL", "channel trim", "wideband",
                 "–", root.fmt(trim.left_db, 1) + " / " + root.fmt(trim.right_db, 1) + " dB"])
    rows.push(["HP", "high-pass" + (stages > 1 ? " ×" + stages : ""),
               root.fmt(highpass.frequency_hz || fit.highpass_hz || 55, 0) + " Hz",
               root.fmt(highpass.q || 0.707, 2), "–"])
    return rows
  }
  function verificationRows() {
    var check = service.status.verification
    if (!check) return []
    var model = check.model_error_db || {}
    var errors = check.target_error_db || {}
    var rows = [
      { key: "Checked", value: String(check.checked_at || "").slice(0, 16).replace("T", " ")
        + (check.profile_label ? "  ·  " + check.profile_label : "") },
      { key: "Follows the plan", value: "within " + root.fmt(model.rms, 2) + " dB"
        + "  ·  worst " + root.fmt(model.worst, 1) + " dB at " + root.fmt(model.worst_hz, 0) + " Hz" },
      { key: "Distance to target", value: root.fmt(errors.before, 2) + " dB raw  →  "
        + root.fmt(errors.planned, 2) + " dB planned  →  " + root.fmt(errors.measured, 2) + " dB measured" }
    ]
    var bands = model.bands || {}
    var parts = []
    for (var name in bands) parts.push(name + " " + Number(bands[name]).toFixed(1))
    if (parts.length > 0) rows.push({ key: "Off plan by band", value: parts.join("  ·  ") + " dB" })
    if (check.bass_enhancer_muted)
      rows.push({ key: "Deep bass", value: "muted for the check, since the harmonics it invents are not something the filters predict" })
    if (check.analysis_band_hz)
      rows.push({ key: "Judged over", value: root.fmt(check.analysis_band_hz[0], 0) + " Hz to "
        + root.fmt(check.analysis_band_hz[1] / 1000, 0) + " kHz  ·  " + String(check.analysed_points) + " points" })
    if (check.stale)
      rows.push({ key: "Note", value: "the calibration changed after this check, so it no longer describes what you hear" })
    return rows
  }
  function fitRows() {
    if (!service.proposal || !service.proposal.fit) return []
    var fit = service.proposal.fit
    var validation = fit.cross_validation || {}
    var rows = [
      { key: "Sections", value: String(fit.filter_count || 0) + " used of " + String(fit.maximum_filter_count || "?") + " allowed" },
      { key: "Target error", value: root.fmt(fit.weighted_rmse_before_db, 2) + " → " + root.fmt(fit.weighted_rmse_after_db, 2) + " dB weighted" },
      { key: "Held-out repeat", value: root.fmt(validation.rmse_before_db, 2) + " → " + root.fmt(validation.rmse_after_db, 2) + " dB  ·  " + String(validation.mode || "") },
      { key: "Deepest cut", value: root.fmt(fit.deepest_correction_db, 1) + " dB of " + root.fmt(fit.cut_limit_db, 0) + " dB allowed" },
      { key: "Largest boost", value: "+" + root.fmt(fit.actual_maximum_boost_db, 2) + " dB of +" + root.fmt(fit.maximum_allowed_boost_db, 1) + " dB allowed" },
      { key: "Headroom trim", value: "−" + root.fmt(fit.headroom_db, 2) + " dB" }
    ]
    if (fit.highpass)
      rows.push({ key: "High-pass", value: root.fmt(fit.highpass.frequency_hz, 0) + " Hz  ·  "
        + (Number(fit.highpass.stages) > 1 ? "4th" : "2nd") + " order  ·  "
        + (fit.highpass.knee_hz
            ? "measured knee " + root.fmt(fit.highpass.knee_hz, 0) + " Hz"
            : "no knee found, kept at the minimum") })
    if (fit.bass_shelf)
      rows.push({ key: "Bass shelf", value: "+" + root.fmt(fit.bass_shelf.gain_db, 1) + " dB below " + root.fmt(fit.bass_shelf.frequency_hz, 0) + " Hz" })
    if (fit.loudness_loss_db !== undefined) {
      rows.push({ key: "Loudness lost", value: root.fmt(fit.loudness_loss_db, 1) + " dB, pink noise A-weighted" })
      rows.push({ key: "Make-up", value: "+" + root.fmt(fit.makeup_db, 1) + " dB  ·  " + String(fit.loudness_mode || "protected") })
      rows.push({ key: "Net input gain", value: (Number(fit.net_input_gain_db) > 0 ? "+" : "") + root.fmt(fit.net_input_gain_db, 1) + " dB before the limiter" })
    }
    var refinement = ((service.proposal || {}).measurement || {}).refinement
    if (refinement)
      rows.push({ key: "Refined", value: "round " + refinement.iterations
        + "  ·  biggest change " + root.fmt(refinement.largest_step_db, 1) + " dB"
        + "  ·  from the check at " + String(refinement.from_check_at || "").slice(0, 16).replace("T", " ") })
    var trim = fit.channel_trim
    if (trim)
      rows.push({ key: "Channel balance", value: trim.applied
        ? "left " + root.fmt(trim.left_db, 1) + " dB  ·  right " + root.fmt(trim.right_db, 1)
          + " dB  ·  " + String(trim.reason)
        : String(trim.mode === "auto" ? "not applied" : "off")
          + "  ·  measured difference " + root.fmt(trim.difference_db, 1) + " dB"
          + "  ·  " + String(trim.reason) })
    if (fit.smoothing)
      rows.push({ key: "Smoothing", value: String(fit.smoothing.method)
        + "  ·  " + root.fmt((fit.smoothing.octaves || [])[0], 2) + " octaves at the bottom, "
        + root.fmt((fit.smoothing.octaves || [])[(fit.smoothing.octaves || []).length - 1], 2)
        + " at the top" })
    rows.push({ key: "Optimizer", value: (fit.optimizer_success ? "converged" : "did not converge") + "  ·  " + String(fit.algorithm || "") })
    return rows
  }

  // ---- row components for the advanced view -----------------------------------


  // A clickable row in the shell's control style: glyph, short title, and a
  // dim description.  Long option lists belong in the description, never in
  // the title, so nothing overflows or gets centred into unreadability.
  component ActionRow: BorderSurface {
    id: actionRow
    property string icon: ""
    property string label: ""
    property string description: ""
    signal clicked()
    readonly property bool _hot: actionMouse.containsMouse
    radius: Style.cornerRadius
    implicitHeight: Math.max(Style.space(44), actionContent.implicitHeight + Style.space(16))
    color: Style.controlFill(false, _hot && enabled, root.foreground, Color.accent)
    borderSpec: Border.controlSpec(_hot && enabled ? "hover-cursor" : "normal", root.foreground, Color.accent)
    opacity: enabled ? 1.0 : 0.55
    Behavior on color { ColorAnimation { duration: 100 } }

    Row {
      id: actionContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: actionRow.borderLeft + Style.spacing.rowPaddingX
      anchors.rightMargin: actionRow.borderRight + Style.spacing.rowPaddingX
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: actionRow.icon
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
        width: Style.space(22)
        horizontalAlignment: Text.AlignHCenter
        anchors.verticalCenter: parent.verticalCenter
      }
      Column {
        width: parent.width - Style.space(22) - parent.spacing
        spacing: Style.spacing.xs
        anchors.verticalCenter: parent.verticalCenter
        Text {
          textFormat: Text.PlainText
          text: actionRow.label
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
          elide: Text.ElideRight
          width: parent.width
        }
        Text {
          textFormat: Text.PlainText
          visible: actionRow.description !== ""
          text: actionRow.description
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          width: parent.width
        }
      }
    }

    MouseArea {
      id: actionMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: actionRow.clicked()
    }
  }

  component DetailRow: RowLayout {
    property string key: ""
    property string value: ""
    spacing: Style.space(10)
    Text {
      textFormat: Text.PlainText
      Layout.preferredWidth: Style.space(130)
      Layout.alignment: Qt.AlignTop
      text: key
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
    Text {
      textFormat: Text.PlainText
      Layout.fillWidth: true
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }
  }
  component TableRow: RowLayout {
    id: tableRow
    property var cells: []
    property bool header: false
    spacing: Style.space(8)
    Repeater {
      model: tableRow.cells
      Text {
        textFormat: Text.PlainText
        Layout.preferredWidth: [Style.space(26), Style.space(92), Style.space(84), Style.space(46), Style.space(76)][index]
        horizontalAlignment: index >= 2 ? Text.AlignRight : Text.AlignLeft
        text: String(modelData)
        color: tableRow.header ? root.dim : root.foreground
        font.family: root.fontFamily
        font.pixelSize: tableRow.header ? Style.font.caption : Style.font.bodySmall
        font.bold: tableRow.header
        elide: Text.ElideRight
      }
    }
  }


  // ---- biquad magnitudes for the equalizer view ----------------------------------
  function biquadDb(b0, b1, b2, a0, a1, a2, w) {
    var c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w)
    var numRe = b0 + b1 * c1 + b2 * c2, numIm = -(b1 * s1 + b2 * s2)
    var denRe = a0 + a1 * c1 + a2 * c2, denIm = -(a1 * s1 + a2 * s2)
    var magnitude = Math.sqrt((numRe * numRe + numIm * numIm) / Math.max(denRe * denRe + denIm * denIm, 1e-24))
    return 20 * Math.log10(Math.max(magnitude, 1e-12))
  }
  function sectionDb(shape, frequency, corner, q, gain, rate) {
    var w0 = 2 * Math.PI * corner / rate
    var alpha = Math.sin(w0) / (2 * Math.max(q, 0.05))
    var c = Math.cos(w0)
    var w = 2 * Math.PI * frequency / rate
    var A = Math.pow(10, gain / 40)
    var root2 = 2 * Math.sqrt(A) * alpha
    if (shape === "highpass")
      return biquadDb((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha, w)
    if (shape === "lowshelf")
      return biquadDb(A * ((A + 1) - (A - 1) * c + root2), 2 * A * ((A - 1) - (A + 1) * c),
                      A * ((A + 1) - (A - 1) * c - root2), (A + 1) + (A - 1) * c + root2,
                      -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - root2, w)
    if (shape === "highshelf")
      return biquadDb(A * ((A + 1) + (A - 1) * c + root2), -2 * A * ((A - 1) + (A + 1) * c),
                      A * ((A + 1) + (A - 1) * c - root2), (A + 1) - (A - 1) * c + root2,
                      2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - root2, w)
    return biquadDb(1 + alpha * A, -2 * c, 1 - alpha * A, 1 + alpha / A, -2 * c, 1 - alpha / A, w)
  }
  // Every section of a fit in graph order: high-pass, optimizer shelves and
  // peaks, and the full-bass shelf.
  function eqSections(fit) {
    var sections = []
    if (!fit) return sections
    var highpass = fit.highpass || {}
    var corner = Number(highpass.frequency_hz || fit.highpass_hz || 55)
    var stages = Number(highpass.stages || fit.highpass_stages || 2)
    for (var stage = 0; stage < stages; stage++)
      sections.push({ shape: "highpass", frequency: corner, q: Number(highpass.q || 0.707),
                      gain: 0, label: "HP", node: false })
    var filters = fit.filters || []
    for (var index = 0; index < filters.length; index++) {
      var item = filters[index]
      sections.push({ shape: item.type || "peaking", frequency: Number(item.frequency_hz),
                      q: Number(item.q), gain: Number(item.gain_db), label: String(index + 1), node: true })
    }
    if (fit.bass_shelf)
      sections.push({ shape: "lowshelf", frequency: Number(fit.bass_shelf.frequency_hz),
                      q: Number(fit.bass_shelf.q), gain: Number(fit.bass_shelf.gain_db), label: "B", node: true })
    return sections
  }
  function bandColor(index) {
    return Qt.hsla((0.55 + index * 0.13) % 1.0, 0.65, 0.58, 1.0)
  }
  function paintEqualizer(canvas, fit) {
    var context = canvas.getContext("2d")
    var width = canvas.width, height = canvas.height
    context.clearRect(0, 0, width, height)
    if (!fit) return
    var sections = root.eqSections(fit)
    var rate = 48000
    var padLeft = 30, padRight = 10, padTop = 10, padBottom = 18
    var plotWidth = width - padLeft - padRight, plotHeight = height - padTop - padBottom
    var minFrequency = 40, maxFrequency = 20000
    var minDb = -21, maxDb = 9
    function xFor(frequency) {
      return padLeft + (Math.log(frequency / minFrequency) / Math.log(maxFrequency / minFrequency)) * plotWidth
    }
    function yFor(value) {
      return padTop + ((maxDb - Math.max(minDb, Math.min(maxDb, value))) / (maxDb - minDb)) * plotHeight
    }
    var points = 160
    var frequencies = []
    for (var p = 0; p < points; p++)
      frequencies.push(minFrequency * Math.pow(maxFrequency / minFrequency, p / (points - 1)))

    // Grid.
    context.lineWidth = 1
    context.font = "9px " + root.fontFamily
    context.fillStyle = root.dim
    context.strokeStyle = root.dim
    context.textBaseline = "middle"
    context.textAlign = "right"
    for (var tick = minDb; tick <= maxDb; tick += 3) {
      var y = yFor(tick)
      context.globalAlpha = tick === 0 ? 0.6 : 0.16
      context.beginPath(); context.moveTo(padLeft, y); context.lineTo(width - padRight, y); context.stroke()
      if (tick % 6 === 0) { context.globalAlpha = 0.7; context.fillText((tick > 0 ? "+" : "") + tick, padLeft - 4, y) }
    }
    var frequencyTicks = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
    var frequencyLabels = ["50", "100", "200", "500", "1k", "2k", "5k", "10k", "20k"]
    context.textAlign = "center"
    context.textBaseline = "top"
    for (var t = 0; t < frequencyTicks.length; t++) {
      var x = xFor(frequencyTicks[t])
      context.globalAlpha = 0.16
      context.beginPath(); context.moveTo(x, padTop); context.lineTo(x, height - padBottom); context.stroke()
      context.globalAlpha = 0.7
      context.fillText(frequencyLabels[t], x, height - padBottom + 3)
    }

    // Each section: a translucent fill to the 0 dB line and its own curve.
    var combined = []
    for (p = 0; p < points; p++) combined.push(0)
    var bands = sections.filter(function(section) { return section.node })
    for (var s = 0; s < sections.length; s++) {
      var section = sections[s]
      var curve = []
      for (p = 0; p < points; p++) {
        var value = root.sectionDb(section.shape, frequencies[p], section.frequency, section.q, section.gain, rate)
        curve.push(value)
        combined[p] += value
      }
      var color = section.node ? root.bandColor(bands.indexOf(section)) : root.dim
      context.fillStyle = color
      context.strokeStyle = color
      context.globalAlpha = section.node ? 0.16 : 0.08
      context.beginPath()
      context.moveTo(xFor(frequencies[0]), yFor(0))
      for (p = 0; p < points; p++) context.lineTo(xFor(frequencies[p]), yFor(curve[p]))
      context.lineTo(xFor(frequencies[points - 1]), yFor(0))
      context.closePath()
      context.fill()
      context.globalAlpha = section.node ? 0.7 : 0.35
      context.lineWidth = 1.1
      context.beginPath()
      for (p = 0; p < points; p++) {
        if (p === 0) context.moveTo(xFor(frequencies[p]), yFor(curve[p]))
        else context.lineTo(xFor(frequencies[p]), yFor(curve[p]))
      }
      context.stroke()
    }

    // The sum of every section: what the speakers actually get.
    context.globalAlpha = 0.95
    context.strokeStyle = root.foreground
    context.lineWidth = 2.2
    context.beginPath()
    for (p = 0; p < points; p++) {
      if (p === 0) context.moveTo(xFor(frequencies[p]), yFor(combined[p]))
      else context.lineTo(xFor(frequencies[p]), yFor(combined[p]))
    }
    context.stroke()

    // Input gain after the sections: trim in protected mode, make-up when louder.
    var net = Number(fit.net_input_gain_db !== undefined ? fit.net_input_gain_db : -(fit.headroom_db || 1))
    context.globalAlpha = 0.55
    context.strokeStyle = Color.accent
    context.lineWidth = 1
    context.setLineDash([3, 3])
    context.beginPath(); context.moveTo(padLeft, yFor(net)); context.lineTo(width - padRight, yFor(net)); context.stroke()
    context.setLineDash([])
    context.fillStyle = Color.accent
    context.textAlign = "left"
    context.textBaseline = "bottom"
    context.globalAlpha = 0.8
    context.fillText("gain " + (net > 0 ? "+" : "") + net.toFixed(1) + " dB", padLeft + 3, yFor(net) - 1)

    // Nodes last, so they sit on top.
    for (s = 0; s < bands.length; s++) {
      var band = bands[s]
      var nx = xFor(band.frequency), ny = yFor(band.gain)
      context.globalAlpha = 1.0
      context.fillStyle = root.bandColor(s)
      context.beginPath(); context.arc(nx, ny, 6, 0, 2 * Math.PI); context.fill()
      context.strokeStyle = root.foreground
      context.lineWidth = 1.2
      context.beginPath(); context.arc(nx, ny, 6, 0, 2 * Math.PI); context.stroke()
      context.fillStyle = root.foreground
      context.font = "bold 8px " + root.fontFamily
      context.textAlign = "center"
      context.textBaseline = "middle"
      context.fillText(band.label, nx, ny)
    }
    context.globalAlpha = 1.0
  }

  Service { id: service; helperPath: root.helperPath }

  // The panel is drawn from the last known state before the helper has
  // answered.  Reading that file is the helper's job, not this process's.
  Component.onCompleted: service.loadCache()

  Connections {
    target: service
    function onStatusChanged() {
      if (!root._optionsAdopted && service.status.profile) {
        root.adoptOptions(service.status.profile)
        root._optionsAdopted = true
      }
      eqCanvas.requestPaint()
    }
    function onSinksChanged() { root.selectInternalDevices() }
    function onMicrophonesChanged() { root.selectInternalDevices(); channelBox.currentIndex = 0 }
    function onProposalChanged() { responseCanvas.requestPaint() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: content
          // Held a hair inside the scrolling viewport.  A row whose border
          // lands exactly on the clip boundary loses that edge, which reads
          // as an unfinished box on one side.
          x: Style.space(2)
          width: parent.width - Style.space(4)
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Speaker Calibrator"
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: "󰓃"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }
          Text {
            textFormat: Text.PlainText
            visible: service.error !== ""
            width: parent.width
            text: service.error
            color: bar ? bar.urgent : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: service.message !== "" && !service.busy
            width: parent.width
            text: service.message
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: !service.busy && service.proposal !== null && service.proposal !== undefined
              && service.proposal.quality !== undefined && service.proposal.quality.accepted === false
            width: parent.width
            text: root.qualityIssuesText() + (root.qualityGuidanceText() !== "" ? "\n" + root.qualityGuidanceText() : "")
            color: bar ? bar.urgent : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            visible: !service.busy && service.status.verification !== undefined
              && service.status.verification !== null
            width: parent.width
            text: service.status.verification && service.status.verification.stale
              ? "The calibration has changed since it was last checked."
              : service.verificationSummary(service.status.verification)
            color: service.status.verification && !service.status.verification.stale
              && service.status.verification.verdict === "fail"
              ? (bar ? bar.urgent : Color.urgent) : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Column {
            // The switches that get used every day, then the picture of what
            // they do.  The setup that gets used once sits below both.
            visible: service.status.profile !== null && service.status.profile !== undefined
            width: parent.width
            spacing: Style.space(12)

            Toggle {
              width: parent.width
              label: "Loudness"
              description: "Fuller sound with more bass, like the loudness button on a stereo."
              checked: root.bassMode === "full"
              enabled: !service.busy
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                root.bassMode = root.bassMode === "full" ? "normal" : "full"
                root.applyOptions()
              }
            }

            Toggle {
              width: parent.width
              label: "Make it louder"
              description: "Gives back the volume the correction takes away. At full volume the limiter works harder."
              checked: root.loudnessMode !== "protected"
              enabled: !service.busy
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: {
                root.loudnessMode = root.loudnessMode === "protected" ? "matched" : "protected"
                root.applyOptions()
              }
            }

            Toggle {
              width: parent.width
              label: "Deep bass"
              description: root.deepBassDescription()
              checked: service.status.deepBass === "on"
                && ((service.status.bassEnhancer || {}).usable === true)
              enabled: !service.busy
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: service.deepBass()
            }

            RowLayout {
              visible: root.bassWarningText() !== ""
              width: parent.width
              spacing: Style.space(8)
              Text {
                textFormat: Text.PlainText
                Layout.alignment: Qt.AlignTop
                text: "󰀪"
                color: bar ? bar.urgent : Color.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.icon
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: root.bassWarningText()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Toggle {
              visible: service.status.enabled
              width: parent.width
              label: "Calibration"
              description: service.status.bypass
                ? "Off — the plain speakers" + root.bypassMatchText()
                : "On — switch off to hear the speakers as they were" + root.bypassMatchText()
              checked: !service.status.bypass
              enabled: !service.busy
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: service.bypass()
            }

            Column {
              visible: service.status.profile !== null && service.status.profile !== undefined
                && service.status.profile.fit !== null && service.status.profile.fit !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSectionHeader {
                text: "EQUALIZER — WHAT THE CALIBRATION DOES"
                  + (service.status.bypass ? " (SWITCHED OFF)" : "")
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Canvas {
                id: eqCanvas
                width: parent.width
                height: Style.space(160)
                antialiasing: true
                opacity: service.status.bypass ? 0.35 : 1.0
                onPaint: root.paintEqualizer(eqCanvas, service.status.profile ? service.status.profile.fit : null)
                onVisibleChanged: if (visible) requestPaint()
                onWidthChanged: requestPaint()
              }
              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Each coloured band is one filter, its node at the frequency and gain; the bright line is everything added together. The dashed line is the gain applied before the limiter."
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }
          }



          PanelSeparator { foreground: root.foreground }

          Button {
            width: parent.width
            text: service.busy && service.phase === "measure" ? "Measuring… keep quiet"
              : (service.status.profile ? "Calibrate again" : "Calibrate speakers")
            iconText: service.busy && service.phase === "measure" ? "󰑓" : "󰋋"
            iconSpinning: service.busy && service.phase === "measure"
            bordered: true
            selected: true
            focusable: true
            enabled: !service.busy && root.sinkIndex >= 0 && root.micIndex >= 0
            onClicked: {
              var sink = service.sinks[root.sinkIndex]
              var mic = service.microphones[root.micIndex]
              service.measure(sink.name, mic.name, root.selectedChannelValue(), root.options(), true)
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "Measures the speakers with the microphone and corrects their sound. Keep the room quiet for about 30 seconds; the result installs itself when the measurement passes."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader { text: "SPEAKERS"; foreground: root.foreground; fontFamily: root.fontFamily }
            Repeater {
              model: service.sinks
              Button {
                width: parent.width
                leftAlign: true
                bordered: true
                selected: index === root.sinkIndex
                iconText: "󰓃"
                text: modelData.description + (modelData.internal ? "  ·  built-in" : "  ·  external")
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !service.busy
                onClicked: root.sinkIndex = index
              }
            }
            Text {
              textFormat: Text.PlainText
              visible: service.sinks.length === 0
              width: parent.width
              text: "No speakers found. Middle-click the bar icon to refresh."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader { text: "MICROPHONE"; foreground: root.foreground; fontFamily: root.fontFamily }
            Repeater {
              model: service.microphones
              // A button carries one line of text.  What this microphone has
              // measured belongs under it, the way every other labelled
              // control in this panel is built, not crammed into the label.
              Column {
                width: parent.width
                spacing: Style.space(2)

                Button {
                  width: parent.width
                  leftAlign: true
                  bordered: true
                  selected: index === root.micIndex
                  iconText: "󰍬"
                  text: modelData.description
                    + (modelData.internal
                        ? "  ·  built-in" + (Number(modelData.channels || 1) > 1 ? ", " + modelData.channels + " mics" : "")
                        : "  ·  external")
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  enabled: !service.busy
                  onClicked: { root.micIndex = index; channelBox.currentIndex = 0 }
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  // A Column owns its children's x, so the indent that lines
                  // this up under the button's label has to be padding.
                  leftPadding: Style.space(11)
                  bottomPadding: Style.space(3)
                  text: root.microphoneNote(modelData)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
            }
            Text {
              textFormat: Text.PlainText
              visible: service.microphones.length === 0
              width: parent.width
              text: "No microphone found. Plug one in or middle-click the bar icon to refresh."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(8)
              Text {
                textFormat: Text.PlainText
                Layout.alignment: Qt.AlignTop
                text: "󰋽"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.icon
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                text: "The built-in mics already give a big improvement. An external measuring mic, placed where you sit, improves the sound a lot more."
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }
          }













          PanelSeparator { foreground: root.foreground }

          Toggle {
            width: parent.width
            label: "Advanced"
            description: "Voicing, loudness level, microphone options, measurement details, and profile comparison."
            checked: root.advanced
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: {
              root.advanced = !root.advanced
              if (root.advanced && !service.micComparison) service.loadMicrophones()
            }
          }

          Column {
            visible: root.advanced
            width: parent.width
            spacing: Style.space(10)

            // ---------------------------------------------------------- settings
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "SETTINGS"; foreground: root.foreground; fontFamily: root.fontFamily }

            GridLayout {
              width: parent.width
              columns: 2
              columnSpacing: Style.space(10)
              rowSpacing: Style.space(8)

              Text { textFormat: Text.PlainText; text: "VOICING"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: voicingBox
                Layout.fillWidth: true
                model: ["Flat — balanced, more detail", "Warm — softer, less sharp"]
                enabled: !service.busy
                currentIndex: root.voicingMode === "warm" ? 1 : 0
                onActivated: function(index) { root.voicingMode = index === 1 ? "warm" : "neutral" }
              }

              Text { textFormat: Text.PlainText; text: "LOUDER"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: loudnessBox
                Layout.fillWidth: true
                model: ["Protected — cleanest, a little quieter",
                        "Balanced — half the lost loudness back",
                        "Matched — as loud as before"]
                enabled: !service.busy
                currentIndex: ["protected", "balanced", "matched"].indexOf(root.loudnessMode)
                onActivated: function(index) { root.loudnessMode = ["protected", "balanced", "matched"][index] }
              }

              Text { textFormat: Text.PlainText; text: "BASS"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: bassBox
                Layout.fillWidth: true
                model: ["Normal — measured correction only",
                        "Full — +3 dB shelf below the knee"]
                enabled: !service.busy
                currentIndex: root.bassMode === "full" ? 1 : 0
                onActivated: function(index) { root.bassMode = index === 1 ? "full" : "normal" }
              }

              Text { textFormat: Text.PlainText; text: "CHANNEL BALANCE"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: channelTrimBox
                Layout.fillWidth: true
                model: ["Off — both channels get the same correction",
                        "Automatic — level-match them, external mic only"]
                enabled: !service.busy
                currentIndex: root.channelTrimMode === "auto" ? 1 : 0
                onActivated: function(index) { root.channelTrimMode = index === 1 ? "auto" : "off" }
              }

              Text {
                textFormat: Text.PlainText
                visible: root.multiMicAvailable() || !root.selectedMicIsInternal()
                text: "MIC CHANNEL"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true
              }
              QQC.ComboBox {
                id: channelBox
                visible: root.multiMicAvailable() || !root.selectedMicIsInternal()
                Layout.fillWidth: true
                model: root.channelOptions()
                enabled: !service.busy
              }

              Text {
                textFormat: Text.PlainText
                visible: !root.selectedMicIsInternal()
                text: "MIC CAL FILE"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true
              }
              QQC.TextField {
                id: micCalPath
                visible: !root.selectedMicIsInternal()
                Layout.fillWidth: true
                enabled: !service.busy
                placeholderText: "Optional path to serial-number calibration .txt"
                selectByMouse: true
              }
            }

            Toggle {
              width: parent.width
              label: "Loudness compensation"
              description: root.loudnessCompensationDescription()
              checked: service.status.loudnessCompensation === "on"
              enabled: !service.busy && service.status.enabled
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: service.loudnessCompensation()
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Loudness toggle = Bass full. Make it louder = Louder matched. Refit applies these to the last measurement without new sweeps. CHANNEL BALANCE only ever acts on a measurement made with an external microphone placed where you listen, and only when the difference stands clear of what the measurement itself varies by; built-in microphones sit closer to one speaker than the other, so what they measure is where they are rather than what reaches you."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            // ---------------------------------------------------------- actions
            PanelSeparator { foreground: root.foreground }
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader {
              text: "MICROPHONES — WHAT EACH ONE MEASURED"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              width: parent.width
              spacing: Style.space(6)

              Canvas {
                id: microphoneCanvas
                width: parent.width
                height: Style.space(150)
                antialiasing: true
                visible: !!(service.micComparison
                  && (service.micComparison.internal || service.micComparison.external))
                onPaint: root.paintMicrophones(microphoneCanvas, service.micComparison)
                onVisibleChanged: if (visible) requestPaint()
              }

              // Which line is which, without a floating legend to misread.
              Row {
                width: parent.width
                spacing: Style.space(12)
                visible: microphoneCanvas.visible
                Text {
                  textFormat: Text.PlainText
                  text: "—  built-in"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  textFormat: Text.PlainText
                  text: "—  measuring microphone"
                  color: bar && bar.accent ? bar.accent : Color.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: root.microphoneComparisonText()
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Column {
                width: parent.width
                spacing: Style.space(3)
                visible: !!(service.micComparison && service.micComparison.available)
                Repeater {
                  model: service.micComparison ? (service.micComparison.bands || []) : []
                  DetailRow {
                    width: parent.width
                    key: modelData.band
                    value: (Number(modelData.difference_db) > 0 ? "+" : "")
                      + Number(modelData.difference_db).toFixed(1)
                      + " dB on the built-in microphones"
                  }
                }
              }
            }

            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "ACTIONS"; foreground: root.foreground; fontFamily: root.fontFamily }

            Column {
              width: parent.width
              spacing: Style.space(6)

              ActionRow {
                visible: root.hasMeasurement()
                width: parent.width
                icon: "󰑓"
                label: service.busy && service.phase === "refit" ? "Refitting…" : "Refit and play"
                description: "Apply " + service.optionsLabel(root.options()) + " to the last measurement and play it"
                enabled: !service.busy
                onClicked: service.refit(root.options(), true)
              }
              ActionRow {
                visible: root.hasMeasurement()
                width: parent.width
                icon: "󰑓"
                label: "Refit only"
                description: "Compute with the settings above, keep playing what is playing now"
                enabled: !service.busy
                onClicked: service.refit(root.options(), false)
              }
              ActionRow {
                visible: service.proposal !== null && service.proposal !== undefined
                  && service.proposal.quality !== undefined && service.proposal.quality.accepted === true
                  && !(service.status.profile && service.status.profile.created_at === service.proposal.created_at)
                width: parent.width
                icon: "󰄬"
                label: service.busy && service.phase === "install" ? "Installing…" : "Install last measurement"
                description: service.optionsLabel(service.proposal)
                  + (service.proposal && service.proposal.created_at
                      ? "  ·  measured " + String(service.proposal.created_at).slice(0, 16).replace("T", " ") : "")
                enabled: !service.busy
                onClicked: service.install()
              }
              ActionRow {
                visible: service.status.enabled && !service.status.bypass
                  && service.status.profile !== null && service.status.profile !== undefined
                width: parent.width
                icon: "󰄾"
                label: service.busy && service.phase === "verify" ? "Checking…" : "Check the calibration"
                description: "Measure again through the corrected output and compare it with the plan"
                enabled: !service.busy
                onClicked: service.verify()
              }
              ActionRow {
                visible: service.status.verification !== undefined
                  && service.status.verification !== null
                  && service.status.verification.stale === false
                  && service.status.enabled && !service.status.bypass
                width: parent.width
                icon: "󰁨"
                label: service.busy && service.phase === "refine" ? "Improving…" : "Improve from the check"
                description: "Feed what the check measured back in, fit again, and play the result"
                enabled: !service.busy
                onClicked: service.refine()
              }
              ActionRow {
                visible: service.status.enabled && service.status.compare !== undefined
                  && service.status.compare.available === true
                width: parent.width
                icon: "󰓦"
                label: service.busy && service.phase === "compare" ? "Switching…" : "Switch profile"
                description: "Play the other stored profile: " + service.otherLabel()
                enabled: !service.busy
                onClicked: service.compare()
              }
              ActionRow {
                visible: service.status.enabled
                width: parent.width
                icon: "󰅖"
                label: "Stop calibration"
                description: "Remove it from the output; the profiles stay saved"
                enabled: !service.busy
                onClicked: service.disable()
              }
            }

            DetailRow {
              visible: service.status.enabled && service.playingLabel() !== ""
              width: parent.width
              key: "Playing"
              value: service.playingLabel()
            }

            // ---------------------------------------------------------- measurement
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader {
              text: service.proposal && service.proposal.quality
                ? "LAST MEASUREMENT  ·  " + String(service.proposal.quality.verdict).toUpperCase()
                  + (service.status.profile && service.status.profile.created_at === service.proposal.created_at
                      ? "  ·  INSTALLED" : "  ·  NOT INSTALLED")
                : "LAST MEASUREMENT  ·  NONE YET"
              foreground: service.proposal && service.proposal.quality
                && !service.proposal.quality.accepted
                ? (bar ? bar.urgent : Color.urgent) : root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.quality !== undefined
              width: parent.width
              spacing: Style.space(3)
              DetailRow {
                width: parent.width
                key: "Options"
                value: service.optionsLabel(service.proposal)
                  + (service.proposal && service.proposal.plugin_version ? "  ·  plugin " + service.proposal.plugin_version : "")
              }
              Repeater {
                model: root.measurementRows()
                DetailRow { width: parent.width; key: modelData.key; value: modelData.value }
              }
            }

            Column {
              visible: root.issueLines().length > 0
              width: parent.width
              spacing: Style.space(2)
              Repeater {
                model: root.issueLines()
                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  text: "•  " + modelData
                  color: service.proposal && service.proposal.quality
                    && !service.proposal.quality.accepted
                    ? (bar ? bar.urgent : Color.urgent) : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }
            }

            Column {
              visible: root.guidanceLines().length > 0
              width: parent.width
              spacing: Style.space(2)
              Text {
                textFormat: Text.PlainText
                text: "TRY"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
              Repeater {
                model: root.guidanceLines()
                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  text: "→  " + modelData
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }
            }

            Text {
              textFormat: Text.PlainText
              visible: root.microphoneArrayText() !== ""
              width: parent.width
              text: root.microphoneArrayText()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            // ---------------------------------------------------------- response
            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.measurement !== undefined
                && service.proposal.measurement.channels !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSeparator { foreground: root.foreground }
              PanelSectionHeader {
                text: "MEASURED RESPONSE  ·  LEFT / RIGHT"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Canvas {
                id: responseCanvas
                width: parent.width
                height: Style.space(155)
                antialiasing: true

                onPaint: {
                  var context = getContext("2d")
                  context.clearRect(0, 0, width, height)
                  var proposal = service.proposal
                  if (!proposal || !proposal.measurement) return
                  var frequencies = proposal.measurement.frequency_hz || []
                  var channels = proposal.measurement.channels || []
                  if (frequencies.length < 2 || channels.length < 1) return

                  var padLeft = 34
                  var padRight = 8
                  var padTop = 8
                  var padBottom = 20
                  var plotWidth = width - padLeft - padRight
                  var plotHeight = height - padTop - padBottom
                  var minFrequency = 80
                  var maxFrequency = 16000
                  var minDb = -12
                  var maxDb = 12
                  function xFor(frequency) {
                    return padLeft + (Math.log(frequency / minFrequency)
                      / Math.log(maxFrequency / minFrequency)) * plotWidth
                  }
                  function yFor(value) {
                    return padTop + ((maxDb - Math.max(minDb, Math.min(maxDb, value)))
                      / (maxDb - minDb)) * plotHeight
                  }
                  var references = []
                  for (var channelIndex = 0; channelIndex < channels.length; channelIndex++) {
                    var response = channels[channelIndex].response_db || []
                    for (var index = 0; index < Math.min(frequencies.length, response.length); index++)
                      if (frequencies[index] >= 250 && frequencies[index] <= 1000)
                        references.push(Number(response[index]))
                  }
                  references.sort(function(left, right) { return left - right })
                  var reference = references.length > 0
                    ? references[Math.floor(references.length / 2)] : 0

                  context.lineWidth = 1
                  context.strokeStyle = root.dim
                  context.fillStyle = root.dim
                  context.font = "10px " + root.fontFamily
                  context.textBaseline = "middle"
                  var dbTicks = [-12, -6, 0, 6, 12]
                  for (var tickIndex = 0; tickIndex < dbTicks.length; tickIndex++) {
                    var tick = dbTicks[tickIndex]
                    var y = yFor(tick)
                    context.globalAlpha = tick === 0 ? 0.55 : 0.22
                    context.beginPath()
                    context.moveTo(padLeft, y)
                    context.lineTo(width - padRight, y)
                    context.stroke()
                    context.globalAlpha = 0.75
                    context.fillText((tick > 0 ? "+" : "") + tick, 2, y)
                  }
                  var frequencyTicks = [100, 1000, 10000]
                  var frequencyLabels = ["100", "1k", "10k"]
                  context.textAlign = "center"
                  context.textBaseline = "top"
                  for (tickIndex = 0; tickIndex < frequencyTicks.length; tickIndex++) {
                    var x = xFor(frequencyTicks[tickIndex])
                    context.globalAlpha = 0.22
                    context.beginPath()
                    context.moveTo(x, padTop)
                    context.lineTo(x, height - padBottom)
                    context.stroke()
                    context.globalAlpha = 0.75
                    context.fillText(frequencyLabels[tickIndex], x, height - padBottom + 4)
                  }

                  var colors = [root.foreground, root.dim]
                  for (channelIndex = 0; channelIndex < Math.min(2, channels.length); channelIndex++) {
                    response = channels[channelIndex].response_db || []
                    var uncertainty = channels[channelIndex].uncertainty_db || []
                    var count = Math.min(frequencies.length, response.length)
                    if (count < 2) continue
                    context.fillStyle = colors[channelIndex]
                    context.globalAlpha = 0.10
                    context.beginPath()
                    for (index = 0; index < count; index++) {
                      x = xFor(frequencies[index])
                      y = yFor(Number(response[index]) - reference
                        + Number(uncertainty[index] || 0))
                      if (index === 0) context.moveTo(x, y)
                      else context.lineTo(x, y)
                    }
                    for (index = count - 1; index >= 0; index--)
                      context.lineTo(xFor(frequencies[index]), yFor(Number(response[index])
                        - reference - Number(uncertainty[index] || 0)))
                    context.closePath()
                    context.fill()

                    context.strokeStyle = colors[channelIndex]
                    context.globalAlpha = channelIndex === 0 ? 0.95 : 0.70
                    context.lineWidth = channelIndex === 0 ? 1.8 : 1.4
                    context.beginPath()
                    for (index = 0; index < count; index++) {
                      x = xFor(frequencies[index])
                      y = yFor(Number(response[index]) - reference)
                      if (index === 0) context.moveTo(x, y)
                      else context.lineTo(x, y)
                    }
                    context.stroke()
                  }

                  var fit = proposal.fit
                  if (fit && fit.predicted_response_db) {
                    var predicted = fit.predicted_response_db
                    count = Math.min(frequencies.length, predicted.length)
                    context.strokeStyle = Color.accent
                    context.globalAlpha = 0.95
                    context.lineWidth = 2.0
                    context.setLineDash([7, 4])
                    context.beginPath()
                    for (index = 0; index < count; index++) {
                      x = xFor(frequencies[index])
                      y = yFor(Number(predicted[index]) - reference)
                      if (index === 0) context.moveTo(x, y)
                      else context.lineTo(x, y)
                    }
                    context.stroke()
                  }
                  if (fit && fit.target && fit.target.aligned_db) {
                    var targetCurve = fit.target.aligned_db
                    count = Math.min(frequencies.length, targetCurve.length)
                    context.strokeStyle = root.foreground
                    context.globalAlpha = 0.82
                    context.lineWidth = 1.6
                    context.setLineDash([2, 4])
                    context.beginPath()
                    for (index = 0; index < count; index++) {
                      x = xFor(frequencies[index])
                      y = yFor(Number(targetCurve[index]) - reference)
                      if (index === 0) context.moveTo(x, y)
                      else context.lineTo(x, y)
                    }
                    context.stroke()
                  }
                  if (fit && fit.filters) {
                    context.setLineDash([])
                    context.strokeStyle = Color.accent
                    context.globalAlpha = 0.62
                    context.lineWidth = 1.5
                    for (var filterIndex = 0; filterIndex < fit.filters.length; filterIndex++) {
                      var filterFrequency = Number(fit.filters[filterIndex].frequency_hz || 0)
                      if (filterFrequency < minFrequency || filterFrequency > maxFrequency) continue
                      x = xFor(filterFrequency)
                      context.beginPath()
                      context.moveTo(x, padTop)
                      context.lineTo(x, padTop + 7)
                      context.stroke()
                    }
                  }
                  context.setLineDash([])
                  context.globalAlpha = 1.0
                }
                onVisibleChanged: if (visible) requestPaint()
                onWidthChanged: requestPaint()
              }
              Column {
                width: parent.width
                spacing: Style.space(2)
                DetailRow { width: parent.width; key: "Solid lines"; value: "measured left (bright) and right (dim), ±12 dB around the 250–1000 Hz median" }
                DetailRow { width: parent.width; key: "Shaded band"; value: "repeat and noise uncertainty" }
                DetailRow { width: parent.width; key: "Accent dashed"; value: "predicted response after correction" }
                DetailRow { width: parent.width; key: "Dotted"; value: "target curve  ·  top ticks mark section centres" }
              }
            }

            // ---------------------------------------------------------- sections
            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.fit !== null && service.proposal.fit !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSeparator { foreground: root.foreground }
              PanelSectionHeader { text: "SECTIONS"; foreground: root.foreground; fontFamily: root.fontFamily }

              Column {
                width: parent.width
                spacing: Style.space(3)
                TableRow { width: parent.width; header: true; cells: ["#", "TYPE", "FREQUENCY", "Q", "GAIN"] }
                Repeater {
                  model: root.sectionRows()
                  TableRow { width: parent.width; cells: modelData }
                }
              }

              PanelSeparator { foreground: root.foreground }
              PanelSectionHeader { text: "FIT"; foreground: root.foreground; fontFamily: root.fontFamily }

              Column {
                width: parent.width
                spacing: Style.space(3)
                Repeater {
                  model: root.fitRows()
                  DetailRow { width: parent.width; key: modelData.key; value: modelData.value }
                }
              }
            }

            // ---------------------------------------------------------- check
            Column {
              visible: service.status.verification !== undefined
                && service.status.verification !== null
              width: parent.width
              spacing: Style.space(7)

              PanelSeparator { foreground: root.foreground }
              PanelSectionHeader {
                text: "LAST CHECK  ·  " + String((service.status.verification || {}).verdict || "").toUpperCase()
                  + ((service.status.verification || {}).stale ? "  ·  STALE" : "")
                foreground: (service.status.verification || {}).verdict === "fail"
                  ? (bar ? bar.urgent : Color.urgent) : root.foreground
                fontFamily: root.fontFamily
              }
              Column {
                width: parent.width
                spacing: Style.space(3)
                Repeater {
                  model: root.verificationRows()
                  DetailRow { width: parent.width; key: modelData.key; value: modelData.value }
                }
              }
              Column {
                visible: ((service.status.verification || {}).notes || []).length > 0
                width: parent.width
                spacing: Style.space(2)
                Repeater {
                  model: (service.status.verification || {}).notes || []
                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: "•  " + modelData
                    color: (service.status.verification || {}).verdict === "fail"
                      ? (bar ? bar.urgent : Color.urgent) : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }
                }
              }
            }

            // ---------------------------------------------------------- how it works
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "HOW IT WORKS"; foreground: root.foreground; fontFamily: root.fontFamily }
            Column {
              width: parent.width
              spacing: Style.space(3)
              DetailRow { width: parent.width; key: "Measure"; value: "three sine sweeps per speaker, gated to the direct sound, noise floor measured between sweeps" }
              DetailRow { width: parent.width; key: "Fit"; value: "sections are added one at a time and kept only when a held-out repeat also improves" }
              DetailRow { width: parent.width; key: "Limits"; value: "cuts preferred; whole correction never below -15 dB, boosts capped, shelves cut-only" }
              DetailRow { width: parent.width; key: "Protect"; value: "high-pass at the frequency where the speaker gives up; input trim pays for every boost; -1 dBFS limiter; make-up only when chosen" }
              DetailRow {
                width: parent.width
                key: "Deep bass"
                value: ((service.status.bassEnhancer || {}).usable === true)
                  ? "bankstown add-on installed at " + String((service.status.bassEnhancer || {}).path)
                    + "; makes harmonics from below the high-pass corner and keeps them above it"
                  : "optional bankstown add-on, not installed; would come from "
                    + String((service.status.bassEnhancer || {}).source || "AUR")
                    + "; nothing in the chain depends on it"
              }
            }
          }
        }
      }
    }
  }
}
