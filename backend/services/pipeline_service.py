from __future__ import annotations

import logging
import shutil
from pathlib import Path
from threading import Thread

from adapters.ffmpeg_adapter import FFmpegAdapter
from adapters.process_utils import append_log
from adapters.yt_dlp_adapter import YtDlpAdapter
from domain.enums import JobStatus, SourceType
from plugins import CancelledError, PluginContext, plugin_registry
from services.cancellation_service import cancellation_service
from storage.job_store import JobStore

log = logging.getLogger("audiomass.pipeline")


class PipelineService:
    """Phase 1 processing pipeline with pragmatic fallbacks for local development."""

    def __init__(self, job_service: 'JobService') -> None:
        self.job_service = job_service
        self.job_store = JobStore()
        self.yt_dlp = YtDlpAdapter()
        self.ffmpeg = FFmpegAdapter()

    def start(self, job_id: str) -> None:
        Thread(target=self.run, args=(job_id,), daemon=True).start()

    def run(self, job_id: str) -> None:
        job_dir = self.job_store.ensure_job_dir(job_id)
        log_path = job_dir / 'logs' / 'pipeline.log'
        handler = self._attach_log_handler(log_path)
        try:
            manifest = self.job_service.get_manifest(job_id)
            if manifest is None:
                raise RuntimeError('Manifest missing')

            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.validating_input, progress=0.05, step=JobStatus.validating_input.value, message='Validating job request')
            source_path = self._prepare_source(job_id, manifest.source.type.value, manifest.source.url, manifest.source.filename, job_dir, log_path)
            self.job_service.update_manifest_files(job_id, {'input_original': str(source_path)})

            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.transcoding, progress=0.25, step=JobStatus.transcoding.value, message='Converting source to canonical WAV')
            canonical_wav = job_dir / 'source' / 'input.wav'
            self.ffmpeg.transcode_to_wav(str(source_path), str(canonical_wav), job_id=job_id, log_path=log_path)
            self.job_service.update_manifest_files(job_id, {'input_wav': str(canonical_wav)})

            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.separating, progress=0.55, step=JobStatus.separating.value, message='Separating selected stems')
            manifest_after_transcode = self.job_service.get_manifest(job_id)
            selected_stems = manifest_after_transcode.selected_stems if manifest_after_transcode else []
            final_stems = self._separate_stems(job_id, canonical_wav, selected_stems, job_dir, log_path)
            self.job_service.update_manifest_files(job_id, final_stems)

            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.postprocessing, progress=0.82, step=JobStatus.postprocessing.value, message='Creating mix and original tracks')
            stem_inputs = [path for key, path in final_stems.items() if key in selected_stems]
            mix_path = job_dir / 'stems' / 'mix.wav'
            self.ffmpeg.mix_wavs(stem_inputs, str(mix_path), job_id=job_id, log_path=log_path)
            original_path = job_dir / 'stems' / 'original.wav'
            self.ffmpeg.copy_audio(str(canonical_wav), str(original_path))
            self.job_service.update_manifest_files(job_id, {'mix': str(mix_path), 'original': str(original_path)})

            # --- Analyzing ---
            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.analyzing, progress=0.88, step=JobStatus.analyzing.value, message='Analyzing audio (BPM, key, loudness)')
            all_stem_paths = dict(final_stems)
            all_stem_paths['mix'] = str(mix_path)
            all_stem_paths['original'] = str(original_path)
            analysis = self._analyze_stems(job_id, str(canonical_wav), all_stem_paths, job_dir, log_path)
            self.job_service.update_analysis(job_id, analysis)

            # --- Waveform generation ---
            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.analyzing, progress=0.92, step='generating_waveforms', message='Generating waveform summaries')
            waveform_map = self._generate_waveforms(job_id, all_stem_paths, job_dir, log_path)
            for stem_name, wf_path in waveform_map.items():
                self.job_service.update_manifest_files(job_id, {f'waveform_{stem_name}': wf_path})

            # --- Packaging ---
            self._check_cancel(job_id)
            self.job_service.update_job(job_id, status=JobStatus.packaging, progress=0.95, step=JobStatus.packaging.value, message='Finalizing manifest and outputs')
            self.job_service.mark_done(job_id)
        except CancelledError:
            append_log(log_path, 'Job cancelled')
            self._cleanup_partial_outputs(job_dir)
            self.job_service.mark_cancelled(job_id, 'Job cancelled before completion')
        except Exception as exc:
            append_log(log_path, f'Pipeline error: {exc}')
            self._cleanup_partial_outputs(job_dir)
            self.job_service.mark_failed(job_id, self._friendly_error_message(exc))
        finally:
            self._detach_log_handler(handler)
            cancellation_service.clear(job_id)

    def _attach_log_handler(self, log_path: Path) -> logging.Handler:
        """Route `audiomass.*` logger output into the per-job pipeline log.

        Without this the separator's progress messages only go to stdout and
        never reach pipeline.log, so the separation phase looks silent/hung.
        """
        append_log(log_path, '')  # ensure file exists
        handler = logging.FileHandler(log_path, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        handler.setLevel(logging.INFO)
        logger = logging.getLogger('audiomass')
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return handler

    def _detach_log_handler(self, handler: logging.Handler | None) -> None:
        if handler is None:
            return
        logging.getLogger('audiomass').removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    def _prepare_source(self, job_id: str, source_type: str, url: str | None, filename: str | None, job_dir: Path, log_path: Path) -> Path:
        self.job_service.update_job(job_id, status=JobStatus.ingesting_source, progress=0.12, step=JobStatus.ingesting_source.value, message='Preparing source media')
        source_dir = job_dir / 'source'
        if source_type == SourceType.upload.value:
            if not filename:
                raise RuntimeError('Upload job missing filename')
            candidate = Path(filename)
            if not candidate.exists():
                raise RuntimeError('Uploaded file placeholder path does not exist yet')
            target = source_dir / candidate.name
            shutil.copy2(candidate, target)
            return target
        if not url:
            raise RuntimeError('URL-based job missing source URL')
        return Path(self.yt_dlp.download(url, str(source_dir), job_id=job_id, log_path=log_path))

    def _separate_stems(self, job_id: str, canonical_wav: Path, selected_stems: list[str], job_dir: Path, log_path: Path) -> dict[str, str]:
        """Drive the 'htdemucs' plugin through the uniform plugin contract.

        The plugin owns device detection, the PyTorch worker child process
        and the CLI fallback; it reports progress through the context. This
        method only maps the plugin's phase-relative progress onto the job's
        separating span and relays cancellation/logging.
        """
        plugin = plugin_registry.require('htdemucs')

        def on_progress(fraction: float, *, stage: str | None = None, message: str | None = None) -> None:
            base, span = 0.55, 0.25
            pct = base + fraction * span
            self.job_service.update_job(
                job_id,
                status=JobStatus.separating,
                progress=round(pct, 2),
                step=stage or JobStatus.separating.value,
                message=message or 'Separating selected stems',
            )

        ctx = PluginContext(
            job_id=job_id,
            work_dir=job_dir,
            params={'input_path': str(canonical_wav), 'stems': selected_stems},
            progress=on_progress,
            log=lambda line: append_log(log_path, line),
            log_path=log_path,
            is_cancelled=lambda: cancellation_service.is_cancelled(job_id),
        )
        return plugin.run(ctx)

    def _analyze_stems(self, job_id: str, canonical_wav: str, all_stem_paths: dict[str, str], job_dir: Path, log_path: Path) -> dict:
        """Drive the 'analyze' plugin through the uniform plugin contract.

        The plugin reports step-wise progress (tempo/key, loudness, duration,
        per-stem energy) which this method maps onto the job's analyzing span
        (0.88 -> 0.92, where waveform generation takes over)."""
        plugin = plugin_registry.require('analyze')

        def on_progress(fraction: float, *, stage: str | None = None, message: str | None = None) -> None:
            base, span = 0.88, 0.04
            pct = base + fraction * span
            self.job_service.update_job(
                job_id,
                status=JobStatus.analyzing,
                progress=round(pct, 2),
                step=stage or JobStatus.analyzing.value,
                message=message or 'Analyzing audio (BPM, key, loudness)',
            )

        ctx = PluginContext(
            job_id=job_id,
            work_dir=job_dir,
            params={'input_path': canonical_wav, 'stems': all_stem_paths},
            progress=on_progress,
            log=lambda line: append_log(log_path, line),
            is_cancelled=lambda: cancellation_service.is_cancelled(job_id),
        )
        return plugin.run(ctx)

    def _generate_waveforms(self, job_id: str, all_stem_paths: dict[str, str], job_dir: Path, log_path: Path) -> dict[str, str]:
        """Drive the 'waveform' plugin through the uniform plugin contract.

        The plugin reports one progress step per stem, mapped onto the job's
        0.92 -> 0.95 span (packaging takes over at 0.95)."""
        plugin = plugin_registry.require('waveform')

        def on_progress(fraction: float, *, stage: str | None = None, message: str | None = None) -> None:
            base, span = 0.92, 0.03
            pct = base + fraction * span
            self.job_service.update_job(
                job_id,
                status=JobStatus.analyzing,
                progress=round(pct, 2),
                step=stage or 'generating_waveforms',
                message=message or 'Generating waveform summaries',
            )

        ctx = PluginContext(
            job_id=job_id,
            work_dir=job_dir,
            params={'stems': all_stem_paths},
            progress=on_progress,
            log=lambda line: append_log(log_path, line),
            is_cancelled=lambda: cancellation_service.is_cancelled(job_id),
        )
        return plugin.run(ctx)

    def _check_cancel(self, job_id: str) -> None:
        if cancellation_service.is_cancelled(job_id):
            raise CancelledError()

    def _cleanup_partial_outputs(self, job_dir: Path) -> None:
        # Only purge transient/intermediate artefacts. The `stems` directory is
        # intentionally left intact: separation is the expensive step and may
        # have already completed when a cancel/failed event arrives. Wiping it
        # destroys finished work and leaves the manifest pointing at files that
        # no longer exist. Stale entries there are harmless; delete_job cleans
        # the whole job dir when the user removes the job.
        for folder_name in ['stems_raw', 'analysis', 'waveforms']:
            target = job_dir / folder_name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    def _friendly_error_message(self, exc: Exception) -> str:
        message = str(exc)
        if 'Required command not found' in message:
            return message + '. Install the missing tool and ensure it is on PATH.'
        return f'Pipeline failed: {message}'
