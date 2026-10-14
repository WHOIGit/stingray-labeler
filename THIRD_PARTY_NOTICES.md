# Third-party notices

Stingray Labeler's own source code is licensed under the MIT License (see
[LICENSE](LICENSE)). The Windows executable published on GitHub Releases also
contains the third-party software listed below, each under its own license.

## Qt for Python (PySide6, Shiboken6) and Qt

- License: GNU Lesser General Public License v3.0 (LGPL-3.0), see
  [licenses/LGPL-3.0.txt](licenses/LGPL-3.0.txt), which supplements the GNU
  General Public License v3.0, see [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt).
- Copyright: The Qt Company Ltd. and other contributors.
- Used unmodified, from the PySide6 packages on PyPI. Source code:
  <https://code.qt.io/cgit/pyside/pyside-setup.git/> (PySide6, Shiboken6) and
  <https://code.qt.io/cgit/qt/qt5.git/> (Qt), or the source packages at
  <https://download.qt.io/official_releases/>.

The executable is built with PyInstaller as a single file, so the Qt libraries
are packed inside it. To use the application with a modified version of Qt or
PySide6, install that version into a Python environment and rebuild the
executable from this repository's source with `pyinstaller.spec`, as described
in the README under "Build the Windows application". Running from source
(`pip install .`) uses whichever PySide6 is installed in that environment.

## Pillow

- License: MIT-CMU (historical permission notice), see
  <https://github.com/python-pillow/Pillow/blob/main/LICENSE>.
- Copyright: 1997-2011 Secret Labs AB, 1995-2011 Fredrik Lundh and
  contributors, 2010 Jeffrey A. Clark and contributors.

## Python

- License: Python Software Foundation License, see
  <https://docs.python.org/3/license.html>.
- Copyright: Python Software Foundation.

## PyInstaller bootloader

- License: GNU General Public License v2.0 or later, with an exception that
  allows the bootloader to be distributed as part of a built application under
  any license; see <https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt>.
- Copyright: PyInstaller Development Team.
