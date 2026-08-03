# services/paperless.py

import asyncio
import time

import httpx

from models.paperless_document import PaperlessDocument


# Why a login attempt failed. The caller turns these into translated text — this
# module has no user context and must not build UI strings.
AUTH_NOT_CONFIGURED = "not_configured"   # PAPERLESS_URL never filled in
AUTH_UNREACHABLE = "unreachable"         # DNS / refused / timeout
AUTH_INVALID = "invalid"                 # Paperless rejected the credentials
AUTH_SERVER_ERROR = "server_error"       # reachable, but answered with an error

# Hostnames shipped in .env.example. Someone who starts the app without editing
# .env lands on a name that does not resolve, and "check your details" would send
# them hunting through their password instead of their config.
_PLACEHOLDER_MARKERS = (".example.", "example.lan", "example.com", "changeme")


def is_placeholder_url(base_url: str) -> bool:
    """True when PAPERLESS_URL is empty or still the .env.example value."""
    url = (base_url or "").strip().lower()
    return not url or any(m in url for m in _PLACEHOLDER_MARKERS)


def authenticate(
    base_url: str, username: str, password: str
) -> tuple[str | None, str]:
    """Fetch a Paperless API token. Returns (token, "") or (None, reason).

    The reason matters: a wrong password and an unreachable server used to be
    indistinguishable, so a fresh install with an unedited .env told the user to
    check credentials that were perfectly fine.
    """
    if is_placeholder_url(base_url):
        return None, AUTH_NOT_CONFIGURED
    try:
        r = httpx.post(
            f"{base_url.rstrip('/')}/api/token/",
            json={"username": username, "password": password},
            timeout=5,
        )
    except Exception:
        # httpx raises RequestError for DNS/connect/timeout, but a malformed URL
        # surfaces as other types — none of them mean "bad password".
        return None, AUTH_UNREACHABLE

    if r.status_code in (400, 401, 403):
        return None, AUTH_INVALID
    if r.status_code >= 400:
        return None, AUTH_SERVER_ERROR
    try:
        token = r.json().get("token")
    except Exception:
        return None, AUTH_SERVER_ERROR
    return (token, "") if token else (None, AUTH_SERVER_ERROR)


def get_token(base_url: str, username: str, password: str) -> str | None:
    """Token or None. Use authenticate() when the failure reason matters."""
    return authenticate(base_url, username, password)[0]


