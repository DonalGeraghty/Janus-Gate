"""Console logging helpers for Janus API."""

import logging
import os


def setup_logging(environment=None):
    environment = environment or os.environ.get("FLASK_ENV", "production")
    level = logging.DEBUG if environment == "development" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger("janus-api")


def get_logger(name, _environment=None):
    return logging.getLogger(name)


def get_janus_api_logger():
    return get_logger("janus-api")


def get_flask_app_logger():
    return get_logger("flask_app")


def get_access_logger():
    return get_logger("access")


def get_error_logger():
    return get_logger("error")


logger = setup_logging()
