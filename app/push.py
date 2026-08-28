"""
MUFCA v4.0 — Push Notifications (Firebase Cloud Messaging)

Sends push notifications to registered Android devices — the counterpart to
Discord messages about signals/TP1, but arrives even when the app is closed
(unlike the WebSocket, which only lives while the screen is open).

Initialization is lazy and safe: if firebase-credentials.json isn't placed on
disk (Atom hasn't set up Firebase yet), push simply doesn't send, without
crashing the bot — same as how WEB_USERNAME/WEB_PASSWORD are optional-with-warning.
"""

import logging
import datetime
import threading
from typing import Optional, Dict, List

import config as _cfg

logger = logging.getLogger(__name__)

_firebase_app = None
_firebase_unavailable_logged = False
# 🆕 FIX: send_push() is called via asyncio.to_thread from bot.py's scanner,
# and _get_firebase_app() can also be reached from a concurrent web API
# request (e.g. /api/devices/test-push) on a different thread. Both could
# pass the `if _firebase_app is not None` check before either one finishes
# initializing, and the second firebase_admin.initialize_app(cred) call
# raises ValueError ("The default Firebase app already exists"). This lock
# makes the lazy init atomic across threads.
_firebase_init_lock = threading.Lock()


def _get_firebase_app():
    """Initializes the Firebase Admin SDK exactly once (lazily, on the first real send)."""
    global _firebase_app, _firebase_unavailable_logged

    if _firebase_app is not None:
        return _firebase_app

    with _firebase_init_lock:
        # Re-check inside the lock: another thread may have already
        # initialized it while we were waiting to acquire the lock.
        if _firebase_app is not None:
            return _firebase_app

        import os
        if not os.path.isfile(_cfg.FIREBASE_CREDENTIALS_PATH):
            if not _firebase_unavailable_logged:
                logger.warning(
                    f"[PUSH] Firebase credentials not found at path {_cfg.FIREBASE_CREDENTIALS_PATH} — "
                    f"Android push notifications are disabled. Signals/TP1 will still arrive in Discord "
                    f"and on the web dashboard (WebSocket) as usual."
                )
                _firebase_unavailable_logged = True
            return None

        try:
            import firebase_admin
            from firebase_admin import credentials
            cred = credentials.Certificate(_cfg.FIREBASE_CREDENTIALS_PATH)
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("[PUSH] Firebase Admin SDK initialized")
            return _firebase_app
        except Exception as e:
            logger.error(f"[PUSH] Failed to initialize Firebase: {e}", exc_info=True)
            return None


def register_device(token: str, device_name: Optional[str] = None) -> dict:
    """
    Registers/updates a device's FCM token. Called from /api/devices/register —
    both on the app's first setup and on every token rotation (onNewToken on
    the Android side), which can happen at any time independent of a
    reinstall.

    🆕 FIX: records used to be added keyed by `token`, so on a token rotation
    (or a reinstall on the same device) the old record with the previous
    token wasn't removed — it only "went stale" once send_push actually got
    an UNREGISTERED response from FCM for it, which could take a long time to
    happen. This let duplicates of the same physical device pile up in the
    list ("Xiaomi ..." x2), and some pushes would go out to a dead token and
    show up as "failed". Now, on registration, we first remove any existing
    records with the same device_name (except the new token itself, if it was
    already known) — for personal use, device_name is reliable enough as an
    identifier for a physical device.

    🆕 FIX: device_name is Optional in the API — if it's not supplied, we
    fall back to the generic "Android device" label. That label is NOT
    device-identifying: if two different physical devices both register
    without a name, they'd both resolve to the same fallback, and this
    function would treat the second device's registration as a "stale
    duplicate" of the first and delete its token. So the dedup-by-name pass
    below only runs when a real device_name was actually provided.
    """
    devices = _cfg.load_devices()
    resolved_name = device_name or "Android device"

    stale_tokens = [
        t for t, info in devices.items()
        if t != token and device_name and info.get("device_name") == resolved_name
    ]
    for t in stale_tokens:
        devices.pop(t, None)
    if stale_tokens:
        logger.info(
            f"[PUSH] Removed {len(stale_tokens)} stale record(s) for device "
            f"'{resolved_name}' (new token from the same rotation)"
        )

    devices[token] = {
        "device_name": resolved_name,
        "registered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _cfg.save_devices(devices)
    logger.info(f"[PUSH] Device registered: {device_name or token[:16] + '...'}")
    return devices[token]


def unregister_device(token: str) -> bool:
    devices = _cfg.load_devices()
    if token not in devices:
        return False
    del devices[token]
    _cfg.save_devices(devices)
    return True


def send_push(title: str, body: str, data: Optional[Dict[str, str]] = None) -> Dict:
    """
    Sends a push to all registered devices. A synchronous function — the
    caller (bot.py) should invoke it via asyncio.to_thread so it doesn't
    block the event loop (firebase-admin makes blocking HTTP requests).

    Invalid/stale tokens (device uninstalled the app, etc.) are automatically
    cleaned up from devices.json based on FCM's response.
    """
    app = _get_firebase_app()
    if app is None:
        return {"sent": 0, "failed": 0, "skipped": "firebase_not_configured"}

    devices = _cfg.load_devices()
    tokens = list(devices.keys())
    if not tokens:
        return {"sent": 0, "failed": 0, "skipped": "no_devices_registered"}

    from firebase_admin import messaging

    # 🆕 FIX: FCM caps MulticastMessage at 500 tokens per request — without
    # batching, sending would fail outright once there were >500 registered
    # devices. For personal use this is purely a defensive margin for the future.
    _FCM_BATCH_SIZE = 500
    total_sent = 0
    total_failed = 0
    stale_tokens: List[str] = []

    for batch_start in range(0, len(tokens), _FCM_BATCH_SIZE):
        batch = tokens[batch_start:batch_start + _FCM_BATCH_SIZE]

        # 🆕 FIX: deliberately NOT using a notification+data hybrid. Hybrid
        # messages get shown automatically by the system when the app is
        # backgrounded/killed, bypassing onMessageReceived() on Android —
        # meaning our intent extras (ticker/tf) never get attached, and
        # tapping such an auto-generated notification couldn't open the
        # right signal on the chart. Data-only guarantees the Android client
        # itself always gets control and builds the notification with the
        # correct deep link — always, not just while the app is open on screen.
        message = messaging.MulticastMessage(
            data={"title": title, "body": body, **{k: str(v) for k, v in (data or {}).items()}},
            tokens=batch,
            android=messaging.AndroidConfig(priority="high"),
        )

        try:
            response = messaging.send_each_for_multicast(message)
        except Exception as e:
            logger.error(f"[PUSH] Batch send error: {e}", exc_info=True)
            total_failed += len(batch)
            continue

        total_sent += response.success_count
        total_failed += response.failure_count

        # Clean up stale tokens (device unsubscribed / uninstalled the app)
        for idx, result in enumerate(response.responses):
            if not result.success:
                err = getattr(result.exception, "code", "") or str(result.exception)
                if "UNREGISTERED" in str(err) or "invalid-registration-token" in str(err).lower():
                    stale_tokens.append(batch[idx])

    if stale_tokens:
        devices = _cfg.load_devices()
        for t in stale_tokens:
            devices.pop(t, None)
        _cfg.save_devices(devices)
        logger.info(f"[PUSH] Removed {len(stale_tokens)} stale token(s)")

    logger.info(f"[PUSH] Sent: {total_sent}/{len(tokens)} | title={title!r}")
    return {"sent": total_sent, "failed": total_failed}
