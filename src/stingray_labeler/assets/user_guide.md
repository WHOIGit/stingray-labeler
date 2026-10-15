# Stingray Labeler — User Guide

Stingray Labeler draws and reviews bounding boxes on images and on frames of uncompressed AVI video, and saves them as a standard COCO JSON project.

## Key ideas

**Project.** A project is an *image folder* plus a *project JSON*. The JSON holds image names, boxes, categories and users. Saving copies each saved image into the project folder; video frames are written there as PNG.

**Image state.** Every image or frame is in exactly one state:

| State | Meaning | Saved? |
|---|---|---|
| **Unverified** | Not reviewed. | When it holds boxes (e.g. from an opened file), a favorite mark or earlier saved work; an empty one is left out (it can always be added again). |
| **Review** | Worth keeping in the dataset; its boxes are being checked. | Yes |
| **Verified** | Every box is verified and the image has been inspected as a whole. | Yes |

The first edit of an Unverified image (drawing, moving, resizing, deleting or verifying a box, or ticking Background) sets it to **Review** automatically. An image is never set to Verified automatically; you set it after inspecting it. A Verified image goes back to Review as soon as it breaks the rule below: a new or restored box, an unverified box, or its last box deleted (and not Background).

**Box state.** Each box is *verified* or *unverified* on its own. An image can be Verified only when every one of its boxes is verified, or it has no boxes and is marked Background. When a project opens or a merge brings one in, each Verified image with an unverified box is set to Review and marked `*`; on opening, the status line says how many. Saving records the change in the image's state history with no user, since no one decided it. Restoring a data version applies the same rule, as part of your restore.

**Users.** Every box records the user who last changed it. Choose the active user when a project opens; change it from **Edit → Change User…**. The active user's name is shown in the top-right corner.

**Names link everything.** Images are matched by file name. A frame of video `X.avi` is named `X_<frame>.png`, with frames counted from 0 and no zero-padding (e.g. `X_0.png`, `X_152.png`). A COCO file whose images follow this naming opens straight from the video.

## Getting started

1. **File → New Project…** (Ctrl+N): choose the folder of images. Choose or type your user name.
2. Or **File → Open Project…** (Ctrl+O): choose the project's image folder, then its JSON. Images without an image state (any plain COCO file, e.g. model output) open as **Unverified**; to set many at once, use **Data → Set State of Listed Images**.
3. Add more sources at any time from the **File** menu: **Add Images…**, **Add Image Folder…**, **Add Video…**, **Add Video Folder…**.

16-bit and floating-point images (e.g. scientific TIFFs) are shown stretched from their own lowest to highest value; the file itself is never changed.

### Where images are read from

When a project opens, images are read from the project folder. When you add a folder or video, images with the same name **now open from the source you just added** (their boxes stay attached). The most recently added source always wins. Nothing is loaded until you click an image.

## The window

**Left panel (top to bottom)**

- **Videos** — appears once videos are added, with its own search box.
- **Images** — every image or frame with work, plus scanned images; its search box filters by name (Ctrl+F). Tick **Unsaved only**, beside the title, to list just the images marked `*`.
- **Filters** — **Clear** (beside the title) resets every filter, including Unsaved only and those in the Data menu; it is greyed out when none is set. User, Categories (search box; All / None apply to the categories shown), Image state (All / Verified / Unverified / Review), Box verification (All / Verified / Unverified).

**Data → Filter** holds the less common filters:

- **Overlap IoU** — Off, or a comparison and value: lists images where at least one pair of overlapping boxes meets it.
- **User: Include Earlier Versions** — with a user chosen, also lists images where that user made an earlier, saved version of a box.
- **Clear Filters** — same as the panel's **Clear**.
- **Counts** — one line that wraps to the panel's width; hover for the full breakdown.

**Centre** — the image, rulers, and below it the info line: position in the list, size in pixels, file size (raw size for a video frame) and boxes (how many are verified, ✓). With a video selected, the frame slider and frame number box appear on the right of that line.

