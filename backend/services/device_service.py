from __future__ import annotations

import os

import torch


class DeviceService:
    """Detects the best available acceleration target for separation.

    Preference order: CUDA (NVIDIA), Vulkan (AMD/Intel, torch's experimental
    backend), then CPU. Only capability flags are checked here — the real
    smoke test (a warmup forward pass of the actual model) happens at
    separator load time, and the separator falls back to CPU if the chosen
    device turns out unusable despite reporting available.

    Overrides (env):
      AUDIOMASS_DEMUCS_DEVICE   force a device string (e.g. "cuda", "vulkan", "cpu")
      AUDIOMASS_DEMUCS_THREADS  CPU threads for inference (default: all cores)

    Note on AMD: torch's Vulkan backend is only compiled into the CPU wheel
    (`torch+cpu`); the CUDA wheel ships without it, and ROCm does not support
    Polaris (RX 580). So on this box detection correctly lands on CPU — the
    win is multi-threaded inference in the dedicated worker process.
    """

    def detect(self) -> dict:
        device = "cpu"
        label = "CPU"
        experimental = False
        try:
            if torch.cuda.is_available():
                device, label = "cuda", "CUDA"
        except Exception:
            pass

        if device == "cpu":
            vulkan = getattr(torch.backends, "vulkan", None)
            if vulkan is not None and vulkan.is_available():
                device, label, experimental = "vulkan", "Vulkan (experimental)", True

        override = os.environ.get("AUDIOMASS_DEMUCS_DEVICE")
        if override:
            device = override
            label = override.upper()
            experimental = override != "cpu"

        cpu_count = max(1, os.cpu_count() or 2)
        threads = int(os.environ.get("AUDIOMASS_DEMUCS_THREADS", str(cpu_count)))
        # OpenMP oversubscription just adds contention; cap at physical cores.
        threads = min(max(1, threads), cpu_count)
        return {
            "device": device,
            "label": label,
            "experimental": experimental,
            "threads": threads,
        }
