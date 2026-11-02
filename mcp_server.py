"""Lets tools you connect work with your archive, through the Model Context Protocol.

It speaks JSON-RPC over stdin and stdout, so it opens no network connection. A tool
such as Claude Desktop starts it as a child process. Nothing is available until
"Let connected tools read this archive" is on in Settings, and changing anything
needs a second switch. Both are read for every request, so turning one off takes
effect at once. Only the archive is reachable, never files that were not added.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from offline import enforce_offline
from core import ArchiveCore, data_location

NAME = "roostfile"
VERSION = "0.1.0"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2024-11-05")
MAX_RESULTS = 50
MAX_TEXT = 20_000
NOT_ALLOWED = "Roostfile has not allowed this. Turn it on in Settings, under connected tools."
TOOLS = (
    {"name": "search_files", "description": "Search the archive by words in file names, text and tags. Chinese and English both work.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "The words to look for"},
         "limit": {"type": "integer", "description": f"Most results to return, up to {MAX_RESULTS}", "default": 10}}}},
    {"name": "read_file_text", "description": "Read the text of a file found by search_files, including words read from images.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "required": ["file_id"], "properties": {
         "file_id": {"type": "integer"},
         "offset": {"type": "integer", "description": "Character to start from", "default": 0}}}},
    {"name": "list_collections", "description": "List the collections the person has made.",
     "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_collection_files", "description": "List the files in one collection.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "required": ["collection"], "properties": {"collection": {"type": "string", "description": "The collection's name"}}}},
    {"name": "list_linked_files", "description": "List the files the person has linked to this one, directly or through others, which usually belong to the same piece of work.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "required": ["file_id"], "properties": {"file_id": {"type": "integer"}}}},
    {"name": "link_files", "description": "Link files that belong together. Files are never moved. Needs write access in Settings.",
     "annotations": {"readOnlyHint": False, "destructiveHint": False},
     "inputSchema": {"type": "object", "required": ["file_ids"], "properties": {
         "file_ids": {"type": "array", "items": {"type": "integer"}, "description": "Two or more files. The first is linked to each of the others."}}}},
    {"name": "add_to_collection", "description": "Add files to a collection, creating it if needed. Files are never moved. Needs write access in Settings.",
     "annotations": {"readOnlyHint": False, "destructiveHint": False},
     "inputSchema": {"type": "object", "required": ["collection", "file_ids"], "properties": {
         "collection": {"type": "string"}, "file_ids": {"type": "array", "items": {"type": "integer"}}}}},
)


class ToolError(Exception):
    """A problem the connected tool should show or act on, not a protocol failure."""


class Server:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.core = None

    def allowed(self, key):
        try:
            return json.loads((self.data_dir / "ui-state.json").read_text(encoding="utf-8")).get(key) is True
        except (OSError, ValueError, AttributeError):
            return False

    def archive(self):
        if not self.allowed("mcp"):
            raise ToolError(NOT_ALLOWED)
        if self.core is None:
            self.core = ArchiveCore(self.data_dir)
        return self.core

    @staticmethod
    def summary(row):
        try:
            modified = datetime.fromtimestamp(row["mtime"]).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            modified = None  # Windows has no date for a file from before 1970.
        return {"id": row["id"], "name": row["name"], "path": row["path"], "type": row["kind"], "tags": row["tags"],
                "modified": modified, "missing": bool(row["missing"])}

    def collection_named(self, core, name):
        wanted = str(name).strip().casefold()
        found = next((row for row in core.collections() if row["name"].casefold() == wanted), None)
        if found is None:
            raise ToolError(f'There is no collection called "{name}".')
        return found

    def search_files(self, query, limit=10):
        core = self.archive()
        page = core.search_page(str(query), limit=max(1, min(int(limit), MAX_RESULTS)))
        return {"total": page["total"], "files": [self.summary(row) for row in page["rows"]]}

    def read_file_text(self, file_id, offset=0):
        row = self.archive().get_file(int(file_id))
        if row is None:
            raise ToolError(f"There is no file with id {file_id}.")
        text = row.get("text") or ""
        start = max(0, int(offset))
        return {"name": row["name"], "text": text[start:start + MAX_TEXT], "length": len(text), "more": start + MAX_TEXT < len(text)}

    def list_collections(self):
        return {"collections": [{"name": row["name"], "files": row["count"]} for row in self.archive().collections()]}

    def list_collection_files(self, collection):
        core = self.archive()
        page = core.search_page(collection=self.collection_named(core, collection)["id"], limit=MAX_RESULTS)
        return {"total": page["total"], "files": [self.summary(row) for row in page["rows"]]}

    def known_ids(self, core, file_ids):
        if not isinstance(file_ids, list):
            raise ToolError("file_ids must be a list of numbers.")
        ids = [int(file_id) for file_id in file_ids]
        missing = [file_id for file_id in ids if core.get_file(file_id) is None]
        if missing:
            raise ToolError(f"There are no files with ids {missing}.")
        return ids

    def list_linked_files(self, file_id):
        core = self.archive()
        file_id = self.known_ids(core, [file_id])[0]
        return {"files": [self.summary(row) for row in core.bundle(file_id) if row["id"] != file_id][:MAX_RESULTS]}

    def link_files(self, file_ids):
        core = self.archive()
        if not self.allowed("mcp_write"):
            raise ToolError(NOT_ALLOWED)
        ids = self.known_ids(core, file_ids)
        if len(set(ids)) < 2:
            raise ToolError("Link needs at least two different files.")
        return {"linked": core.link(ids)}

    def add_to_collection(self, collection, file_ids):
        core = self.archive()
        if not self.allowed("mcp_write"):
            raise ToolError(NOT_ALLOWED)
        ids = self.known_ids(core, file_ids)
        try:
            target = self.collection_named(core, collection)["id"]
        except ToolError:
            target = core.create_collection(collection)
        core.collect(target, ids)
        return {"added": len(ids), "collection": str(collection).strip()}

    def call(self, name, arguments):
        if name not in [tool["name"] for tool in TOOLS]:
            raise ToolError(f"There is no tool called {name}.")
        try:
            return getattr(self, name)(**arguments)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ToolError(f"Those arguments do not fit {name}: {exc}") from None

    def handle(self, message):
        """The reply to one JSON-RPC message, or None for a notification. Whatever goes wrong, the server stays up."""
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "This was not a JSON-RPC request."}}
        if message.get("id") is None:
            return None
        try:
            return self.answer(message.get("method"), message.get("params") or {}, message["id"])
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": f"Roostfile could not answer: {exc}"}}

    def answer(self, method, params, identifier):
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "params must be an object."}}
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {}},
                      "serverInfo": {"name": NAME, "version": VERSION}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list(TOOLS)}
        elif method == "tools/call":
            try:
                reply = self.call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}], "isError": False}
            except ToolError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(data_dir, incoming=None, outgoing=None):
    server = Server(data_dir)
    for line in incoming or sys.stdin:
        if not line.strip():
            continue
        try:
            reply = server.handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "This was not valid JSON."}}
        if reply is not None:
            # Escaped, so text no encoding can write still goes out.
            (outgoing or sys.stdout).write(json.dumps(reply) + "\n")
            (outgoing or sys.stdout).flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_location(BASE), help="The archive folder")
    args = parser.parse_args()
    enforce_offline()
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    serve(args.data)


if __name__ == "__main__":
    main()
