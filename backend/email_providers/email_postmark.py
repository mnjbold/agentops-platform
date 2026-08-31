"""Postmark adapter (issue #27, provider stub).

TODO: implement when the Postmark server token is approved. The
``/email`` endpoint takes a JSON body; inbound arrives via the
Inbound Webhook (raw RFC822 in ``RawEmail``).
"""
from __future__ import annotations

from typing import Any, Optional

from . import EmailProvider, register_provider


class PostmarkProvider(EmailProvider):
    name = "postmark"

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
        # TODO: POST https://api.postmarkapp.com/email with
        # X-Postmark-Server-Token: <token>; map 200 → ok, else error.
        raise NotImplementedError(
            "Postmark provider is not wired yet. Set EMAIL_PROVIDER=dev or implement this."
        )

    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        # TODO: Postmark inbound payload is JSON with From/To/Subject/
        # TextBody/HtmlBody. Map them to the standard dict shape.
        raise NotImplementedError("Postmark inbound parsing is not implemented.")


register_provider("postmark", PostmarkProvider)
