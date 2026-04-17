"""search_image — Pexels photos + Tenor GIFs.

Ported from ``feishu_channel/server.py::_handle_search_image``. Behavior
identical; downloads results into ``/tmp/search_img_<hex>.<ext>``.
"""

from __future__ import annotations

import logging
import uuid

import httpx

logger = logging.getLogger(__name__)


async def search_image(
    http: httpx.AsyncClient,
    pexels_api_key: str,
    tenor_api_key: str,
    query: str,
    img_type: str = "photo",
    count: int = 1,
) -> dict:
    """Return local_paths for photos (Pexels) or GIFs (Tenor)."""
    count = min(max(count, 1), 5)
    try:
        urls: list[dict] = []
        if img_type == "gif":
            resp = await http.get(
                "https://tenor.googleapis.com/v2/search",
                params={
                    "q": query,
                    "key": tenor_api_key,
                    "limit": count,
                    "media_filter": "gif",
                },
            )
            data = resp.json()
            for r in data.get("results", [])[:count]:
                media = r.get("media_formats", {})
                url = (
                    media.get("gif", {}).get("url", "")
                    or media.get("tinygif", {}).get("url", "")
                )
                if url:
                    urls.append({
                        "url": url,
                        "title": r.get("content_description", ""),
                        "ext": "gif",
                    })
        else:
            if not pexels_api_key:
                return {"status": "error", "message": "PEXELS_API_KEY not set"}
            resp = await http.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": pexels_api_key},
                params={"query": query, "per_page": count, "size": "medium"},
            )
            data = resp.json()
            for p in data.get("photos", [])[:count]:
                url = (
                    p.get("src", {}).get("large", "")
                    or p.get("src", {}).get("medium", "")
                )
                if url:
                    urls.append({
                        "url": url,
                        "title": p.get("alt", ""),
                        "ext": "jpg",
                        "photographer": p.get("photographer", ""),
                    })

        # Auto-download to /tmp/
        results: list[dict] = []
        for item in urls:
            filename = f"/tmp/search_img_{uuid.uuid4().hex[:8]}.{item['ext']}"
            try:
                dl = await http.get(item["url"])
                if dl.status_code == 200:
                    with open(filename, "wb") as f:
                        f.write(dl.content)
                    item["local_path"] = filename
            except Exception:
                item["local_path"] = ""
            results.append(item)

        return {
            "status": "ok",
            "type": img_type,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        return {"status": "error", "message": f"Image search error: {e}"}
