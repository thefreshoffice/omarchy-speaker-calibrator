import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "local.speaker-calibrator"
  ipcTarget: "local.speaker-calibrator.panel"
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
  property string voicingMode: "warm"
  property string bassMode: "normal"
  property string loudnessMode: "protected"
  property bool advanced: false
  property bool _optionsAdopted: false

  function open() { root.controller.show(); service.refresh() }
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
    var mic = service.microphones[micBox.currentIndex]
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
    var mic = service.microphones[micBox.currentIndex]
    var count = mic ? Math.max(1, Number(mic.channels || 1)) : 1
    if (mic && mic.internal === true && count > 1)
      return channelBox.currentIndex === 0 ? "all" : channelBox.currentIndex - 1
    return channelBox.currentIndex
  }
  function multiMicAvailable() {
    var mic = service.microphones[micBox.currentIndex]
    return !!(mic && mic.internal === true && Number(mic.channels || 1) > 1)
  }
  function selectedMicIsInternal() {
    var mic = service.microphones[micBox.currentIndex]
    return mic ? mic.internal === true : true
  }
  // Zero-knowledge default: the laptop's own speakers and microphones.
  function selectInternalDevices() {
    for (var sinkIndex = 0; sinkIndex < service.sinks.length; sinkIndex++)
      if (service.sinks[sinkIndex].internal === true) { sinkBox.currentIndex = sinkIndex; break }
    for (var micIndex = 0; micIndex < service.microphones.length; micIndex++)
      if (service.microphones[micIndex].internal === true) { micBox.currentIndex = micIndex; break }
  }

  // ---- options -------------------------------------------------------------
  function options() {
    return { voicing: root.voicingMode, bass: root.bassMode, loudness: root.loudnessMode,
             micCalibrationFile: micCalPath.text.trim() }
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
    root.voicingMode = profile.voicing === "neutral" ? "neutral" : "warm"
    root.bassMode = profile.bass === "full" ? "full" : "normal"
    root.loudnessMode = profile.loudness || "protected"
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
    var highpass = Number(fit.highpass_hz || 55)
    sections.push({ shape: "highpass", frequency: highpass, q: 0.707, gain: 0, label: "HP", node: false })
    sections.push({ shape: "highpass", frequency: highpass, q: 0.707, gain: 0, label: "HP", node: false })
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
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: content
          width: parent.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Speaker Calibrator"
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                text: "󰓃"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            width: parent.width
            text: "Measures the speakers with the microphone and corrects their sound. Keep the room quiet for about 30 seconds; the calibration installs itself when the measurement passes."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          GridLayout {
            width: parent.width
            columns: 2
            columnSpacing: Style.space(10)
            rowSpacing: Style.space(8)

            Text { text: "SPEAKERS"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
            QQC.ComboBox {
              id: sinkBox
              Layout.fillWidth: true
              model: service.sinks.map(function(item) { return item.description })
              enabled: !service.busy && model.length > 0
            }

            Text { text: "MICROPHONE"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
            QQC.ComboBox {
              id: micBox
              Layout.fillWidth: true
              model: service.microphones.map(function(item) {
                return (item.internal ? "Built-in — " : "External — ") + item.description
              })
              enabled: !service.busy && model.length > 0
              onCurrentIndexChanged: channelBox.currentIndex = 0
            }
          }

          Button {
            width: parent.width
            text: service.busy && service.phase === "measure" ? "Measuring… keep quiet"
              : (service.status.profile ? "Calibrate again" : "Calibrate speakers")
            iconText: service.busy && service.phase === "measure" ? "󰑓" : "󰋋"
            iconSpinning: service.busy && service.phase === "measure"
            bordered: true
            selected: true
            focusable: true
            enabled: !service.busy && sinkBox.currentIndex >= 0 && micBox.currentIndex >= 0
            onClicked: {
              var sink = service.sinks[sinkBox.currentIndex]
              var mic = service.microphones[micBox.currentIndex]
              service.measure(sink.name, mic.name, root.selectedChannelValue(), root.options(), true)
            }
          }

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
            visible: service.status.enabled
            width: parent.width
            label: "Calibration"
            description: service.status.bypass
              ? "Off — you are hearing the plain speakers"
              : "On — switch off to hear the speakers as they were"
            checked: !service.status.bypass
            enabled: !service.busy
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: service.bypass()
          }

          Text {
            visible: service.error !== ""
            width: parent.width
            text: service.error
            color: bar ? bar.urgent : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Text {
            visible: service.message !== "" && !service.busy
            width: parent.width
            text: service.message
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            visible: !service.busy && service.proposal !== null && service.proposal !== undefined
              && service.proposal.quality !== undefined && service.proposal.quality.accepted === false
            width: parent.width
            text: root.qualityIssuesText() + (root.qualityGuidanceText() !== "" ? "\n" + root.qualityGuidanceText() : "")
            color: bar ? bar.urgent : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
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
              width: parent.width
              text: "Each coloured band is one filter, its node at the frequency and gain; the bright line is everything added together. The dashed line is the gain applied before the limiter."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Toggle {
            width: parent.width
            label: "Advanced"
            description: "Voicing, loudness level, microphone options, measurement details, and profile comparison."
            checked: root.advanced
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.advanced = !root.advanced
          }

          Column {
            visible: root.advanced
            width: parent.width
            spacing: Style.space(12)

            GridLayout {
              width: parent.width
              columns: 2
              columnSpacing: Style.space(10)
              rowSpacing: Style.space(8)

              Text { text: "VOICING"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: voicingBox
                Layout.fillWidth: true
                model: ["Warm — softer, less sharp", "Flat — balanced, more detail"]
                enabled: !service.busy
                currentIndex: root.voicingMode === "neutral" ? 1 : 0
                onActivated: function(index) { root.voicingMode = index === 1 ? "neutral" : "warm" }
              }

              Text { text: "LOUDER"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: loudnessBox
                Layout.fillWidth: true
                model: ["Protected — cleanest, a little quieter",
                        "Balanced — half the lost loudness added back",
                        "Matched — as loud as before, limiter works harder"]
                enabled: !service.busy
                currentIndex: ["protected", "balanced", "matched"].indexOf(root.loudnessMode)
                onActivated: function(index) { root.loudnessMode = ["protected", "balanced", "matched"][index] }
              }

              Text { text: "BASS"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
              QQC.ComboBox {
                id: bassBox
                Layout.fillWidth: true
                model: ["Normal — the measured correction only",
                        "Full — +3 dB shelf below the speaker's knee (the Loudness toggle)"]
                enabled: !service.busy
                currentIndex: root.bassMode === "full" ? 1 : 0
                onActivated: function(index) { root.bassMode = index === 1 ? "full" : "normal" }
              }

              Text {
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

            Text {
              width: parent.width
              text: "WARM softens sharp voices, cymbals, and hiss; FLAT keeps more clarity. LOUDER adds back part or all of the loudness the cuts removed, up to 6 dB, before the limiter. BASS full is the Loudness toggle: a +3 dB shelf at the measured knee, paid for by input trim. Refit applies the selectors to the last measurement without new sweeps."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Button {
              visible: root.hasMeasurement()
              width: parent.width
              text: service.busy && service.phase === "refit" ? "Refitting…"
                : "Refit and play: " + service.optionsLabel(root.options())
              iconText: "󰑓"
              bordered: true
              enabled: !service.busy
              onClicked: service.refit(root.options(), true)
            }

            Button {
              visible: root.hasMeasurement()
              width: parent.width
              text: "Refit only, do not install"
              iconText: "󰑓"
              bordered: true
              enabled: !service.busy
              onClicked: service.refit(root.options(), false)
            }

            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.quality !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSectionHeader {
                text: service.proposal && service.proposal.quality
                  ? "LAST MEASUREMENT: " + service.optionsLabel(service.proposal).toUpperCase()
                    + (service.status.profile && service.status.profile.created_at === service.proposal.created_at
                        ? " — INSTALLED" : " — NOT INSTALLED")
                    + " · QUALITY " + String(service.proposal.quality.verdict).toUpperCase()
                  : "MEASUREMENT QUALITY"
                foreground: service.proposal && service.proposal.quality
                  && !service.proposal.quality.accepted
                  ? (bar ? bar.urgent : Color.urgent) : root.foreground
                fontFamily: root.fontFamily
              }
              Text {
                width: parent.width
                text: root.qualityMetricsText()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
              }
              Text {
                visible: root.qualityIssuesText() !== ""
                width: parent.width
                text: root.qualityIssuesText()
                color: service.proposal && service.proposal.quality
                  && !service.proposal.quality.accepted
                  ? (bar ? bar.urgent : Color.urgent) : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
              Text {
                visible: root.qualityGuidanceText() !== ""
                width: parent.width
                text: "Retry guidance:\n" + root.qualityGuidanceText()
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
              Text {
                visible: root.microphoneArrayText() !== ""
                width: parent.width
                text: root.microphoneArrayText()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.measurement !== undefined
                && service.proposal.measurement.channels !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSectionHeader {
                text: "MEASURED RESPONSE — LEFT / RIGHT"
                  + (service.proposal && service.proposal.fit
                    ? " · " + service.optionsLabel(service.proposal).toUpperCase() + " TARGET" : "")
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
              Text {
                width: parent.width
                text: "Solid: measured L/R   ·   accent dashed: predicted correction   ·   dotted: target   ·   top ticks: section centres   ·   shaded: repeat uncertainty   ·   ±12 dB relative scale"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Column {
              visible: service.proposal !== null && service.proposal !== undefined
                && service.proposal.fit !== null && service.proposal.fit !== undefined
              width: parent.width
              spacing: Style.space(7)

              PanelSectionHeader {
                text: "SECTIONS OF THE LAST MEASUREMENT"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Text {
                width: parent.width
                text: root.gainsText()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
              }
              Text {
                width: parent.width
                text: root.fitSummaryText()
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
              Text {
                width: parent.width
                text: {
                  var fit = service.proposal && service.proposal.fit
                  var maximum = fit ? Number(fit.maximum_allowed_boost_db || 0).toFixed(1) : "0.0"
                  return "The optimizer moves each section to the measured problem and chooses its width; Q means width (a smaller Q is broader, a larger Q is narrower). It adds a section only when a separate held-out repeat also improves. Cuts are preferred: the whole correction never goes deeper than 15 dB at any frequency, shallower toward the band edges with built-in microphones. A residual that stays high all the way to the bass or treble end is handled by a shelf instead of several overlapping filters. Boosts are capped at +"
                    + maximum + " dB and require a broad, repeatable, high-confidence deficit that passes the same holdout check. "
                    + "Protection: automatic input trim, dual 55 Hz high-pass, −1 dBFS limiter; loudness make-up only when selected."
                }
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
              Button {
                width: parent.width
                text: service.busy && service.phase === "install" ? "Installing…"
                  : "Install and play: " + service.optionsLabel(service.proposal)
                iconText: "󰄬"
                bordered: true
                enabled: !service.busy && service.proposal !== null
                  && service.proposal.quality !== undefined
                  && service.proposal.quality.accepted === true
                onClicked: service.install()
              }
            }

            Text {
              visible: service.status.enabled && service.status.compare !== undefined
                && service.status.compare.available === true
              width: parent.width
              text: "STORED PROFILES — now playing: " + service.playingLabel()
                + "\nThe other one: " + service.otherLabel()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Button {
              visible: service.status.enabled && service.status.compare !== undefined
                && service.status.compare.available === true
              width: parent.width
              text: service.busy && service.phase === "compare" ? "Switching…"
                : "Switch to: " + service.otherLabel()
              iconText: "󰓦"
              bordered: true
              enabled: !service.busy
              onClicked: service.compare()
            }

            Button {
              visible: service.status.enabled
              width: parent.width
              text: "Stop calibration and remove it from the output"
              iconText: "󰅖"
              bordered: true
              enabled: !service.busy
              onClicked: service.disable()
            }
          }
        }
      }
    }
  }
}
