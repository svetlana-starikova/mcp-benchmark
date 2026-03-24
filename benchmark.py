#!/usr/bin/env python3
"""
MCP Server Benchmark Tool

Runs concurrent benchmark tests against an MCP server using multiprocessing.
Each virtual user runs in its own Python process.
"""

import argparse
import json
import logging
import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

# Suppress MCP client transport warnings (e.g., 202 Accepted for session termination)
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)


@dataclass
class BenchmarkResult:
    """Results from a single virtual user's benchmark run."""

    user_id: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    latencies: list[float]
    last_result: Any = None


def run_user_benchmark(
    user_id: int,
    server_url: str,
    requests_per_user: int,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
) -> BenchmarkResult:
    """
    Run benchmark for a single virtual user.

    Each user runs in its own process with its own MCP client session.
    """

    async def _run() -> BenchmarkResult:
        latencies: list[float] = []
        successful = 0
        failed = 0
        last_result = None

        async with streamable_http_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=Implementation(name="benchmark", version="1.0.0"),
            ) as session:
                # Initialize the session
                await session.initialize()

                for _ in range(requests_per_user):
                    start_time = time.perf_counter()
                    try:
                        result = await session.call_tool(tool_name, tool_arguments)
                        elapsed_ms = (time.perf_counter() - start_time) * 1000
                        successful += 1
                        latencies.append(elapsed_ms)
                        last_result = result
                    except Exception:
                        elapsed_ms = (time.perf_counter() - start_time) * 1000
                        failed += 1
                        latencies.append(elapsed_ms)

        total_latency = sum(latencies)
        return BenchmarkResult(
            user_id=user_id,
            total_requests=requests_per_user,
            successful_requests=successful,
            failed_requests=failed,
            total_latency_ms=total_latency,
            min_latency_ms=min(latencies) if latencies else 0.0,
            max_latency_ms=max(latencies) if latencies else 0.0,
            latencies=latencies,
            last_result=last_result,
        )

    return anyio.run(_run)


def run_benchmark(
    server_url: str,
    requests_per_user: int,
    num_users: int,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
) -> None:
    """
    Run the benchmark with multiple virtual users using multiprocessing.
    """
    print(f"\n{'='*60}")
    print("MCP Server Benchmark")
    print(f"{'='*60}")
    print(f"Server URL:          {server_url}")
    print(f"Virtual Users:       {num_users}")
    print(f"Requests per User:   {requests_per_user}")
    print(f"Tool:                {tool_name}")
    print(f"Arguments:           {json.dumps(tool_arguments) if tool_arguments else '(none)'}")
    print(f"Total Requests:      {num_users * requests_per_user}")
    print(f"{'='*60}\n")

    start_time = time.perf_counter()

    # Use multiprocessing - each user runs in its own process
    with mp.Pool(processes=num_users) as pool:
        results = pool.starmap(
            run_user_benchmark,
            [
                (user_id, server_url, requests_per_user, tool_name, tool_arguments)
                for user_id in range(num_users)
            ],
        )

    total_time = time.perf_counter() - start_time

    # Aggregate results
    total_requests = sum(r.total_requests for r in results)
    successful_requests = sum(r.successful_requests for r in results)
    failed_requests = sum(r.failed_requests for r in results)
    all_latencies = [latency for r in results for latency in r.latencies]

    avg_latency = sum(all_latencies) / len(all_latencies) if all_latencies else 0.0
    min_latency = min(all_latencies) if all_latencies else 0.0
    max_latency = max(all_latencies) if all_latencies else 0.0

    # Calculate percentiles
    sorted_latencies = sorted(all_latencies)
    p50_idx = int(len(sorted_latencies) * 0.50)
    p90_idx = int(len(sorted_latencies) * 0.90)
    p95_idx = int(len(sorted_latencies) * 0.95)
    p99_idx = int(len(sorted_latencies) * 0.99)

    p50 = sorted_latencies[p50_idx] if sorted_latencies else 0.0
    p90 = sorted_latencies[p90_idx] if sorted_latencies else 0.0
    p95 = sorted_latencies[p95_idx] if sorted_latencies else 0.0
    p99 = sorted_latencies[p99_idx] if sorted_latencies else 0.0

    # Calculate throughput
    throughput = total_requests / total_time if total_time > 0 else 0.0

    # Print last tool call results first
    if results and any(r.last_result is not None for r in results):
        print(f"\n{'='*60}")
        print("LAST TOOL CALL RESULTS")
        print(f"{'='*60}")
        for r in results:
            if r.last_result is not None:
                print(f"\nUser {r.user_id}:")
                print(json.dumps(r.last_result.model_dump() if hasattr(r.last_result, 'model_dump') else r.last_result, indent=2, default=str))
        print()

    # Print summary
    print(f"\n{'='*60}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*60}")
    print(f"\nTiming:")
    print(f"  Total Time:          {total_time:.2f} seconds")
    print(f"\nThroughput:")
    print(f"  Requests/sec:        {throughput:.2f}")
    print(f"\nLatency:")
    print(f"  Average:             {avg_latency:.2f} ms")
    print(f"  Min:                 {min_latency:.2f} ms")
    print(f"  Max:                 {max_latency:.2f} ms")
    print(f"  p50:                 {p50:.2f} ms")
    print(f"  p90:                 {p90:.2f} ms")
    print(f"  p95:                 {p95:.2f} ms")
    print(f"  p99:                 {p99:.2f} ms")
    print(f"\nRequests:")
    print(f"  Total:               {total_requests}")
    print(f"  Successful:          {successful_requests}")
    print(f"  Failed:              {failed_requests}")
    print(f"  Success Rate:        {(successful_requests/total_requests*100):.2f}%")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark MCP server with concurrent virtual users"
    )
    parser.add_argument(
        "-s",
        "--server",
        required=True,
        help="MCP server URL (e.g., http://localhost:8000/mcp)",
    )
    parser.add_argument(
        "-r",
        "--requests",
        type=int,
        required=True,
        help="Number of requests per virtual user",
    )
    parser.add_argument(
        "-u",
        "--users",
        type=int,
        required=True,
        help="Number of virtual users (processes)",
    )
    parser.add_argument(
        "-t",
        "--tool",
        required=True,
        help="Tool name to call",
    )
    parser.add_argument(
        "-a",
        "--arguments",
        help="Tool arguments in JSON format (e.g., '{\"name\":\"value}')",
    )

    args = parser.parse_args()

    # Parse arguments JSON
    tool_arguments: dict[str, Any] | None = None
    if args.arguments:
        try:
            tool_arguments = json.loads(args.arguments)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in arguments: {e}")
            return

    # Set multiprocessing start method to 'spawn' for clean process isolation
    mp.set_start_method("spawn", force=True)

    run_benchmark(
        server_url=args.server,
        requests_per_user=args.requests,
        num_users=args.users,
        tool_name=args.tool,
        tool_arguments=tool_arguments,
    )


if __name__ == "__main__":
    main()