**Right panel** — **State** (Unverified / Review / Verified), **Draw box**, **Background**, **Box verified** for the selected box, and the list of boxes in the image, with a search box that shows only boxes whose current category matches. Deleted boxes stay at the end of the list in grey: select one to outline where it was, right-click it to restore it.

## Working with images

- Click an image in the list, or use **A / D** (or Left / Right, or Previous / Next image) to move through the visible list.
- **E / Q** jump to the next / previous image that is not Verified.
- **S / W** select the next / previous box in the image (in box list order, skipping boxes hidden by the box search) and scroll it into view.
- Up / Down move through whichever list has the keyboard focus.
- The **Navigation** menu lists these moves.
- **Right-click an image in the list → Advanced ▸ Delete Image Permanently…** removes an image added by mistake (e.g. a damaged frame), after asking: its record, its boxes with all their earlier versions, and its state history leave the project at the next save, and its file in the project folder goes to the Recycle Bin. Files in added folders and videos are never touched. It cannot be undone, and a copy of the project that still has the image brings it back if merged later.

## Working with videos

Only **uncompressed AVI** video can be read; frames are read directly, one at a time, with no extra software.

1. **File → Add Video…** or **Add Video Folder…** lists the videos (only their headers are read). A frame is saved as `<video name>_<frame number>.png`; a project image with exactly that name is linked to its frame, so one frame is always one image.
2. Click a video. The frame controls appear under the image, and the arrow keys now step frames:

| Key / control | Action |
|---|---|
| **A / D**, **Left / Right**, ◀ ▶ buttons | previous / next frame (hold to keep stepping) |
| **Shift+A / D** | back / forward 10 frames |
| **Home / End** | first / last frame |
| Slider | drag to scrub |
| **G**, then type a number and **Enter** | jump to a frame (**Esc** cancels) |
| **E / Q** | next / previous frame of this video that has work |
| Previous / Next video buttons | move between videos |

- A frame you only look at is not added to the project. It joins the image list once it has work.
- Frames already in the project JSON open with their boxes when you step onto them.
- Click any image in the image list to go back to stepping through images.

## Drawing and editing boxes

- **Draw box** or **B**, then drag on the image. **Esc** cancels.
- When you release, choose a category — or **type a new name** to create it. The last category used is offered first. Tick **Repeat last category** (beside Draw box) to give new boxes that category without being asked.
- Drag a box to move it; drag any of its four corner handles to resize it.
- **R**, double-clicking the box in the list, or right-click → **Change Category…** changes the selected box's category; type to search, or type a new name to add it.
- **V** toggles the selected box between verified and unverified.
- **X** or **Delete** (or right-click the box → **Delete**) removes the selected box; it stays in the list in grey and can be restored.
- **Background** marks an image with no objects; it can be ticked only when the image has no boxes.
- **Ctrl+Z / Ctrl+Y** undo and redo edits on the image shown; moving to another image ends the step-by-step undo. Back on an image with unsaved box edits, **Ctrl+Z** puts its boxes back as last saved in one step (**Ctrl+Y** brings the edits back). Undoing back to the saved image clears its `*`. For older changes use Box History or data versions.
- **Space + drag** or dragging with the middle mouse button pans; the mouse wheel zooms. A middle-button click (without dragging) works like **F**: automatic black/white levels, click again for the defaults.

## Favorites

**T** marks the image shown as a favorite, e.g. a good example for a report or a talk; **T** again unmarks it. A favorite shows **★** before its name in the image list and after its position below the image. It is saved with the image (it is not part of the box history or the training export) and kept by a merge if either copy has it. **Data → Filter → Favorites Only** lists only favorites, and **File → Export → Favorites…** copies the favorite images, whatever their state, to a folder with `favorites.json`, a plain COCO file of their boxes as they are now.

## Setting the image state

Click **Unverified**, **Review** or **Verified**, or use the keys:

- **Shift+V** — Verified ↔ Review.
- **C** — Review ↔ Unverified (on a Verified image, C sets Review).

