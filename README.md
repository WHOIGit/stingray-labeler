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

## Create and annotate a project

1. Choose **File → New Project…** and select the folder containing the images. PNG, JPEG, TIFF, BMP, and WebP files are supported. Choose or enter your user name when prompted.
2. To continue an existing project, choose **File → Open Project…**, select its image folder, and select the annotation JSON file if it is not found automatically. Choose an existing user or enter a new name when prompted. Use **Edit → Change User…** or the **User** control in the menu bar to switch users during the session.
3. Add categories from **Edit → Edit Categories…**. Add more source images at any time with **File → Add Images…** or **File → Add Folder…**.
4. Select an image and draw a box with **Draw box** or the **B** key. Assign a category in the side panel. New or edited boxes are attributed to the active user. Use the **User** frame filter to show frames containing that user’s boxes, or choose **Background / no annotations** for background frames.
5. Mark reviewed images with **Image verified** and reviewed boxes with **Box verified**. For an image with no objects, select **Background (no annotations)**.
6. Save the project with **File → Save Project** (Ctrl+S). The first save asks where to put the project JSON and what to call it; later saves overwrite that same file. Use **File → Save Project As…** (Ctrl+Shift+S) to save to a different file. Reviewed images are copied into the project image folder.

## Export and shortcuts

Choose **File → Export Training Dataset…** to export verified images and their annotations for model training. Verified background images can be included. After choosing the training image folder, choose where to save the training JSON file and what to call it.

**File → Save Image…** exports the displayed image with its boxes drawn on it. **File → Export Crops…** saves each box in the current image as a separate crop.

- **B** starts drawing a box.
- **V** toggles verification for the selected box.
- **Shift+V** toggles verification for the selected image.
- Arrow keys move between images.

To calibrate image scale, open **Display → Scale → Calibrate from line…**, draw across a known dimension in the image, then enter its real-world length and unit in one dialog. The app calculates the pixel resolution and updates its rulers and scale bar. The **Pixel resolution** field remains editable for manual adjustments. Ruler labels adapt to zoom, scale bars shorten automatically when needed, and zooming out stops at the image's fit-to-view size. This is useful for fixed-camera imaging setups where the camera-to-subject distance does not change.
