import json
import logging
import os
import re
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError
from flask import Flask, jsonify, redirect, request
from flask import send_from_directory

# ---------------------------------------------------------------------------
# Config from environment variables — no hardcoded values
# ---------------------------------------------------------------------------
APP_ENV = os.environ.get(
    "APP_ENV", "production"
)  # "development" | "staging" | "production"
PORT = int(os.environ.get("PORT", 3000))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BASE_URL = os.environ.get("BASE_URL", "https://api.yourdomain.xyz")


# ---------------------------------------------------------------------------
# Structured JSON logging — writes to stdout for Docker / log collectors
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log.update(record.extra)
        return json.dumps(log)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
LOG_LEVEL = logging.DEBUG if APP_ENV == "development" else logging.INFO
logging.basicConfig(handlers=[_handler], level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
ssm = boto3.client("ssm", region_name=AWS_REGION)
dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)


def get_ssm_param(name: str, fallback: str | None = None) -> str:
    """Fetch a parameter from SSM Parameter Store, with an optional local fallback."""
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        if fallback is not None:
            logger.warning("SSM param '%s' unavailable, using fallback: %s", name, exc)
            return fallback
        raise


# Resolve DynamoDB table name at startup (falls back to env var for local dev)
TABLE_NAME = get_ssm_param(
    f"/yourapp/{APP_ENV}/dynamodb_table_name",
    fallback=os.environ.get("DYNAMODB_TABLE", "url-shortener"),
)
table = dynamo.Table(TABLE_NAME)
logger.info(
    "Starting (env=%s, table=%s, port=%d)",
    APP_ENV,
    TABLE_NAME,
    PORT,
)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Paths that must never be matched by the /<code> wildcard routes.
_RESERVED_PATHS = {"health", "shorten", "urls", "stats"}


@app.route("/ui")
def ui():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "ui.html")


# ── Request lifecycle hooks ─────────────────────────────────────────────────


@app.before_request
def start_timer():
    request.start_time = time.time()
    request.request_id = str(uuid.uuid4())


@app.after_request
def log_request(response):
    duration_ms = round((time.time() - request.start_time) * 1000, 2)
    logger.info(
        "request",
        extra={
            "extra": {
                "request_id": request.request_id,
                "method": request.method,
                "path": request.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            }
        },
    )
    response.headers["X-Request-ID"] = request.request_id
    return response


# ── Health ──────────────────────────────────────────────────────────────────


@app.route("/health", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def health():
    """
    Liveness + readiness probe.
    Verifies DynamoDB connectivity so the orchestrator can route traffic away
    from an instance that has lost its backing store.
    """
    if request.method != "GET":
        return jsonify({"error": "method not allowed"}), 405

    try:
        boto3.client("dynamodb", region_name=AWS_REGION).describe_table(
            TableName=TABLE_NAME
        )
        payload = {
            "status": "ok",
            "env": APP_ENV,
        }
        logger.debug("Health check passed: %s", payload)
        return jsonify(payload), 200
    except Exception as exc:
        logger.error(
            "Health check failed",
            extra={"extra": {"error_type": type(exc).__name__, "error": str(exc)}},
        )
        return jsonify({"status": "degraded", "reason": str(exc)}), 500


# ── Shorten ─────────────────────────────────────────────────────────────────


@app.route("/shorten", methods=["POST"])
def shorten():
    """
    Create a short URL.

    Request body (JSON):
        { "url": "https://example.com/very/long/path" }

    Response 201:
        { "code": "a3f9b21", "short_url": "https://api.yourdomain.xyz/a3f9b21" }
    """
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"error": "missing 'url' in request body"}), 400

    original_url: str = body["url"]
    if not original_url.startswith(("http://", "https://")):
        return jsonify({"error": "url must start with http:// or https://"}), 422

    code = uuid.uuid4().hex[:7]  # e.g. "a3f9b21"
    created_at = int(time.time())

    table.put_item(
        Item={
            "code": code,
            "original_url": original_url,
            "created_at": created_at,
            "click_count": 0,
        }
    )

    logger.info(
        "URL shortened",
        extra={"extra": {"code": code, "original_url": original_url}},
    )
    return (
        jsonify({"code": code, "short_url": f"{BASE_URL}/{code}"}),
        201,
    )


# ── Redirect ────────────────────────────────────────────────────────────────


