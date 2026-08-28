"""Entry point: ``python -m webhooks`` runs the webhook server."""
from webhooks.server import main

if __name__ == "__main__":
    raise SystemExit(main())
