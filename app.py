import os
from flask import Flask, jsonify

app = Flask(__name__)

# Config from environment variables — no hardcoded values
APP_NAME    = os.environ.get("APP_NAME")
APP_ENV     = os.environ.get("APP_ENV")
APP_VERSION = os.environ.get("APP_VERSION")
PORT        = int(os.environ.get("PORT"))


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "url shortener backend server is healthy",
        "app":     APP_NAME,
        "env":     APP_ENV,
        "version": APP_VERSION,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
