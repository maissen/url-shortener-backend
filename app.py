import logging
import os
import sys

from flask import Flask, jsonify, request

# Config from environment variables — no hardcoded values
APP_NAME    = os.environ.get("APP_NAME", "flask-backend")
APP_ENV     = os.environ.get("APP_ENV", "production")   # "development" | "production"
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
PORT        = int(os.environ.get("PORT", 8080))

# ---------------------------------------------------------------------------
# Logging — DEBUG in development, INFO in production.
# Writes to stdout so Docker / any log collector picks it up automatically.
# ---------------------------------------------------------------------------
LOG_LEVEL = logging.DEBUG if APP_ENV == "development" else logging.INFO

logging.basicConfig(
    stream=sys.stdout,
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    logger.info(
        "Health check hit  method=%s path=%s remote=%s",
        request.method,
        request.path,
        request.remote_addr,
    )
    payload = {
        "status":  "ok",
        "app":     APP_NAME,
        "env":     APP_ENV,
        "version": APP_VERSION,
    }
    logger.debug("Health response payload: %s", payload)
    return jsonify(payload), 200


if __name__ == "__main__":
    logger.info("Starting %s v%s on port %d (env=%s)", APP_NAME, APP_VERSION, PORT, APP_ENV)
    app.run(host="0.0.0.0", port=PORT)