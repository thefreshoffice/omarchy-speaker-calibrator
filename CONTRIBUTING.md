# Contributing

Thank you for wanting to help. A few things that save both of us a round trip.

## Branches and releases

- **`main` is the leading edge.** Open pull requests against `main`. It always
  holds the newest accepted work and is expected to pass the tests.
- **A release is a tag on a commit of `main`** (`v1.1.0`). There is no separate
  release branch. If a released version ever needs a fix that cannot wait for
  the next release, a `release/1.1` branch is cut from its tag for that fix
  only, and the fix also goes to `main`.
- **One exception to "always moving": the marketplace review.** The Omarchy
  plugin marketplace reviews one exact commit and requires the default branch
  to stay on it until the review is done, usually about a day. During that
  window nothing is merged into `main`; pull requests stay open and are merged
  right after. A pinned issue says when a freeze is on.
- **`v2-room-acoustics`** is the one long-lived branch. Version 1 tunes the
  laptop's own speakers. Room correction, external speakers and measuring at
  several positions belong to version 2 and are collected there; `main` is
  merged into it regularly.

## Scope of version 1

Built-in speakers, measured with the built-in microphones or one external
measuring microphone. A change that serves that is welcome. A change aimed at
room acoustics or external speaker setups is welcome too, on the version 2
branch.

## No external code at install or run time

The plugin is listed in the Omarchy marketplace and every release is security
reviewed. It does not download, build or recommend third-party code: no
out-of-tree drivers, no AUR packages, no `curl | sh`, no unpinned sources, in
code or in the documentation. Configuration is fine (a WirePlumber rule, a
mixer setting); code is not. If something cannot be done without outside code,
open an issue first. The preferred answer is to build it in, the way deep bass
was rebuilt from PipeWire's own nodes in 1.1.0.

## Tests

```console
cd tests
/usr/bin/python -m unittest discover .
```

They need `python-numpy` and `python-scipy`. Use the system Python; a version
manager's Python usually lacks them. A change to the measurement or the fit
should come with a test that fails without it. After editing QML, check the
syntax before trying it in the shell, because the shell only logs a syntax
error and the panel silently stays closed:

```console
/usr/lib/qt6/bin/qmllint Panel.qml Service.qml
```

Messages about unknown Quickshell modules are noise; `Expected token` is not.

## What makes a change easy to accept

- Say what you measured and on which machine. Numbers beat adjectives.
- Keep safety limits as they are unless the change is about them: the cut and
  boost ceilings, the sweep levels, the clipping and repeatability gates.
- Every `Text` in QML carries `textFormat: Text.PlainText`. Device names and
  imported files are input, not trusted strings.
- Processes are started as argument lists with absolute paths, never as shell
  strings.
- Small pull requests. Three of five hundred lines are reviewed faster than
  one of fifteen hundred.
