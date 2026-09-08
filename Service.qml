import QtQuick
import Quickshell.Io

Item {
  id: root
  property string helperPath: ""
  property var sinks: []
  property var microphones: []
  property var status: ({ service: "unknown", enabled: false, profile: null })
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
      ? "Checking the level, then playing six left/right sweeps — about 30 seconds…"
      : operation === "compare" ? "Switching profiles…"
      : operation === "refit" ? "Refitting the saved measurement…" : "Working…"
    _stdout = ""
    _stderr = ""
    // Keep mise/user-site packages from shadowing Arch's matched NumPy/SciPy
    // pair.  -s disables only the user site; /usr/lib Python packages remain.
    process.command = ["/usr/bin/env", "-u", "PYTHONHOME", "-u", "PYTHONPATH",
                       "/usr/bin/python3", "-s", helperPath].concat(arguments)
    process.running = true
  }
  function refresh() { if (!busy) start("devices", ["devices-json"]) }
  function refreshStatus() { start("status", ["status-json"]) }
  function measure(sink, mic, channel, voicing, micCalibrationFile, loudness, bass) {
    proposal = null
    var arguments = ["calibrate-json", "--sink", sink, "--mic", mic,
                     "--channel", String(channel), "--voicing", voicing,
                     "--loudness", loudness || "protected", "--bass", bass || "normal"]
    if (micCalibrationFile && micCalibrationFile.length > 0)
      arguments.push("--mic-cal-file", micCalibrationFile)
    start("measure", arguments)
  }
  // Re-fit the last recorded sweeps with different options, without playing
  // anything.
  function refit(voicing, loudness, bass) {
    start("refit", ["reanalyze-saved-json", "--voicing", voicing,
                    "--loudness", loudness || "protected", "--bass", bass || "normal"])
  }
  function install() { start("install", ["install-proposal"]) }
  function disable() { start("disable", ["disable"]) }
  function compare() { start("compare", ["compare-toggle"]) }

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
          root.message = "Devices refreshed"
          root.phase = ""
          Qt.callLater(root.refreshStatus)
          return
        }
        if (root.phase === "status") {
          var statusPayload = JSON.parse(raw)
          root.status = statusPayload
          root.proposal = statusPayload.proposal || null
          root.message = ""
        }
        else if (root.phase === "measure" || root.phase === "refit") {
          root.proposal = JSON.parse(raw)
          if (root.proposal.quality && root.proposal.quality.accepted) {
            var count = root.proposal.fit ? Number(root.proposal.fit.filter_count || 0) : 0
            root.message = "Measurement accepted — " + count
              + (count === 1 ? " adaptive filter passed" : " adaptive filters passed")
              + " repeat validation"
          }
          else
            root.message = "Measurement rejected — follow the retry guidance below"
        } else if (root.phase === "install") {
          root.status = { service: "active", enabled: true, profile: JSON.parse(raw),
                          compare: root.status.compare }
          root.message = "Profile installed and enabled"
          Qt.callLater(root.refreshStatus)
        } else if (root.phase === "compare") {
          var compare = JSON.parse(raw)
          var playing = compare[compare.active]
          root.status = Object.assign({}, root.status, { compare: compare })
          root.message = "Now playing the " + compare.active + " profile"
            + (playing && playing.label ? " · " + playing.label : "")
            + (compare.method === "restart" ? " · tuning restarted" : " · switched live")
        } else if (root.phase === "disable") {
          root.status = { service: "inactive", enabled: false, profile: root.status.profile }
          root.message = "Calibration disabled"
        }
      } catch (exception) {
        root.error = "Invalid response from calibration helper"
      }
      root.phase = ""
    }
  }
}
