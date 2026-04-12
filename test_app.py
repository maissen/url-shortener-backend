"""
API tests for app.py

Run:
    pytest -v
"""

import base64
import importlib
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_error(code: str, message: str = "error") -> ClientError:
    """Construct a minimal botocore ClientError."""
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "operation",
    )


def _make_item(
    code="abc1234",
    original_url="https://example.com",
    created_at=1_713_000_000,
    click_count=0,
):
    return {
        "code": code,
        "original_url": original_url,
        "created_at": Decimal(str(created_at)),
        "click_count": Decimal(str(click_count)),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_aws(monkeypatch):
    """
    Patch every AWS touch-point used at module import time so that
    `importlib.reload(app)` never tries to reach real AWS.
    """
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "url-shortener-test"}}

    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    mock_dynamo_client = MagicMock()
    mock_dynamo_client.describe_table.return_value = {}

    def fake_boto3_client(service, **kwargs):
        if service == "ssm":
            return mock_ssm
        if service == "dynamodb":
            return mock_dynamo_client
        return MagicMock()

    def fake_boto3_resource(service, **kwargs):
        if service == "dynamodb":
            return mock_dynamo
        return MagicMock()

    monkeypatch.setattr("boto3.client", fake_boto3_client)
    monkeypatch.setattr("boto3.resource", fake_boto3_resource)

    return {"ssm": mock_ssm, "table": mock_table, "dynamo_client": mock_dynamo_client}


@pytest.fixture()
def client(monkeypatch, mock_aws):
    monkeypatch.setenv("APP_NAME", "test-app")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("BASE_URL", "https://short.test")
    monkeypatch.setenv("DYNAMODB_TABLE", "url-shortener-test")

    import app

    importlib.reload(app)
    app.app.config["TESTING"] = True

    with app.app.test_client() as c:
        # Expose the mock table so individual tests can configure it.
        c.mock_table = mock_aws["table"]
        c.mock_dynamo_client = mock_aws["dynamo_client"]
        yield c


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health_returns_200(client):
    assert client.get("/health").status_code == 200


def test_health_response_body(client):
    data = client.get("/health").get_json()
    assert data["status"] == "ok"
    assert data["app"] == "test-app"
    assert data["version"] == "1.2.3"
    assert data["env"] == "development"


def test_health_degraded_when_dynamo_unavailable(client):
    client.mock_dynamo_client.describe_table.side_effect = Exception("unreachable")
    resp = client.get("/health")
    assert resp.status_code == 500
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert "reason" in data


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_health_disallows_non_get_methods(client, method):
    assert getattr(client, method)("/health").status_code == 405


# ---------------------------------------------------------------------------
# POST /shorten
# ---------------------------------------------------------------------------


def test_shorten_returns_201(client):
    client.mock_table.put_item.return_value = {}
    resp = client.post("/shorten", json={"url": "https://example.com/path"})
    assert resp.status_code == 201


def test_shorten_response_body_structure(client):
    client.mock_table.put_item.return_value = {}
    data = client.post("/shorten", json={"url": "https://example.com"}).get_json()
    assert "code" in data
    assert "short_url" in data
    assert len(data["code"]) == 7
    assert data["short_url"].endswith(f"/{data['code']}")


def test_shorten_short_url_uses_base_url(client):
    client.mock_table.put_item.return_value = {}
    data = client.post("/shorten", json={"url": "https://example.com"}).get_json()
    assert data["short_url"].startswith("https://short.test/")


def test_shorten_missing_body_returns_400(client):
    resp = client.post("/shorten", content_type="application/json", data="")
    assert resp.status_code == 400
    assert "url" in resp.get_json()["error"]


def test_shorten_missing_url_field_returns_400(client):
    resp = client.post("/shorten", json={"link": "https://example.com"})
    assert resp.status_code == 400


def test_shorten_non_json_body_returns_400(client):
    resp = client.post(
        "/shorten", data="url=https://example.com", content_type="text/plain"
    )
    assert resp.status_code == 400


def test_shorten_rejects_non_http_url(client):
    resp = client.post("/shorten", json={"url": "ftp://example.com"})
    assert resp.status_code == 422
    assert "http" in resp.get_json()["error"].lower()


def test_shorten_accepts_http_scheme(client):
    client.mock_table.put_item.return_value = {}
    resp = client.post("/shorten", json={"url": "http://example.com"})
    assert resp.status_code == 201


def test_shorten_accepts_https_scheme(client):
    client.mock_table.put_item.return_value = {}
    resp = client.post("/shorten", json={"url": "https://example.com"})
    assert resp.status_code == 201


