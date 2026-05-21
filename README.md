# 🎬 Video Compressor Plus

A Windows desktop app that batch-compresses videos using H.265 (HEVC), producing dramatically smaller files with minimal perceptible quality loss.

> **Real-world result:** 516 MB (1-min, 1080p60, H.264) → 34 MB (93% reduction) with default settings.

---

## Features

- **Scan & compress** — point it at a folder and it queues every video that hasn't been compressed yet, skipping ones that already have a `compressed_*` output file
- **Watchdog** — after the initial scan, keeps watching the folder and auto-queues any new videos dropped in
- **Multi-track audio mixing** — automatically detects and mixes multiple audio tracks (e.g. game audio + mic + chat) into a single stereo stream
- **Queue with concurrency control** — process multiple videos in parallel or throttle to one at a time
- **Persistent settings** — all settings are saved to `settings.json` next to the exe and restored on next launch
- **Modern dark UI** built with CustomTkinter

---

## Requirements

### To run the `.exe`
- Windows 10/11 (64-bit)
- [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) installed and available on your system PATH  
  *(or paste the full path to `ffmpeg.exe` / `ffprobe.exe` in the Advanced section)*

### To run from source / recompile
- Python 3.10 or newer
- The dependencies listed in [Installation](#installation-from-source)

---

## Download & Run

1. Go to the [Releases](../../releases) page and download `VideoCompressor.exe`
2. Place it anywhere you like (Desktop, a tools folder, etc.)
3. Double-click to launch — no installation needed

> **Note:** First launch may take a few seconds. The single-file exe extracts itself to a temp folder on startup — this is normal PyInstaller behaviour.

---

## Usage

1. **Set your folders**
   - **Watch Folder** — where your raw/uncompressed videos live
   - **Output Folder** — where compressed files will be saved (named `compressed_<original_name>.mp4`)

2. **Adjust settings** to taste (see [Settings Reference](#settings-reference) below)

3. **Click ▶ Start**
   - The app scans the Watch Folder and queues any video that doesn't already have a matching compressed file in the Output Folder
   - If nothing new is found, it exits cleanly with a log message
   - Otherwise it compresses the queue and then stays running, watching for new files

4. **Click ■ Stop** at any time to shut down the watcher (the current encode finishes before it exits)

---

## Settings Reference

### Video
| Setting | Default | Description |
|---|---|---|
| **CRF** | `32` | Quality level (0 = lossless, 51 = worst). 28 is near-transparent, 32 is aggressive, 35+ has noticeable loss |
| **Preset** | `slow` | Encoder speed vs. compression efficiency. Slower = smaller file at the same CRF, but takes longer. `slow` is the best practical balance |
| **Reduce to 30 fps** | Off | Halves the frame rate before encoding — roughly halves file size again. Useful for content where 60fps playback isn't needed |

### Audio
| Setting | Default | Description |
|---|---|---|
| **Bitrate** | `128k` | AAC audio bitrate. `128k` is transparent for most content; use `96k` for speech-only, `192k` if audio quality is critical |

### Resources
| Setting | Default | Description |
|---|---|---|
| **Concurrent Jobs** | `1` | How many videos to compress simultaneously. Raise to 2+ to clear the queue faster; each extra job adds proportional CPU load |
| **CPU Threads per Job** | `0` | Max threads ffmpeg can use per encode. `0` = no limit (all cores). Set to e.g. `8` to cap at ~50% on a 16-core machine |
| **File Wait Timeout** | `600` | Seconds to wait for a newly detected file to finish copying before starting compression |

### Advanced
| Setting | Default | Description |
|---|---|---|
| **ffmpeg path** | `ffmpeg` | Leave as `ffmpeg` if it's on your system PATH, or paste the full path to `ffmpeg.exe` |
| **ffprobe path** | `ffprobe` | Same as above for `ffprobe.exe` |

---

## Codec Notes

The app uses **H.265 (libx265)** software encoding by default, which works on any hardware.

### AV1 — 20–40% smaller than H.265 at the same quality

If you want even smaller files, two AV1 options are commented out in `compress_video()` inside `main.py`:

**Option 1 — `av1_nvenc` (NVIDIA RTX 40-series only: 4070 / 4080 / 4090)**  
Hardware-accelerated AV1. Fast encoding, great compression. RTX 30-series and older do not support AV1 encoding.

**Option 2 — `libaom-av1` (any hardware, very slow)**  
Software AV1. Same quality gain as `av1_nvenc` but encodes at ~2–5 fps — expect 10–20 minutes for a 1-minute clip.

To switch, open `main.py`, find the `# --- VIDEO CODEC OPTIONS ---` block in `compress_video()`, comment out the current `cmd.extend(...)` call, and uncomment the one you want.

---

## Installation (from source)

```bash
git clone https://github.com/YOUR_USERNAME/VideoCompressorPlus.git
cd VideoCompressorPlus

python -m venv .venv
.venv\Scripts\activate

pip install customtkinter watchdog pyinstaller
```

Run directly:
```bash
python main.py
```

---

## Recompiling the `.exe`

After making changes to `main.py`, rebuild with:

```bash
.venv\Scripts\pyinstaller --onefile --windowed --collect-data customtkinter --name "VideoCompressor" main.py
```

The output will be at `dist\VideoCompressor.exe`.

| PyInstaller flag | Purpose |
|---|---|
| `--onefile` | Bundle everything into a single `.exe` |
| `--windowed` | Suppress the console window |
| `--collect-data customtkinter` | Include CustomTkinter's theme and image assets |
| `--name "VideoCompressor"` | Set the output filename |

> `settings.json` is saved in the same folder as the `.exe`, so your settings survive rebuilds.

---

## How it works

1. On **Start**, the app scans the Watch Folder and compares each video against the Output Folder — anything without a matching `compressed_*.mp4` gets added to the queue
2. A pool of worker threads (size = Concurrent Jobs) pulls from the queue and runs ffmpeg
3. A [watchdog](https://github.com/gorakhargosh/watchdog) observer keeps running after the scan, catching any new files dropped into the folder
4. **Duplicate guard** — a thread-safe set tracks files currently in progress, so the same file can never be queued twice even if the watchdog fires multiple events for it
5. Settings are saved to `settings.json` both when you click Start and when you close the window

---

## License

MIT
