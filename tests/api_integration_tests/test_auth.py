import httpx
import pytest
import respx


@pytest.mark.asyncio
async def test_auth_missing_headers_returns_401(client):
    async with client:
        r = await client.get("/api/chatbot/availablechatbots")
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert "Some necessary fields for authentication weren't found" in detail
        assert "nginx proxy" in detail


@pytest.mark.asyncio
async def test_auth_non_bearer_header_422(client):
    async with client:
        r = await client.get(
            "/api/chatbot/availablechatbots",
            headers={
                "Authorization": "Token abc",
                "x-freva-rest-url": "http://rest.example",
            },
        )
        assert r.status_code == 422
        assert (
            r.json()["detail"]
            == "Authorization header is not a Bearer token. Please use the Bearer token format."
        )


@pytest.mark.asyncio
async def test_auth_missing_rest_url_400(client):
    async with client:
        r = await client.get(
            "/api/chatbot/availablechatbots",
            headers={"Authorization": "Bearer abc"},
        )
        assert r.status_code == 400
        assert (
            r.json()["detail"]
            == "Authentication not successful! RestURL not found. Please use the nginx proxy. (rest)"
        )


@pytest.mark.asyncio
async def test_auth_token_check_network_error_503(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "http://rest.example/api/freva-nextgen/auth/v2/systemuser"
        ).side_effect = httpx.ConnectError("boom")
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 503
            assert (
                r.json()["detail"]
                == "Error sending token validation request, is the URL correct?"
            )


@pytest.mark.asyncio
async def test_auth_token_check_http_401_like_401_message(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            401, json={"detail": "Token expired."}
        )
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 401
            assert "Token validation failed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_auth_token_check_malformed_json_502(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            200, content=b"not-json"
        )
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 502
            assert (
                r.json()["detail"]
                == "Token validation response is malformed, not valid JSON."
            )


@pytest.mark.asyncio
async def test_auth_token_check_json_missing_username_detail_502(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            200, json={"foo": "bar"}
        )
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 502
            assert (
                r.json()["detail"]
                == "Token validation response is malformed, no username found."
            )


@pytest.mark.asyncio
async def test_auth_token_check_json_detail_403(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            403, json={"detail": "Not a system user."}
        )
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer abc",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 403
            assert "login using a DKRZ account" in r.json()["detail"]


@pytest.mark.asyncio
async def test_auth_success_200(client):
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://rest.example/api/freva-nextgen/auth/v2/systemuser").respond(
            200, json={"username": "alice"}
        )
        async with client:
            r = await client.get(
                "/api/chatbot/availablechatbots",
                headers={
                    "Authorization": "Bearer good",
                    "x-freva-rest-url": "http://rest.example",
                },
            )
            assert r.status_code == 200
