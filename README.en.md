# VHS Effect Add-on · VHS Tape Effect Processor

English | [简体中文](README.md)

Applies a VHS videotape look to photos and videos. The program simulates chroma
bleed, time-base error, head-switching noise, tracking distortion, dropouts,
noise, scanlines and vignetting, and can overlay camcorder-style on-screen
displays (REC indicator, playback status, date and time). It processes an entire
directory tree recursively and reproduces the same folder structure under the
output directory. Both a graphical interface and a command-line interface are
provided. The only dependencies are NumPy, Pillow and OpenCV; video encoding is
handled by ffmpeg.

- Place the files to be processed in the `input` directory
- Results are written to the `output` directory
- The `input` directory is read-only: no original file is modified, moved or deleted
- Sample results are provided in the `example` directory: `before.jpg` is the
  original image and `after.jpg` is the processed result

---

## 1. Directory Rules

The tool detects subfolders inside `input` and creates folders with the same
names under `output`:

| Content in the input directory | Location of the result |
| --- | --- |
| `input/photo.jpg` (file at the root) | `output/photo.jpg` |
| `input/A/` (a whole folder) | Results in `output/A/` |
| `input/A/B/` (nested folders) | `output/A/B/`, structure mirrored completely |
| Files and folders mixed together | Each goes to its corresponding place |

```
input/                        output/
├─ photo.jpg         -->      ├─ photo.jpg
├─ A/                          ├─ A/
│  ├─ 01.jpg         -->      │  ├─ 01.jpg
│  └─ 02.mp4         -->      │  └─ 02.mp4
└─ A/B/                        └─ A/B/
   └─ 03.jpg         -->         └─ 03.jpg
```

- Output file names match the input names by default, which keeps input and
  output easy to compare. If a target file already exists, the suffix `_vhs`,
  `_vhs2` and so on is appended automatically, so nothing is overwritten.
- Add `--suffix` (or enable the corresponding option in the GUI) to always
  append the `_vhs` suffix.
- Non-media files in the input directory (for example `.txt`) are ignored and
  do not affect the mirrored structure.
- The output directory must not be located inside the input directory; the
  program rejects such a configuration to avoid scanning its own results.
- Any other directories can be used instead, without involving `input`/`output`:

```bat
python vhs_processor.py run -i <input dir> -o <output dir>
```

---

## 2. Usage

### 2.1 Graphical interface (recommended)

Run **`运行-图形界面.bat`**, select the directories, adjust the parameters and
click "开始处理" (Start).

The window is organised into five tabs:

| Tab | Contents |
| --- | --- |
| Effect strength | Preset selection, overall strength multiplier, and 19 sliders covering chroma bleed, noise, scanlines, jitter, softness, bloom, saturation and more |
| Output settings | Maximum image dimension and JPEG quality; video output height and quality parameter; encoder and speed preset; audio retention; processing scope |
| Date/time overlay | Start date and time with live validation, progression mode, and time-scale factor |
| Overlay text | Switches for the REC / PLAY / date-time elements, REC dot colour and blink rate, text size and opacity |
| Processing log | Per-file results, progress bar and status message |

- Every slider shows its current value together with a short explanation of the parameter.
- After changing the preset, click "按预设重置各项参数" to restore all sliders to that preset's values.
- "预览单张" processes only the first image in the input directory, which is useful for a quick check.
- Processing runs on a background thread, so the window stays responsive. Click "中止" to stop after the current file finishes.
- When processing ends, the folders containing the results are listed and you are asked whether to open the output directory.
- All settings are saved to `_gui_settings.json` and restored on the next launch.

### 2.2 Command-line batch processing

Run **`运行-命令行.bat`**. Photos and videos in `input` (including all
subfolders) are processed and written to the matching folders under `output`.

### 2.3 Command line with options

```bat
python vhs_processor.py                          :: process input/, write to output/
python vhs_processor.py --preset heavy           :: use the heavy preset
python vhs_processor.py --strength 1.5           :: strengthen the preset overall
python vhs_processor.py --time "2001-09-11 08:46:00"   :: set the on-screen date and time
python vhs_processor.py --encoder cpu            :: force CPU encoding
python vhs_processor.py --no-osd                 :: overlay no text at all
python vhs_processor.py presets                  :: list all presets
python vhs_processor.py show --preset heavy      :: print every effective parameter
```

Press **Ctrl+C** at any time to stop. Progress is not saved: files that were
already finished remain in the output directory, while the half-written file
currently being processed is deleted.

---

## 3. Command-line Options

