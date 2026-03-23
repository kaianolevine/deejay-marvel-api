from __future__ import annotations


async def test_live_plays_simple_insert_skip_and_recent(client) -> None:
    payload = {
        "plays": [
            {
                "played_at": "2026-03-19T01:02:03Z",
                "title": "Song A",
                "artist": "Artist A",
            },
            {
                "played_at": "2026-03-19T01:02:03Z",
                "title": "Song A",
                "artist": "Artist A",
            },
            {
                "played_at": "2026-03-19T02:03:04Z",
                "title": "Song B",
                "artist": "Artist B",
            },
        ]
    }

    ingest_resp = await client.post("/v1/live-plays", json=payload)
    assert ingest_resp.status_code == 200
    ingest_json = ingest_resp.json()
    assert ingest_json["meta"]["count"] == 1
    assert ingest_json["data"]["inserted"] == 2
    assert ingest_json["data"]["skipped"] == 1

    recent_resp = await client.get("/v1/live-plays/recent", params={"limit": 10})
    assert recent_resp.status_code == 200
    recent_json = recent_resp.json()
    assert "data" in recent_json and "meta" in recent_json
    assert recent_json["meta"]["count"] == 2
    assert recent_json["data"][0]["title"] == "Song B"
    assert recent_json["data"][1]["title"] == "Song A"
    assert "id" in recent_json["data"][0]
    assert "created_at" in recent_json["data"][0]

