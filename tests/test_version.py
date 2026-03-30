from __future__ import annotations


async def test_version_endpoint_returns_package_version(client) -> None:
    resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    # pyproject uses full semver (e.g. 1.13.2)
    assert isinstance(data["version"], str)
    assert "." in data["version"]
