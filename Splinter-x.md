# splinter-x



Add song file like mp3 ,wav, or mp4 or video web files download on web instantly 
Paste a YouTube URL, get the audio split into stems (vocals / drums / bass / guitar / piano / other) and play them back in a DAW-style multitrack mixer. Mute, solo, mix, zoom the waveform, loop a region, and download individual stems or a custom mix.

Local-only, single-user. One Python process serves both the REST/SSE API and the static frontend.



## Features ##
-  
- **6-stem separation** via Demucs `htdemucs_6s`. Auto-detects the best Torch device (AMD, vulkan, CPU); on Apple Silicon you get ~3-5× speedup over CPU for free.
- **DAW-style waveform editor.** Min/max sample rendering across all stems with shared global normalization, zoom in / out / Fit (`+` / `−` / `Cmd-wheel`), loop drag on the ruler, gold playhead overlay, and stem-aligned lanes.
- **Stem subset extraction.** Click stem chips on the import page to pick which stems to keep. Filter-chip semantics: clicking from "all selected" snaps to "only this one"; subsequent clicks add or remove. Selection persists in `localStorage`.
- **"Original" backing track.** When you pick a subset, the studio includes a 7th lane with the *complement* (full song minus the selected stems). Playing it alongside the isolated stems reconstructs the full mix without doubling, which is perfect for A/B reference.
- **Downloadable selected mix.** A single `mix.wav` of just the selected stems, summed via ffmpeg amix. Surfaces as the **Download Mix** button in the footer.
- **Per-stem mixer.** Volume fader, mute, solo, and "monitor" (solo-only) per stem. State is synced between the preview mixer and the stems sidebar.
- **Live VU per stem.** Post-gain RMS via Web Audio analysers on each stem's gain node. Peak hold + slow falloff for the classic DAW meter feel.
- **Song analysis.** BPM (librosa beat tracker on percussive HPSS), key + scale + confidence (Albrecht-Shanahan profile with root-prominence weighting), integrated LUFS (BS.1770 via pyloudnorm), sample peak in dBFS. All surfaced in the now-playing card.
- **Cancellable jobs.** Click cancel mid-pipeline (download, Demucs, ffmpeg amix) and the runner terminates the active subprocess immediately, deletes the partial job dir, and returns the API to ready.

**VULKAN**
Regarding vulkan integrated here are some update or you can use for thhe see https://github.com/KhronosGroup/Vulkan-Docsgot lot off usefull info and update to use in this project

Pick the CPU variant if you don't have an NVIDIA GPU, or just want a smaller download — separation still works, it's just slower. Pick the NVIDIA variant if you have a CUDA-capable GPU and want faster separation.

**Running it:**

*(for running from source on macOS / Linux)*

- Python 3.10+
- `ffmpeg` on `PATH` (install instructions per-platform below)
- [uv](https://github.com/astral-sh/uv) (recommended) or `pip`
- ~170 MB free disk for the Demucs `htdemucs_6s` model (downloaded automatically on first run)
- Reasonably modern CPU. An Apple-Silicon `mps` or NVIDIA `cuda` GPU dramatically speeds up separation.

## Setup

### macOS / Linux (one-shot)

```sh
./run.sh setup     # detects OS, installs ffmpeg + uv, runs `uv sync`
./run.sh start
```


Open <http://localhost:8000>.

## Running

### `run.sh` (macOS / Linux)


```

## Troubleshooting

- **`uvicorn not found at .venv/bin/uvicorn`** when running `./run.sh start`: you haven't installed deps yet. Run `uv sync` first.
- **`ffmpeg: command not found`** during a job: install ffmpeg per the platform-specific Setup section above and restart the server (`./run.sh restart`).
- **`WARNING: [youtube] No supported JavaScript runtime could be found`**: yt-dlp needs a JS runtime to reliably pick the best YouTube audio format. Install deno (`brew install deno` on macOS, `curl -fsSL https://deno.land/install.sh | sh` on Linux) and restart. Without it, downloads still work but may pick suboptimal formats.
- **First separation is very slow**: the Demucs model weights download on first run. Subsequent runs reuse the cached weights in `~/.cache/torch/hub/checkpoints`.
- **Demucs runs on CPU and takes minutes**: Demucs picks the best device automatically (CUDA, MPS, CPU). On Apple Silicon you should see MPS acceleration; if not, your `torch` install may be CPU-only. Check the server log on startup for `demucs config: model=htdemucs_6s device=mps`.
- **Browser memory grows on long videos**: the multitrack player decodes each stem into a Web Audio buffer for the overview waveform. A 6-minute song uses a few hundred MB of decoded audio in memory; long lectures will be uncomfortable. Trim the input or close other tabs.
- **Page reloaded mid-job**: the job keeps running on the server, but the UI loses track of it. Wait for it to finish, then re-submit (the stems on disk get overwritten, or, if you want the previous output, find it under `jobs/<job_id>/stems/`).
- **`./run.sh: Permission denied`**: the script lost its executable bit. Run `chmod +x run.sh`.

## API (for tinkering)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/jobs` | Body `{url, stems?}` → `{job_id}`. `stems` is an optional array of stem names; defaults to all 6. |
| GET | `/api/jobs/{id}` | Job state snapshot. |
| GET | `/api/jobs/{id}/events` | Server-Sent Events stream of job state. |
| POST | `/api/jobs/{id}/cancel` | Set the cancel flag and terminate the active subprocess. |
| GET | `/api/jobs/{id}/stems/{name}.wav` | Stream/download a single stem (range requests). `name` ∈ {6 demucs stems, `original`, `mix`}. |
| DELETE | `/api/jobs/{id}` | Remove the job dir from disk (must be done/error/cancelled). |

## Disclaimer



- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** for downloading audio from URLs you provide.
- **[Demucs](https://github.com/facebookresearch/demucs)** (`htdemucs_6s` model) for source separation.





