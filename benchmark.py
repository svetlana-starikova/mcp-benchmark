#!/usr/bin/env python3
"""
MCP Streamable HTTP benchmark (Python).

Matches the Go benchmark: Init, Tool/list, Tool call phases and sdk-style output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, cast

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import (
    MCP_DEFAULT_SSE_READ_TIMEOUT,
    MCP_DEFAULT_TIMEOUT,
    create_mcp_http_client,
)
from mcp.types import Implementation

# The MCP streamable HTTP transport logs logger.exception("Error in post_writer") on failures,
# which spams tracebacks to stderr. Silence that logger; benchmark code reports errors via stats.
_log_mcp_http = logging.getLogger("mcp.client.streamable_http")
_log_mcp_http.setLevel(logging.CRITICAL)
_log_mcp_http.propagate = False

CLIENT_INFO = Implementation(name="demo", version="0.0.1")


def parse_header_args(header_parts: list[str]) -> dict[str, str]:
    """Parse -H 'Name: value' or -H 'Name=value' into a header dict."""
    out: dict[str, str] = {}
    for part in header_parts:
        if ":" in part:
            key, value = part.split(":", 1)
        elif "=" in part:
            key, value = part.split("=", 1)
        else:
            continue
        out[key.strip()] = value.strip()
    return out


def merge_http_headers(
    cli_headers: list[str],
    auth_token: str | None,
) -> dict[str, str]:
    headers = parse_header_args(cli_headers)

    if auth_token:
        headers.setdefault("Authorization", f"Bearer {auth_token}")

    env_auth = os.environ.get("MCP_AUTHORIZATION") or os.environ.get("AUTHORIZATION")
    if env_auth and "Authorization" not in headers:
        headers["Authorization"] = env_auth.strip()

    env_bearer = os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("MCP_BEARER_TOKEN")
    if env_bearer and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {env_bearer.strip()}"

    return headers


def make_http_client(headers: dict[str, str]) -> httpx.AsyncClient:
    timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
    return create_mcp_http_client(headers=headers or None, timeout=timeout)


def _is_unauthorized(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return True
    msg = str(exc).lower()
    return "401" in msg or "unauthorized" in msg


@dataclass
class PhaseStats:
    name: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    latencies_ms: list[float]
    elapsed_s: float

    def avg_latency_ms(self) -> float:
        if self.successful_requests <= 0:
            return 0.0
        return sum(self.latencies_ms) / self.successful_requests

    def throughput(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return self.successful_requests / self.elapsed_s

    def print_results(self) -> None:
        print(f"\n📊 {self.name} Results:")
        print(
            f"   Total: {self.total_requests} requests "
            f"({self.successful_requests} success, {self.failed_requests} failed)"
        )
        print(f"   Elapsed: {self.elapsed_s:.2f}s")
        print(f"   Avg latency: {self.avg_latency_ms():.2f}ms")
        print(f"   Throughput: {self.throughput():.2f} req/s")


def print_summary(init: PhaseStats, list_phase: PhaseStats, call: PhaseStats) -> None:
    print("\n📈 Summary:")
    print(
        f"   Init:      {init.throughput():.2f} req/s,  {init.avg_latency_ms():.2f}ms avg latency"
    )
    print(
        f"   Tool/list: {list_phase.throughput():.2f} req/s,  "
        f"{list_phase.avg_latency_ms():.2f}ms avg latency"
    )
    print(
        f"   Tool call: {call.throughput():.2f} req/s,  "
        f"{call.avg_latency_ms():.2f}ms avg latency"
    )


async def verify_tool_exists(
    server_url: str,
    tool_name: str,
    http_client: httpx.AsyncClient,
) -> None:
    try:
        async with streamable_http_client(server_url, http_client=http_client) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=CLIENT_INFO,
            ) as session:
                await session.initialize()
                result = await session.list_tools()
                names = [t.name for t in result.tools]
                if tool_name not in names:
                    print(
                        f"\n❌ Tool '{tool_name}' not found on server",
                        file=sys.stderr,
                    )
                    print("\nAvailable tools:", file=sys.stderr)
                    for n in names:
                        print(f"  - {n}", file=sys.stderr)
                    raise SystemExit(1)
    except SystemExit:
        raise
    except BaseException as e:
        if _is_unauthorized(e):
            print(
                "HTTP 401 Unauthorized: add credentials, e.g.\n"
                "  --auth-token YOUR_TOKEN\n"
                "  -H 'Authorization: Bearer YOUR_TOKEN'\n"
                "or set MCP_AUTH_TOKEN / MCP_AUTHORIZATION in the environment.",
                file=sys.stderr,
            )
            raise SystemExit(1) from e
        raise


async def user_init_benchmark(
    server_url: str,
    init_runs: int,
    http_client: httpx.AsyncClient,
) -> tuple[int, int, list[float]]:
    """One virtual user: init_runs full session handshakes (new connection each run)."""
    successes = 0
    failures = 0
    latencies_ms: list[float] = []

    for _ in range(init_runs):
        t0 = time.perf_counter()
        try:
            async with streamable_http_client(server_url, http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    client_info=CLIENT_INFO,
                ) as session:
                    await session.initialize()
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            successes += 1
        except Exception:
            failures += 1

    return successes, failures, latencies_ms


async def run_init_phase(
    server_url: str,
    users: int,
    init_runs: int,
    http_client: httpx.AsyncClient,
) -> PhaseStats:
    total_requests = users * init_runs
    print(
        f"\n🚀 Phase 1 - Init Benchmark: {users} clients × {init_runs} runs = "
        f"{total_requests} total requests"
    )
    print(f"   Server: {server_url}")

    bench_start = time.perf_counter()
    results = await asyncio.gather(
        *[
            user_init_benchmark(server_url, init_runs, http_client)
            for _ in range(users)
        ]
    )
    elapsed_s = time.perf_counter() - bench_start

    successes = sum(r[0] for r in results)
    failures = sum(r[1] for r in results)
    latencies: list[float] = []
    for r in results:
        latencies.extend(r[2])

    return PhaseStats(
        name="Init",
        total_requests=total_requests,
        successful_requests=successes,
        failed_requests=failures,
        latencies_ms=latencies,
        elapsed_s=elapsed_s,
    )


async def user_list_tools(
    session: ClientSession,
    init_runs: int,
) -> tuple[int, int, list[float]]:
    successes = 0
    failures = 0
    latencies_ms: list[float] = []
    for _ in range(init_runs):
        t0 = time.perf_counter()
        try:
            await session.list_tools()
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            successes += 1
        except Exception:
            failures += 1
    return successes, failures, latencies_ms


async def run_list_phase(
    sessions: list[ClientSession],
    init_runs: int,
    server_url: str,
) -> PhaseStats:
    users = len(sessions)
    total_requests = users * init_runs
    print(
        f"\n🚀 Phase 2 - Tool/list Benchmark: {users} clients × {init_runs} runs = "
        f"{total_requests} total requests"
    )
    print(f"   Server: {server_url}")

    bench_start = time.perf_counter()
    results = await asyncio.gather(
        *[user_list_tools(s, init_runs) for s in sessions]
    )
    elapsed_s = time.perf_counter() - bench_start

    successes = sum(r[0] for r in results)
    failures = sum(r[1] for r in results)
    latencies: list[float] = []
    for r in results:
        latencies.extend(r[2])

    return PhaseStats(
        name="Tool/list",
        total_requests=total_requests,
        successful_requests=successes,
        failed_requests=failures,
        latencies_ms=latencies,
        elapsed_s=elapsed_s,
    )


async def user_tool_calls(
    session: ClientSession,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
    runs: int,
) -> tuple[int, int, list[float]]:
    successes = 0
    failures = 0
    latencies_ms: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        try:
            await session.call_tool(tool_name, tool_arguments)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            successes += 1
        except Exception:
            failures += 1
    return successes, failures, latencies_ms


async def run_tool_call_phase(
    sessions: list[ClientSession],
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
    runs: int,
    server_url: str,
    args_json: str,
) -> PhaseStats:
    users = len(sessions)
    total_requests = users * runs
    print(
        f"\n🚀 Phase 3 - Tool Call Benchmark: {users} clients × {runs} runs = "
        f"{total_requests} total requests"
    )
    print(f"   Server: {server_url}")
    print(f"   Tool: {tool_name}")
    if args_json:
        print(f"   Arguments: {args_json}")

    bench_start = time.perf_counter()
    results = await asyncio.gather(
        *[
            user_tool_calls(s, tool_name, tool_arguments, runs)
            for s in sessions
        ]
    )
    elapsed_s = time.perf_counter() - bench_start

    successes = sum(r[0] for r in results)
    failures = sum(r[1] for r in results)
    latencies: list[float] = []
    for r in results:
        latencies.extend(r[2])

    return PhaseStats(
        name="Tool call",
        total_requests=total_requests,
        successful_requests=successes,
        failed_requests=failures,
        latencies_ms=latencies,
        elapsed_s=elapsed_s,
    )


async def async_main(
    server_url: str,
    users: int,
    init_runs: int,
    tool_runs: int,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
    args_json: str,
    http_headers: dict[str, str],
) -> None:
    async with make_http_client(http_headers) as http_client:
        await _async_main_inner(
            server_url,
            users,
            init_runs,
            tool_runs,
            tool_name,
            tool_arguments,
            args_json,
            http_client,
        )


async def _async_main_inner(
    server_url: str,
    users: int,
    init_runs: int,
    tool_runs: int,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
    args_json: str,
    http_client: httpx.AsyncClient,
) -> None:
    print("🔌 MCP Streamable HTTP Benchmark")
    print("   Transport: Streamable HTTP")
    print(
        f"   Users: {users}, Init runs: {init_runs}, Tool call runs: {tool_runs}"
    )
    print(f"   Server: {server_url}")

    print(f"   Verifying tool '{tool_name}' exists...")
    await verify_tool_exists(server_url, tool_name, http_client)
    print(f"   ✅ Tool '{tool_name}' found")

    init_stats = await run_init_phase(server_url, users, init_runs, http_client)
    init_stats.print_results()

    async with AsyncExitStack() as stack:
        sessions: list[ClientSession] = []
        for _ in range(users):
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(server_url, http_client=http_client)
            )
            session = ClientSession(
                read_stream,
                write_stream,
                client_info=CLIENT_INFO,
            )
            s = cast(
                ClientSession,
                await stack.enter_async_context(session),
            )
            await s.initialize()
            sessions.append(s)

        list_stats = await run_list_phase(sessions, init_runs, server_url)
        list_stats.print_results()

        call_stats = await run_tool_call_phase(
            sessions,
            tool_name,
            tool_arguments,
            tool_runs,
            server_url,
            args_json,
        )
        call_stats.print_results()

        print_summary(init_stats, list_stats, call_stats)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP Streamable HTTP benchmark (matches Go / sdk-benchmark output)"
    )
    parser.add_argument(
        "-s",
        "--server",
        default="http://localhost:8000/mcp",
        help="MCP server URL",
    )
    parser.add_argument(
        "-r",
        "--requests",
        type=int,
        default=100,
        dest="runs",
        help="Tool call runs per user",
    )
    parser.add_argument(
        "-i",
        type=int,
        default=0,
        dest="init_runs",
        help="Init and tools/list runs per user (default: same as -r)",
    )
    parser.add_argument(
        "-u",
        "--users",
        type=int,
        default=1,
        help="Number of concurrent clients",
    )
    parser.add_argument(
        "-t",
        "--tool",
        default="say_hello",
        help="Tool name to call",
    )
    parser.add_argument(
        "-a",
        "--arguments",
        default="{}",
        help='Tool arguments as JSON object (default: "{}")',
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        dest="headers",
        metavar="HEADER",
        help='Extra HTTP header, e.g. -H "Authorization: Bearer <token>"',
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Shortcut for Authorization: Bearer <token>",
    )

    ns = parser.parse_args()
    runs: int = getattr(ns, "runs", 100)
    init_runs_arg: int = getattr(ns, "init_runs", 0)
    users: int = getattr(ns, "users", 1)
    server_url: str = getattr(ns, "server", "http://localhost:8000/mcp")
    tool_name: str = getattr(ns, "tool", "say_hello")
    arguments_str: str = getattr(ns, "arguments", "{}")
    cli_headers: list[str] = getattr(ns, "headers", []) or []
    auth_token: str | None = getattr(ns, "auth_token", None)

    if os.environ.get("RUNS"):
        try:
            n = int(os.environ["RUNS"])
            if n > 0:
                runs = n
        except ValueError:
            pass

    init_runs = init_runs_arg if init_runs_arg > 0 else runs

    tool_arguments: dict[str, Any] | None = None
    args_json = arguments_str.strip()
    if args_json:
        try:
            parsed = json.loads(args_json)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in arguments: {e}", file=sys.stderr)
            raise SystemExit(1) from e
        if not isinstance(parsed, dict):
            print("Error: JSON arguments must be an object", file=sys.stderr)
            raise SystemExit(1)
        tool_arguments = parsed

    http_headers = merge_http_headers(cli_headers, auth_token)

    asyncio.run(
        async_main(
            server_url=server_url,
            users=users,
            init_runs=init_runs,
            tool_runs=runs,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            args_json=args_json,
            http_headers=http_headers,
        )
    )


if __name__ == "__main__":
    main()
