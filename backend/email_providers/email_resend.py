"""Resend adapter (issue #27, provider stub).

TODO: implement when the Resend API key is approved. The
``POST /v1/emails`` endpoint takes a JSON body with from/to/subject/html;
inbound arrives via the Resend Inbound (webhook with FormData).
"""
from __future__ import annotations

from typing import Any, Optional

from . import EmailProvider, register_provider


class ResendProvider(EmailProvider):
    name = "resend"

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
        # TODO: POST https://api.resend.com/emails with
        # Authorization: Bearer re_xxx; map 200/202 → ok, else error.
        raise NotImplementedError(
            "Resend provider is not wired yet. Set EMAIL_PROVIDER=dev or implement this."
        )

    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        # TODO: Resend inbound is multipart/form-data; map to the
        # standard dict shape.
        raise NotImplementedError("Resend inbound parsing is not implemented.")


register_provider("resend", ResendProvider)
