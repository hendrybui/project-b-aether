"""Uniform plugin contract for AudioMass processing capabilities.

A plugin is a named capability — stem separation, transcription, effects, ... —
that the pipeline or an API endpoint drives through a single interface. The
caller supplies a :class:`PluginContext` (inputs, params, a progress mapping,
a cancellation probe, a log sink) and the plugin returns a JSON-ready dict.
This keeps progress reporting, cancellation and logging uniform across tools,
so a job-scoped pipeline step and a direct API request behave the same way.
"""

from __future__ import annotations

from pathlib import Path


class PluginError(RuntimeError):
    """A plugin failed in a way the caller should surface to the user."""


class CancelledError(Exception):
    """Raised by plugins when the caller reports the job was cancelled.

    Defined here (not in the pipeline layer) so plugins can raise it without
    importing the orchestration code; the pipeline maps it to a cancelled job.
    """


class PluginContext:
    """Everything a plugin needs to run, handed in by the caller.

    params:        plugin-specific inputs, e.g. {'input_path': ..., 'stems': [...]}
    work_dir:      scratch/output directory (for job plugins: the job dir)
    job_id:        the job this runs under, if any (used for process/cancel tracking)
    progress:      optional callable ``progress(fraction, *, stage=None, message=None)``
                   with fraction in [0, 1] relative to this plugin's phase
    log:           optional callable ``log(line)`` appending to the job's log
    log_path:      optional Path to the job's log file (adapters that append
                   logs directly need it)
    is_cancelled:  optional callable -> bool; plugins must poll it between long
                   steps and raise CancelledError when it turns true
    """

    def __init__(
        self,
        *,
        params: dict,
        work_dir: Path,
        job_id: str | None = None,
        progress=None,
        log=None,
        log_path: Path | None = None,
        is_cancelled=None,
    ) -> None:
        self.params = params
        self.work_dir = Path(work_dir)
        self.job_id = job_id
        self.progress = progress
        self.log = log if log is not None else lambda _line: None
        self.log_path = Path(log_path) if log_path else None
        self.is_cancelled = is_cancelled if is_cancelled is not None else (lambda: False)

    def check_cancelled(self) -> None:
        """Convenience: raise CancelledError when the caller reports a cancel."""
        if self.is_cancelled():
            raise CancelledError()


class AudioPlugin:
    """Base class for registered capabilities.

    Subclass, set ``name``/``description``, implement ``run()`` and register an
    instance with the registry (see ``plugins/__init__.py``).
    """

    name: str = ""
    description: str = ""

    def run(self, ctx: PluginContext) -> dict:
        raise NotImplementedError
