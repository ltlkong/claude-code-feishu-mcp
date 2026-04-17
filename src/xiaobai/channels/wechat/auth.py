"""QR code login flow for WeChat iLink bot. Ported from wechat_channel/auth.py."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .ilink import (
    ILinkClient,
    QR_CONFIRMED,
    QR_EXPIRED,
    QR_REDIRECT,
    QR_SCANNED,
    QR_WAIT,
)

logger = logging.getLogger(__name__)

QR_REFRESH_MAX = 3
QR_POLL_INTERVAL = 1.0


async def login(client: ILinkClient, qr_notify_fn=None) -> bool:
    """Run the full QR code login flow.

    Args:
        client: ILinkClient instance
        qr_notify_fn: Optional async callback(image_url) for external QR delivery

    Returns True on successful login.
    """
    if client.is_authed:
        # Verify token still works
        try:
            await client.get_config()
            logger.info("Existing WeChat token is valid")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.info("WeChat token expired, re-authenticating")
                client.token = ""
            else:
                raise

    refreshes = 0
    while refreshes <= QR_REFRESH_MAX:
        qr_data = await client.get_qr_code()
        qrcode_id = qr_data.get("qrcode", "")
        qr_img_url = qr_data.get("qrcode_img_content", "")

        if not qrcode_id:
            logger.error("Failed to get QR code: %s", qr_data)
            return False

        logger.info("QR code ready: %s", qrcode_id[:20])
        _print_qr_terminal(qr_img_url)

        if qr_notify_fn and qr_img_url:
            try:
                await qr_notify_fn(qr_img_url)
            except Exception as e:
                logger.warning("QR notify failed: %s", e)

        confirmed = await _poll_qr(client, qrcode_id, client.base_url)

        if confirmed:
            logger.info("WeChat login successful: bot_id=%s", client.bot_id[:12])
            return True

        refreshes += 1
        if refreshes <= QR_REFRESH_MAX:
            logger.info("QR expired, refreshing (%d/%d)", refreshes, QR_REFRESH_MAX)

    logger.error("QR login failed after %d refreshes", QR_REFRESH_MAX)
    return False


async def _poll_qr(client: ILinkClient, qrcode_id: str, base_url: str) -> bool:
    """Poll QR status until confirmed, expired, or error."""
    original_base = client.base_url

    while True:
        try:
            result = await client.poll_qr_status(qrcode_id)
        except (httpx.TimeoutException, httpx.ConnectError):
            await asyncio.sleep(QR_POLL_INTERVAL)
            continue

        status = result.get("status", "")

        if status == QR_WAIT:
            continue

        elif status == QR_SCANNED:
            logger.info("QR code scanned, waiting for confirmation...")
            continue

        elif status == QR_REDIRECT:
            redirect_host = result.get("redirect_host", "")
            if redirect_host:
                client.base_url = redirect_host.rstrip("/")
                logger.info("Redirected to: %s", client.base_url)
            continue

        elif status == QR_CONFIRMED:
            client.token = result.get("bot_token", "")
            client.bot_id = result.get("ilink_bot_id", "")
            client.user_id = result.get("ilink_user_id", "")
            if result.get("baseurl"):
                client.base_url = result["baseurl"].rstrip("/")
            client.save_auth()
            return True

        elif status == QR_EXPIRED:
            logger.info("QR code expired")
            client.base_url = original_base
            return False

        else:
            logger.warning("Unknown QR status: %s", status)
            await asyncio.sleep(QR_POLL_INTERVAL)


def _print_qr_terminal(qr_content: str) -> None:
    """Print QR code or URL to terminal."""
    try:
        import qrcode as qr_lib
        qr = qr_lib.QRCode(
            error_correction=qr_lib.constants.ERROR_CORRECT_L,
            box_size=1, border=1,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        logger.info("Install 'qrcode' package for terminal QR display")
        print(f"\nWeChat login QR URL:\n{qr_content}\n")
