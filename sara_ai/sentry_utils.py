import os
import logging

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None


def init_sentry(service_name: str = "sara_ai"):
    """
    Initialize Sentry error tracking if DSN is available.
    Logs either 'success' or 'skipped' via root logger.
    """
    logger = logging.getLogger()

    dsn = os.getenv("SENTRY_DSN", "").strip()

    if not dsn:
        logger.info(
            {
                "service": "sentry",
                "event": "init",
                "status": "skipped",
                "message": "SENTRY_DSN not set; skipping initialization",
            }
        )
        return

    if sentry_sdk is None:
        logger.warning(
            {
                "service": "sentry",
                "event": "init",
                "status": "failed",
                "message": "sentry_sdk not installed; cannot initialize",
            }
        )
        return

    try:
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
            environment=os.getenv("SARA_ENV", "development"),
        )
        logger.info(
            {
                "service": "sentry",
                "event": "init",
                "status": "success",
                "message": f"Sentry initialized for service={service_name}",
            }
        )
    except Exception as e:
        logger.error(
            {
                "service": "sentry",
                "event": "init",
                "status": "error",
                "message": f"Sentry initialization failed: {e}",
            }
        )
