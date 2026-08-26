from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from adapters.process_utils import ExternalToolError, require_command, run_command


class FFmpegAdapter:
    def transcode_to_wav(self, input_path: str, output_path: str, *, job_id: str, log_path: Path) -> str:
        ffmpeg = require_command('ffmpeg')
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [ffmpeg, '-y', '-i', input_path, '-ac', '2', '-ar', '44100', output_path],
            job_id=job_id,
            log_path=log_path,
        )
        return str(output)

    def copy_audio(self, input_path: str, output_path: str) -> str:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, output)
        return str(output)

    def create_silent_wav(self, output_path: str, *, job_id: str, log_path: Path, duration_sec: float = 1.0) -> str:
        ffmpeg = require_command('ffmpeg')
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [ffmpeg, '-y', '-f', 'lavfi', '-i', f'anullsrc=r=44100:cl=stereo', '-t', str(duration_sec), output_path],
            job_id=job_id,
            log_path=log_path,
        )
        return str(output)

    def mix_wavs(self, inputs: Iterable[str], output_path: str, *, job_id: str, log_path: Path) -> str:
        input_list = list(inputs)
        if not input_list:
            raise ExternalToolError('No inputs provided for mixdown')
        if len(input_list) == 1:
            return self.copy_audio(input_list[0], output_path)

        ffmpeg = require_command('ffmpeg')
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [ffmpeg, '-y']
        for audio in input_list:
            command.extend(['-i', audio])
        command.extend([
            '-filter_complex',
            f'amix=inputs={len(input_list)}:normalize=0',
            output_path,
        ])
        run_command(command, job_id=job_id, log_path=log_path)
        return str(output)
