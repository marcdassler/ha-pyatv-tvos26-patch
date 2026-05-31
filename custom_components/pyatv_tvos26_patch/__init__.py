"""Runtime monkeypatch for pyatv Companion protocol on tvOS 26.x.

Fixes the chronic "Command FetchAttentionState failed" /
"Command FetchLaunchableApplicationsEvent failed" errors that pyatv
0.17.0 emits against Apple TV 4K (gen 3) running tvOS 26.4 / 26.5.

Upstream context:
- pyatv issue:  https://github.com/postlund/pyatv/issues/2845
- pyatv fix:    https://github.com/postlund/pyatv/pull/2847  (MERGED, awaiting release)
- HA blocker:   https://github.com/home-assistant/core/issues/170075

The patch adds a missing `TVRCSessionStart` command to the Companion
protocol's connect sequence. Without it, tvOS 26.x silently drops
subsequent Companion commands — `FetchAttentionState`,
`FetchLaunchableApplicationsEvent`, and (by extension) `turn_off`.

This integration is **temporary**: remove it from configuration.yaml +
delete the directory once pyatv ships a release containing PR #2847
(expected pyatv 0.17.1 or later — check the bundled version in HA via
`pip show pyatv` inside the homeassistant container).

The monkeypatch is idempotent. It tags `CompanionAPI.connect` with
`_pyatv_tvos26_patch_applied = True` after the first application so a
config reload doesn't reapply it.

Source of the patch: contributor `albaintor` on the issue thread,
verified working by `TheFab21` and `kroehre`.

Configuration:
    # configuration.yaml
    pyatv_tvos26_patch:

No options — loading the integration applies the patch globally.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "pyatv_tvos26_patch"

# Marker attribute set on the patched method so we can tell whether
# the patch has already been applied (e.g. on a config reload).
_PATCH_MARKER = "_pyatv_tvos26_patch_applied"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Apply the Companion-protocol monkeypatch once at HA startup.

    Returns True even when patching fails — we should never block HA
    startup over this. Failures are loud-logged so the user knows.
    """
    try:
        import pyatv.auth.hap_srp  # noqa: F401
        import pyatv.protocols.companion.api
        import pyatv.protocols.companion.connection
        import pyatv.protocols.companion.protocol
    except ImportError as err:
        _LOGGER.error(
            "pyatv not importable: %s — cannot apply tvOS 26.x patch",
            err,
        )
        return True  # don't block HA

    CompanionAPI = pyatv.protocols.companion.api.CompanionAPI

    if getattr(CompanionAPI.connect, _PATCH_MARKER, False):
        _LOGGER.debug("pyatv tvOS 26 patch already applied — no-op")
        return True

    async def patched_pyatv_companion_connect(self):
        """Patched `CompanionAPI.connect` adding `TVRCSessionStart`.

        Verbatim from issue #2845 comment by `albaintor` (the author of
        PR #2847). Adds one line — the `TVRCSessionStart` command —
        between `_session_start()` and `_text_input_start()`.
        """
        # pylint: disable=protected-access
        if self._protocol:
            return
        self._connection = (
            pyatv.protocols.companion.connection.CompanionConnection(
                self.core.loop,
                str(self.core.config.address),
                self.core.service.port,
                self.core.device_listener,
            )
        )
        self._protocol = pyatv.protocols.companion.protocol.CompanionProtocol(
            self._connection,
            pyatv.auth.hap_srp.SRPAuthHandler(),
            self.core.service,
        )
        self._protocol.listener = self
        await self._protocol.start()
        await self.system_info()
        await self._touch_start()
        await self._session_start()
        # ↓↓ THE FIX — missing in pyatv 0.17.0; required by tvOS 26.x.
        await self._send_command(
            "TVRCSessionStart", {"ProtocolVersionKey": "1.2"}
        )
        # ↑↑
        await self._text_input_start()
        await self.subscribe_event("_iMC")

    setattr(patched_pyatv_companion_connect, _PATCH_MARKER, True)
    CompanionAPI.connect = patched_pyatv_companion_connect

    _LOGGER.warning(
        "pyatv tvOS 26 Companion-protocol patch APPLIED. This is a "
        "temporary workaround for https://github.com/postlund/pyatv/issues/2845 "
        "— remove this integration once pyatv ships a release containing "
        "PR #2847 (currently merged but unreleased)."
    )
    return True
