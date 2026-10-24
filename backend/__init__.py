"""Nimbus voice-agent backend."""

import platform
import sys

if sys.platform == "win32":
    # Python's platform module asks WMI for the Windows version, and a wedged WMI service makes
    # that call block forever. The OpenAI SDK triggers it on first request (via truststore),
    # which hung every LLM call on this machine. Raising OSError makes platform fall back to
    # sys.getwindowsversion(), which needs no WMI.
    def _no_wmi(*_args, **_kwargs):
        raise OSError("WMI lookups disabled")

    platform._wmi_query = _no_wmi
