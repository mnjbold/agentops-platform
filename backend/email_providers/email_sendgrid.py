"""SendGrid adapter (issue #27, provider stub).

TODO: implement when the SendGrid API key is approved. The mail/send
endpoint takes a personalisation payload; inbound arrives via the
Event Webhook + Inbound Parse webhook.
"""
from __future__ import annotations

from typing import Any, Optional

from . import EmailProvider, register_provider


class SendGridProvider(EmailProvider):
    name = "sendgrid"

    def send(
        self,
        *,
        to: str,
        from_addr: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        # TODO: POST https://api.sendgrid.com/v3/mail/send with
        # Bearer SG.xxxxx; map 202 → ok, 4xx/5xx → error.
        raise NotImplementedError(
            "SendGrid provider is not wired yet. Set EMAIL_PROVIDER=dev or implement this."
        )

    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        # TODO: parse SendGrid Inbound Parse multipart/form-data.
        raise NotImplementedError("SendGrid inbound parsing is not implemented.")


register_provider("sendgrid", SendGridProvider)
