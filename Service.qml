import QtQuick
import Quickshell.Io

Item {
  id: root
  property string helperPath: ""
  property var sinks: []
  property var microphones: []
  property var status: ({ service: "unknown", enabled: false, profile: null, bypass: false })
  property var proposal: null
  property bool busy: process.running
  property string phase: ""
  property string error: ""
  property string message: ""
  property string _stdout: ""
  property string _stderr: ""

  function start(operation, arguments) {
    if (busy || helperPath === "") return
    phase = operation
    error = ""
    message = operation === "measure"
      ? "Measuring — a short level check, then six sweeps, about 30 seconds. Keep quiet…"
      : operation === "compare" ? "Switching profiles…"
      : operation === "bypass" ? "Switching…"
      : operation === "refit" ? "Applying…" : "Working…"
    _stdout = ""
    _stderr = ""
    // Keep mise/user-site packages from shadowing Arch's matched NumPy/SciPy
    // pair.  -s disables only the user site; /usr/lib Python packages remain.
    process.command = ["/usr/bin/env", "-u", "PYTHONHOME", "-u", "PYTHONPATH",
                       "/usr/bin/python3", "-s", helperPath].concat(arguments)
    process.running = true
  }

  // Plain-language label for a profile: what the two simple toggles are set to.
  function simpleLabel(profile) {
    if (!profile) return ""
    var parts = []
    parts.push(profile.bass === "full" ? "Loudness on" : "Loudness off")
    var loudness = profile.loudness || "protected"
    parts.push(loudness === "matched" ? "Louder on"
      : loudness === "balanced" ? "Louder halfway" : "Louder off")
    if (profile.voicing === "neutral") parts.push("flat voicing")
    return parts.join(" · ")
  }
  // Technical label: "warm · full bass · matched".
  function optionsLabel(profile) {
    if (!profile) return ""
    var voicing = profile.voicing === "neutral" ? "flat" : "warm"
    var bass = profile.bass === "full" ? "full bass" : "normal bass"
    var loudness = profile.loudness || "protected"
    return voicing + " · " + bass + " · " + loudness
  }
  // The summary of the profile that is playing right now.
  function playingSummary() {
    var compare = status.compare
    if (compare && compare.available && compare[compare.active]) return compare[compare.active]
    if (compare && compare.current) return compare.current
    return null
  }
  function playingLabel() {
    var summary = playingSummary()
    if (summary && summary.label) return summary.label
    return status.profile ? optionsLabel(status.profile) : ""
  }
  // Label of the profile the compare button would switch to.
  function otherLabel() {
    var compare = status.compare
    if (!compare || !compare.available) return ""
    var other = compare.active === "previous" ? compare.current : compare.previous
    return other && other.label ? other.label : (compare.active === "previous" ? "current" : "previous")
  }
  function optionArguments(options) {
    return ["--voicing", options.voicing || "warm",
            "--loudness", options.loudness || "protected",
            "--bass", options.bass || "normal"]
  }

  function refresh() { if (!busy) start("devices", ["devices-json"]) }
  function refreshStatus() { start("status", ["status-json"]) }
  // Measure; with install=true the result is installed and played as soon as
  // it passes, so one press does the whole job.
  function measure(sink, mic, channel, options, install) {
    proposal = null
    var arguments = ["calibrate-json", "--sink", sink, "--mic", mic,
                     "--channel", String(channel)].concat(optionArguments(options))
    if (options.micCalibrationFile && options.micCalibrationFile.length > 0)
      arguments.push("--mic-cal-file", options.micCalibrationFile)
    if (install) arguments.push("--install")
    start("measure", arguments)
  }
  // Re-fit the last recorded sweeps with different options, without playing
  // anything; with install=true the result is applied immediately.
  function refit(options, install) {
    var arguments = ["reanalyze-saved-json"].concat(optionArguments(options))
    if (install) arguments.push("--install")
    start("refit", arguments)
  }
  function install() { start("install", ["install-proposal"]) }
  function disable() { start("disable", ["disable"]) }
  function compare() { start("compare", ["compare-toggle"]) }
  function bypass() { start("bypass", ["bypass-toggle"]) }

  Process {
    id: process
    running: false
    command: []
    stdout: StdioCollector { id: stdoutCollector; waitForEnd: true; onStreamFinished: root._stdout = text }
    stderr: StdioCollector { id: stderrCollector; waitForEnd: true; onStreamFinished: root._stderr = text }
    onExited: function(exitCode) {
      var raw = String(stdoutCollector.text || root._stdout || "").trim()
      var err = String(stderrCollector.text || root._stderr || "").trim()
      if (exitCode !== 0) {
        root.error = err || raw || "Operation failed"
        root.message = ""
        root.phase = ""
        return
      }
      try {
        if (root.phase === "devices") {
          var devices = JSON.parse(raw)
          root.sinks = devices.sinks || []
          root.microphones = devices.microphones || []
          root.message = ""
          root.phase = ""
          Qt.callLater(root.refreshStatus)
          return
        }
        if (root.phase === "status") {
          var statusPayload = JSON.parse(raw)
          root.status = statusPayload
          root.proposal = statusPayload.proposal || null
        }
        else if (root.phase === "measure" || root.phase === "refit") {
          var result = JSON.parse(raw)
          root.proposal = result
          var accepted = result.quality && result.quality.accepted
          if (accepted && result.installed) {
            root.status = Object.assign({}, root.status, { enabled: true, profile: result, bypass: false })
            root.message = (root.phase === "measure" ? "Calibrated and playing: " : "Applied: ")
              + root.simpleLabel(result)
              + (result.activation === "restart" ? " · tuning restarted once" : "")
            Qt.callLater(root.refreshStatus)
          } else if (accepted) {
            var count = result.fit ? Number(result.fit.filter_count || 0) : 0
            root.message = (root.phase === "refit" ? "Refit ready: " : "Measurement accepted: ")
              + root.optionsLabel(result) + " · " + count
              + (count === 1 ? " section" : " sections")
              + " · nothing changes until you press Install"
          } else
            root.message = "The measurement was rejected — see the guidance below and try again"
        } else if (root.phase === "install") {
          var installed = JSON.parse(raw)
          root.status = Object.assign({}, root.status, { enabled: true, profile: installed, bypass: false })
          root.message = "Installed and playing: " + root.optionsLabel(installed)
            + (installed.activation === "restart" ? " · tuning restarted" : " · switched live")
          Qt.callLater(root.refreshStatus)
        } else if (root.phase === "compare") {
          var compare = JSON.parse(raw)
          var playing = compare[compare.active]
          root.status = Object.assign({}, root.status, { compare: compare, bypass: false })
          root.message = "Now playing: " + (playing && playing.label ? playing.label : compare.active)
            + (compare.method === "restart" ? " · tuning restarted" : " · switched live")
        } else if (root.phase === "bypass") {
          var bypassPayload = JSON.parse(raw)
          root.status = Object.assign({}, root.status, { compare: bypassPayload, bypass: bypassPayload.bypass })
          root.message = bypassPayload.bypass
            ? "Calibration switched off — you are hearing the plain speakers"
            : "Calibration switched on"
        } else if (root.phase === "disable") {
          root.status = Object.assign({}, root.status, { service: "inactive", enabled: false })
          root.message = "Calibration stopped and removed from the output"
        }
      } catch (exception) {
        root.error = "Invalid response from calibration helper"
      }
      root.phase = ""
    }
  }
}