| Option | Description |
| --- | --- |
| `-i, --input` | Input directory; defaults to `input` inside the tool folder |
| `-o, --output` | Output directory; defaults to `output` inside the tool folder |
| `--preset, -p` | Preset: `light` / `classic` / `heavy` / `worn` / `clean_tv`; default `classic` |
| `--strength, -s` | Overall strength multiplier: `1.0` keeps the preset, `0.5` halves it, `2.0` doubles it |
| `--set key=value` | Override a single parameter; may be repeated, e.g. `--set luma_noise=1.2` |
| `--time, -t` | Start date and time; many formats are recognised (see section 5) |
| `--time-offset` | Shift the start time by a number of seconds, e.g. `--time-offset -3600` |
| `--timestamp-mode` | `auto` (default: fixed for photos, advancing for video) / `fixed` / `advance` |
| `--timestamp-speed` | Time-scale factor; `2.0` makes time pass twice as fast in the video |
| `--timestamp-text` | Use a literal timestamp string and skip all time progression |
| `--rec-color` | REC dot colour, e.g. `#FF2E2E` or `255,46,46` |
| `--rec-blink` | REC dot blink rate in hertz; `0` keeps it permanently lit |
| `--no-osd` / `--osd` | Disable / force-enable all overlay text |
| `--no-timestamp` | Disable only the date and time in the lower-right corner |
| `--height` | Maximum video output height; default `1080` |
| `--crf` | Video quality parameter; lower values mean higher quality; default `18` |
| `--encode-preset` | Encoder speed preset; default `medium` |
| `--encoder, -e` | Encoder: `auto` / `cpu` / `gpu` / `nvenc` / `qsv` / `amf` |
| `--no-audio` | Discard the audio track |
| `--video-ext` | Force an output extension, e.g. `.mp4` |
| `--video-seconds` | Process only the first N seconds of each video, useful for quick previews |
| `--no-image` / `--no-video` | Process only videos / only images |
| `--suffix` | Append the `_vhs` suffix to output file names |
| `--quality` | JPEG output quality, 1-100; default `95` |
| `--seed` | Random seed; an identical seed reproduces the result exactly |
| `--traceback` | Print the full traceback when an error occurs |

Single-file processing:

```bat
python vhs_processor.py image "input/photo.jpg" -o "output/photo.jpg"
python vhs_processor.py video "input/movie.mp4" -o "output/movie.mp4"
```

---

## 4. Presets

| Preset | Result |
| --- | --- |
| `light` | Subtle. Very little quality loss, just a hint of tape texture |
| `classic` | Classic. The default; balanced across all aspects |
| `heavy` | Heavy. Clearly visible noise, jitter and colour bleeding |
| `worn` | Worn. Simulates a tape that has been re-recorded many times |
| `clean_tv` | Television. A clean tape played back on an old CRT set |

Parameters are applied in this order of increasing priority:
preset → configuration file → command-line options.
The configuration file `vhs_config.json` can set a default preset, an overall
strength and arbitrary parameter overrides:

```jsonc
{
  "preset": "classic",
  "strength": 1.2,
  "params": {
    "luma_noise": 1.0,      // stronger grain
    "jitter": 0.9,          // more pronounced jitter
    "osd_rec_blink": 2.0    // faster REC dot blinking
  }
}
```

---

## 5. Date and Time Overlay

The program recognises the time format you provide; there is no fixed syntax to
memorise.

- **Photos**: the start time is printed directly and stays fixed.
- **Video**: time advances by one second at a time starting from the start time
  (08:46:01 at the first second, 08:46:02 at the second, and so on).
- The timestamp **changes once per second**, matching the behaviour of real
  camcorder displays rather than changing on every frame.

### Accepted formats

| Input | Parsed result |
| --- | --- |
| `1994-06-11 12:00:00` | Full date and time |
| `1994/6/11 12:00` | Slash separators, seconds omitted |
| `1994-06-11`, `19940611` | Date only; time defaults to 12:00:00 |
| `12:00:00`, `12:00`, `120000` | Time only; date defaults to 1994-06-11 |
| `1994年6月11日 12点00分00秒` | Chinese notation |
| `1994年6月11日12时30分` | Chinese notation, seconds omitted |
| `19940611120000` | 14-digit numeric form |
| `now`, `现在`, `今天` | Current system time |

Any part that is not supplied keeps its default value (`1994-06-11 12:00:00`).
An unrecognised value produces an explicit error listing every accepted format
and exits with code 2; the program never continues with a wrong time.

### Common adjustments

```bat
:: shift forward or backward (in seconds)
python vhs_processor.py --time "12:00:00" --time-offset -3600
python vhs_processor.py --time "2001-09-11" --time-offset 28800

:: make time pass faster
python vhs_processor.py --time "12:00:00" --timestamp-speed 4.0

:: keep a single fixed time even for video
python vhs_processor.py --timestamp-mode fixed --time "12:00:00"

:: specify the text literally, with no time progression
python vhs_processor.py --timestamp-text "1994-06-11  12:00:00"
```

### On-screen elements

