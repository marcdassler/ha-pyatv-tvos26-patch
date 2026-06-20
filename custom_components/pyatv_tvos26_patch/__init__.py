"""Runtime monkeypatch for pyatv Companion protocol on tvOS 26.x.

Two workarounds, applied at HA startup:

1. **Connect-handshake fix** (v1.0) — adds the missing `TVRCSessionStart`
   command to `CompanionAPI.connect`. Without it, tvOS 26.x silently drops
   subsequent Companion commands (`FetchAttentionState`,
   `FetchLaunchableApplicationsEvent`, `turn_off`). Upstream: pyatv #2845 /
   PR #2847 (merged, awaiting release).

2. **Push-subscription heartbeat** (v1.1) — fixes the SEPARATE, deeper
   failure the handshake fix does NOT cover: after a healthy connect, the
   `_iMC` media-remote event subscription goes *silent mid-session* without
   the connection erroring, so HA's `media_player` freezes at its last
   state (`playing`) for hours. pyatv never notices, so it never reconnects.
   Live-reported on Marc's Apple TV 4K repeatedly (9–73 min freezes).

   The heartbeat: a per-connection background task timestamps every push
   (via `event_received`). When pushes go silent past a threshold it sends
   an active probe (`system_info`); if the command channel is alive but the
   subscription is quiet, it **re-subscribes `_iMC`** — re-initiating the
   push WITHOUT a disconnect (no entity flap, no listener race). If the
   probe fails (connection truly dead) it does nothing and leaves recovery
   to the downstream HA-side reload (`appletv_mgmt` v0.19.3). Fail-safe:
   self-disables after repeated errors; never forces a disconnect.

This integration is **temporary**: remove it once pyatv ships a release
with PR #2847 AND the subscription-drop is fixed upstream.

Configuration (no options):
    # configuration.yaml
    pyatv_tvos26_patch:
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "pyatv_tvos26_patch"

# Marker attribute set on the patched method so we can tell whether
# the patch has already been applied (e.g. on a config reload).
_PATCH_MARKER = "_pyatv_tvos26_patch_applied"
_EVENT_PATCH_MARKER = "_pyatv_tvos26_event_patch_applied"

# ---- Heartbeat tuning -----------------------------------------------------
# Loop cadence. Cheap (a couple of attribute reads) when healthy.
HEARTBEAT_CHECK_S: float = 120.0
# Push-silence past this -> probe + (if alive) re-subscribe. 10 min is past
# the ~7 min median healthy push gap on this device, so steady playback that
# happens to be quiet for a few minutes is NOT disturbed.
HEARTBEAT_SILENCE_S: float = 600.0
# Active-probe timeout. A live connection answers _systemInfo in well under 1s.
HEARTBEAT_PROBE_TIMEOUT_S: float = 5.0
# Don't re-subscribe more often than this (avoid hammering if a re-subscribe
# doesn't immediately revive the push — the HA-reload backstop handles the
# truly-stuck case).
HEARTBEAT_RESUB_COOLDOWN_S: float = 300.0
# Exit the task after the connection has been gone (protocol is None) for
# this many consecutive checks — covers integration unload without leaking a
# task, while tolerating the brief None during a normal reconnect.
HEARTBEAT_GONE_EXITS: int = 3
# Self-disable after this many consecutive UNEXPECTED loop errors.
HEARTBEAT_MAX_CONSEC_ERRORS: int = 3

# Per-instance attribute names (stashed on the CompanionAPI instance).
_HB_TASK = "_pyatv_tvos26_hb_task"
_HB_LAST_EVENT = "_pyatv_tvos26_hb_last_event_at"   # time.monotonic()
_HB_LAST_RESUB = "_pyatv_tvos26_hb_last_resub_at"   # time.monotonic() | None

ACTION_NOOP = "noop"
ACTION_COOLDOWN = "cooldown"
ACTION_ACT = "act"


def decide_heartbeat_action(
    event_silence_s: float, since_last_resub_s: float | None
) -> str:
    """Pure decision: given how long the push has been silent and how long
    since the last re-subscribe, decide what the heartbeat should do.

      - ACTION_NOOP: recent push -> connection healthy, do nothing.
      - ACTION_COOLDOWN: silent, but we re-subscribed recently -> wait.
      - ACTION_ACT: silent past threshold and out of cooldown -> probe +
        (if the connection is alive) re-subscribe `_iMC`.

    Pure + HA-free so it can be unit-tested without a live connection.
    """
    if event_silence_s < HEARTBEAT_SILENCE_S:
        return ACTION_NOOP
    if since_last_resub_s is not None and since_last_resub_s < HEARTBEAT_RESUB_COOLDOWN_S:
        return ACTION_COOLDOWN
    return ACTION_ACT


async def _heartbeat_loop(api) -> None:
    """Per-connection heartbeat. Re-initiates a silent `_iMC` push.

    Bound to a single CompanionAPI instance. Exits cleanly when the
    connection is permanently gone (integration unload) or after repeated
    unexpected errors. Never raises out; never forces a disconnect.
    """
    consec_gone = 0
    consec_err = 0
    _LOGGER.warning(
        "pyatv tvOS 26 push-subscription heartbeat ARMED "
        "(check=%.0fs, silence=%.0fs)",
        HEARTBEAT_CHECK_S,
        HEARTBEAT_SILENCE_S,
    )
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_CHECK_S)

            # Connection torn down? Tolerate the brief None during a normal
            # reconnect, but exit if it stays gone (unload).
            if getattr(api, "_protocol", None) is None:
                consec_gone += 1
                if consec_gone >= HEARTBEAT_GONE_EXITS:
                    _LOGGER.debug(
                        "heartbeat: connection gone — exiting task"
                    )
                    return
                continue
            consec_gone = 0

            now = time.monotonic()
            last_event = getattr(api, _HB_LAST_EVENT, now)
            silence = now - last_event
            last_resub = getattr(api, _HB_LAST_RESUB, None)
            since_resub = (now - last_resub) if last_resub is not None else None

            action = decide_heartbeat_action(silence, since_resub)
            if action != ACTION_ACT:
                consec_err = 0
                continue

            # Silent past threshold -> probe the command channel.
            _LOGGER.warning(
                "heartbeat: _iMC push silent for %.0fs — probing connection",
                silence,
            )
            try:
                await asyncio.wait_for(
                    api.system_info(), timeout=HEARTBEAT_PROBE_TIMEOUT_S
                )
            except Exception as probe_err:  # noqa: BLE001
                # Command channel dead too -> a re-subscribe wouldn't reach
                # the device. Leave recovery to the HA-side reload backstop
                # (appletv_mgmt v0.19.3). Not a heartbeat bug.
                _LOGGER.warning(
                    "heartbeat: probe FAILED (%s) — connection dead; leaving "
                    "recovery to the HA reload backstop",
                    probe_err,
                )
                consec_err = 0
                continue

            # Command channel ALIVE but the push is silent -> the `_iMC`
            # subscription has gone quiet. Re-initiate it (dereg + reg) with
            # NO disconnect. Fire-and-forget opack; XID-keyed so it can't
            # collide with HA's concurrent commands.
            try:
                await api.unsubscribe_event("_iMC")
                await api.subscribe_event("_iMC")
                setattr(api, _HB_LAST_RESUB, time.monotonic())
                _LOGGER.warning(
                    "heartbeat: connection alive but _iMC silent %.0fs — "
                    "RE-SUBSCRIBED _iMC to re-initiate the push",
                    silence,
                )
            except Exception as resub_err:  # noqa: BLE001
                _LOGGER.warning(
                    "heartbeat: re-subscribe failed (%s) — leaving recovery "
                    "to the HA reload backstop",
                    resub_err,
                )
            consec_err = 0

        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            consec_err += 1
            _LOGGER.error(
                "heartbeat loop error (%d/%d): %s",
                consec_err,
                HEARTBEAT_MAX_CONSEC_ERRORS,
                err,
            )
            if consec_err >= HEARTBEAT_MAX_CONSEC_ERRORS:
                _LOGGER.error(
                    "heartbeat: too many consecutive errors — disabling "
                    "heartbeat for this connection"
                )
                return


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Apply the Companion-protocol monkeypatches once at HA startup.

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

    # ---- Patch 2: event_received timestamp (idempotent) -------------------
    # Stamp the time of every push so the heartbeat can measure silence.
    # Wraps the original; marker-guarded so a config reload won't double-wrap.
    if not getattr(CompanionAPI.event_received, _EVENT_PATCH_MARKER, False):
        _orig_event_received = CompanionAPI.event_received

        def patched_event_received(self, event_name, data):
            try:
                setattr(self, _HB_LAST_EVENT, time.monotonic())
            except Exception:  # noqa: BLE001 — never break event delivery
                pass
            return _orig_event_received(self, event_name, data)

        setattr(patched_event_received, _EVENT_PATCH_MARKER, True)
        CompanionAPI.event_received = patched_event_received

    # ---- Patch 1: connect handshake + heartbeat spawn (idempotent) --------
    if getattr(CompanionAPI.connect, _PATCH_MARKER, False):
        _LOGGER.debug("pyatv tvOS 26 patch already applied — no-op")
        return True

    async def patched_pyatv_companion_connect(self):
        """Patched `CompanionAPI.connect` adding `TVRCSessionStart` and
        arming the push-subscription heartbeat."""
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

        # ---- arm the heartbeat (v1.1) ----
        # Seed the last-event clock so silence is measured from connect.
        setattr(self, _HB_LAST_EVENT, time.monotonic())
        existing = getattr(self, _HB_TASK, None)
        if existing is None or existing.done():
            try:
                setattr(
                    self, _HB_TASK, self.core.loop.create_task(_heartbeat_loop(self))
                )
            except Exception as hb_err:  # noqa: BLE001 — never break connect
                _LOGGER.error("heartbeat: failed to arm (%s)", hb_err)

    setattr(patched_pyatv_companion_connect, _PATCH_MARKER, True)
    CompanionAPI.connect = patched_pyatv_companion_connect

    _LOGGER.warning(
        "pyatv tvOS 26 Companion-protocol patch APPLIED (handshake + "
        "push-subscription heartbeat). Temporary workaround for "
        "https://github.com/postlund/pyatv/issues/2845 — remove once pyatv "
        "ships the connect fix AND the subscription-drop fix."
    )
    return True
