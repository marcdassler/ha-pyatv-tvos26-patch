# ha-pyatv-tvos26-patch

A tiny Home Assistant custom-integration that patches **pyatv** at runtime to work around two bugs between **tvOS 26.x** and **pyatv ≤ 0.17.0** on Apple TV 4K (gen 3, model `AppleTV14,1`).

> **v1.1** adds a second workaround — a **push-subscription heartbeat**. The v1.0 handshake fix (below) cures the *connect-time* errors, but a deeper failure remains: after a healthy connect, the `_iMC` media-remote event subscription goes **silent mid-session** without the connection erroring, so HA's `media_player` freezes at its last state (`playing`) for hours. The heartbeat timestamps every push; when one goes silent past ~10 min it sends an active probe and, if the command channel is alive, **re-subscribes `_iMC`** to re-initiate the push — no disconnect, no entity flap. If the connection is genuinely dead it does nothing and leaves recovery to the downstream HA-side reload. Self-disables on repeated error; never forces a disconnect. See `decide_heartbeat_action` + `_heartbeat_loop` in `__init__.py`.

## The bug it fixes

Without the patch, Home Assistant's `apple_tv` integration logs these every time it (re)connects to your Apple TV:

```
ERROR [pyatv.protocols.companion] Could not fetch SystemStatus, power_state will not work
    (Command FetchAttentionState failed)
ERROR [homeassistant.components.apple_tv.media_player] Failed to update app list
    pyatv.exceptions.ProtocolError: Command FetchLaunchableApplicationsEvent failed
```

Downstream effects observed in the wild:
- `media_player.heimkinoaaa.last_updated` stops refreshing, sometimes for hours, while the cached state stays `playing` — your usage-tracking integrations (like [`appletv_mgmt`](https://github.com/marcdassler/ha-appletv-mgmt)) think the kid is still watching long after the TV is off.
- `media_player.turn_off` calls against the Apple TV fail silently (Companion protocol can't reach the device through the broken connect handshake).
- App-list lookups return empty, so launching apps via HA service calls is broken.

## Upstream status

| Where | What |
|---|---|
| pyatv issue | https://github.com/postlund/pyatv/issues/2845 (open) |
| pyatv fix PR | https://github.com/postlund/pyatv/pull/2847 (**merged**, not yet in a release as of pyatv 0.17.0) |
| HA tracking issue | https://github.com/home-assistant/core/issues/170075 |

The root cause: tvOS 26.x added a requirement that the Companion connect handshake send `TVRCSessionStart` between `_session_start()` and `_text_input_start()`. pyatv ≤ 0.17.0 doesn't send it, so subsequent commands silently time out. Fix is one line.

## What this integration does

Applies a runtime monkeypatch at HA startup that adds the missing `TVRCSessionStart` command. The patch lives entirely on the local HA instance — no fork of pyatv needed.

The patch is idempotent (won't be reapplied on config reloads) and tagged with a marker attribute so you can verify it landed.

## Install

### Manual

1. Copy `custom_components/pyatv_tvos26_patch/` to your HA `config/custom_components/` directory.
2. Add to `configuration.yaml`:
   ```yaml
   pyatv_tvos26_patch:
   ```
3. Restart Home Assistant.
4. Watch the log for a single WARNING-level line:
   ```
   WARNING (MainThread) [custom_components.pyatv_tvos26_patch]
       pyatv tvOS 26 Companion-protocol patch APPLIED.
   ```

That's the entire installation. No UI configuration.

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo URL, category "Integration"
3. Install
4. Add `pyatv_tvos26_patch:` to `configuration.yaml`
5. Restart HA

## Verify it works

After installing, the `apple_tv` integration's reload should produce **zero** of these log lines:

```
ERROR [pyatv.protocols.companion] Could not fetch SystemStatus
ERROR [homeassistant.components.apple_tv.media_player] Failed to update app list
```

If you're still seeing them, double-check the patch's WARNING line appears in HA's logs.

## When to remove this

Once pyatv ships a release containing PR #2847 (probably pyatv 0.17.1 or 0.18.0) AND your Home Assistant has been updated to that version of pyatv, this integration is no longer needed:

1. Remove the `pyatv_tvos26_patch:` line from `configuration.yaml`
2. Delete `custom_components/pyatv_tvos26_patch/`
3. Restart HA

The patch's override would otherwise stay applied — harmless, just identical to upstream — but cleaner to remove.

You can check pyatv's bundled version in the homeassistant Docker container with:

```bash
sudo docker exec homeassistant sh -c "pip show pyatv | head -3"
```

## Why a custom integration?

The runtime monkeypatch is a single function override. Other options were considered and rejected:

- **Vendoring a forked pyatv**: too much code to keep updated.
- **Patching pyatv via `pip install` from the PR branch**: fragile against HA upgrades; HA bundles its own pyatv.
- **Inlining the patch in another integration** (like [`appletv_mgmt`](https://github.com/marcdassler/ha-appletv-mgmt)): couples unrelated integrations.

A separate custom_component is the minimum-surface-area workaround that's easy to remove later.

## Credits

The patch code is verbatim from [issue #2845's comment](https://github.com/postlund/pyatv/issues/2845#issuecomment-3098023567) by contributor **albaintor**, the author of PR #2847. Verified working by users `TheFab21` and `kroehre` on the same thread.

This repo just wraps it in a Home Assistant integration.

## License

MIT — see LICENSE.
