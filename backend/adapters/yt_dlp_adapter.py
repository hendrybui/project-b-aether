from __future__ import annotations

from pathlib import Path

from adapters.process_utils import require_command, run_command


class YtDlpAdapter:
    def download(self, url: str, output_dir: str, *, job_id: str, log_path: Path) -> str:
        yt_dlp = require_command('yt-dlp')
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        template = str(out_dir / 'downloaded.%(ext)s')
        run_command(
            [yt_dlp, '--no-playlist', '-f', 'bestaudio/best', '-o', template, url],
            job_id=job_id,
            log_path=log_path,
        )
        candidates = sorted(out_dir.glob('downloaded.*'))
        if not candidates:
            raise RuntimeError('yt-dlp completed but no media file was produced')
        return str(candidates[0])
