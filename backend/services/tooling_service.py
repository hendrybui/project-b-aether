from __future__ import annotations

from domain.models import DiagnosticsResponse, SeparationBackend, ToolReadiness
from adapters.process_utils import find_command_path
from plugins.registry import plugin_registry


class ToolingService:
    TOOL_NAMES = ['ffmpeg', 'yt-dlp', 'demucs']

    def diagnostics(self) -> DiagnosticsResponse:
        tools: list[ToolReadiness] = []
        for name in self.TOOL_NAMES:
            path = find_command_path(name)
            tools.append(
                ToolReadiness(
                    name=name,
                    available=bool(path),
                    path=path,
                    detail=None if path else f'{name} is not installed or not on PATH',
                )
            )
        # Which separation engine the next job uses (ROCm container vs local
        # CPU worker), with measured per-job overhead from the last run.
        separation = None
        htdemucs = plugin_registry.get('htdemucs')
        runtime_info = getattr(htdemucs, 'runtime_info', None)
        if callable(runtime_info):
            separation = SeparationBackend(**runtime_info())
        return DiagnosticsResponse(
            ready=all(tool.available for tool in tools),
            tools=tools,
            # Registered processing capabilities (see plugins/). Importing the
            # package self-registers the built-ins.
            plugins=plugin_registry.names(),
            separation=separation,
        )


tooling_service = ToolingService()