def test_shorten_puts_item_in_dynamo(client):
    client.mock_table.put_item.return_value = {}
    client.post("/shorten", json={"url": "https://example.com/page"})
    call_args = client.mock_table.put_item.call_args
    item = call_args.kwargs["Item"] if call_args.kwargs else call_args[1]["Item"]
    assert item["original_url"] == "https://example.com/page"
    assert item["click_count"] == 0
    assert "created_at" in item
    assert "code" in item


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_shorten_only_accepts_post(client, method):
    assert getattr(client, method)("/shorten").status_code in (404, 405)


# ---------------------------------------------------------------------------
# GET /<code>  (redirect)
# ---------------------------------------------------------------------------


def test_redirect_follows_to_original_url(client):
    client.mock_table.update_item.return_value = {
        "Attributes": _make_item(click_count=1)
    }
    resp = client.get("/abc1234")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "https://example.com"


def test_redirect_increments_click_count(client):
    client.mock_table.update_item.return_value = {
        "Attributes": _make_item(click_count=5)
    }
    client.get("/abc1234")
    update_call = client.mock_table.update_item.call_args
    kwargs = update_call.kwargs if update_call.kwargs else update_call[1]
    assert ":inc" in kwargs["ExpressionAttributeValues"]
    assert kwargs["ExpressionAttributeValues"][":inc"] == 1


def test_redirect_unknown_code_returns_404(client):
    client.mock_table.update_item.side_effect = _client_error(
        "ConditionalCheckFailedException"
    )
    resp = client.get("/notfound")
    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"].lower()


def test_redirect_dynamo_error_returns_500(client):
    client.mock_table.update_item.side_effect = _client_error("InternalServerError")
    resp = client.get("/abc1234")
    assert resp.status_code == 500


def test_redirect_response_has_request_id_header(client):
    client.mock_table.update_item.return_value = {
        "Attributes": _make_item(click_count=1)
    }
    resp = client.get("/abc1234")
    assert "X-Request-ID" in resp.headers


# ---------------------------------------------------------------------------
# GET /stats/<code>
# ---------------------------------------------------------------------------


def test_stats_returns_200(client):
    client.mock_table.get_item.return_value = {"Item": _make_item(click_count=42)}
    assert client.get("/stats/abc1234").status_code == 200


def test_stats_response_body(client):
    client.mock_table.get_item.return_value = {
        "Item": _make_item(
            code="abc1234",
            original_url="https://example.com",
            created_at=1_713_000_000,
            click_count=42,
        )
    }
    data = client.get("/stats/abc1234").get_json()
    assert data["code"] == "abc1234"
    assert data["original_url"] == "https://example.com"
    assert data["created_at"] == 1_713_000_000
    assert data["click_count"] == 42
    assert data["short_url"] == "https://short.test/abc1234"


def test_stats_unknown_code_returns_404(client):
    client.mock_table.get_item.return_value = {}  # no "Item" key
    resp = client.get("/stats/unknown")
    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"].lower()


def test_stats_dynamo_error_returns_500(client):
    client.mock_table.get_item.side_effect = _client_error("InternalServerError")
    assert client.get("/stats/abc1234").status_code == 500


def test_stats_click_count_defaults_to_zero(client):
    item = _make_item()
    del item["click_count"]
    client.mock_table.get_item.return_value = {"Item": item}
    data = client.get("/stats/abc1234").get_json()
    assert data["click_count"] == 0


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_stats_disallows_non_get_methods(client, method):
    assert getattr(client, method)("/stats/abc1234").status_code == 405


# ---------------------------------------------------------------------------
# GET /urls
# ---------------------------------------------------------------------------


def test_list_urls_returns_200(client):
    client.mock_table.scan.return_value = {"Items": []}
    assert client.get("/urls").status_code == 200


def test_list_urls_returns_items(client):
    client.mock_table.scan.return_value = {
        "Items": [
            _make_item("aaa0001", "https://a.com", click_count=1),
            _make_item("bbb0002", "https://b.com", click_count=2),
        ]
    }
    data = client.get("/urls").get_json()
    assert len(data["items"]) == 2
    codes = {i["code"] for i in data["items"]}
    assert codes == {"aaa0001", "bbb0002"}


def test_list_urls_cursor_is_none_when_no_more_pages(client):
    client.mock_table.scan.return_value = {"Items": []}
    data = client.get("/urls").get_json()
    assert data["cursor"] is None


def test_list_urls_cursor_present_when_more_pages(client):
    client.mock_table.scan.return_value = {
        "Items": [_make_item()],
        "LastEvaluatedKey": {"code": {"S": "abc1234"}},
    }
    data = client.get("/urls").get_json()
    assert data["cursor"] is not None
    # Cursor must be a valid base64-encoded JSON string.
    decoded = json.loads(base64.b64decode(data["cursor"]))
    assert "code" in decoded


