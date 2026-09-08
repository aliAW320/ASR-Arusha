import json
from dataclasses import dataclass
from typing import Any

import httpx


class OutlineMCPError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class OutlineDestination:
    collection_id: str
    parent_document_id: str | None
    node: dict[str, Any] | None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


class OutlineMCPClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        client: httpx.AsyncClient | None = None,
    ):
        endpoint = base_url.rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"
        self.endpoint = endpoint if endpoint.endswith("/mcp") else f"{endpoint}/mcp"
        self.api_key = api_key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._id = 0

    @staticmethod
    def _decode_response(raw: str) -> dict[str, Any]:
        data_lines = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
        candidate = data_lines[-1] if data_lines else raw
        try:
            return json.loads(candidate)
        except ValueError as error:
            raise OutlineMCPError(
                "Outline MCP returned an invalid JSON/SSE response",
                code="mcp_invalid_response",
                retryable=True,
            ) from error

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        try:
            response = await self.client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": self._id,
                    "method": method,
                    "params": params,
                },
            )
        except httpx.TimeoutException as error:
            raise OutlineMCPError(
                "Outline MCP request timed out", code="mcp_timeout", retryable=True
            ) from error
        except httpx.TransportError as error:
            raise OutlineMCPError(
                "Outline MCP network request failed",
                code="mcp_network_error",
                retryable=True,
            ) from error
        if response.status_code >= 400:
            raise OutlineMCPError(
                f"Outline MCP returned HTTP {response.status_code}: {response.text[:500]}",
                code=(
                    "mcp_authentication_failed"
                    if response.status_code in {401, 403}
                    else f"mcp_http_{response.status_code}"
                ),
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        payload = self._decode_response(response.text)
        if payload.get("error"):
            error = payload["error"]
            raise OutlineMCPError(
                f"Outline MCP error: {error}",
                code="mcp_rpc_error",
                retryable=False,
            )
        return payload.get("result") or {}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[Any]:
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if result.get("isError"):
            message = " ".join(
                str(item.get("text", ""))
                for item in result.get("content") or []
                if isinstance(item, dict)
            )
            raise OutlineMCPError(
                f"Outline tool {name} failed: {message[:1000]}",
                code=f"mcp_{name}_failed",
                retryable=False,
            )
        values: list[Any] = []
        if result.get("structuredContent") is not None:
            values.append(result["structuredContent"])
        for item in result.get("content") or []:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                values.append(json.loads(item["text"]))
            except ValueError:
                values.append(item["text"])
        return values

    async def resolve_destination(self, path: str) -> OutlineDestination:
        parts = path.split("/")
        collection_values = await self.call_tool(
            "list_collections", {"query": parts[0], "offset": 0, "limit": 100}
        )
        collections = [
            item
            for value in collection_values
            for item in _walk_dicts(value)
            if item.get("id") and (item.get("name") or item.get("title"))
        ]
        collection = next(
            (
                item
                for item in collections
                if (item.get("name") or item.get("title")) == parts[0]
            ),
            None,
        )
        if collection is None:
            raise OutlineMCPError(
                f"Outline destination collection does not exist: {parts[0]}",
                code="destination_not_found",
                retryable=False,
            )
        collection_id = str(collection["id"])
        if len(parts) == 1:
            return OutlineDestination(collection_id, None, None)

        tree_values = await self.call_tool(
            "list_collection_documents", {"collectionId": collection_id}
        )
        roots = [
            node
            for value in tree_values
            for node in (value if isinstance(value, list) else [value])
            if isinstance(node, dict) and node.get("id") and node.get("title")
        ]
        current = None
        siblings = roots
        for title in parts[1:]:
            current = next((node for node in siblings if node.get("title") == title), None)
            if current is None:
                raise OutlineMCPError(
                    f"Outline destination document does not exist: {path}",
                    code="destination_not_found",
                    retryable=False,
                )
            siblings = current.get("children") or []
        return OutlineDestination(collection_id, str(current["id"]), current)

    @staticmethod
    def find_child(node: dict[str, Any] | None, title: str) -> str | None:
        if node is None:
            return None
        child = next(
            (item for item in node.get("children") or [] if item.get("title") == title),
            None,
        )
        return str(child["id"]) if child else None

    @staticmethod
    def _document_id(values: list[Any]) -> str:
        for value in values:
            for item in _walk_dicts(value):
                if item.get("id") and any(
                    key in item for key in ("title", "url", "collectionId", "document")
                ):
                    return str(item["id"])
        raise OutlineMCPError(
            "Outline did not return the created document id",
            code="mcp_invalid_response",
            retryable=True,
        )

    async def upsert_document(
        self,
        *,
        document_id: str | None,
        title: str,
        text: str,
        collection_id: str | None = None,
        parent_document_id: str | None = None,
    ) -> str:
        if document_id:
            await self.call_tool(
                "update_document",
                {
                    "id": document_id,
                    "title": title,
                    "text": text,
                    "editMode": "replace",
                    "publish": True,
                },
            )
            return document_id
        arguments: dict[str, Any] = {
            "title": title,
            "text": text,
            "format": "markdown",
            "publish": True,
        }
        if parent_document_id:
            arguments["parentDocumentId"] = parent_document_id
        elif collection_id:
            arguments["collectionId"] = collection_id
        else:
            raise ValueError("A document location is required")
        return self._document_id(await self.call_tool("create_document", arguments))

    async def upload_attachment(
        self, *, name: str, content_type: str, content: bytes
    ) -> str:
        values = await self.call_tool(
            "create_attachment",
            {"contentType": content_type, "name": name, "size": len(content)},
        )
        candidates = [item for value in values for item in _walk_dicts(value)]
        upload_url = next(
            (
                item.get("uploadUrl") or item.get("upload_url")
                for item in candidates
                if item.get("uploadUrl") or item.get("upload_url")
            ),
            None,
        )
        fields = next(
            (
                item.get("form") or item.get("fields")
                for item in candidates
                if isinstance(item.get("form") or item.get("fields"), dict)
            ),
            {},
        )
        attachment = next(
            (
                item["attachment"]
                for item in candidates
                if isinstance(item.get("attachment"), dict)
            ),
            None,
        )
        if not upload_url:
            raise OutlineMCPError(
                "Outline did not return an attachment upload URL",
                code="mcp_invalid_attachment_response",
                retryable=True,
            )
        try:
            if fields:
                response = await self.client.post(
                    upload_url,
                    data=fields,
                    files={"file": (name, content, content_type)},
                )
            else:
                response = await self.client.put(
                    upload_url,
                    content=content,
                    headers={"Content-Type": content_type},
                )
        except httpx.TransportError as error:
            raise OutlineMCPError(
                "Outline attachment upload failed",
                code="mcp_attachment_upload_failed",
                retryable=True,
            ) from error
        if response.status_code >= 400:
            raise OutlineMCPError(
                f"Outline attachment upload returned HTTP {response.status_code}",
                code="mcp_attachment_upload_failed",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        attachment = attachment or next(
            (
                item
                for item in candidates
                if item.get("url") and item.get("url") != upload_url
            ),
            None,
        )
        if not attachment or not attachment.get("url"):
            raise OutlineMCPError(
                "Outline did not return a stable attachment URL",
                code="mcp_invalid_attachment_response",
                retryable=True,
            )
        return str(attachment["url"])

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
