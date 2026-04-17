"""Feishu-specific tool handlers: docs, bitables, tasks, search_docs.

All handlers take a ``FeishuChannel`` — they read ``feishu.http`` and
``feishu.token`` (a :class:`TokenProvider`) and inline the two-attempt
token-refresh pattern. Token-error detection goes through
``feishu_channel.api.is_token_error``.

These handlers are verbatim ports of:

* ``_handle_create_doc``          — server.py:1999-2084
* ``_handle_create_bitable``      — server.py:2086-2244
* ``_handle_bitable_records``     — server.py:1754-1846
* ``_handle_manage_task``         — server.py:1657-1750
* ``_handle_search_docs``         — server.py:1556-1599
"""

from __future__ import annotations

import json
import logging

from ..channels.feishu.api import is_token_error

logger = logging.getLogger(__name__)


# ── search_docs ──────────────────────────────────────────────────


async def search_docs(feishu, query: str) -> dict:
    """Search Feishu cloud docs by keyword (tenant-token API)."""
    http = feishu.http
    token_provider = feishu.token
    for attempt in range(2):
        try:
            token = await token_provider.get()
            body: dict = {"search_key": query[:50], "count": 20, "offset": 0}
            resp = await http.post(
                "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            data = resp.json()
            if attempt == 0 and is_token_error(data):
                logger.info("Search docs: token expired, refreshing and retrying")
                token_provider.invalidate()
                continue
            if data.get("code") != 0:
                return {
                    "status": "error",
                    "message": f"Wiki search failed: {data.get('msg', 'unknown error')}",
                }

            items = data.get("data", {}).get("docs_entities", [])
            total = data.get("data", {}).get("total", 0)
            results = []
            for item in items:
                doc_token = item.get("docs_token", "")
                doc_type = item.get("docs_type", "doc")
                if doc_type == "bitable":
                    url = f"https://feishu.cn/base/{doc_token}"
                elif doc_type == "sheet":
                    url = f"https://feishu.cn/sheets/{doc_token}"
                else:
                    url = f"https://feishu.cn/docx/{doc_token}"
                results.append({
                    "title": item.get("title", ""),
                    "url": url,
                    "doc_token": doc_token,
                    "doc_type": doc_type,
                })
            return {
                "status": "ok",
                "count": len(results),
                "total": total,
                "results": results,
            }
        except Exception as e:
            return {"status": "error", "message": f"Wiki search error: {e}"}
    return {"status": "error", "message": "Wiki search failed after retry"}


# ── create_doc ───────────────────────────────────────────────────


async def create_doc(feishu, title: str, content: list, chat_id: str = "") -> dict:
    """Create a Feishu cloud document with structured content blocks."""
    http = feishu.http
    token_provider = feishu.token

    # Safety: if content arrives as JSON string, parse it
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            content = []
    logger.info(
        "create_doc: title=%s, content type=%s, len=%s",
        title,
        type(content).__name__,
        len(content) if isinstance(content, list) else "N/A",
    )

    for attempt in range(2):
        try:
            token = await token_provider.get()
            # Step 1: Create document
            resp = await http.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                json={"title": title},
            )
            data = resp.json()
            if attempt == 0 and is_token_error(data):
                logger.info("Create doc: token expired, refreshing and retrying")
                token_provider.invalidate()
                continue
            if data.get("code") != 0:
                return {"status": "error", "message": f"Doc creation failed: {data}"}
            doc_id = data["data"]["document"]["document_id"]

            # Step 2: Add content blocks
            if content:
                block_type_map = {
                    "heading1": 3, "heading2": 4, "heading3": 5, "text": 2,
                    "bullet": 12, "ordered": 13, "code": 14, "quote": 15,
                }
                children = []
                for block in content:
                    bt = block.get("type", "text")
                    text = block.get("text", "")
                    block_type = block_type_map.get(bt, 2)
                    block_key = bt if bt in block_type_map else "text"
                    if block_key in ("heading1", "heading2", "heading3"):
                        children.append({
                            "block_type": block_type,
                            block_key: {"elements": [{"text_run": {"content": text}}]},
                        })
                    elif block_key == "code":
                        lang = block.get("language", "plain_text")
                        children.append({
                            "block_type": block_type,
                            "code": {
                                "elements": [{"text_run": {"content": text}}],
                                "language": lang,
                            },
                        })
                    else:
                        children.append({
                            "block_type": block_type,
                            block_key: {"elements": [{"text_run": {"content": text}}]},
                        })

                # Feishu API limit: max 50 children per call
                for batch_start in range(0, len(children), 50):
                    batch = children[batch_start:batch_start + 50]
                    blocks_resp = await http.post(
                        f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"children": batch, "index": -1},
                    )
                    blocks_data = blocks_resp.json()
                    if blocks_data.get("code") != 0:
                        logger.error("Doc add blocks failed: %s", blocks_data)

            # Step 3: Resolve URL
            try:
                doc_resp = await http.get(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                doc_data = doc_resp.json()
                url = doc_data.get("data", {}).get("document", {}).get("url", "")
                if not url:
                    url = f"https://feishu.cn/docx/{doc_id}"
            except Exception:
                url = f"https://feishu.cn/docx/{doc_id}"

            # Step 4: Optionally broadcast link
            if chat_id:
                await http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": url}),
                    },
                )

            return {"status": "ok", "document_id": doc_id, "url": url}
        except Exception as e:
            logger.error("Create doc failed: %s", e)
            return {"status": "error", "message": f"Create doc failed: {e}"}
    return {"status": "error", "message": "Create doc failed after retry"}


