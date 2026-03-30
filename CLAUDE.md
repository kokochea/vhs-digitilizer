# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A Flask web application for capturing and digitizing VHS tapes. It reads raw video from a V4L2 capture device (`/dev/video0`), analyzes frames to detect actual content vs. blank/static, and uses FFmpeg to encode segments to MP4. A real-time MJPEG preview and video library are served via a single-page web UI.

## Running the App

```bash
python3 app.py
```

Serves on `http://0.0.0.0:5000`. No build step needed.

## Dependencies

No requirements.txt — dependencies are expected system-wide:
- `flask`, `numpy` (Python packages)
- `ffmpeg` (system binary, must be in PATH)
- Hardware: V4L2 capture device at `/dev/video0`, ALSA audio at `hw:0`
- Storage mount: `/mnt/vhs-disk/vhs-captures/` (output directory)

## Architecture

Everything lives in two files:

- **`app.py`** — Flask app + all recording logic (271 lines)
- **`templates/index.html`** — Single-page UI with vanilla JS/CSS (167 lines)

### Recording Pipeline (app.py)

1. `recorder_thread()` spawns an FFmpeg subprocess that outputs raw `YUYV422` frames to stdout.
2. Each frame is passed to `is_blank_frame()` which extracts the Y (brightness) channel via NumPy and checks against `BLACK_THRESHOLD` (15) and `STATIC_VARIANCE` (1800).
3. When non-blank content is detected, a second FFmpeg subprocess (`encoding_process`) is started to encode an MP4 segment.
4. After `BLANK_SECONDS` (5) of consecutive blank frames, the encoding process is terminated and the segment is finalized.
5. A shared `state` dict is updated throughout and read by `/api/status`.

### Preview Stream

`/preview_feed` is an MJPEG stream. Every 15th frame is scaled to 640×360 and JPEG-encoded via a short FFmpeg subprocess (`frame_to_jpeg()`), then sent as a multipart HTTP response.

### Key Configuration Constants (top of app.py, lines 14–26)

```python
VIDEO_DEVICE = "/dev/video0"
AUDIO_DEVICE = "hw:0"
OUTPUT_DIR = "/mnt/vhs-disk/vhs-captures"
BLACK_THRESHOLD = 15
STATIC_VARIANCE = 1800
BLANK_SECONDS = 5
CAPTURE_WIDTH, CAPTURE_HEIGHT, CAPTURE_FPS = 1920, 1080, 30
PREVIEW_WIDTH, PREVIEW_HEIGHT = 640, 360
```

## Notes

- UI text and status messages are in Spanish ("Grabando", "Esperando contenido", etc.).
- FFmpeg stderr is discarded (`DEVNULL`) throughout; errors fail silently.
- There is no test suite.