class PaperlessClient:
    """Wraps the Paperless-ngx REST API."""

    _MAP_TTL = 60.0  # seconds — tags/correspondents/types/users change rarely

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Token {token}"}
        self._maps_cache: tuple[dict, dict, dict, dict] | None = None
        self._maps_ts: float = 0.0

    async def _fetch_maps(self) -> tuple[dict, dict, dict, dict]:
        """Return (tag_map, corr_map, dt_map, user_map), cached per client for _MAP_TTL.

        These were previously re-fetched on every list_documents call (4 extra
        requests each). They change rarely, so a short TTL cache is safe and cuts
        request volume dramatically for chat sessions that list documents often.
        """
        now = time.monotonic()
        if self._maps_cache is not None and now - self._maps_ts < self._MAP_TTL:
            return self._maps_cache
        try:
            # Explicit timeout: httpx's 5 s default is too tight for a Paperless
            # busy with its own consumer, and a read timeout here used to abort
            # whatever called it — including a running sync.
            async with httpx.AsyncClient(headers=self.headers, timeout=30) as client:
                tags_resp, corr_resp, dt_resp, users_resp = await asyncio.gather(
                    client.get(f"{self.base_url}/api/tags/?page_size=1000"),
                    client.get(f"{self.base_url}/api/correspondents/?page_size=1000"),
                    client.get(f"{self.base_url}/api/document_types/?page_size=1000"),
                    client.get(f"{self.base_url}/api/users/?page_size=100"),
                )
            for resp in (tags_resp, corr_resp, dt_resp, users_resp):
                resp.raise_for_status()
            maps = (
                {t["id"]: t["name"] for t in tags_resp.json()["results"]},
                {c["id"]: c["name"] for c in corr_resp.json()["results"]},
                {d["id"]: d["name"] for d in dt_resp.json()["results"]},
                {u["id"]: u["username"] for u in users_resp.json().get("results", [])},
            )
        except Exception as exc:
            # Tags/correspondents/types change rarely, so serving the previous
            # maps past their TTL beats failing the caller. Only a client that
            # never fetched successfully has nothing to fall back on.
            if self._maps_cache is None:
                raise
            print(
                f"[paperless] metadata map refresh failed "
                f"({type(exc).__name__}: {exc}) — serving stale maps",
                flush=True,
            )
            self._maps_ts = now  # don't hammer a struggling server every call
            return self._maps_cache
        self._maps_cache = maps
        self._maps_ts = now
        return maps

    async def get_user_map(self) -> dict[int, str]:
        return (await self._fetch_maps())[3]

    async def get_tag_map(self) -> dict[int, str]:
        return (await self._fetch_maps())[0]

    async def get_correspondent_map(self) -> dict[int, str]:
        return (await self._fetch_maps())[1]

    async def get_document_types_map(self) -> dict[int, str]:
        return (await self._fetch_maps())[2]

    async def list_users(self) -> dict[int, str]:
        """Return {user_id: username} for all Paperless users."""
        return (await self._fetch_maps())[3]

    async def list_documents(
        self,
        page: int = 1,
        page_size: int = 100,
        fetch_all: bool = True,
        ids: list[int] | None = None,
        query: str | None = None,
        correspondent: list[int] | None = None,
        document_type: list[int] | None = None,
        tags: list[int] | None = None,
        tag_ids_any: list[int] | None = None,
        created: dict[str:str] | None = None,
        added: dict[str:str] | None = None,
        owner: int | None = None,
    ) -> list[PaperlessDocument]:
        """Fetch a page / all of documents, optionally filtered by IDs."""
        # List of tuples supports repeated keys (needed for tags__id__all)
        filter_params: list[tuple] = [("page_size", page_size)]

        if ids:
            filter_params.append(("id__in", ",".join(str(i) for i in ids)))
        if query:
            filter_params.append(("search", query))
        if correspondent:
            filter_params.append(
                ("correspondent__id__in", ",".join(str(c) for c in correspondent))
            )
        if document_type:
            filter_params.append(
                ("document_type__id__in", ",".join(str(d) for d in document_type))
            )
        # tags filtered client-side after fetch (server-side tags__id__all is broken
        # for repeated params — Django Filter base class takes last-wins)
        if tag_ids_any:
            filter_params.append(("tags__id__in", ",".join(str(i) for i in tag_ids_any)))
        if created:
            if created.get("after"):
                filter_params.append(("created__gt", created["after"]))
            if created.get("before"):
                filter_params.append(("created__lt", created["before"]))
        if added:
            if added.get("after"):
                filter_params.append(("added__gt", added["after"]))
            if added.get("before"):
                filter_params.append(("added__lt", added["before"]))
        if owner is not None:
            filter_params.append(("owner__id__in", str(owner)))

        results = []
        # Fetch the (cached) metadata maps concurrently with the first docs page.
        maps_task = asyncio.create_task(self._fetch_maps())
        async with httpx.AsyncClient(headers=self.headers) as client:
            docs_resp = await client.get(
                f"{self.base_url}/api/documents/",
                params=filter_params + [("page", page)],
            )
            response = docs_resp.json()
            results.extend(response["results"])
            if fetch_all:
                while response["next"]:
                    page += 1
                    docs_resp = await client.get(
                        f"{self.base_url}/api/documents/",
                        params=filter_params + [("page", page)],
                    )
                    response = docs_resp.json()
                    results.extend(response["results"])

        tag_map, corr_map, dt_map, user_map = await maps_task

        if tags:
            tag_set = set(tags)
            results = [r for r in results if tag_set.issubset(r["tags"])]

        return [
            self._parse_document(doc, tag_map, corr_map, dt_map, user_map)
            for doc in results
        ]

    async def list_specific_fields(
        self,
        page: int = 1,
        page_size: int = 100,
        fetch_all: bool = True,
        tags: list[int] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Fetch optional fields of a page / all of documents, optionally filtered by tag names."""
        params = {"page": page, "page_size": page_size}
        if fields:
            params["fields"] = ",".join(fields)

        results = []
        async with httpx.AsyncClient(headers=self.headers) as client:
            docs_resp = await client.get(
                f"{self.base_url}/api/documents/", params=params
            )
            response = docs_resp.json()
            results.extend(response["results"])
            if fetch_all:
                while response["next"]:
                    page += 1
                    params.update({"page": page})
                    docs_resp = await client.get(
                        f"{self.base_url}/api/documents/", params=params
                    )
                    response = docs_resp.json()
                    results.extend(response["results"])
        if tags:
            tag_set = set(tags)
            results = [r for r in results if tag_set.issubset(r["tags"])]

        return results

    async def get_document(self, doc_id: int) -> PaperlessDocument:
        """Get a specific document from paperless by id"""
        return (await self.list_documents(ids=[doc_id]))[0]

    async def download_document(self, doc_id: int) -> bytes:
        """Download a specific document as bytes by id"""
        async with httpx.AsyncClient(headers=self.headers, timeout=60) as client:
            resp = await client.get(f"{self.base_url}/api/documents/{doc_id}/download/")
            resp.raise_for_status()
            return resp.content

    async def download_document_named(self, doc_id: int) -> tuple[bytes, str]:
        """Download a document's original file; returns (bytes, filename).

        Filename comes from the Content-Disposition header Paperless sends,
        so the extension matches the actual original file type.
        """
        import re as _re

        async with httpx.AsyncClient(headers=self.headers, timeout=60) as client:
            resp = await client.get(f"{self.base_url}/api/documents/{doc_id}/download/")
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            m = _re.search(r'filename="?([^";]+)"?', cd)
            filename = m.group(1) if m else f"dokument_{doc_id}.pdf"
            return resp.content, filename

    async def get_thumbnail(self, doc_id: int) -> bytes:
        async with httpx.AsyncClient(headers=self.headers) as client:
            response = await client.get(
                f"{self.base_url}/api/documents/{doc_id}/thumb/"
            )
        response.raise_for_status()
        return response.content

    async def update_document_content(self, doc_id: int, content: str) -> None:
        """Overwrite the OCR text Paperless stores for a document.

        Paperless keeps no history of the text: the previous OCR result is gone
        after this call, and re-running OCR (reprocess, rotate, split, merge)
        overwrites it back. Callers must have decided that the new text is the
        better one.
        """
        async with httpx.AsyncClient(headers=self.headers, timeout=60) as client:
            resp = await client.patch(
                f"{self.base_url}/api/documents/{doc_id}/",
                json={"content": content},
            )
        resp.raise_for_status()

    async def create_note(self, doc_id: int, note: str) -> str:
        """Add a note to a document. Returns "" on success, else the error text.

        Paperless attributes the note to the owner of the token in use, so this
        must be called with the user's own client — never the superuser one.
        Requires `change_document` on the document; Paperless answers 403 when
        the user may only view it, and 200 with an ``error`` key when saving
        itself fails.
        """
        async with httpx.AsyncClient(headers=self.headers, timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/documents/{doc_id}/notes/",
                json={"note": note},
            )
        if resp.status_code == 403:
            return "no permission to add notes to this document"
        if resp.status_code == 404:
            return "document not found"
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            return ""
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
        return ""

    def _get_or_create_sync(self, endpoint: str, name: str) -> int:
        """Sync helper: find entity by name or create it; returns ID."""
        import requests as _req
        resp = _req.get(
            f"{self.base_url}/api/{endpoint}/",
            headers=self.headers,
            params={"name__iexact": name, "page_size": 20},
            timeout=15,
        )
        resp.raise_for_status()
        for item in resp.json().get("results", []):
            if item["name"].lower() == name.lower():
                return item["id"]
        resp2 = _req.post(
            f"{self.base_url}/api/{endpoint}/",
            headers=self.headers,
            json={"name": name},
            timeout=15,
        )
        resp2.raise_for_status()
        return resp2.json()["id"]

    def _upload_document_sync(
        self,
        pdf_bytes: bytes,
        filename: str,
        title: str,
        tag_names: list[str],
        correspondent_name: str | None,
        document_type_name: str | None,
    ) -> str:
        import requests as _req
        from datetime import date as _date

        tag_ids = [self._get_or_create_sync("tags", n) for n in tag_names if n]
        correspondent_id = (
            self._get_or_create_sync("correspondents", correspondent_name)
            if correspondent_name else None
        )
        doc_type_id = (
            self._get_or_create_sync("document_types", document_type_name)
            if document_type_name else None
        )

        form_data: list[tuple[str, str]] = [
            ("title", title),
            ("created", _date.today().isoformat()),
        ]
        if correspondent_id is not None:
            form_data.append(("correspondent", str(correspondent_id)))
        if doc_type_id is not None:
            form_data.append(("document_type", str(doc_type_id)))
        for tid in tag_ids:
            form_data.append(("tags", str(tid)))

        resp = _req.post(
            f"{self.base_url}/api/documents/post_document/",
            headers=self.headers,
            data=form_data,
            files={"document": (filename, pdf_bytes, "application/pdf")},
            timeout=60,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
            if isinstance(body, dict):
                return str(body.get("task_id", "ok"))
            return str(body)
        except Exception:
            return "ok"

    async def upload_document(
        self,
        pdf_bytes: bytes,
        filename: str,
        title: str,
        tag_names: list[str],
        correspondent_name: str | None = None,
        document_type_name: str | None = None,
    ) -> str:
        """Upload PDF to Paperless. Returns the task_id string."""
        import asyncio
        return await asyncio.to_thread(
            self._upload_document_sync,
            pdf_bytes, filename, title, tag_names, correspondent_name, document_type_name,
        )

    async def poll_task_result(
        self,
        upload_task_id: str,
        timeout: int = 90,
        interval: float = 3.0,
    ) -> int | None:
        """Poll Paperless /api/tasks/ until the document ID is available.

        Returns the document ID (int) on SUCCESS, or None on timeout / FAILURE.
        Paperless processes uploads asynchronously — typical delay is 5–30 s.
        """
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(headers=self.headers, timeout=10) as client:
                    resp = await client.get(
                        f"{self.base_url}/api/tasks/",
                        params={"task_id": upload_task_id},
                    )
                    resp.raise_for_status()
                    items = resp.json()
                    if isinstance(items, list) and items:
                        item = items[0]
                        status = item.get("status", "")
                        if status == "SUCCESS":
                            doc_id = item.get("related_document")
                            return int(doc_id) if doc_id else None
                        if status in ("FAILURE", "REVOKED"):
                            return None
            except Exception:
                pass
            await asyncio.sleep(interval)
        return None

    def _parse_document(
        self,
        raw: dict,
        tag_map: dict,
        corr_map: dict,
        dt_map: dict,
        user_map: dict | None = None,
    ) -> PaperlessDocument:
        """Convert raw API dict into a typed PaperlessDocument."""
        owner_id = raw.get("owner")
        raw.update(
            {
                "tags": [tag_map.get(t, str(t)) for t in raw["tags"]],
                "correspondent": corr_map.get(raw["correspondent"]),
                "document_type": dt_map.get(raw["document_type"]),
                "owner_name": (user_map or {}).get(owner_id) if owner_id else None,
            }
        )
        return PaperlessDocument(
            **raw,
            pdf_url=f"{self.base_url}/api/documents/{raw['id']}/download/",
        )
