import QtQuick
import Quickshell.Io

Item {
  id: root
  property string helperPath: ""
  property var sinks: []
  property var microphones: []
  property var status: ({ service: "unknown", enabled: false, profile: null, bypass: false })
  property var proposal: null
  // Asked for only when the comparison is on screen: it carries two full
  // curves, which have no business in every status refresh.
  property var micComparison: null
  property bool busy: process.running
  property string phase: ""
  property string error: ""
  property string message: ""
  property string _stdout: ""
  property string _stderr: ""
  // The helper is ours, but its output is still input to this process: the
  // shell hosts every widget, so a run that never stops printing must not be
  // allowed to grow without bound here.  Collected in chunks against a budget
  // rather than whole, and the run is killed the moment it goes over.
  readonly property int _maxOutput: 262144
  readonly property int _maxError: 8192
  property bool _overflowed: false

  function start(operation, arguments) {
    if (busy || helperPath === "") return
    phase = operation
    error = ""
    message = operation === "measure"
      ? "Measuring — a short level check, then six sweeps, about 30 seconds. Keep quiet…"
      : operation === "compare" ? "Switching profiles…"
      : operation === "bypass" ? "Switching…"
      : operation === "verify" ? "Checking — playing the sweeps through the calibration…"
      : operation === "refine" ? "Improving from the last check…"
      : operation === "deepbass" ? "Switching deep bass…"
      : operation === "loudness" ? "Switching loudness compensation…"
      : operation === "refit" ? "Applying…" : "Working…"
    _stdout = ""
    _stderr = ""
    _overflowed = false
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
    if (profile.voicing === "warm") parts.push("warm voicing")
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
    return ["--voicing", options.voicing || "neutral",
            "--loudness", options.loudness || "protected",
            "--bass", options.bass || "normal",
            "--channel-trim", options.channelTrim || "off"]
  }

  // Draw the last known answer straight away.  The real query is already on
  // its way and lands on top of this, so a stale cache is visible for a few
  // dozen milliseconds at most and never decides anything.
  function applyCachedStatus(raw) {
    if (busy || status.service !== "unknown") return false
    try {
      var payload = JSON.parse(String(raw || ""))
      if (!payload || !payload.service) return false
      status = payload
      proposal = payload.proposal || null
      return true
    } catch (exception) {
      return false
    }
  }
  // Asking while something else is running used to drop the request on the
  // floor, which left the device list showing whatever it last managed to
  // fetch: a microphone plugged in since then simply never appeared.  A
  // refresh asked for now happens, even if it has to wait its turn.
  property bool _refreshPending: false
  function refresh() {
    if (busy) { _refreshPending = true; return }
    start("devices", ["devices-json"])
  }
  // Draw from the last known state before the full check answers.  The helper
  // reads that file, not the panel: it lives at a predictable name that any
  // process running as this user could replace with a symlink or a pipe, and
  // this is the shell.
  function loadCache() { if (!busy) start("cache", ["status-cache-json"]) }
  // The two microphones, side by side.
  function loadMicrophones() {
    if (!busy) start("mics", ["microphone-comparison-json"])
  }
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
  function verify() { start("verify", ["verify-json"]) }
  function refine() { start("refine", ["refine-json", "--install"]) }
  // One button: installs the add-on the first time, switches it after that.
  function deepBass() { start("deepbass", ["deep-bass-toggle"]) }
  function loudnessCompensation() { start("loudness", ["loudness-toggle"]) }

  // One plain sentence about the last check.
  function verificationSummary(check) {
    if (!check) return ""
    if (check.verdict === "inconclusive")
      return "The check could not measure cleanly, so it says nothing about the calibration."
    var errors = check.target_error_db || {}
    var gained = Number(errors.before || 0) - Number(errors.measured || 0)
    var off = Number((check.model_error_db || {}).rms || 0).toFixed(1)
    if (check.verdict === "pass")
      return "Checked: the sound follows the plan within " + off + " dB, and sits "
        + gained.toFixed(1) + " dB closer to the target than the plain speakers."
    return "Check " + String(check.verdict) + ": "
      + ((check.notes && check.notes.length > 0) ? check.notes[0] : "off plan by " + off + " dB.")
  }

  // Append one chunk if it fits, and stop the run if it does not.  Killing on
  // overflow is the point: truncating would leave a half-read JSON document
  // that parses into something arbitrary.
  function _collect(chunk, isError) {
    if (_overflowed) return
    var limit = isError ? _maxError : _maxOutput
    var current = isError ? _stderr : _stdout
    if (current.length + chunk.length > limit) {
      _overflowed = true
      _stdout = ""
      _stderr = ""
      process.signal(15)
      killTimer.restart()
      return
    }
    if (isError) _stderr += chunk
    else _stdout += chunk
  }

  // If the term did not land, insist.
  Timer {
    id: killTimer
    interval: 2000
    onTriggered: if (process.running) process.signal(9)
  }

  // Whatever the run was, if a refresh was asked for while it held the
  // process, do it now.
  onBusyChanged: if (!busy && _refreshPending) Qt.callLater(function () {
    if (!root.busy && root._refreshPending) {
      root._refreshPending = false
      root.start("devices", ["devices-json"])
    }
  })

  Component.onDestruction: if (process.running) process.signal(15)

  Process {
    id: process
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function (chunk) { root._collect(chunk, false) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function (chunk) { root._collect(chunk, true) }
    }
    onExited: function(exitCode) {
      var raw = String(root._stdout || "").trim()
      var err = String(root._stderr || "").trim()
      if (root._overflowed) {
        root.error = "The helper produced more output than the panel will read."
        root.message = ""
        root.phase = ""
        return
      }
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
        if (root.phase === "mics") {
          root.micComparison = JSON.parse(raw)
          root.message = ""
          root.phase = ""
          return
        }
        if (root.phase === "cache") {
          root.applyCachedStatus(raw)
          root.message = ""
          root.phase = ""
          Qt.callLater(root.refresh)
          return
        }
        if (root.phase === "status") {
          var statusPayload = JSON.parse(raw)
          root.status = statusPayload
          root.proposal = statusPayload.proposal || null
          // A background refresh has nothing to report; leaving "Working…" on
          // screen makes an idle panel look busy.
          if (root.message === "Working…") root.message = ""
        }
        else if (root.phase === "measure" || root.phase === "refit" || root.phase === "refine") {
          var result = JSON.parse(raw)
          root.proposal = result
          var accepted = result.quality && result.quality.accepted
          if (accepted && result.installed) {
            root.status = Object.assign({}, root.status, { enabled: true, profile: result, bypass: false })
            var refinement = ((result.measurement || {}).refinement) || {}
            root.message = root.phase === "measure" ? "Calibrated and playing: " + root.simpleLabel(result)
              : root.phase === "refine"
                ? "Improved from the check, round " + refinement.iterations
                  + " · biggest change " + Number(refinement.largest_step_db || 0).toFixed(1)
                  + " dB · check it again to see if it helped"
                : "Applied: " + root.simpleLabel(result)
            if (result.activation === "restart") root.message += " · tuning restarted once"
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
        } else if (root.phase === "verify") {
          var check = JSON.parse(raw)
          root.status = Object.assign({}, root.status, { verification: check })
          root.message = root.verificationSummary(check)
        } else if (root.phase === "deepbass") {
          var bass = JSON.parse(raw)
          root.status = Object.assign({}, root.status, {
            bassEnhancer: bass,
            deepBass: bass.deep_bass !== undefined ? bass.deep_bass : root.status.deepBass
          })
          root.message = bass.message || ""
          if (!bass.started) Qt.callLater(root.refreshStatus)
        } else if (root.phase === "loudness") {
          var loudness = JSON.parse(raw)
          root.status = Object.assign({}, root.status, {
            loudnessCompensation: loudness.loudness_compensation,
            loudnessTracker: loudness.tracker
          })
          root.message = loudness.message || ""
          Qt.callLater(root.refreshStatus)
        } else if (root.phase === "bypass") {
          var bypassPayload = JSON.parse(raw)
          root.status = Object.assign({}, root.status, { compare: bypassPayload, bypass: bypassPayload.bypass })
          var match = Number(bypassPayload.level_match_db || 0)
          root.message = bypassPayload.bypass
            ? "Calibration off — the plain speakers"
              + (match < -0.05 ? ", turned down " + Math.abs(match).toFixed(1)
                                 + " dB to the same loudness so only the tone changes" : "")
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
