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
  function gainsText() {
    if (!service.proposal || !service.proposal.fit) return ""
    var centers = service.proposal.fit.centers_hz || []
    var qValues = service.proposal.fit.q || []
    var gains = service.proposal.fit.gains_db || []
    var rows = []
    for (var index = 0; index < Math.min(centers.length, gains.length, qValues.length); index++) {
      var gain = Number(gains[index])
      rows.push(Number(centers[index]).toFixed(1) + " Hz   ·   Q "
        + Number(qValues[index]).toFixed(2) + "   ·   "
        + (gain > 0 ? "+" : "") + gain.toFixed(2) + " dB")
    }
    return rows.join("   ·   ")
  }
  function fitSummaryText() {
    if (!service.proposal || !service.proposal.fit) return ""
    var fit = service.proposal.fit
    var validation = fit.cross_validation || {}
    return Number(fit.filter_count || 0) + " adaptive parametric filters"
      + "   ·   weighted target error " + Number(fit.weighted_rmse_before_db || 0).toFixed(2)
      + " → " + Number(fit.weighted_rmse_after_db || 0).toFixed(2) + " dB"
      + "   ·   held-out repeat " + Number(validation.rmse_before_db || 0).toFixed(2)
      + " → " + Number(validation.rmse_after_db || 0).toFixed(2) + " dB"
      + "   ·   max boost " + Number(fit.actual_maximum_boost_db || 0).toFixed(2) + " dB"
      + "   ·   protected headroom " + Number(fit.headroom_db || 1).toFixed(2) + " dB"
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

  Service { id: service; helperPath: root.helperPath }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(620))

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
            meta: service.busy ? service.message
              : (service.status.enabled ? "Protected profile active" : "Ready to measure")
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
            text: "Choose the physical speakers and a microphone. The test repeats an exponential sweep three times per speaker, measures left and right separately, and rejects unreliable captures. Pause other audio and keep the room quiet for about 24 seconds."
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
                return (item.internal ? "Internal — " : "External — ") + item.description
              })
              enabled: !service.busy && model.length > 0
              onCurrentIndexChanged: channelBox.currentIndex = 0
            }

            Text { text: "MIC INPUT"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
            QQC.ComboBox {
              id: channelBox
              Layout.fillWidth: true
              model: root.channelOptions()
              enabled: !service.busy
            }

            Text {
              visible: !root.selectedMicIsInternal()
              text: "MIC CAL FILE"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            QQC.TextField {
              id: micCalPath
              visible: !root.selectedMicIsInternal()
              Layout.fillWidth: true
              enabled: !service.busy
              placeholderText: "Optional path to serial-number calibration .txt"
              selectByMouse: true
            }

            Text { text: "VOICING"; color: root.dim; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
            QQC.ComboBox {
              id: voicingBox
              Layout.fillWidth: true
              model: ["Warm — softer, less sharp", "Flat — balanced, more detail"]
              enabled: !service.busy
            }
          }

          Text {
            visible: root.multiMicAvailable()
            width: parent.width
            text: "ALL BUILT-IN MICROPHONES — Recommended. They are measured separately and combined only after analysis, avoiding false cancellations. If they disagree, the calibration automatically becomes more cautious. Choose one microphone only for troubleshooting."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            text: "WARM — Makes sharp voices, cymbals, and hiss gentler. It sounds softer and can be easier to enjoy for a long time.\n\nFLAT — Keeps the sound more balanced, without adding Warm’s extra softness. It preserves more clarity and may sound a little brighter. ‘Flat’ does not mean the graph must become a perfectly straight line."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
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

          Button {
            width: parent.width
            text: service.busy && service.phase === "measure" ? "Measuring…" : "Measure speakers"
            iconText: service.busy ? "󰑓" : "󰋋"
            iconSpinning: service.busy
            bordered: true
            focusable: true
            enabled: !service.busy && sinkBox.currentIndex >= 0 && micBox.currentIndex >= 0
            onClicked: {
              var sink = service.sinks[sinkBox.currentIndex]
              var mic = service.microphones[micBox.currentIndex]
              service.measure(sink.name, mic.name, root.selectedChannelValue(),
                              voicingBox.currentIndex === 0 ? "warm" : "neutral",
                              micCalPath.text.trim())
            }
          }

          Column {
            visible: service.proposal !== null && service.proposal.quality !== undefined
            width: parent.width
            spacing: Style.space(7)

            PanelSectionHeader {
              text: service.proposal && service.proposal.quality
                ? "MEASUREMENT QUALITY — " + String(service.proposal.quality.verdict).toUpperCase()
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
            visible: service.proposal !== null && service.proposal.measurement !== undefined
              && service.proposal.measurement.channels !== undefined
            width: parent.width
            spacing: Style.space(7)

            PanelSectionHeader {
              text: "MEASURED RESPONSE — LEFT / RIGHT"
                + (service.proposal && service.proposal.fit
                  ? " · " + (service.proposal.voicing === "warm" ? "WARM" : "FLAT")
                    + " TARGET" : "")
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

              Connections {
                target: service
                function onProposalChanged() {
                  responseCanvas.requestPaint()
                  if (service.proposal && service.proposal.voicing) {
                    voicingBox.currentIndex = service.proposal.voicing === "warm" ? 0 : 1
                    var savedChannel = service.proposal.microphone
                      ? service.proposal.microphone.channel : 0
                    if (root.multiMicAvailable())
                      channelBox.currentIndex = savedChannel === "all"
                        ? 0 : Number(savedChannel) + 1
                    else
                      channelBox.currentIndex = Number(savedChannel || 0)
                  }
                }
              }
              onVisibleChanged: if (visible) requestPaint()
              onWidthChanged: requestPaint()
            }
            Text {
              width: parent.width
              text: "Solid: measured L/R   ·   accent dashed: predicted correction   ·   dotted: pleasant in-room loudspeaker target   ·   top ticks: adaptive filter centers   ·   shaded: repeat uncertainty   ·   ±12 dB relative scale"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Column {
            visible: service.proposal !== null && service.proposal.fit !== null
            width: parent.width
            spacing: Style.space(7)

            PanelSectionHeader {
              text: "ADAPTIVE PARAMETRIC FILTERS"
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
                return "The optimizer moves each filter to the measured problem and chooses its width; Q means width (a smaller Q is broader, a larger Q is narrower). It adds a filter only when a separate held-out repeat also improves. Cuts are preferred and each is limited to −6 dB. With built-in microphones, at most six broad filters are allowed and bass/extreme-treble cuts stay tighter. Boosts are capped at +"
                  + maximum + " dB and require a broad, repeatable, high-confidence deficit that passes the same holdout check. "
                  + "Protection: automatic input trim, dual 55 Hz high-pass, −1 dBFS limiter, no makeup gain."
              }
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Button {
              width: parent.width
              text: service.busy && service.phase === "install" ? "Installing…" : "Install profile"
              iconText: "󰄬"
              bordered: true
              selected: true
              enabled: !service.busy && service.proposal !== null
                && service.proposal.quality !== undefined
                && service.proposal.quality.accepted === true
              onClicked: service.install()
            }
          }

          Button {
            visible: service.status.enabled
            width: parent.width
            text: "Disable calibration"
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
