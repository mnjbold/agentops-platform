"""AWS SES adapter (issue #27, provider stub).

TODO: implement when the SES region + IAM credentials are approved.
The interface is fixed; only ``send()`` and ``handle_inbound()`` need real
HTTP/SNS work. For now every call raises so the operator can see the gap
in the logs immediately if they accidentally flip ``EMAIL_PROVIDER=ses``.
"""
from __future__ import annotations

from typing import Any, Optional

from . import EmailProvider, register_provider


class SESProvider(EmailProvider):
    name = "ses"

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
        # TODO: SES requires boto3 + an IAM key. Sign the request with SigV4,
        # POST to https://email.{region}.amazonaws.com/v2/email/outbound-emails,
        # and map the response back to {"ok", "message_id", "error"}.
        raise NotImplementedError(
            "SES provider is not wired yet. Set EMAIL_PROVIDER=dev or implement this."
        )

    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        # TODO: SES inbound arrives via SNS → S3. The webhook would fetch
        # the S3 object, parse the raw RFC822, and normalise to the
        # standard dict shape.
        raise NotImplementedError("SES inbound parsing is not implemented.")


register_provider("ses", SESProvider)
