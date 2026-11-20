from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.db import init_db, make_engine
from app.web_api import create_app


def _app(tmp_path):
    db_path = tmp_path / "tocmemex.db"

    async def _init():
        engine = make_engine(db_path)
        await init_db(engine)
        await engine.dispose()

    asyncio.run(_init())
    return create_app(db_path, tmp_path)


def test_collection_crud(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/collections",
            json={"name": "电子标准", "description": "国标行标"},
        )
        assert created.status_code == 200
        collection = created.json()["collection"]
        assert collection["name"] == "电子标准"
        assert collection["description"] == "国标行标"
        assert collection["docCount"] == 0

        duplicate = client.post("/api/collections", json={"name": "电子标准"})
        assert duplicate.status_code == 409

        listed = client.get("/api/collections").json()
        assert [row["name"] for row in listed["collections"]] == ["电子标准"]
        assert listed["unassigned"] == 0

        renamed = client.patch(
            f"/api/collections/{collection['id']}",
            json={"name": "电子类标准", "description": "改过"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["collection"]["name"] == "电子类标准"

        deleted = client.delete(f"/api/collections/{collection['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == collection["id"]
        assert client.get("/api/collections").json()["collections"] == []


def test_collection_missing_is_404(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.patch("/api/collections/nope", json={"name": "x"}).status_code == 404
        assert client.delete("/api/collections/nope").status_code == 404


def test_assign_documents_to_collection(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        collection = client.post(
            "/api/collections", json={"name": "求职"}
        ).json()["collection"]
        payload = client.put(
            "/api/documents/collection",
            json={"docIds": ["missing-doc"], "collectionId": collection["id"]},
        )
        assert payload.status_code == 200
        assert payload.json() == {"updated": 0, "missing": ["missing-doc"]}

        unknown = client.put(
            "/api/documents/collection",
            json={"docIds": [], "collectionId": "no-such-collection"},
        )
        assert unknown.status_code == 404


def test_assign_documents_too_many_is_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.put(
            "/api/documents/collection",
            json={"docIds": [f"d{i}" for i in range(501)], "collectionId": None},
        )
        assert response.status_code == 400


def test_collection_name_validation(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/collections", json={"name": "  "}).status_code == 422
        assert (
            client.post("/api/collections", json={"name": "unassigned"}).status_code
            == 422
        )
        assert client.post("/api/collections", json={"name": "x" * 81}).status_code == 422
        long_desc = client.post(
            "/api/collections", json={"name": "ok", "description": "y" * 501}
        )
        assert long_desc.status_code == 422


def test_search_with_collection_and_unknown_collection(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/collections", json={"name": "电子标准"})
        response = client.get(
            "/api/search", params={"q": "振动", "collection": "电子标准"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["results"] == []
        assert body["scope"]["collection"]["name"] == "电子标准"
        assert "0 hits in collection" in body["hint"]

        unknown = client.get(
            "/api/search", params={"q": "振动", "collection": "不存在"}
        )
        assert unknown.status_code == 400
        assert "collection not found" in unknown.json()["detail"]