# ── create_bitable ───────────────────────────────────────────────


async def create_bitable(
    feishu,
    title: str,
    fields: list | None = None,
    records: list | None = None,
    views: list | None = None,
    chat_id: str = "",
) -> dict:
    """Create a Feishu Bitable (多维表格) with custom fields, data and views."""
    http = feishu.http
    token_provider = feishu.token

    for attempt in range(2):
        try:
            token = await token_provider.get()
            field_type_map = {
                "text": 1, "number": 2, "single_select": 3, "multi_select": 4,
                "date": 5, "checkbox": 7, "user": 11, "phone": 13, "url": 15,
                "attachment": 17, "created_time": 1001, "modified_time": 1002,
            }
            base = "https://open.feishu.cn/open-apis/bitable/v1/apps"
            headers = {"Authorization": f"Bearer {token}"}

            def _check(data: dict, step: str) -> None:
                if data.get("code") != 0:
                    logger.error("Bitable %s failed: %s", step, data)
                    raise RuntimeError(
                        f"{step}: code={data.get('code')} msg={data.get('msg')}"
                    )

            # Step 1: create app
            resp = await http.post(base, headers=headers, json={"name": title})
            data = resp.json()
            if attempt == 0 and is_token_error(data):
                logger.info("Create bitable: token expired, refreshing and retrying")
                token_provider.invalidate()
                continue
            _check(data, "create app")
            app_token = data["data"]["app"]["app_token"]
            url = data["data"]["app"]["url"]

            # Default table
            resp = await http.get(f"{base}/{app_token}/tables", headers=headers)
            data = resp.json()
            _check(data, "list tables")
            items = data.get("data", {}).get("items", [])
            if not items:
                return {"status": "error", "message": "Bitable created but no default table found"}
            table_id = items[0]["table_id"]
            tbl = f"{base}/{app_token}/tables/{table_id}"

            # Default fields
            resp = await http.get(f"{tbl}/fields", headers=headers)
            data = resp.json()
            _check(data, "list fields")
            default_fields = data["data"]["items"]

            # Drop non-primary default fields
            for f in default_fields[1:]:
                resp = await http.delete(f"{tbl}/fields/{f['field_id']}", headers=headers)
                d = resp.json()
                if d.get("code") != 0:
                    logger.warning("Delete default field %s: %s", f["field_id"], d)

            # Drop default empty placeholder records
            resp = await http.get(f"{tbl}/records", headers=headers, params={"page_size": 20})
            d = resp.json()
            if d.get("code") == 0:
                default_recs = [r["record_id"] for r in d.get("data", {}).get("items", [])]
                if default_recs:
                    await http.post(
                        f"{tbl}/records/batch_delete",
                        headers=headers,
                        json={"records": default_recs},
                    )

            # Step 2: add custom fields
            field_names: list[str] = []
            if fields:
                first_field = fields[0]
                ft = field_type_map.get(first_field.get("type", "text"), 1)
                update_json: dict = {"field_name": first_field["name"], "type": ft}
                if first_field.get("options"):
                    update_json["property"] = {
                        "options": [{"name": o} for o in first_field["options"]]
                    }
                resp = await http.put(
                    f"{tbl}/fields/{default_fields[0]['field_id']}",
                    headers=headers,
                    json=update_json,
                )
                d = resp.json()
                if d.get("code") != 0:
                    logger.error("Update first field failed: %s", d)
                else:
                    field_names.append(first_field["name"])

                for f in fields[1:]:
                    ft = field_type_map.get(f.get("type", "text"), 1)
                    field_json: dict = {"field_name": f["name"], "type": ft}
                    if f.get("options"):
                        field_json["property"] = {
                            "options": [{"name": o} for o in f["options"]]
                        }
                    resp = await http.post(f"{tbl}/fields", headers=headers, json=field_json)
                    d = resp.json()
                    if d.get("code") != 0:
                        logger.error("Create field '%s' failed: %s", f["name"], d)
                    else:
                        field_names.append(f["name"])

            # Field type lookup for value coercion
            field_type_lookup: dict[str, str] = {}
            if fields:
                for f in fields:
                    field_type_lookup[f["name"]] = f.get("type", "text")

            # Step 3: add records
            record_errors = 0
            first_error: dict | None = None
            if records and field_names:
                for r in records:
                    filtered = (
                        {k: v for k, v in r.items() if k in field_names}
                        if field_names else r
                    )
                    if not filtered:
                        continue
                    for k, v in list(filtered.items()):
                        ft = field_type_lookup.get(k, "text")
                        if ft == "url" and isinstance(v, str):
                            filtered[k] = {"text": v, "link": v} if v else None
                    resp = await http.post(
                        f"{tbl}/records", headers=headers, json={"fields": filtered}
                    )
                    d = resp.json()
                    if d.get("code") != 0:
                        record_errors += 1
                        if first_error is None:
                            first_error = {
                                "api_code": d.get("code"),
                                "msg": d.get("msg"),
                                "sample_data": filtered,
                            }
                        if record_errors <= 3:
                            logger.error("Create record failed: %s (data=%s)", d, filtered)

            # Step 4: add views
            if views:
                view_type_map = {
                    "kanban": "kanban", "gallery": "gallery",
                    "gantt": "gantt", "form": "form", "grid": "grid",
                }
                for v in views:
                    vt = view_type_map.get(v.get("type", "grid"), "grid")
                    resp = await http.post(
                        f"{tbl}/views",
                        headers=headers,
                        json={"view_name": v.get("name", vt), "view_type": vt},
                    )
                    d = resp.json()
                    if d.get("code") != 0:
                        logger.warning("Create view failed: %s", d)

            # Step 5: optionally broadcast link
            if chat_id:
                await http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": url}),
                    },
                )

            result: dict = {
                "status": "ok",
                "app_token": app_token,
                "table_id": table_id,
                "url": url,
                "fields_created": len(field_names),
                "fields_requested": len(fields or []),
            }
            if record_errors:
                result["record_errors"] = record_errors
                result["first_error"] = first_error
            return result
        except Exception as e:
            logger.error("Create bitable failed: %s", e)
            return {"status": "error", "message": f"Create bitable failed: {e}"}
    return {"status": "error", "message": "Create bitable failed after retry"}


