import os
import json
import time
import logging
import subprocess
import threading
from queue import Queue, Empty
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Constants ─────────────────────────────────────────────────────────────────
# When frozen by PyInstaller, __file__ points to a temp extraction folder that is
# deleted on exit. sys.executable always points to the actual .exe location.
import sys
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
SETTINGS_PATH = APP_DIR / "settings.json"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov")
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
AUDIO_BITRATES = ["64k", "96k", "128k", "192k", "256k", "320k"]

DEFAULTS = {
    "watch_folder": "",
    "output_folder": "",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "video_crf": 32,
    "video_preset": "slow",
    "audio_bitrate": "128k",
    "reduce_to_30fps": False,
    "max_concurrent_jobs": 1,
    "cpu_thread_limit": 0,
    "file_wait_timeout": 600,
}

log = logging.getLogger("compressor")


# ── Logging → GUI ─────────────────────────────────────────────────────────────
class GUILogHandler(logging.Handler):
    """Forwards log records to a CTkTextbox, scheduled on the main thread."""

    def __init__(self, textbox: ctk.CTkTextbox):
        super().__init__()
        self.textbox = textbox

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record) + "\n"
        self.textbox.after(0, self._write, msg)

    def _write(self, msg: str) -> None:
        self.textbox.configure(state="normal")
        self.textbox.insert("end", msg)
        self.textbox.see("end")
        self.textbox.configure(state="disabled")


