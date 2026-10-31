"""The MCP server, spoken to the way a connected tool does. Needs no Qt."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_server
from core import ArchiveCore
from mcp_server import NOT_ALLOWED, MAX_TEXT, Server, serve

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.data = self.root / "data"
        source = self.root / "source"
        source.mkdir()
        (source / "租房合同.txt").write_text("房东与租客的约定，押金两个月", encoding="utf-8")
        (source / "周报.txt").write_text("这周做完了首页", encoding="utf-8")
        (source / "长文.txt").write_text("字" * (MAX_TEXT + 10), encoding="utf-8")
        self.outside = self.root / "没有收录.txt"
        self.outside.write_text("这份没有加入资料库", encoding="utf-8")
        core = ArchiveCore(self.data)
        core.add_source(source)
        core.scan_all()
        self.core = core
        self.server = Server(self.data)

    def allow(self, **settings):
        (self.data / "ui-state.json").write_text(json.dumps(settings), encoding="utf-8")

    def call(self, name, **arguments):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
        text = reply["content"][0]["text"]
        return (text, True) if reply["isError"] else (json.loads(text), False)


class PermissionTests(Base):
    def test_nothing_is_available_until_it_is_turned_on(self):
        for settings in (None, {}, {"mcp": False}, {"mcp": "yes"}, []):
            if settings is not None:
                (self.data / "ui-state.json").write_text(json.dumps(settings), encoding="utf-8")
            with self.subTest(settings=settings):
                self.assertEqual(self.call("search_files", query="合同"), (NOT_ALLOWED, True))

    def test_a_damaged_settings_file_means_no(self):
        (self.data / "ui-state.json").write_text('{"mcp": tr', encoding="utf-8")
        self.assertEqual(self.call("list_collections"), (NOT_ALLOWED, True))

    def test_turning_it_off_takes_effect_on_the_next_request(self):
        self.allow(mcp=True)
        self.assertFalse(self.call("search_files", query="合同")[1])
        self.allow(mcp=False)
        self.assertEqual(self.call("search_files", query="合同"), (NOT_ALLOWED, True))

    def test_reading_does_not_allow_changing(self):
        self.allow(mcp=True)
        file_id = self.call("search_files", query="合同")[0]["files"][0]["id"]
        self.assertEqual(self.call("add_to_collection", collection="家", file_ids=[file_id]), (NOT_ALLOWED, True))
        self.assertEqual(self.core.collections(), [])

    def test_changing_alone_does_not_allow_anything(self):
        self.allow(mcp_write=True)
        self.assertEqual(self.call("add_to_collection", collection="家", file_ids=[1]), (NOT_ALLOWED, True))


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.allow(mcp=True, mcp_write=True)

    def test_a_tool_can_find_a_file_and_read_it(self):
        found, failed = self.call("search_files", query="押金")
        self.assertFalse(failed)
        self.assertEqual(found["total"], 1)
        entry = found["files"][0]
        self.assertEqual(entry["name"], "租房合同.txt")
        self.assertRegex(entry["modified"], r"^\d{4}-\d\d-\d\dT")
        text, failed = self.call("read_file_text", file_id=entry["id"])
        self.assertFalse(failed)
        self.assertIn("押金两个月", text["text"])
        self.assertFalse(text["more"])

    def test_results_are_capped(self):
        self.assertEqual(len(self.call("search_files", query="", limit=1000)[0]["files"]), 3)
        self.assertEqual(len(self.call("search_files", query="", limit=1)[0]["files"]), 1)

    def test_long_text_comes_back_in_parts(self):
        file_id = self.call("search_files", query="长文")[0]["files"][0]["id"]
        first = self.call("read_file_text", file_id=file_id)[0]
        self.assertEqual(len(first["text"]), MAX_TEXT)
        self.assertTrue(first["more"])
        rest = self.call("read_file_text", file_id=file_id, offset=MAX_TEXT)[0]
        self.assertEqual(len(rest["text"]), 10)
        self.assertFalse(rest["more"])

    def test_only_what_is_in_the_archive_can_be_reached(self):
        self.assertEqual(self.call("search_files", query="没有收录")[0]["total"], 0)
        self.assertTrue(self.call("read_file_text", file_id=9999)[1])
        self.assertTrue(self.call("read_file_text", file_id=str(self.outside))[1])
        self.assertFalse(any(tool["name"] in ("read_file", "read_path") for tool in mcp_server.TOOLS))

    def test_files_go_into_a_collection_without_being_touched(self):
        entry = self.call("search_files", query="合同")[0]["files"][0]
        before = Path(entry["path"]).read_bytes()
        self.assertEqual(self.call("add_to_collection", collection="家", file_ids=[entry["id"]])[0], {"added": 1, "collection": "家"})
        self.assertEqual(self.call("add_to_collection", collection="家", file_ids=[entry["id"]])[0]["added"], 1)
        self.assertEqual(self.call("list_collections")[0], {"collections": [{"name": "家", "files": 1}]})
        self.assertEqual(self.call("list_collection_files", collection="家")[0]["files"][0]["id"], entry["id"])
        self.assertEqual(Path(entry["path"]).read_bytes(), before)

    def test_mistakes_come_back_as_messages_a_tool_can_act_on(self):
        for name, arguments in (("no_such_tool", {}), ("search_files", {}), ("search_files", {"query": "x", "limit": "many"}),
                                ("search_files", {"query": "x", "extra": 1}), ("list_collection_files", {"collection": "没有"}),
                                ("add_to_collection", {"collection": "家", "file_ids": [9999]}),
                                ("add_to_collection", {"collection": " ", "file_ids": []})):
            with self.subTest(name=name, arguments=arguments):
                self.assertTrue(self.call(name, **arguments)[1])
        self.assertEqual(self.core.collections(), [])


class ProtocolTests(Base):
    def exchange(self, *messages):
        incoming = [json.dumps(message) if not isinstance(message, str) else message for message in messages]
        outgoing = io.StringIO()
        serve(self.data, incoming, outgoing)
        return [json.loads(line) for line in outgoing.getvalue().splitlines()]

    def test_the_handshake_describes_the_server(self):
        reply = self.exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})[0]["result"]
        self.assertEqual(reply["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["serverInfo"]["name"], "roostfile")
        self.assertIn("tools", reply["capabilities"])

    def test_an_unknown_protocol_gets_the_newest_this_server_speaks(self):
        reply = self.exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})[0]["result"]
        self.assertEqual(reply["protocolVersion"], mcp_server.PROTOCOLS[0])

    def test_notifications_get_no_reply(self):
        self.assertEqual(self.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"}), [])

    def test_a_bad_message_does_not_end_the_session(self):
        replies = self.exchange("not json", "[1, 2]", {"jsonrpc": "2.0", "id": 2, "method": "nope"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual([reply["error"]["code"] for reply in replies[:3]], [-32700, -32600, -32601])
        self.assertEqual(replies[3], {"jsonrpc": "2.0", "id": 3, "result": {}})

    def test_every_tool_says_how_to_call_it(self):
        tools = self.exchange({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
        self.assertEqual(len(tools), 5)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"])
        self.assertEqual([tool["name"] for tool in tools if not tool["annotations"]["readOnlyHint"]], ["add_to_collection"])

    def test_it_runs_with_only_the_standard_library(self):
        # -I keeps out site-packages' paths from the environment, as the embedded runtime does.
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import mcp_server;"
                  "print(sorted(name for name in sys.modules if name.split('.')[0] in ('PySide6', 'socket_open')))")
        output = subprocess.run([sys.executable, "-I", "-c", script, str(mcp_server.BASE)], capture_output=True, text=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.strip(), "[]")

    def test_it_cannot_open_a_network_connection(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ['mcp_server', '--data', sys.argv[2]]\n"
                  "import mcp_server, socket\nmcp_server.main()\n"
                  "try:\n    socket.create_connection(('127.0.0.1', 9), 1)\n"
                  "except Exception as exc:\n    print(type(exc).__name__)\n")
        output = subprocess.run([sys.executable, "-c", script, str(mcp_server.BASE), str(self.data)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.strip(), "PermissionError")


@unittest.skipUnless(ClientSession, "The official MCP client is a development dependency.")
class OfficialClientTests(Base):
    """The reference client over a real pipe: what Claude Desktop and others are built on."""

    def test_the_official_client_can_use_every_read_tool(self):
        self.allow(mcp=True, mcp_write=True)
        collection = self.core.create_collection("工作")
        self.core.collect(collection, [row["id"] for row in self.core.search_page("周报")["rows"]])

        async def session():
            server = StdioServerParameters(command=sys.executable, args=[str(Path(mcp_server.__file__)), "--data", str(self.data)], env=dict(os.environ))
            async with stdio_client(server) as (reading, writing), ClientSession(reading, writing) as client:
                await client.initialize()
                tools = await client.list_tools()
                found = await client.call_tool("search_files", {"query": "押金"})
                file_id = json.loads(found.content[0].text)["files"][0]["id"]
                text = await client.call_tool("read_file_text", {"file_id": file_id})
                collections = await client.call_tool("list_collections", {})
                files = await client.call_tool("list_collection_files", {"collection": "工作"})
                added = await client.call_tool("add_to_collection", {"collection": "家", "file_ids": [file_id]})
                wrong = await client.call_tool("read_file_text", {"file_id": 99999})
                return tools, text, collections, files, added, wrong

        tools, text, collections, files, added, wrong = asyncio.run(session())
        self.assertEqual({tool.name for tool in tools.tools}, {tool["name"] for tool in mcp_server.TOOLS})
        self.assertIn("押金两个月", json.loads(text.content[0].text)["text"])
        self.assertEqual(json.loads(collections.content[0].text)["collections"][0]["name"], "工作")
        self.assertEqual(json.loads(files.content[0].text)["files"][0]["name"], "周报.txt")
        self.assertEqual(json.loads(added.content[0].text)["added"], 1)
        self.assertTrue(wrong.is_error)


class RobustnessTests(Base):
    """Whatever a connected tool sends, the server answers and carries on."""

    def setUp(self):
        super().setUp()
        self.allow(mcp=True, mcp_write=True)

    def run_server(self, *lines):
        outgoing = io.StringIO()
        serve(self.data, list(lines) + [json.dumps({"jsonrpc": "2.0", "id": "last", "method": "ping"})], outgoing)
        return [json.loads(line) for line in outgoing.getvalue().splitlines()]

    def test_strange_arguments_get_a_reply_with_the_request_id(self):
        calls = [("search_files", '{"query": "x", "limit": 1e999}'), ("read_file_text", '{"file_id": %d}' % 2 ** 70),
                 (["search_files"], "{}"), ("list_collection_files", '{"collection": "\\ud800"}'),
                 ("add_to_collection", '{"collection": "家", "file_ids": "123"}'), ("search_files", "[1]")]
        lines = ['{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":%s,"arguments":%s}}' % (index, json.dumps(name), arguments)
                 for index, (name, arguments) in enumerate(calls)]
        replies = self.run_server(*lines)
        self.assertEqual([reply["id"] for reply in replies], [*range(len(calls)), "last"])
        for reply in replies[:-1]:
            self.assertTrue(reply.get("error") or reply["result"]["isError"], reply)
        self.assertEqual(self.core.collections(), [])

    def test_params_that_are_not_an_object_still_get_their_id_back(self):
        reply = self.run_server('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":[1]}')[0]
        self.assertEqual((reply["id"], reply["error"]["code"]), (7, -32602))

    def test_text_that_cannot_be_encoded_still_goes_out(self):
        with self.core._connect() as db:
            db.execute("UPDATE file_bodies SET text=? WHERE file_id=1", ("坏字符",))
        reply = self.run_server('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_file_text","arguments":{"file_id":1}}}')[0]
        self.assertIn("坏字符", reply["result"]["content"][0]["text"])

    def test_a_failure_inside_is_an_error_not_an_exit(self):
        with patch.object(ArchiveCore, "search_page", side_effect=KeyError(5)):
            replies = self.run_server('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_files","arguments":{"query":"x"}}}')
        self.assertEqual(replies[0]["error"]["code"], -32603)
        self.assertEqual(replies[1]["id"], "last")

    def test_a_file_with_no_date_windows_can_show(self):
        row = {"id": 1, "name": "a", "path": "p", "kind": "k", "tags": [], "mtime": 1e30, "missing": 0}
        self.assertIsNone(Server.summary(row)["modified"])

    def test_invalid_bytes_on_stdin_do_not_stop_the_server(self):
        code = "import sys; sys.argv = ['x', '--data', sys.argv[2]]; sys.path.insert(0, sys.argv[1] if False else %r); import mcp_server; mcp_server.main()" % str(mcp_server.BASE)
        output = subprocess.run([sys.executable, "-c", code, "x", str(self.data)], input=b'\xff\xfe not utf8\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n',
                                capture_output=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(json.loads(output.stdout.splitlines()[-1])["id"], 2)


if __name__ == "__main__":
    unittest.main()
