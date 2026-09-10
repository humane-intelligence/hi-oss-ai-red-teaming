"""Tests for the central exception handlers.

Builds a tiny ad-hoc FastAPI app with one route per exception type, then
asserts the handlers produce RFC 7807 `Problem` responses with the right
status code, content type, and headers.
"""

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import status
from fastapi.testclient import TestClient

from app.core.error_handlers import api_error_to_problem
from app.core.error_handlers import register_error_handlers
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.exceptions import UnauthorizedError

PROBLEM_MEDIA_TYPE = "application/problem+json"


def _build_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise-not-found")
    def _r_404() -> None:
        raise NotFoundError("missing thing")

    @app.get("/raise-conflict")
    def _r_409() -> None:
        raise ConflictError("dup")

    @app.get("/raise-unauthorized")
    def _r_401() -> None:
        raise UnauthorizedError

    @app.get("/raise-forbidden")
    def _r_403() -> None:
        raise ForbiddenError

    @app.get("/raise-bad")
    def _r_400() -> None:
        raise BadRequestError("nope")

    @app.get("/raise-http-with-headers")
    def _r_http() -> None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
            headers={"WWW-Authenticate": 'Bearer realm="api"'},
        )

    @app.get("/boom")
    def _r_boom() -> None:
        raise RuntimeError("internal secret detail")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "expected_status", "expected_title"),
    [
        ("/raise-not-found", status.HTTP_404_NOT_FOUND, "Not Found"),
        ("/raise-conflict", status.HTTP_409_CONFLICT, "Conflict"),
        ("/raise-unauthorized", status.HTTP_401_UNAUTHORIZED, "Unauthorized"),
        ("/raise-forbidden", status.HTTP_403_FORBIDDEN, "Forbidden"),
        ("/raise-bad", status.HTTP_400_BAD_REQUEST, "Bad Request"),
    ],
)
def test_api_error_subclasses_map_to_problem(
    client: TestClient, path: str, expected_status: int, expected_title: str
) -> None:
    response = client.get(path)

    assert response.status_code == expected_status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"] == expected_title
    assert body["instance"] == path


def test_api_error_includes_detail_when_provided(client: TestClient) -> None:
    response = client.get("/raise-not-found")

    assert response.json()["detail"] == "missing thing"


def test_api_error_omits_detail_when_not_provided(client: TestClient) -> None:
    response = client.get("/raise-unauthorized")

    assert "detail" not in response.json()


@pytest.mark.integration
def test_http_exception_preserves_headers_and_emits_problem(client: TestClient) -> None:
    response = client.get("/raise-http-with-headers")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["www-authenticate"] == 'Bearer realm="api"'
    body = response.json()
    assert body["status"] == 401
    assert body["detail"] == "login required"


@pytest.mark.unit
def test_api_error_to_problem_maps_fields() -> None:
    problem = api_error_to_problem(ConflictError("dup"), instance="/things/bulk")

    assert problem.status == status.HTTP_409_CONFLICT
    assert problem.title == "Conflict"
    assert problem.detail == "dup"
    assert problem.instance == "/things/bulk"
    assert problem.type == "about:blank"


@pytest.mark.unit
def test_api_error_to_problem_accepts_none_instance() -> None:
    problem = api_error_to_problem(NotFoundError("nope"), instance=None)

    assert problem.instance is None
    assert problem.status == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
def test_unexpected_exception_returns_generic_500(client: TestClient) -> None:
    response = client.get("/boom")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["status"] == 500
    assert body["title"] == "Internal Server Error"
    assert "secret" not in body["detail"]
