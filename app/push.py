"""
MUFCA v4.0 — Push Notifications (Firebase Cloud Messaging)

Отправляет push-уведомления на зарегистрированные Android-устройства —
аналог Discord-сообщений о сигналах/TP1, но приходит даже когда приложение
закрыто (в отличие от WebSocket, который живёт только пока экран открыт).

Инициализация ленивая и безопасная: если firebase-credentials.json не
положен на диск (Атом ещё не настроил Firebase), push просто не отправляется,
без падения бота — так же как WEB_USERNAME/WEB_PASSWORD optional-with-warning.
"""

import logging
import datetime
from typing import Optional, Dict, List

import config as _cfg

logger = logging.getLogger(__name__)

_firebase_app = None
_firebase_unavailable_logged = False


def _get_firebase_app():
    """Инициализирует Firebase Admin SDK один раз (лениво, при первой реальной отправке)."""
    global _firebase_app, _firebase_unavailable_logged

    if _firebase_app is not None:
        return _firebase_app

    import os
    if not os.path.isfile(_cfg.FIREBASE_CREDENTIALS_PATH):
        if not _firebase_unavailable_logged:
            logger.warning(
                f"[PUSH] Firebase credentials не найдены по пути {_cfg.FIREBASE_CREDENTIALS_PATH} — "
                f"push-уведомления на Android отключены. Signals/TP1 продолжат приходить в Discord "
                f"и на веб-дашборд (WebSocket) как обычно."
            )
            _firebase_unavailable_logged = True
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials
        cred = credentials.Certificate(_cfg.FIREBASE_CREDENTIALS_PATH)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("[PUSH] Firebase Admin SDK инициализирован")
        return _firebase_app
    except Exception as e:
        logger.error(f"[PUSH] Не удалось инициализировать Firebase: {e}", exc_info=True)
        return None


def register_device(token: str, device_name: Optional[str] = None) -> dict:
    """
    Регистрирует/обновляет FCM-токен устройства. Вызывается из /api/devices/register —
    как при первой настройке приложения, так и при каждой ротации токена (onNewToken
    на Android-стороне), которая может произойти в любой момент независимо от
    переустановки приложения.

    🆕 FIX: раньше запись добавлялась по ключу `token`, поэтому при ротации токена
    (или переустановке на то же устройство) старая запись с прежним токеном не
    удалялась — она "протухала" только после того, как send_push реально получал
    от FCM UNREGISTERED, что могло не происходить долго. В итоге в списке
    накапливались дубликаты одного и того же физического устройства ("Xiaomi ... "
    x2), и часть пушей уходила на мёртвый токен как "failed". Теперь при регистрации
    сначала убираем все существующие записи с тем же device_name (кроме самого
    нового токена, если он уже был знаком) — для персонального использования
    device_name достаточно надёжен как идентификатор физического устройства.
    """
    devices = _cfg.load_devices()
    resolved_name = device_name or "Android device"

    stale_tokens = [
        t for t, info in devices.items()
        if t != token and info.get("device_name") == resolved_name
    ]
    for t in stale_tokens:
        devices.pop(t, None)
    if stale_tokens:
        logger.info(
            f"[PUSH] Убрал {len(stale_tokens)} устаревших записей устройства "
            f"'{resolved_name}' (новый токен той же ротации)"
        )

    devices[token] = {
        "device_name": resolved_name,
        "registered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _cfg.save_devices(devices)
    logger.info(f"[PUSH] Устройство зарегистрировано: {device_name or token[:16] + '...'}")
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
    Рассылает push всем зарегистрированным устройствам. Синхронная функция —
    вызывающий код (bot.py) должен звать её через asyncio.to_thread, чтобы не
    блокировать event loop (firebase-admin делает блокирующие HTTP-запросы).

    Невалидные/протухшие токены (устройство удалило приложение и т.п.)
    автоматически вычищаются из devices.json по ответу FCM.
    """
    app = _get_firebase_app()
    if app is None:
        return {"sent": 0, "failed": 0, "skipped": "firebase_not_configured"}

    devices = _cfg.load_devices()
    tokens = list(devices.keys())
    if not tokens:
        return {"sent": 0, "failed": 0, "skipped": "no_devices_registered"}

    from firebase_admin import messaging

    # 🆕 FIX: FCM ограничивает MulticastMessage 500 токенами за один запрос —
    # без батчинга отправка упала бы целиком при >500 зарегистрированных
    # устройств. Для личного использования это чисто защитный запас на будущее.
    _FCM_BATCH_SIZE = 500
    total_sent = 0
    total_failed = 0
    stale_tokens: List[str] = []

    for batch_start in range(0, len(tokens), _FCM_BATCH_SIZE):
        batch = tokens[batch_start:batch_start + _FCM_BATCH_SIZE]

        # 🆕 FIX: намеренно НЕ используем notification+data гибрид. Гибридные сообщения
        # при свёрнутом/убитом приложении показываются системой автоматически, минуя
        # onMessageReceived() на Android — а значит без наших intent-экстра (ticker/tf),
        # и тап по такому автосозданному уведомлению не мог бы открыть нужный сигнал на
        # графике. Data-only гарантирует, что Android-клиент сам получает управление и
        # сам строит уведомление с правильным deep-link — всегда, а не только когда
        # приложение открыто на экране.
        message = messaging.MulticastMessage(
            data={"title": title, "body": body, **{k: str(v) for k, v in (data or {}).items()}},
            tokens=batch,
            android=messaging.AndroidConfig(priority="high"),
        )

        try:
            response = messaging.send_each_for_multicast(message)
        except Exception as e:
            logger.error(f"[PUSH] Ошибка отправки батча: {e}", exc_info=True)
            total_failed += len(batch)
            continue

        total_sent += response.success_count
        total_failed += response.failure_count

        # Чистим протухшие токены (устройство отписалось / удалило приложение)
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
        logger.info(f"[PUSH] Убрал {len(stale_tokens)} протухших токенов")

    logger.info(f"[PUSH] Отправлено: {total_sent}/{len(tokens)} | title={title!r}")
    return {"sent": total_sent, "failed": total_failed}