| Element | Position | Notes |
| --- | --- | --- |
| REC indicator | Upper left | Red dot with label. Stays lit on photos; blinks at the configured rate in video |
| Playback status | Upper right | Play triangle with `PLAY` and `SP` labels |
| Date and time | Lower right | Progresses according to the rules above |

The play triangle is drawn as a polygon and does not depend on any font
containing that glyph, so it renders correctly on every system.
The dot colour and blink rate are configurable, as are text size, opacity,
jitter amplitude and chroma ghosting. Excessive ghosting tints red markers
towards purple, so the default value is balanced for that trade-off.

---

## 6. Video and Encoding

- The audio track is preserved: it is extracted to AAC and merged back into the
  processed video.
- Output is always H.264 with `yuv420p`, compatible with common players and
  editing software.
- Per-frame processing is CPU-intensive, so **lowering the output resolution is
  by far the most effective way to speed things up**.
- Processing can be stopped at any time; the incomplete file is removed.

### About hardware encoding

Encoding can be delegated to a hardware encoder (NVIDIA NVENC, Intel Quick
Sync, AMD AMF) through `--encoder`, which defaults to `auto` (prefer hardware,
fall back to CPU when unavailable).

**Note that hardware encoding has almost no effect on total runtime.** The time
is dominated by per-frame pixel operations, while encoding accounts for only a
small part of it. At 1920×1080 for example, a hardware encoder can encode
hundreds of frames per second on its own, whereas the full effect pipeline runs
at roughly one frame per second. Moving encoding from the CPU to a dedicated
encoder therefore changes end-to-end runtime by only a few percent.

The real benefit of hardware encoding is **lower CPU usage and power draw during
the encoding stage**, not a shorter total runtime. To speed things up
substantially, reduce the output resolution (`--height`).

---

## 7. Frequently Asked Questions

**Processing is slow.**
Per-frame pixel operations are inherently expensive, so this is expected. You
can lower the video output resolution with `--height 720`, try a fragment first
with `--video-seconds 5`, or reduce `luma_noise` and `chroma_noise`. Image
processing is far faster than video: a single 5120×2880 image takes a few
seconds.

**Switching to hardware encoding did not make it faster.**
That is expected. The bottleneck is pixel processing, not encoding; see
section 6.

**The REC dot is not red.**
Use `--rec-color` to set the colour explicitly, for example `#FF2E2E`. Also note
that a large "chroma ghosting" value (`--set osd_ghost=...`) tints red markers
towards purple.

**The REC dot does not blink in my video.**
Blinking is only enabled during video processing; photos always show a steady
dot, since blinking is meaningless within a single frame. The rate is controlled
by `--rec-blink`, where `0` means permanently lit.

**The play triangle in the upper right renders as a box.**
That icon is drawn by the program and does not depend on system fonts, so this
should not happen. If it does, please make sure you are running the current
version.

**The output file name differs from the input, or has an extra suffix.**
The input name is kept by default. When a file with that name already exists,
the program appends `_vhs` and similar suffixes to avoid overwriting. Use
`--suffix` if you want the suffix to be applied consistently.

**Can I run it repeatedly, and will it overwrite earlier results?**
Yes, and no. The program never overwrites an existing output file; it renames
the new file instead.

**Are the original files affected?**
No. The program only reads from the input directory and only writes to the
output directory. There is no code that deletes or moves original files.

**The output video has no sound.**
Audio processing failed — the source track uses an unusual codec, or ffmpeg is
unavailable. The program degrades to a silent output rather than aborting. Run
`python check_env.py` to inspect the video pipeline status.

**Can it handle images with an alpha channel?**
Transparent areas are composited onto a black background before processing, and
the output is an opaque JPEG.

**How do I get exactly reproducible results?**
Fix `--seed` (it already has a fixed default). Time-dependent distortion in
video is driven by the frame index, so the same video always produces the same
result.

---

## 8. Self-test

```bat
python selftest.py
python selftest.py --keep          :: keep the generated artifacts for inspection
python selftest.py --source <asset dir>
```

The self-test is organised into six parts:

1. **Environment** — Python version, dependency libraries, ffmpeg and hardware encoder availability
2. **Time parsing and progression** — parsing of 10 time formats, rejection of invalid input,
   fixed photo timestamps, second-by-second advancement in video, REC dot steady/blinking, dot colour
3. **Parameter assembly** — single-parameter overrides, overall strength scaling, differences between
   presets, error reporting for invalid parameters
4. **Folder mirroring** — recursive scanning, skipping non-media files, output structure with
   non-ASCII names and multiple nesting levels
5. **Image processing** — full processing of every image in the asset directory
6. **Video processing** — processing the first seconds of every video, including audio merging

The self-test never modifies `input` or `output`; all artifacts are written to
`_selftest` and removed at the end unless `--keep` is given.

---

## 9. Files