@app.route("/<code>", methods=["GET"])
def redirect_to_url(code: str):
    """
    Redirect a short code to its original URL.
    Atomically increments the click counter on each visit.
    """
    if code in _RESERVED_PATHS or not re.fullmatch(r"[0-9a-zA-Z]{7}", code):
        return jsonify({"error": "not found"}), 404

    try:
        result = table.update_item(
            Key={"code": code},
            UpdateExpression="SET click_count = click_count + :inc",
            ConditionExpression="attribute_exists(#c)",
            ExpressionAttributeNames={"#c": "code"},
            ExpressionAttributeValues={":inc": 1},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ConditionalCheckFailedException":
            return jsonify({"error": "short URL not found"}), 404
        logger.error(
            "DynamoDB error on redirect",
            extra={
                "extra": {
                    "code": code,
                    "error_type": error_code,
                    "error": exc.response["Error"]["Message"],
                }
            },
        )
        return jsonify({"error": "internal error"}), 500

    original_url = result["Attributes"]["original_url"]
    logger.info(
        "Redirecting",
        extra={
            "extra": {
                "code": code,
                "original_url": original_url,
                "click_count": int(result["Attributes"].get("click_count", 0)),
            }
        },
    )
    return redirect(original_url, code=301)


# ── Stats ───────────────────────────────────────────────────────────────────


@app.route("/stats/<code>", methods=["GET"])
def get_stats(code: str):
    """
    Return metadata and click count for a short code.

    Response 200:
        {
            "code": "a3f9b21",
            "original_url": "https://example.com/...",
            "created_at": 1713000000,
            "click_count": 42,
            "short_url": "https://api.yourdomain.xyz/a3f9b21"
        }
    """
    try:
        result = table.get_item(Key={"code": code})
    except ClientError as exc:
        logger.error(
            "DynamoDB error on stats",
            extra={
                "extra": {
                    "code": code,
                    "error": exc.response["Error"]["Message"],
                }
            },
        )
        return jsonify({"error": "internal error"}), 500

    item = result.get("Item")
    if not item:
        return jsonify({"error": "short URL not found"}), 404

    return jsonify(
        {
            "code": item["code"],
            "original_url": item["original_url"],
            "created_at": int(item["created_at"]),
            "click_count": int(item.get("click_count", 0)),
            "short_url": f"{BASE_URL}/{item['code']}",
        }
    ), 200


# ── List all URLs ────────────────────────────────────────────────────────────


@app.route("/urls", methods=["GET"])
def list_urls():
    """
    Return all shortened URLs (paginated via DynamoDB scan).

    Query params:
        limit  — max items per page (default 50, max 100)
        cursor — base64 ExclusiveStartKey from a previous response

    Response 200:
        {
            "items": [ { "code", "original_url", "created_at", "click_count", "short_url" }, … ],
            "cursor": "<opaque pagination token> | null"
        }
    """
    import base64

    raw_limit = request.args.get("limit", 50)
    try:
        limit = max(1, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer between 1 and 100"}), 422

    scan_kwargs: dict = {"Limit": limit}

    cursor = request.args.get("cursor")
    if cursor:
        try:
            esk = json.loads(base64.b64decode(cursor).decode())
            scan_kwargs["ExclusiveStartKey"] = esk
        except Exception:
            return jsonify({"error": "invalid cursor"}), 422

    try:
        result = table.scan(**scan_kwargs)
    except ClientError as exc:
        logger.error(
            "DynamoDB scan error",
            extra={"extra": {"error": exc.response["Error"]["Message"]}},
        )
        return jsonify({"error": "internal error"}), 500

    items = [
        {
            "code": item["code"],
            "original_url": item["original_url"],
            "created_at": int(item["created_at"]),
            "click_count": int(item.get("click_count", 0)),
            "short_url": f"{BASE_URL}/{item['code']}",
        }
        for item in result.get("Items", [])
    ]

    next_cursor = None
    if "LastEvaluatedKey" in result:
        next_cursor = base64.b64encode(
            json.dumps(result["LastEvaluatedKey"]).encode()
        ).decode()

    return jsonify({"items": items, "cursor": next_cursor}), 200


# ── Delete ───────────────────────────────────────────────────────────────────


@app.route("/<code>", methods=["DELETE"])
def delete_url(code: str):
    """
    Delete a short URL entry.

    Response 200:  { "deleted": true, "code": "a3f9b21" }
    Response 404:  { "error": "short URL not found" }
    """
    if code in _RESERVED_PATHS or not re.fullmatch(r"[0-9a-zA-Z]{7}", code):
        return jsonify({"error": "not found"}), 404

    try:
        table.delete_item(
            Key={"code": code},
            ConditionExpression="attribute_exists(#c)",
            ExpressionAttributeNames={"#c": "code"},
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ConditionalCheckFailedException":
            return jsonify({"error": "short URL not found"}), 404
        logger.error(
            "DynamoDB error on delete",
            extra={
                "extra": {
                    "code": code,
                    "error_type": error_code,
                    "error": exc.response["Error"]["Message"],
                }
            },
        )
        return jsonify({"error": "internal error"}), 500

    logger.info("URL deleted", extra={"extra": {"code": code}})
    return jsonify({"deleted": True, "code": code}), 200


# ---------------------------------------------------------------------------
# Entrypoint (dev only — production uses gunicorn via Dockerfile CMD)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting on port %d (env=%s)", PORT, APP_ENV)
    app.run(host="0.0.0.0", port=PORT, debug=(APP_ENV == "development"))
