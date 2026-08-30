import logging


def configure_logging() -> None:
    """Configure concise process-wide logs without exposing user text or secrets."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
