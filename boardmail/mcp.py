"""Optional official MCP v2 server; configuration is fixed by its operator."""
import argparse
from functools import partial
import json
from pathlib import Path
import sys
import threading

from . import __version__, commands, config, table, transport
from .errors import mcp_error
from .store import Store


# The order in which a tool's schema writes what it says about an argument.
WORDS = ('type', 'enum', 'minimum', 'maximum', 'minLength', 'maxLength', 'pattern', 'default', 'description')
# What a rule of the command table is in a tool's schema.
RULES = {
    table.ONE_OF: lambda first, second: {'oneOf': [{'required': [first]}, {'required': [second]}]},
    table.NEEDS: lambda first, second: {'dependentRequired': {first: [second]}},
    table.NOT_BOTH: lambda value, flag: {'not': {'required': [value, flag], 'properties': {flag: {'const': True}}}},
}


def input_schema(command):
    """What the tool of a command takes, from its entry in the command table."""
    arguments = {argument.name: argument for argument in command.arguments}
    properties = {}
    for name in (*command.tool_first, *(name for name in arguments if name not in command.tool_first)):
        argument = arguments[name]
        text = table.told(command, argument, typed=False)
        said = {**argument.kind, **(argument.tool_kind or {}), **({'description': text} if text else {})}
        properties[name] = {word: said[word] for word in sorted(said, key=WORDS.index)}
    schema = {"type": "object", "properties": properties,
              "required": [name for name in properties if arguments[name].required], "additionalProperties": False}
    for rule, *names in command.rules:
        schema.update(RULES[rule](*names))
    return schema


def passed(command, arguments):
    """What a command is given for the arguments that its tool was called with."""
    # An argument that is left out gets the default of the command table, which is the one of the command line.
    # Where a tool has a default of its own, the tool passes it.
    own = {argument.name: argument.tool_kind['default'] for argument in command.arguments
           if 'default' in (argument.tool_kind or {})}
    return {**own, **arguments}


def refused(command, schema, arguments, fits):
    """The argument that a call of a tool is refused for: the first in the order of the command table that is
    required and left out, or whose value its own part of the schema does not take. None where the call has a
    name that is no argument of the command, or breaks only a rule between two. fits says whether a value is
    what a schema takes."""
    properties = schema['properties']
    if not arguments.keys() <= properties.keys():
        return None
    for name in table.order(command):
        wrong = not fits(properties[name], arguments[name]) if name in arguments else name in schema['required']
        if wrong:
            return name
    return None


def create_server(store, sources=None, *, fetch=transport.fetch):
    """The server of one inbox. fetch asks a board for a tool that can ask one: the transport, or an invented
    board in its place."""
    import anyio
    from jsonschema import Draft202012Validator
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    catalog = {}
    for name, command in sorted(table.COMMANDS.items()):
        catalog["boardmail_" + name] = Tool(
            name="boardmail_" + name, description=table.told(command, typed=False),
            input_schema=input_schema(command),
            annotations=ToolAnnotations(**{hint + "_hint": hint in command.hints for hint in table.HINTS}))
    # Adapter output redirection is process-wide. Do not overlap collectors.
    collection_lock = threading.Lock()

    async def list_tools(ctx, params):
        return ListToolsResult(tools=list(catalog.values()))

    def fits(schema, value):
        return Draft202012Validator(schema).is_valid(value)

    async def call_tool(ctx, params):
        tool = catalog.get(params.name)
        arguments = params.arguments or {}
        command = tool and table.COMMANDS[params.name.removeprefix("boardmail_")]
        if tool is None:
            result, code = commands.error_result("invalid_arguments")
        elif not fits(tool.input_schema, arguments):
            result, code = commands.error_result(
                "invalid_arguments", refused(command, tool.input_schema, arguments, fits))
        else:
            cancelled = threading.Event()
            invoke = partial(commands.outcome, partial(commands.execute, store, command.name, sources=sources,
                             cancelled=cancelled, fetch=fetch, **passed(command, arguments)))
            def operation():
                if command.collects:
                    # Hold this in the worker even if the caller disconnects.
                    with collection_lock:
                        return invoke()
                return invoke()
            try:
                result, code = await anyio.to_thread.run_sync(operation, abandon_on_cancel=command.waits)
            finally:
                cancelled.set()
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=True))],
                              structured_content=result, is_error=mcp_error(code))

    return Server("boardmail", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool,
                  instructions=table.INSTRUCTIONS)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boardmail-mcp")
    parser.add_argument("--config", type=Path, help="Operator-owned configuration; loaded once at startup")
    parser.add_argument("--db", type=Path, help="Fixed database; without --config, remote collection is unavailable")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8766, help="HTTP port on 127.0.0.1 only")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError:
        print('From the boardmail source checkout, use the same Python environment to run: '
              'python3 -m pip install ".[mcp]"', file=sys.stderr)
        return 2

    def configured():
        path = args.config or (None if args.db else Path.home()/".config/boardmail/config.json")
        data = config.load(path) if path else None
        return create_server(Store(args.db or data["database"]), data["sources"] if data else None)

    # Configuration failures belong on stderr; stdout is reserved for MCP frames.
    try:
        server = configured()
    except config.MailError as exc:
        result, code = commands.error_result(str(exc))
        print(json.dumps(result), file=sys.stderr)
        return code
    except (OSError, ValueError, TypeError, KeyError):
        result, code = commands.error_result("local_state_error")
        print(json.dumps(result), file=sys.stderr)
        return code
    if args.transport == "streamable-http":
        import uvicorn
        uvicorn.run(server.streamable_http_app(json_response=True, stateless_http=True),
                    host="127.0.0.1", port=args.port)
    else:
        async def serve():
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        anyio.run(serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
