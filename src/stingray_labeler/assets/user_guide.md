# Stingray Labeler — User Guide

Stingray Labeler draws and reviews bounding boxes on images and on frames of uncompressed AVI video, and saves them as a standard COCO JSON project.

## Key ideas

**Project.** A project is an *image folder* plus a *project JSON*. The JSON holds image names, boxes, categories and users. Saving copies each saved image into the project folder; video frames are written there as PNG.

**Image state.** Every image or frame is in exactly one state:

| State | Meaning | Saved? |
|---|---|---|
| **Unverified** | Not reviewed. | No — it can always be added again. |
| **Review** | Has work, or is worth another look. | Yes |
| **Verified** | You are confident in most of its boxes; some boxes may still be unverified. | Yes |

The first edit of an Unverified image (drawing, moving, resizing, deleting or verifying a box, or ticking Background) sets it to **Review** automatically. Verified is never changed automatically, except that an image that loses its last verified box (and is not Background) goes back to Review.

**Box state.** Each box is *verified* or *unverified* on its own. An image can be Verified only when it has at least one verified box or is marked Background.

**Users.** Every box records the user who last changed it. Choose the active user when a project opens; change it from **Edit → Change User…**. The active user's name is shown in the top-right corner.

**Names link everything.** Images are matched by file name. A frame of video `X.avi` is named `X_<frame>.png`, with frames counted from 0 — the same names your pipeline uses.

## Getting started

1. **File → New Project…** (Ctrl+N): choose the folder of images. Choose or type your user name.
2. Or **File → Open Project…** (Ctrl+O): choose the project's image folder, then its JSON. Files saved before image states existed ask once which state to give their images.
3. Add more sources at any time from the **File** menu: **Add Images…**, **Add Folder…**, **Add Video…**, **Add Video Folder…**.

### Where images are read from

When a project opens, images are read from the project folder. When you add a folder or video, images with the same name **now open from the source you just added** (their boxes stay attached). The most recently added source always wins. Nothing is loaded until you click an image.

## The window

**Left panel (top to bottom)**

- **Search** — filters both lists by name (Ctrl+F).
- **Videos** — appears once videos are added.
- **Images** — every image or frame with work, plus scanned images.
- **Filters** — User, Categories (All / None), Image state, Box verification, Box overlap IoU.
- **Counts** — one line; hover for the full breakdown.

**Centre** — the image, rulers, and below it the info line: `Dim` (pixels), `Size` (file size, or raw size for a video frame) and `Boxes`. With a video selected, the frame slider and frame number box appear on the right of that line.

**Right panel** — **State** (Unverified / Review / Verified), **Draw box**, **Background**, the selected box's **Category** and **Box verified**, **Delete annotation**, and the list of boxes in the image.

## Working with images

- Click an image in the list, or use **Left / Right** (or Previous / Next image) to move through the visible list.
- **N / Shift+N** jump to the next / previous image that is not Verified.
- Up / Down move through whichever list has the keyboard focus.

## Working with videos

Only **uncompressed AVI** video can be read; frames are read directly, one at a time, with no extra software.

1. **File → Add Video…** or **Add Video Folder…** lists the videos (only their headers are read).
2. Click a video. The frame controls appear under the image, and the arrow keys now step frames:

| Key / control | Action |
|---|---|
| **Left / Right**, ◀ ▶ buttons | previous / next frame (hold to keep stepping) |
| **Shift+Left / Right** | back / forward 10 frames |
| **Home / End** | first / last frame |
| Slider | drag to scrub |
| **G**, then type a number and **Enter** | jump to a frame (**Esc** cancels) |
| **N / Shift+N** | next / previous frame of this video that has work |
| Previous / Next video buttons | move between videos |

- A frame you only look at is not added to the project. It joins the image list once it has work.
- Frames already in the project JSON open with their boxes when you step onto them.
- Click any image in the image list to go back to stepping through images.

## Drawing and editing boxes

- **Draw box** or **B**, then drag on the image. **Esc** cancels.
- When you release, choose a category — or **type a new name** to create it. The last category used is offered first.
- Drag a box to move it; drag its handles to resize it.
- **V** toggles the selected box between verified and unverified.
- **Delete** (or **Delete annotation**) removes the selected box.
- **Background** marks an image with no objects; it can be ticked only when the image has no boxes.
- **Ctrl+Z / Ctrl+Y** undo and redo.
- **Space + drag** or the middle mouse button pans; the mouse wheel zooms.

## Setting the image state

Click **Unverified**, **Review** or **Verified**, or use the keys:

- **Shift+V** — Verified ↔ Review.
- **C** — Review ↔ Unverified (on a Verified image, C sets Review).

Setting an image to Unverified means it will not be saved.

## Saving

**File → Save Project** (Ctrl+S) saves every **Verified** and **Review** image with all of its boxes, verified or not. Unverified images are never saved.

- Images not yet in the project folder are copied there; video frames are written as PNG.
- **Nothing is ever deleted from the project folder.** If a saved image already exists there, you are asked per file: **Skip** (keep the folder's file), **Overwrite** (replace it from the current source), **Skip all** or **Overwrite all**. You are asked about each file once per session.
- **Save Project As…** (Ctrl+Shift+S) saves to a different JSON file.

## Working together: merging

Two people can work on copies of the same project and merge their work.

**At Save.** If someone else saved the project after you opened it, Save offers **Merge…**. The app compares three versions — the file as you opened it, your work, and theirs — then shows a summary before saving.

**File → Merge Project…** (Ctrl+M) merges another copy into the open project. Choose the other copy, then the original both copies started from. The result is unsaved until you save.

Merge rules:

- A change made by one side only is taken.
- A box deleted by one side and untouched by the other is deleted.
- A box deleted by one side but edited by the other is kept.
- Two boxes from the two sides on the same image with the same category and overlap IoU ≥ threshold (default 0.80) are duplicates; one is kept — the verified box, then the box on the side whose image is Verified, then yours.
- Overlapping boxes of different categories are both kept, and the image is set to **Review**.
- An image state changed differently on both sides becomes **Review**.

After a merge, use the **Review** filter to see what needs a look. Undo history is cleared after a merge.

## Categories and users

**Edit → Edit Categories…** (Ctrl+Shift+C): **Add**, **Edit** (rename; using an existing name offers to merge into it), **Delete** and **Color**. Deleting a category that boxes use lets you move those boxes to another category, or delete them too.

**Edit → Edit Users…** (Ctrl+Shift+U): **Add**, **Edit** (rename or merge) and **Delete**. Deleting a user lets you reassign their boxes or leave them unassigned; boxes are never deleted. The active user cannot be deleted.

**Edit → Import Categories from JSON…** copies category names from another project.

## Exporting

- **Export Training Dataset…** (Ctrl+Shift+E) — Verified images with their verified boxes (and verified Background images) to a folder and a COCO JSON. Video frames are written from the video.
- **Export Image…** — the displayed image with its boxes drawn on it.
- **Export Crops…** — each box in the displayed image as its own image.

## View

- **Image** — brightness, contrast, gamma, black / white points, invert, and auto levels for the current image. **Reset image settings** (Ctrl+0).
- **Scale** — rulers, scale bar, pixel resolution, and **Calibrate from line…**: draw across a known length and enter it.
- **Reference lines while drawing** (Ctrl+R) — horizontal and vertical lines through the cursor in draw mode.
- **Selected box outline color…**

## Autosave and recovery

Unsaved work is backed up every 5 minutes, on this computer only. If the app closes without saving (for example after a crash), opening the same project offers to restore it. Choosing **No** discards the backup; your saved project file is never changed by it.

## Keyboard shortcuts

| Keys | Action |
|---|---|
| Ctrl+N / Ctrl+O | New / Open project |
| Ctrl+S / Ctrl+Shift+S | Save / Save As |
| Ctrl+M | Merge project |
| Ctrl+I / Ctrl+Shift+I | Add images / Add folder |
| Ctrl+Alt+V / Ctrl+Shift+V | Add video / Add video folder |
| Ctrl+Shift+E | Export training dataset |
| Ctrl+E | Exit |
| Ctrl+Z / Ctrl+Y (Ctrl+Shift+Z) | Undo / Redo |
| Ctrl+F | Find (search box) |
| Ctrl+Shift+C / Ctrl+Shift+U | Edit categories / Edit users |
| Ctrl+U | Change user |
| Ctrl+R | Reference lines while drawing |
| Ctrl+0 | Reset image settings |
| F1 | This guide |
| B | Draw box (Esc cancels) |
| V | Verify / unverify the selected box |
| Shift+V | Image: Verified ↔ Review |
| C | Image: Review ↔ Unverified |
| Delete | Delete the selected box |
| Left / Right | Previous / next image, or frame when a video is selected |
| Shift+Left / Right | Back / forward 10 frames |
| Home / End | First / last frame |
| G | Type a frame number |
| N / Shift+N | Next / previous image not Verified, or frame with work in a video |
| Space + drag | Pan |