# ── Compression logic ─────────────────────────────────────────────────────────
class Compressor:
    def __init__(self, settings: dict):
        self.s = settings
        self.job_queue: Queue = Queue()
        self._processing: set[str] = set()
        self._lock = threading.Lock()
        self._observer = None
        self._running = False

    def output_path_for(self, src_path: str) -> str:
        base, _ = os.path.splitext(os.path.basename(src_path))
        return os.path.join(self.s["output_folder"], f"compressed_{base}.mp4")

    def enqueue(self, src_path: str) -> bool:
        with self._lock:
            if src_path in self._processing:
                return False
            self._processing.add(src_path)
        self.job_queue.put(src_path)
        return True

    def get_audio_track_count(self, input_path: str) -> int:
        cmd = [
            self.s["ffprobe_path"], "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", input_path,
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return len([ln for ln in result.stdout.strip().split("\n") if ln])
        except Exception as e:
            log.warning(f"Could not detect audio tracks ({e}), defaulting to 1.")
            return 1

    def compress_video(self, input_path: str, output_path: str) -> None:
        s = self.s
        track_count = self.get_audio_track_count(input_path)
        log.info(f"  Audio tracks: {track_count}")

        cmd = [s["ffmpeg_path"], "-y", "-i", input_path]

        if track_count > 1:
            log.info("  Mixing audio tracks...")
            cmd.extend([
                "-filter_complex", f"amix=inputs={track_count}:duration=longest",
                "-c:a", "aac", "-b:a", s["audio_bitrate"],
            ])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", s["audio_bitrate"]])

        if s["reduce_to_30fps"]:
            cmd.extend(["-r", "30"])

        if s["cpu_thread_limit"] > 0:
            cmd.extend(["-threads", str(s["cpu_thread_limit"])])

        # --- VIDEO CODEC OPTIONS ---
        # Current: H.265 software — best compression/quality balance for most hardware.
        cmd.extend([
            "-vcodec", "libx265",
            "-crf", str(s["video_crf"]),
            "-preset", s["video_preset"],
            "-pix_fmt", "yuv420p",
            output_path,
        ])

        # --- AV1 ALTERNATIVES (20–40% smaller than H.265 at same quality) ---
        #
        # Option 1: av1_nvenc — NVIDIA RTX 40-series (4070/4080/4090) only.
        #   cmd.extend(["-vcodec","av1_nvenc","-cq",str(s["video_crf"]),"-preset","p4","-pix_fmt","yuv420p",output_path])
        #
        # Option 2: libaom-av1 — software AV1, no GPU required, very slow.
        #   cmd.extend(["-vcodec","libaom-av1","-crf",str(s["video_crf"]),"-cpu-used","4","-pix_fmt","yuv420p",output_path])

        subprocess.run(cmd, check=True)

    def wait_for_file(self, filepath: str) -> bool:
        log.info("  Waiting for file to finish copying...")
        deadline = time.time() + self.s["file_wait_timeout"]
        while time.time() < deadline:
            try:
                os.rename(filepath, filepath)
                return True
            except OSError:
                time.sleep(1)
        log.warning(f"  Timed out waiting for file to unlock: {filepath}")
        return False

    def process_video(self, src_path: str) -> None:
        filename = os.path.basename(src_path)
        out_path = self.output_path_for(src_path)
        input_mb = os.path.getsize(src_path) / (1024 * 1024)
        log.info(f"Processing: {filename} ({input_mb:.1f} MB)")
        try:
            self.compress_video(src_path, out_path)
            output_mb = os.path.getsize(out_path) / (1024 * 1024)
            reduction = (1 - output_mb / input_mb) * 100
            log.info(f"Done: {input_mb:.1f} MB → {output_mb:.1f} MB ({reduction:.1f}% reduction)")
        except Exception as e:
            log.error(f"Failed to compress {filename}: {e}")
        finally:
            with self._lock:
                self._processing.discard(src_path)

    def worker(self) -> None:
        while self._running:
            try:
                src_path = self.job_queue.get(timeout=1)
            except Empty:
                continue
            try:
                self.process_video(src_path)
            finally:
                self.job_queue.task_done()

    def scan_and_queue(self) -> int:
        watch = self.s["watch_folder"]
        log.info(f"Scanning {watch}...")
        queued = skipped = 0
        for filename in sorted(os.listdir(watch)):
            if not filename.lower().endswith(VIDEO_EXTENSIONS):
                continue
            src = os.path.join(watch, filename)
            if os.path.exists(self.output_path_for(src)):
                log.info(f"  Skipping (already compressed): {filename}")
                skipped += 1
            elif self.enqueue(src):
                log.info(f"  Queued: {filename}")
                queued += 1
        log.info(f"Scan complete — {queued} queued, {skipped} already done.")
        return queued

    def start(self) -> bool:
        """Scans, queues, and starts workers. Returns False if nothing to do."""
        os.makedirs(self.s["watch_folder"], exist_ok=True)
        os.makedirs(self.s["output_folder"], exist_ok=True)
        self._running = True

        for _ in range(self.s["max_concurrent_jobs"]):
            threading.Thread(target=self.worker, daemon=True).start()

        if self.scan_and_queue() == 0:
            log.info("No new videos to compress.")
            self._running = False
            return False

        handler = _WatchdogHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, self.s["watch_folder"], recursive=False)
        self._observer.start()
        log.info("Watching for new videos...")
        return True

    def stop(self) -> None:
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        log.info("Stopped.")


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, compressor: Compressor):
        self.c = compressor

    def on_created(self, event):
        if event.is_directory or not event.src_path.lower().endswith(VIDEO_EXTENSIONS):
            return
        log.info(f"\nNew video detected: {event.src_path}")
        if not self.c.wait_for_file(event.src_path):
            return
        if self.c.enqueue(event.src_path):
            log.info(f"  Added to queue. Pending: {self.c.job_queue.qsize()}")


