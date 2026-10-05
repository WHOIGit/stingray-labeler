# Stingray Labeler

A desktop COCO image annotation tool for the Stingray custom tow sled. The same Python package can be launched from Linux or Windows command lines, or bundled as a Windows GUI application.

## Requirements

Python 3.10 or newer is required. The app uses PySide6 for its interface and Pillow for image processing.

## Install and run from the command line

From the repository root, install the package into the active environment:

```bash
python -m pip install .
```

Launch the app on Linux or Windows:

```bash
stingray-labeler
```

You can also launch it as a module:

```bash
python -m stingray_labeler
```

On Windows, the `stingray-labeler-gui` launcher is also installed as a GUI entry point without a console window.

## Build a Windows GUI application

On Windows, install the optional build dependency and build the executable:

```powershell
python -m pip install ".[build]"
pyinstaller --clean pyinstaller.spec
```

The executable is written to `dist/StingrayLabeler.exe`. The spec bundles the package's icon assets and runs without a console window.

## Publish a Windows release

GitHub Actions builds the Windows executable and attaches it to a GitHub Release whenever a version tag is pushed. The tag must match the version in `pyproject.toml` with a `v` prefix. For the first release, that tag is `v0.1.0`:

```bash
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

For later releases, update the package version in `pyproject.toml`, commit the change, then create and push the matching version tag. The workflow uses a Windows runner, builds `StingrayLabeler.exe`, and publishes it with generated release notes.

## Project structure

- `src/stingray_labeler/window.py` contains the main annotation window and user workflows.
- `graphics.py` contains the annotation canvas, rulers, scale bar, and box graphics.
- `image_processing.py` contains the background image preview worker.
- `dataset.py` contains COCO verification and filesystem path helpers.
- `app.py` and `__main__.py` provide the shared app startup paths.