Setting an image to Unverified keeps it in the project when it holds boxes, a favorite mark or earlier saved work; an empty image is left out at the next save.

**Data → Set State of Listed Images ▸ Verified / Review / Unverified** sets every image the list shows, with its filters applied, to one state, after asking. The same rules apply as for one image: an image with an unverified box, or with no boxes and no background mark, cannot be Verified and stays as it is (the confirmation says how many), and verifying removes the model score from its verified boxes (the scored version stays in box history). For example, after opening manual annotations from another pipeline, whose images come in Unverified: list them, then set them to Verified.

## Saving

**File → Save** (Ctrl+S) saves every **Verified** and **Review** image with all of its boxes, verified or not. An Unverified image is saved too, as Unverified and never exported for training, when it holds anything: boxes (e.g. model output from an opened file), a favorite mark, box history or a saved state. Only an empty Unverified image, e.g. one only scanned from a folder, is left out. A saved image must be in the project folder (or come from an added folder or video): if one is missing, Save says which and saves nothing.

- Images not yet in the project folder are copied there; video frames are written as PNG.
- **Nothing is ever deleted from the project folder.** If a saved image already exists there, you are asked per file: **Skip** (keep the folder's file), **Overwrite** (replace it from the current source), **Skip all** or **Overwrite all**. You are asked about each file once per session. After **Skip**, the image opens from the folder's file, since that is the one the project names.
- **Images are identified by file name:** different images must never share a name. A project file that lists two images with the same name is refused when opened.
- **A save is all or nothing.** Files being overwritten are swapped in only at the end; if any step fails (for example a file locked by a viewer or OneDrive), every original is put back and the files this save wrote are removed.
- If the project file was changed by someone else, **Save As** suggests a new name and will not write over that file; use **Save → Merge…** to combine instead.
- **File → Save As…** (Ctrl+Shift+S) saves to a different JSON file.

### Box history

Each save keeps a record of how boxes changed, so earlier work is never lost:

- Every box has a hidden **box id**, a **version** number and the UTC **time** of that version. The box id only ties a box's versions together and is never shown. It follows one fixed rule, so any tool can reproduce it: it is computed from the image's file name, when the box was first created (its first save) and where it was then, so the same box gets the same id on any platform. It never changes when the box is edited, deleted or restored, and is never reused. Boxes from a plain COCO file or with an id in another format (such as an old `2-5`) get theirs the same way when the file is opened.
- Changing a box, yours or someone else's, keeps its saved version in the project's history and makes the change the next version at save. A version holds the box, its category, verification and model score: when verifying an image removes the score from its verified boxes, that is a new version by the user who verified it, and the model's version, score included, stays in the history (Box History shows it).
- Deleting a box keeps its last version in the history, with a record of who deleted it (the user active when it was deleted, even if someone else saves) and when.
- Undo and Redo count as changes by the user who presses them: a box they bring back to a state that differs from the saved one is credited to that user at the next save.
- Edits between two saves count once, so undo and repeated adjustments add nothing.
- A plain COCO file starts its history at the first save.

A line above the box list names the fields of each row, e.g. `Class · ✓ · User · Score · Ver · IoU`, and each row follows it, e.g. `car · ✓ · alex · – · 3 · 0.12`. **✓** marks a verified box; **User** is who last changed the box (**model** for a box with a model score that no one has changed); **Score** is the model score, **Ver** the number of saved versions and **IoU** the highest overlap with another box. A field appears only when a box in the image has it, and `–` stands for a value a box does not have. Deleted rows end with `deleted`. A `*` marks changes not saved yet: at the end of a box row (on grey rows, `deleted*`), after an image's name in the image list, and in the window title. Hover a marked row to see "Unsaved changes". Once saved, the version count shows the box was changed. Right-click a box (on the image or in the list):

- **Add Note… / Edit Note…** — a short note on the box. The box list shows its start (hover for all of it). Changing a note alone is not a new version of the box. Notes are not part of the training export.
- **Box History…** — every saved version, newest first. Selecting one outlines it in magenta on the image; **Restore this version** applies it as your edit (Ctrl+Z undoes it). It also works on grey deleted rows, including ones deleted since the last save; restoring a version brings the box back as that version.
- **Deleted Boxes…** (right-click empty space on the image; shown only when there are some) — boxes deleted on this image, including ones deleted since the last save (marked `deleted*`); select one to see it and **Restore** to bring it back.
- **Advanced ▸ Delete Permanently…** — kept in its own submenu at the bottom so it is not clicked by accident. It removes the box and all its earlier versions from the project for good (asks first; no grey row, no restore, no undo). Use it for boxes that should never have existed; ordinary **Delete** keeps the box's history.

**File → Export → Training Dataset…** leaves the history out.

**Image states have a history too.** When a save changes an image's state or Background mark, the image records when and by whom, and its previous state is kept. A merge uses it like box history: if only one copy changed an image's state since the state both copies last shared, that copy's state is taken; if both changed it, the image is set to Review.

**Data → Data Versions…** starts a new version, with an optional note (it asks you to save first). A version holds your work from when it is added until the next version is added: the newest version is the current project, and the title bar shows its name. You can edit the note later below the list, and **Rename…** changes the name; adding, renaming, deleting and notes apply when you click **Save** (**Cancel** drops them). Click an older version to show the boxes as they were at its end, when the next version was added (it asks first); click the current version at the top to go back. Use it before changing annotation strategy, or for each training set you build: export and revert work from data versions only (not dates, which depend on each PC's clock). Opening data that has no data version creates one, named after the opened file, that marks the data as opened; it is kept when you save.

**Data → Show Data Version…** (or clicking a version in Data Versions) shows every image's boxes as they were at the end of a data version, as last saved. It asks nothing and changes nothing: the project's boxes, unsaved edits included, stay as they are, editing is off, and the title bar shows the version as read-only. **Export → Training Dataset…** offers the version shown. Choose the current (newest) version to go back and edit again. Categories merged away since are shown under their old name, marked "(merged)".

**Data → Restore This Version…** (or the button in Data Versions), while an older version is shown, puts every image's boxes and state back as they were then, as your edits, and starts a new data version (e.g. *Restored v1*; it asks for the name, and to save first if needed). Later versions stay as they are and can still be shown or exported; **Changed in Version** on the new version lists what the restore put back. To bring back a single box instead, use its Box History → Restore this version.

**Data → Compare Data Versions…** reports what changed between two data versions (or a version and the current project): boxes added, deleted and changed, by category and by user, image states changed, and how many images. **List these images** filters the image list to them; **Add to the note** writes the summary line into the later version's note.

**File → Export → Activity (CSV)…** writes, for a date range (UTC days), one row per day and user: boxes drawn, edited, verified, deleted and restored, and images set Verified, Review or Unverified — counted from the saved history.

**Data → Filter → Changed in Version…** lists only images changed during a data version, from when it was added until the next one was, by anyone or by one user. For the current version, unsaved changes count too.

**Settling a merge conflict.** A merge sets an image to **Review** when a box or its image state changed in both copies. Open the box's **Box History** to see both copies' versions in time order, restore the one that makes sense (or edit the box), then set the image state.

**Data → Compact History…** shrinks the history: where a user saved several versions of their own box in a row, only the newest is kept. Versions by other users, deletion records and the version before a deletion are kept; version numbers keep their gaps. It works on saved work only (it asks you to save first), shows how many versions, boxes and images it affects, and cannot be undone. Use it only on the master project after every copy has been merged in: a copy merged later may share only the removed versions, and its boxes would then come in as separate objects (flagged Review).

## Working together: merging

Two people can work on copies of the same project and merge their work.

Only saved work is merged.

**File → Merge Project…** (Ctrl+M) merges another saved copy into your saved project. If you have unsaved changes, it asks you to save first. The merged project is then saved at once and becomes a new session: nothing from the merge shows as an unsaved change. (If the save cannot finish, e.g. images from the other copy are elsewhere, add their folder and save.)

**At Save.** If someone else saved the project after you opened it, Save offers **Merge…**: your work is versioned as in any save, combined with the file on disk on a separate copy, and written. Your session changes only once the merged file is written; if the save fails or is cancelled, nothing changes.

Merge rules — every change is combined, nothing is overwritten:

- **Categories and users first.** Names in both copies (ignoring case) match automatically. A name only one copy has is new there and is added, unless the other copy shows it was renamed or merged since the copies parted (box versions both copies hold name it differently, or the other copy merged the category into another). Only then a table shows both lists side by side with how many boxes each name has, says what the other copy did, and asks: **Merge into** (as the other copy did, or another name), **Keep**, or — for a name of the other copy — **Remove** (a category: its boxes are not taken) or **Leave boxes unassigned** (a user). New names that look like a name of the other copy (e.g. *bicycles* and *bicycle*) are pointed out there. Your decisions are remembered, so merging with that copy again does not ask again. Boxes are combined only after this.
- **Boxes are matched by their hidden box id**, which never changes and is never reused, so the same id in both copies is the same object whatever its category or user is called. Boxes drawn separately in each copy are two boxes (the Overlap IoU filter finds them).
- Every version of every box from both copies is kept, ordered by time (time is used only for ordering). If only one copy changed a box, that copy's version is taken — so a box an earlier merge kept stays kept.
- A conflict keeps both sides and sets the image to **Review**: a box changed (or deleted) in both copies, shown as its newest kept version (kept if either copy still has it), or an image state or Background mark changed in both. Different boxes changed in each copy are no conflict.
- A box note is not part of a version: if the copies' notes on the same version differ, both are kept, joined by a comma.
- **Every merge starts a new data version.** The project you have open is the main one: it keeps its own data versions, and the Merge dialog asks for the name of the version the merged result starts (e.g. *Merged annotator_A*, with an optional note). Data versions made only in the other copy are not taken. Each earlier version still holds exactly its own work: the other copy's work is part of the new version, even if it was made earlier, so **Changed in Version** on that version lists what the merge brought in.
- Images unverified in your copy that hold boxes are combined too, not overwritten, and listed in the summary.
- A category merged away keeps its name for the box history (shown as "… (merged)") and its id is never given to another category; creating a category with that name again brings it back.

After a merge, use the **Review** filter to see what needs a look. Undo history is cleared after a merge.

## Categories and users

**Edit → Categories…** (Ctrl+Shift+C): **Add**, **Edit** (rename; using an existing name offers to merge into it), **Delete** and **Color**. Changes are listed in the dialog and applied together when you click **Save**; **Cancel** drops them. A rename shows the new name on every version. Merging a category into another (or **Delete** on a category boxes use, which moves them) changes the boxes as your edit: each becomes a new version at save, and earlier versions keep the old category, which stays known as "… (merged)" so data versions export exactly as they were. Only a category nothing uses (no box, saved version or undo step) can be deleted. Renaming, merging or moving first shows how many boxes and images it changes and asks to confirm.

**Edit → Users…** (Ctrl+Shift+U): **Add**, **Edit** (rename or merge) and **Delete**, applied together when you click **Save** (**Cancel** drops them). Merging users (the same person under two names) credits all of the merged user's versions and image states, saved ones too, to the user kept. Only a user credited on nothing (no box, saved version, image state or undo step) can be deleted; otherwise merge them into another user. The active user cannot be deleted. Renaming or merging first shows how many boxes and images it changes and asks to confirm. A new user or category never reuses the id of one that was deleted or merged.

**Edit → Import Categories…** copies category names from another project or COCO file.

## Summary

**Data → Summary…** shows the project in numbers, for all images or only those the current filters show (unsaved changes included): images by state, favorites and unsaved; boxes verified, unverified, with a model score and with a note; and the data versions.

- **Image states** — one row each for Verified, Review, Unverified and All: images, Background, with boxes, boxes, verified and unverified boxes, and images with an unverified box (always 0 for Verified).
- **Categories** — every category, rarest first: its boxes (verified / unverified), the images it appears in, and how many of those are Verified, Review and Unverified.
- **Users** — boxes by the user who last changed them.

Click a column to sort; **Copy** puts it all on the clipboard for a spreadsheet.

## Exporting

- **File → Export → Training Dataset…** (Ctrl+Shift+E) — first choose **as of**: the current project or a data version; a data version exports the dataset exactly as it was saved then, without changing the project. Verified images whose boxes are all verified (and verified Background images) to a folder and a plain COCO JSON: standard COCO box fields only, without users, states, verification, history or segmentation. A Verified image that still has unverified boxes is left out (you are told which), because exporting only its verified boxes would teach the model the other objects are background. Exporting again into the same folder asks to replace the images already there. Video frames are written from the video.
- **File → Export → Image…** — the displayed image with its boxes drawn on it.
- **File → Export → Crops…** — each box in the displayed image as its own image.

## View

- **Image** — brightness, contrast, gamma, black and white points (all sliders), invert, and auto levels for the current image. **Reset Image** (Shift+F).
- **Scale** — rulers, scale bar, pixel resolution, and **Calibrate from line…**: draw across a known length and enter it. The ruler is off until you tick it. Pixel resolution starts as *Not calibrated*: the ruler then counts image pixels and no scale bar can be drawn; once a resolution is set (typed in or calibrated), the ruler shows one unit chosen from the image's size (µm, mm, cm or m) that stays the same at any zoom, and the scale bar becomes available. The resolution applies to this session.
- **Reference Lines** (Ctrl+R) — horizontal and vertical lines through the cursor in draw mode.
- **Selection Color…** — outline color of the selected box.

## Autosave and recovery

Unsaved work is backed up every 5 minutes, on this computer only. If the app closes without saving (for example after a crash), opening the same project offers to restore it. Choosing **No** discards the backup; your saved project file is never changed by it.

## Keyboard shortcuts

Made for the left hand on the keyboard and the right on the mouse. The single keys act while you work in the window, not while typing in a box.

**Edit → Keyboard Shortcuts…** changes the single keys in the first table below (e.g. for a keyboard that is not QWERTY); **Restore Defaults** brings these back. They are kept on this computer, for every project. Arrows, Home / End, Delete, Esc, Enter, Space and the Ctrl shortcuts stay as they are. The tables below list the default keys.

**Mouse** — left button: draw, select, drag; middle button: drag to pan, click for auto levels; wheel: zoom; right button: box menu.

| Key | Action |
|---|---|
| **A / D** (or Left / Right) | Previous / next image, or frame when a video is selected |
| **Shift+A / D** | Back / forward 10 frames (video) |
| **Q / E** | Previous / next image not Verified, or frame with work (video) |
| **W / S** | Previous / next box in the image |
| **B** | Draw a box (Esc cancels) |
| **V** | Verify / unverify the selected box |
| **R** | Change the selected box's category |
| **X** (or Delete) | Delete the selected box |
| **Shift+V** | Image: Verified ↔ Review |
| **C** | Image: Review ↔ Unverified |
| **T** | Mark / unmark the image as a favorite (★) |
| **F** (or middle click) | Automatic black/white levels for this image; again for the defaults |
| **Shift+F** | Reset image |
| **G** | Type a frame number (video) |
| Home / End | First / last frame (video) |
| Space + drag | Pan |
| Ctrl+Z / Ctrl+Y | Undo / Redo (the image shown) |

| Keys | Action |
|---|---|
| Ctrl+S / Ctrl+Shift+S | Save / Save As |
| Ctrl+N / Ctrl+O | New / Open project |
| Ctrl+M | Merge project |
| Ctrl+I / Ctrl+Shift+I | Add images / Add folder |
| Ctrl+Alt+V / Ctrl+Shift+V | Add video / Add video folder |
| Ctrl+Shift+E | Export training dataset |
| Ctrl+F | Find (search box) |
| Ctrl+U | Change user |
| Ctrl+Shift+C / Ctrl+Shift+U | Edit categories / Edit users |
| Ctrl+R | Reference lines |
| F1 | This guide |
