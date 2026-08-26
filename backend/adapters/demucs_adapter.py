from __future__ import annotations

from pathlib import Path

from adapters.process_utils import ExternalToolError, require_command, run_command


class DemucsAdapter:
    STEM_NAMES = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other']

    def separate(self, input_path: str, output_dir: str, *, job_id: str, log_path: Path, device: str = 'cpu') -> dict[str, str]:
        demucs = require_command('demucs')
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        run_command(
            [demucs, '--name', 'htdemucs_6s', '--device', device, '--out', str(out_dir), input_path],
            job_id=job_id,
            log_path=log_path,
        )
        matches = list(out_dir.rglob('*.wav'))
        stems: dict[str, str] = {}
        for stem_name in self.STEM_NAMES:
            match = next((p for p in matches if p.stem.lower() == stem_name), None)
            if match:
                stems[stem_name] = str(match)
        if not stems:
            raise ExternalToolError('Demucs completed but no expected stem files were found')
        return stems
