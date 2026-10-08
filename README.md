# Stingray Labeler

Stingray Labeler is a desktop application for reviewing images and creating bounding-box annotations with named labels. It runs from the Linux or Windows command line and can be packaged as a Windows executable.

## Windows executable

Download the latest Windows build from [GitHub Releases](https://github.com/anhph95/stingray-labeler/releases/latest). The executable can be run directly; Python does not need to be installed on the target computer.

## Install from source

Python 3.10 or newer is required. From the repository directory, create and activate a virtual environment, then install the application:

```sh
python -m venv .venv
```

On Linux:

```sh
source .venv/bin/activate
python -m pip install .
stingray-labeler
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install .
stingray-labeler-gui
```

The application can also be launched on either platform with `python -m stingray_labeler`.

## Build the Windows application

On Windows, install the build dependencies and create the executable:

```powershell
python -m pip install ".[build]"
python -m PyInstaller --clean --noconfirm pyinstaller.spec
```

The executable is written to `dist/StingrayLabeler.exe`. GitHub Actions also builds a Windows executable when a version tag is published.

## Using the app

The full guide is in the app under **Help → User Guide** (F1), and in [src/stingray_labeler/assets/user_guide.md](src/stingray_labeler/assets/user_guide.md). In short:

1. **File → New Project…** (Ctrl+N) or **Open Project…** (Ctrl+O), and choose your user name.
2. Add sources from the **File** menu: images, image folders, or uncompressed AVI videos. PNG, JPEG, TIFF, BMP and WebP images are supported.
3. Draw boxes with **Draw box** or **B**; type a new category name when asked to create it. **V** verifies the selected box.
4. Set each image's **State**: Unverified, Review or Verified (**Shift+V**, **C**). Editing an unverified image sets it to Review.
5. **File → Save Project** (Ctrl+S) saves Verified and Review images with all their boxes and copies them into the project folder. Nothing is ever deleted from the project folder.
6. Two people working on copies of a project can combine their work with **Merge** (offered at Save, or **File → Merge Project…**).
7. **File → Export Training Dataset…** exports verified images and verified boxes for model training.

Unsaved work is backed up every 5 minutes on this computer and offered for recovery if the app closes without saving.
