"""AudioMass processing plugins.

Importing this package registers the built-in capabilities with
``plugin_registry``. Add a new capability by dropping a module in this
package that subclasses :class:`AudioPlugin` and registers an instance, then
importing it here (e.g. an 'effects' plugin for a future effects chain).
"""

from plugins.base import AudioPlugin, CancelledError, PluginContext, PluginError
from plugins.registry import PluginRegistry, plugin_registry

# Built-in plugins (registration happens on import).
# NOTE: plain submodule imports here — the `from plugins import X` form fails
# on partially-initialized packages (Python 3.12).
import plugins.htdemucs_plugin  # noqa: E402,F401
import plugins.transcribe_plugin  # noqa: E402,F401
import plugins.analyze_plugin  # noqa: E402,F401
import plugins.waveform_plugin  # noqa: E402,F401

__all__ = [
    "AudioPlugin",
    "CancelledError",
    "PluginContext",
    "PluginError",
    "PluginRegistry",
    "plugin_registry",
]