# ── bitable_records ──────────────────────────────────────────────


async def bitable_records(
    feishu,
    action: str,
    app_token: str,
    table_id: str,
    records: list | None = None,
    filter_str: str = "",
    page_size: int = 20,
) -> dict:
    """CRUD operations on Bitable records (list/create/update/delete)."""
    http = feishu.http
    token_provider = feishu.token

    for attempt in range(2):
        try:
            token = await token_provider.get()
            base_url = (
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
                f"{app_token}/tables/{table_id}/records"
            )
            headers = {"Authorization": f"Bearer {token}"}

            if action == "list":
                params: dict = {"page_size": min(page_size, 500)}
                if filter_str:
                    params["filter"] = filter_str
                resp = await http.get(base_url, headers=headers, params=params)
                data = resp.json()
                if data.get("code") != 0:
                    if attempt == 0 and is_token_error(data):
                        token_provider.invalidate()
                        continue
                    return {"status": "error", "message": data.get("msg", "")}
                items = data.get("data", {}).get("items", [])
                return {
                    "status": "ok",
                    "total": data.get("data", {}).get("total", 0),
                    "records": [
                        {"record_id": r.get("record_id"), "fields": r.get("fields", {})}
                        for r in items
                    ],
                }

            elif action == "create":
                if not records:
                    return {"status": "error", "message": "records required for create"}
                created = []
                for rec in records:
                    fields = (
                        rec
                        if not isinstance(rec, dict) or "fields" not in rec
                        else rec["fields"]
                    )
                    resp = await http.post(base_url, headers=headers, json={"fields": fields})
                    data = resp.json()
                    if data.get("code") == 0:
                        r = data.get("data", {}).get("record", {})
                        created.append({
                            "record_id": r.get("record_id"),
                            "fields": r.get("fields", {}),
                        })
                    elif attempt == 0 and is_token_error(data):
                        token_provider.invalidate()
                        break
                    else:
                        created.append({"error": data.get("msg", "")})
                else:
                    return {
                        "status": "ok",
                        "created": len([c for c in created if "record_id" in c]),
                        "records": created,
                    }
                continue  # retry after token refresh

            elif action == "update":
                if not records:
                    return {
                        "status": "error",
                        "message": "records required for update (each needs record_id + fields)",
                    }
                updated = []
                for rec in records:
                    rid = rec.get("record_id", "")
                    fields = rec.get("fields", {})
                    if not rid:
                        updated.append({"error": "missing record_id"})
                        continue
                    resp = await http.put(
                        f"{base_url}/{rid}", headers=headers, json={"fields": fields}
                    )
                    data = resp.json()
                    if data.get("code") == 0:
                        r = data.get("data", {}).get("record", {})
                        updated.append({
                            "record_id": r.get("record_id"),
                            "fields": r.get("fields", {}),
                        })
                    elif attempt == 0 and is_token_error(data):
                        token_provider.invalidate()
                        break
                    else:
                        updated.append({"record_id": rid, "error": data.get("msg", "")})
                else:
                    return {
                        "status": "ok",
                        "updated": len([u for u in updated if "error" not in u]),
                        "records": updated,
                    }
                continue

            elif action == "delete":
                if not records:
                    return {
                        "status": "error",
                        "message": "records required for delete (list of record_ids)",
                    }
                ids = [
                    r if isinstance(r, str) else r.get("record_id", "")
                    for r in records
                ]
                ids = [i for i in ids if i]
                if not ids:
                    return {"status": "error", "message": "no valid record_ids"}
                resp = await http.post(
                    f"{base_url}/batch_delete",
                    headers=headers,
                    json={"records": ids},
                )
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok", "deleted": len(ids)}
                if attempt == 0 and is_token_error(data):
                    token_provider.invalidate()
                    continue
                return {"status": "error", "message": data.get("msg", "")}

            else:
                return {"status": "error", "message": f"Unknown action: {action}"}

        except Exception as e:
            return {"status": "error", "message": f"Bitable error: {e}"}
    return {"status": "error", "message": "Bitable operation failed after retry"}


