"""wechat_login_qr — generate a WeChat bot login QR and poll for scan.

Ported from ``feishu_channel/server.py::_handle_wechat_login_qr``. The
Session 1 :class:`WeChatChannel` already exposes
``login_new_account`` / ``register_logged_in_client`` — but we cannot just
call ``login_new_account`` here because that helper drives both the QR
generation and the blocking poll inside one coroutine. The legacy behavior
we need to preserve is:

1. Return the QR image path **immediately**.
2. Poll for confirmation in a background ``asyncio.Task`` (2-minute timeout).
3. On confirmation, hook the fresh client into the channel so its listener
   starts right away.

So we replicate the legacy flow directly against the underlying
``ILinkClient``, but register the client via the Session 1
``WeChatChannel.register_logged_in_client`` once login completes.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def wechat_login_qr(wechat, account_id: str = "new", chat_id: str = "") -> dict:
    """Generate a QR, save it to ``/tmp/``, and background-poll for scan.

    ``wechat`` is a :class:`WeChatChannel`. If no WeChat channel is wired,
    returns an error.
    """
    if wechat is None:
        return {"status": "error", "message": "WeChat channel not available"}

    try:
        from ..channels.wechat.ilink import ILinkClient

        client = ILinkClient(
            base_url=wechat._base_url,
            cdn_url=wechat._cdn_url,
            state_dir=wechat._state_dir,
            account_id=account_id,
        )

        # Request a fresh QR
        qr_data = await client.get_qr_code()
        qrcode_id = qr_data.get("qrcode", "")
        qr_url = qr_data.get("qrcode_img_content", "")
        if not qrcode_id or not qr_url:
            await client.close()
            return {"status": "error", "message": "Failed to get QR code from iLink"}

        # Render QR to /tmp
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        qr_path = f"/tmp/wechat_login_{account_id}.png"
        img.save(qr_path)

        result: dict = {
            "status": "ok",
            "qr_image_path": qr_path,
            "qr_url": qr_url,
            "message": (
                f"QR code generated for account '{account_id}'. "
                "Scan with WeChat to login. QR expires in ~2 minutes."
            ),
        }
        if chat_id:
            result["sent_to"] = chat_id

        # Background poll
        async def _poll_and_register() -> None:
            start = time.time()
            while time.time() - start < 120:
                try:
                    status_data = await client.poll_qr_status(qrcode_id)
                except Exception:
                    continue
                status = status_data.get("status", "")

                if status == "confirmed":
                    client.token = status_data.get("bot_token", "")
                    client.bot_id = status_data.get("ilink_bot_id", "")
                    client.user_id = status_data.get("ilink_user_id", "")
                    if status_data.get("baseurl"):
                        client.base_url = status_data["baseurl"].rstrip("/")
                    client.account_id = account_id
                    client.save_auth()
                    logger.info(
                        "WeChat login OK: account=%s bot_id=%s user=%s",
                        account_id, client.bot_id[:12], client.user_id[:20],
                    )
                    # Hand off to the live WeChat channel so its listener starts.
                    wechat.register_logged_in_client(client)
                    return

                elif status == "expired":
                    logger.info(
                        "WeChat login QR expired for account %s", account_id
                    )
                    await client.close()
                    return

                elif status == "scaned_but_redirect":
                    rh = status_data.get("redirect_host", "")
                    if rh:
                        client.base_url = f"https://{rh}".rstrip("/")

            await client.close()
            logger.info("WeChat login timed out for account %s", account_id)

        asyncio.create_task(_poll_and_register())
        return result

    except Exception as e:
        logger.error("WeChat login QR failed: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
