"""VocalizeAI uvicorn entry point.

Exposes ``app`` for ``uvicorn vocalize.main:app`` and a thin ``main()``
helper for ``python -m vocalize``.
"""
import os

from vocalize.server import create_app

app = create_app()


def main() -> None:
    """Backwards-compatible uvicorn entry.

    Defaults match the pre-B1 ``main.py`` (``0.0.0.0:8080``) so any
    deployment that relied on the implicit bind keeps working without
    setting env vars. Dev runs explicitly pass ``--host 127.0.0.1 --port
    8000`` on the uvicorn CLI (see README), so the dev port (``8000``) is
    not the env default.
    """
    import uvicorn

    host = os.getenv("VOCALIZE_HOST", "0.0.0.0")
    port = int(os.getenv("VOCALIZE_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
