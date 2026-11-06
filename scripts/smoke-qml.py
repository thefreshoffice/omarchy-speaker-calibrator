#!/usr/bin/env python3
"""Load the plugin's real QML against an installed Omarchy shell.

Requires Quickshell and a Wayland session. The panel stays hidden. The fixture
uses disposable HOME/state and offline backend/demo data, so it neither fetches live data
nor changes the desktop. Backend behavior is tested separately.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New directory for the fixture and runtime.log')
    args = parser.parse_args()
    shell = Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy')) / 'shell'
    if not (shell / 'Ui/qmldir').is_file() or not shutil.which('quickshell'):
        parser.error('Installed Omarchy shell modules and Quickshell are required')
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='plugin-qml-smoke-'))
    if output == PROJECT or PROJECT in output.parents:
        parser.error('The fixture output must be outside the source tree')
    if args.output:
        output.mkdir(parents=True, exist_ok=False)
    for module in ('Commons', 'Ui'):
        (output / module).symlink_to(shell / module, target_is_directory=True)
    plugin = output / 'plugin'
    shutil.copytree(PROJECT, plugin, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv', 'docs', 'tests'))
    (plugin / 'speaker-calibrate.py').write_text('print(\'{"profile": null, "service": "inactive", "enabled": false}\')\n')
    home = output / 'home'
    home.mkdir()
    (output / 'shell.qml').write_text('''import QtQuick
import Quickshell
import qs.Commons as Commons
import "plugin" as Plugin
ShellRoot {
  id: test
  property var widget: Plugin.BarWidget { }
  Timer { interval: 1800; running: true; onTriggered: {
    console.log("PLUGIN_QML_OK", Commons.Color.foreground); Qt.quit();
  } }
}
''')
    env = {**os.environ, 'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_STATE_HOME': str(home / '.local/state'), 'XDG_CACHE_HOME': str(home / '.cache'),
           'PYTHONDONTWRITEBYTECODE': '1'}
    try:
        run = subprocess.run(['quickshell', '--no-color', '--path', str(output)],
                             env=env, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print('QML smoke timed out; fixture:', output)
        return 1
    log = run.stdout + run.stderr
    (output / 'runtime.log').write_text(log)
    print(log)
    failed = run.returncode or 'PLUGIN_QML_OK' not in log or any(marker in log for marker in
        ('ERROR:', 'ReferenceError:', 'TypeError:', 'Binding loop', 'Unable to assign', 'no signal of the target matches'))
    print('QML smoke ' + ('FAILED' if failed else 'passed') + '; fixture: ' + str(output))
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