# ── GUI ────────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Video Compressor Plus")
        self.geometry("760x920")
        self.minsize(620, 740)
        self.grid_columnconfigure(0, weight=1)
        self._compressor = None
        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── layout helpers ────────────────────────────────────────────────────────
    def _section_label(self, text: str, row: int) -> None:
        ctk.CTkLabel(
            self, text=text,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("gray40", "gray70"),
        ).grid(row=row, column=0, padx=20, pady=(14, 2), sticky="w")

    def _section_frame(self, row: int) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid(row=row, column=0, padx=20, pady=(0, 4), sticky="ew")
        f.grid_columnconfigure(1, weight=1)
        return f

    def _labeled_entry(self, parent, row: int, label: str, default: str = "") -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, padx=12, pady=8, sticky="w")
        e = ctk.CTkEntry(parent)
        e.insert(0, default)
        e.grid(row=row, column=1, padx=12, pady=8, sticky="ew")
        return e

    def _int_entry(self, parent, row: int, label: str, default: int, hint: str = "") -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label).grid(row=row, column=0, padx=12, pady=8, sticky="w")
        e = ctk.CTkEntry(parent, width=80)
        e.insert(0, str(default))
        e.grid(row=row, column=1, padx=12, pady=8, sticky="w")
        if hint:
            ctk.CTkLabel(
                parent, text=hint,
                text_color="gray60", font=ctk.CTkFont(size=11),
            ).grid(row=row, column=2, padx=(0, 12), pady=8, sticky="w")
        return e

    # ── full UI ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        r = 0

        ctk.CTkLabel(
            self, text="🎬  Video Compressor Plus",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=r, column=0, padx=20, pady=(18, 4), sticky="w")
        r += 1

        # Folders
        self._section_label("Folders", r); r += 1
        folders = self._section_frame(r); r += 1

        ctk.CTkLabel(folders, text="Watch Folder").grid(row=0, column=0, padx=12, pady=8, sticky="w")
        self.watch_entry = ctk.CTkEntry(folders)
        self.watch_entry.grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        ctk.CTkButton(folders, text="Browse", width=80,
                      command=lambda: self._browse(self.watch_entry)).grid(row=0, column=2, padx=(4, 12), pady=8)

        ctk.CTkLabel(folders, text="Output Folder").grid(row=1, column=0, padx=12, pady=8, sticky="w")
        self.output_entry = ctk.CTkEntry(folders)
        self.output_entry.grid(row=1, column=1, padx=6, pady=8, sticky="ew")
        ctk.CTkButton(folders, text="Browse", width=80,
                      command=lambda: self._browse(self.output_entry)).grid(row=1, column=2, padx=(4, 12), pady=8)

        # Video
        self._section_label("Video", r); r += 1
        video = self._section_frame(r); r += 1
        video.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(video, text="CRF  (0 = best, 51 = worst)").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        sf = ctk.CTkFrame(video, fg_color="transparent")
        sf.grid(row=0, column=1, columnspan=2, padx=12, pady=8, sticky="ew")
        sf.grid_columnconfigure(0, weight=1)
        self.crf_label = ctk.CTkLabel(sf, text="32", width=28)
        self.crf_slider = ctk.CTkSlider(
            sf, from_=0, to=51, number_of_steps=51,
            command=lambda v: self.crf_label.configure(text=str(int(v))),
        )
        self.crf_slider.set(32)
        self.crf_slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.crf_label.grid(row=0, column=1)

        ctk.CTkLabel(video, text="Preset").grid(row=1, column=0, padx=12, pady=8, sticky="w")
        self.preset_var = ctk.StringVar(value="slow")
        ctk.CTkOptionMenu(video, variable=self.preset_var, values=PRESETS, width=160).grid(
            row=1, column=1, padx=12, pady=8, sticky="w")

        ctk.CTkLabel(video, text="Reduce to 30 fps").grid(row=2, column=0, padx=12, pady=8, sticky="w")
        self.fps_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(video, text="", variable=self.fps_var).grid(row=2, column=1, padx=12, pady=8, sticky="w")

        # Audio
        self._section_label("Audio", r); r += 1
        audio = self._section_frame(r); r += 1
        ctk.CTkLabel(audio, text="Bitrate").grid(row=0, column=0, padx=12, pady=8, sticky="w")
        self.audio_var = ctk.StringVar(value="128k")
        ctk.CTkOptionMenu(audio, variable=self.audio_var, values=AUDIO_BITRATES, width=120).grid(
            row=0, column=1, padx=12, pady=8, sticky="w")

        # Resources
        self._section_label("Resources", r); r += 1
        res = self._section_frame(r); r += 1
        res.grid_columnconfigure(2, weight=1)
        self.concurrent_entry = self._int_entry(res, 0, "Concurrent Jobs", 1, "videos compressed at once")
        self.threads_entry    = self._int_entry(res, 1, "CPU Threads per Job", 0, "0 = all cores")
        self.timeout_entry    = self._int_entry(res, 2, "File Wait Timeout (s)", 600, "seconds to wait for a file to finish copying")

        # Advanced
        self._section_label("Advanced", r); r += 1
        adv = self._section_frame(r); r += 1
        self.ffmpeg_entry  = self._labeled_entry(adv, 0, "ffmpeg path",  "ffmpeg")
        self.ffprobe_entry = self._labeled_entry(adv, 1, "ffprobe path", "ffprobe")

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=r, column=0, padx=20, pady=12, sticky="ew")
        btn_frame.grid_columnconfigure((0, 1), weight=1)
        r += 1

        self.start_btn = ctk.CTkButton(
            btn_frame, text="▶  Start", height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._on_start,
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="■  Stop", height=46,
            fg_color="#c0392b", hover_color="#922b21",
            font=ctk.CTkFont(size=15, weight="bold"),
            state="disabled", command=self._on_stop,
        )
        self.stop_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        # Log
        self._section_label("Log", r); r += 1
        self.log_box = ctk.CTkTextbox(self, height=180, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=r, column=0, padx=20, pady=(0, 20), sticky="nsew")
        self.grid_rowconfigure(r, weight=1)
        self.log_box.configure(state="disabled")

        handler = GUILogHandler(self.log_box)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)

    # ── settings persistence ──────────────────────────────────────────────────
    def _collect_settings(self):
        try:
            return {
                "watch_folder":       self.watch_entry.get().strip(),
                "output_folder":      self.output_entry.get().strip(),
                "ffmpeg_path":        self.ffmpeg_entry.get().strip() or "ffmpeg",
                "ffprobe_path":       self.ffprobe_entry.get().strip() or "ffprobe",
                "video_crf":          int(self.crf_slider.get()),
                "video_preset":       self.preset_var.get(),
                "audio_bitrate":      self.audio_var.get(),
                "reduce_to_30fps":    self.fps_var.get(),
                "max_concurrent_jobs": int(self.concurrent_entry.get()),
                "cpu_thread_limit":   int(self.threads_entry.get()),
                "file_wait_timeout":  int(self.timeout_entry.get()),
            }
        except ValueError as e:
            messagebox.showerror("Invalid setting", f"Please check numeric fields:\n{e}")
            return None

    def _apply_settings(self, s: dict) -> None:
        def fill(widget, value):
            widget.delete(0, "end")
            widget.insert(0, str(value))

        fill(self.watch_entry,     s.get("watch_folder", ""))
        fill(self.output_entry,    s.get("output_folder", ""))
        fill(self.ffmpeg_entry,    s.get("ffmpeg_path", "ffmpeg"))
        fill(self.ffprobe_entry,   s.get("ffprobe_path", "ffprobe"))
        fill(self.concurrent_entry, s.get("max_concurrent_jobs", 1))
        fill(self.threads_entry,   s.get("cpu_thread_limit", 0))
        fill(self.timeout_entry,   s.get("file_wait_timeout", 600))

        crf = s.get("video_crf", 32)
        self.crf_slider.set(crf)
        self.crf_label.configure(text=str(crf))
        self.preset_var.set(s.get("video_preset", "slow"))
        self.audio_var.set(s.get("audio_bitrate", "128k"))
        self.fps_var.set(s.get("reduce_to_30fps", False))

    def _load_settings(self) -> None:
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH) as f:
                    self._apply_settings(json.load(f))
                return
            except Exception:
                pass
        self._apply_settings(DEFAULTS)

    def _save_settings(self, s: dict) -> None:
        try:
            with open(SETTINGS_PATH, "w") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            log.warning(f"Could not save settings: {e}")

    # ── actions ───────────────────────────────────────────────────────────────
    def _browse(self, entry: ctk.CTkEntry) -> None:
        path = filedialog.askdirectory()
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _on_start(self) -> None:
        s = self._collect_settings()
        if s is None:
            return
        if not s["watch_folder"] or not s["output_folder"]:
            messagebox.showwarning("Missing folders", "Please set both Watch Folder and Output Folder.")
            return
        if s["max_concurrent_jobs"] < 1:
            messagebox.showwarning("Invalid setting", "Concurrent Jobs must be at least 1.")
            return

        self._save_settings(s)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._compressor = Compressor(s)

        def run():
            started = self._compressor.start()
            if not started:
                self.after(0, self._reset_buttons)

        threading.Thread(target=run, daemon=True).start()

    def _on_stop(self) -> None:
        compressor, self._compressor = self._compressor, None
        if compressor:
            threading.Thread(target=compressor.stop, daemon=True).start()
        self._reset_buttons()

    def _reset_buttons(self) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _on_close(self) -> None:
        s = self._collect_settings()
        if s:
            self._save_settings(s)
        if self._compressor:
            self._compressor.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