| File / directory | Purpose |
| --- | --- |
| `input/` | Photos and videos to process (read-only; never modified) |
| `output/` | Processing results; subfolders mirror the input names |
| `example/` | Before-and-after sample: `before.jpg` is the original, `after.jpg` the result |
| `vhs_processor.py` | Command-line entry point and argument parsing |
| `gui.py` | Graphical interface |
| `selftest.py` | Self-test script |
| `check_env.py` | Runtime environment check |
| `setup_deps.py` | Install dependencies into the bundled `libs/` directory |
| `vhs_lib/vhs_effects.py` | Core image processing: effect pipeline, parameter container, per-frame context |
| `vhs_lib/vhs_video.py` | Video I/O: encoding and decoding, audio handling, encoder selection |
| `vhs_lib/vhs_task.py` | Directory traversal, output path mapping and per-file processing |
| `requirements.txt` | Dependency list |
| `vhs_config.json` | Optional default preset and parameter overrides |
| `运行-图形界面.bat` | Launch the graphical interface |
| `运行-命令行.bat` | Process `input` into `output` |
| `运行-自检.bat` | Run the self-test |
| `安装依赖.bat` | Install dependencies |
| `libs/` | Where dependencies are installed; delete and reinstall at any time |
| `_wheels/` | Local package cache for offline installation; delete at any time |
| `_selftest/` | Self-test artifacts; delete at any time |
| `_preview/` | GUI preview artifacts; delete at any time |
| `_gui_settings.json` | Saved GUI options; delete at any time |
| `Material/` | Sample assets used by the self-test; delete at any time |

Requirements: Python 3.8 or newer, plus the two mandatory libraries NumPy and
Pillow (run `安装依赖.bat`, or execute `python setup_deps.py`).
OpenCV and imageio-ffmpeg are optional: the former considerably speeds up
blurring and resampling, the latter provides video support.

Dependencies are installed into the `libs/` folder inside the tool directory
rather than into the system Python installation, so the whole folder can be
moved as a unit and other Python projects are unaffected.
To install into the system environment instead, run
`python -m pip install -r requirements.txt`.

---

## 10. Processing Pipeline

The image pipeline applies its stages in the following order, which itself
affects the final look:

1. **Chroma degradation** — convert to YCbCr, then apply anisotropic low-pass
   filtering, downsampling and red/blue displacement to the chroma channels,
   emulating the horizontal smearing and colour fringing caused by the limited
   chroma bandwidth of VHS
2. **Time-base error** — whole-frame jitter, per-line jitter and vertical roll
3. **Optical stage** — softness, highlight bloom, edge enhancement, contrast and saturation
4. **Head switching** — a noise band at the bottom of the frame with horizontal tearing and luma displacement
5. **Tracking distortion** — a slowly upward-moving distorted region
6. **Dropouts** — randomly placed bright or dark lines
7. **Noise** — fine grain, low-frequency clumps, chroma noise and dirty edges
8. **Scanlines and vignetting**
9. **On-screen text** — REC, playback status and date/time
10. **Soft clipping** — emulates composite-video overshoot, then clamps to the legal range

All arithmetic uses `float32` with a `[0, 1]` range, which avoids the banding
that repeated uint8 truncation would introduce.

### Performance

Measured on a single CPU core (with the `classic` preset and OpenCV installed):

| Resolution | Throughput |
| --- | --- |
| 640×360 | about 7 fps |
| 854×480 | about 4.8 fps |
| 1280×720 | about 1.9 fps |
| 1920×1080 | about 1.2 fps |

Optimisations already in place: noise textures are pooled and generated at
quarter resolution; bloom is computed at half resolution; blur kernels,
vignette and OSD glyphs are cached; overlay text blends only the small
rectangles its glyphs occupy instead of touching the whole frame; colour
adjustments are performed in place with a reused luma buffer; and per-line
displacement is applied with a single resampling pass rather than a per-line
loop.

---

## 11. Using as a Library

```python
from vhs_lib import resolve_preset, parse_start_time, VHSContext

params = resolve_preset("heavy")
ctx = VHSContext(
    params,
    (1080, 1920),                            # (height, width)
    fps=25.0,
    start_time=parse_start_time("2001-09-11 08:46:00"),
    advancing=True,                          # True for video; False keeps a fixed time
)
out_frame = ctx.process(frame, frame_index=0)   # BGR uint8 in, BGR uint8 out
```

`VHSContext` binds one parameter set to one resolution. Frames at that
resolution can be fed repeatedly, and static layers are computed only once.

For batch work, reuse `vhs_lib.vhs_task`:

```python
from vhs_lib.vhs_task import scan_tree, mirror_out_path, do_image

images, videos, skipped = scan_tree("input")
for src in images:
    dst = mirror_out_path(src, "input", "output")
    do_image(src, dst, params, quality=95)
```