def test_list_urls_uses_cursor_from_query_param(client):
    esk = {"code": {"S": "abc1234"}}
    cursor = base64.b64encode(json.dumps(esk).encode()).decode()
    client.mock_table.scan.return_value = {"Items": []}
    client.get(f"/urls?cursor={cursor}")
    scan_call = client.mock_table.scan.call_args
    kwargs = scan_call.kwargs if scan_call.kwargs else scan_call[1]
    assert kwargs.get("ExclusiveStartKey") == esk


def test_list_urls_invalid_cursor_returns_422(client):
    resp = client.get("/urls?cursor=notvalidbase64!!!")
    assert resp.status_code == 422
    assert "cursor" in resp.get_json()["error"].lower()


def test_list_urls_respects_limit_param(client):
    client.mock_table.scan.return_value = {"Items": []}
    client.get("/urls?limit=10")
    scan_call = client.mock_table.scan.call_args
    kwargs = scan_call.kwargs if scan_call.kwargs else scan_call[1]
    assert kwargs["Limit"] == 10


def test_list_urls_clamps_limit_to_100(client):
    client.mock_table.scan.return_value = {"Items": []}
    client.get("/urls?limit=999")
    scan_call = client.mock_table.scan.call_args
    kwargs = scan_call.kwargs if scan_call.kwargs else scan_call[1]
    assert kwargs["Limit"] == 100


def test_list_urls_clamps_limit_minimum_to_1(client):
    client.mock_table.scan.return_value = {"Items": []}
    client.get("/urls?limit=0")
    scan_call = client.mock_table.scan.call_args
    kwargs = scan_call.kwargs if scan_call.kwargs else scan_call[1]
    assert kwargs["Limit"] == 1


def test_list_urls_invalid_limit_returns_422(client):
    resp = client.get("/urls?limit=abc")
    assert resp.status_code == 422


def test_list_urls_dynamo_error_returns_500(client):
    client.mock_table.scan.side_effect = _client_error("InternalServerError")
    assert client.get("/urls").status_code == 500


def test_list_urls_item_shape(client):
    client.mock_table.scan.return_value = {"Items": [_make_item(click_count=7)]}
    item = client.get("/urls").get_json()["items"][0]
    assert set(item.keys()) == {
        "code",
        "original_url",
        "created_at",
        "click_count",
        "short_url",
    }
    assert item["short_url"].startswith("https://short.test/")


# ---------------------------------------------------------------------------
# DELETE /<code>
# ---------------------------------------------------------------------------


def test_delete_returns_200(client):
    client.mock_table.delete_item.return_value = {}
    resp = client.delete("/abc1234")
    assert resp.status_code == 200


def test_delete_response_body(client):
    client.mock_table.delete_item.return_value = {}
    data = client.delete("/abc1234").get_json()
    assert data["deleted"] is True
    assert data["code"] == "abc1234"


def test_delete_unknown_code_returns_404(client):
    client.mock_table.delete_item.side_effect = _client_error(
        "ConditionalCheckFailedException"
    )
    resp = client.delete("/notfound")
    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"].lower()


def test_delete_dynamo_error_returns_500(client):
    client.mock_table.delete_item.side_effect = _client_error("InternalServerError")
    assert client.delete("/abc1234").status_code == 500


def test_delete_uses_correct_key(client):
    client.mock_table.delete_item.return_value = {}
    client.delete("/mycode7")
    call_kwargs = client.mock_table.delete_item.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
    assert kwargs["Key"] == {"code": "mycode7"}


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


def test_unknown_route_returns_404(client):
    assert client.get("/does-not-exist").status_code == 404


def test_root_returns_404(client):
    assert client.get("/").status_code == 404


# ---------------------------------------------------------------------------
# Request-ID header is present on all responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_request",
    [
        lambda c: c.get("/health"),
        lambda c: c.post("/shorten", json={"url": "https://x.com"}),
        lambda c: c.get("/urls"),
    ],
)
def test_request_id_header_on_all_responses(client, make_request):
    client.mock_table.put_item.return_value = {}
    client.mock_table.scan.return_value = {"Items": []}
    resp = make_request(client)
    assert "X-Request-ID" in resp.headers


# ---------------------------------------------------------------------------
# Conflict: redirect vs delete on the same code path
# ---------------------------------------------------------------------------


def test_get_and_delete_share_code_route(client):
    """GET /<code> redirects; DELETE /<code> deletes — same path, different verbs."""
    client.mock_table.update_item.return_value = {
        "Attributes": _make_item(click_count=1)
    }
    assert client.get("/abc1234").status_code == 301

    client.mock_table.delete_item.return_value = {}
    assert client.delete("/abc1234").status_code == 200