# ── manage_task ──────────────────────────────────────────────────


async def manage_task(
    feishu,
    action: str,
    summary: str = "",
    description: str = "",
    due: str = "",
    task_id: str = "",
    page_size: int = 20,
) -> dict:
    """Manage Feishu Tasks (v1 API)."""
    http = feishu.http
    token_provider = feishu.token

    for attempt in range(2):
        try:
            token = await token_provider.get()
            headers = {"Authorization": f"Bearer {token}"}
            base_url = "https://open.feishu.cn/open-apis/task/v1/tasks"

            if action == "create":
                if not summary:
                    return {"status": "error", "message": "summary required"}
                body: dict = {
                    "summary": summary,
                    "origin": {
                        "platform_i18n_name": '{"zh_cn": "小白", "en_us": "Xiaobai"}',
                        "href": {"url": "", "title": ""},
                    },
                }
                if description:
                    body["description"] = description
                if due:
                    body["due"] = {"time": due, "is_all_day": False}
                resp = await http.post(base_url, headers=headers, json=body)
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Manage task (create): token expired, refreshing and retrying")
                    token_provider.invalidate()
                    continue
                if data.get("code") == 0:
                    task = data.get("data", {}).get("task", {})
                    return {
                        "status": "ok",
                        "task_id": task.get("id", ""),
                        "summary": task.get("summary", ""),
                    }
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "list":
                resp = await http.get(
                    base_url, headers=headers, params={"page_size": min(page_size, 50)}
                )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Manage task (list): token expired, refreshing and retrying")
                    token_provider.invalidate()
                    continue
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    tasks = []
                    for t in items:
                        tasks.append({
                            "task_id": t.get("id", ""),
                            "summary": t.get("summary", ""),
                            "description": t.get("description", ""),
                            "completed": t.get("complete_time", "0") != "0",
                            "due": t.get("due", {}).get("time", "") if t.get("due") else "",
                        })
                    return {"status": "ok", "count": len(tasks), "tasks": tasks}
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "update":
                if not task_id:
                    return {"status": "error", "message": "task_id required"}
                body = {}
                if summary:
                    body["summary"] = summary
                if description:
                    body["description"] = description
                if due:
                    body["due"] = {"timestamp": due, "is_all_day": False}
                resp = await http.patch(f"{base_url}/{task_id}", headers=headers, json=body)
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Manage task (update): token expired, refreshing and retrying")
                    token_provider.invalidate()
                    continue
                if data.get("code") == 0:
                    return {"status": "ok", "task_id": task_id}
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "complete":
                if not task_id:
                    return {"status": "error", "message": "task_id required"}
                resp = await http.post(
                    f"{base_url}/{task_id}/complete", headers=headers, json={}
                )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Manage task (complete): token expired, refreshing and retrying")
                    token_provider.invalidate()
                    continue
                if data.get("code") == 0:
                    return {"status": "ok", "task_id": task_id, "completed": True}
                return {"status": "error", "message": data.get("msg", "")}

            else:
                return {"status": "error", "message": f"Unknown action: {action}"}
        except Exception as e:
            return {"status": "error", "message": f"Task error: {e}"}
    return {"status": "error", "message": "Task operation failed after retry"}
