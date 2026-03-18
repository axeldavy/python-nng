#!/usr/bin/env python3
"""Async REQ client subprocess for the async REQ-REP demo.

Spawns NUM_PARALLEL_TASKS concurrent asyncio tasks inside a TaskGroup.  Each
task owns one nng Context (an independent REQ state machine on the shared
socket). Collectively the tasks send NUM_REQUESTS requests.

Why contexts?
-------------
asend and arecv can be performed directly on a ReqSocket. However
each request will wait for a reply before the next request can be sent.

NNG Contexts lift that restriction: each Context are associated to the
same socket but each can send and receive independently.  This allows multiple
requests to be in-flight at the same time, without any application-level routing.

Contexts only make sense for a restricted set of socket types (Req/Rep, Pair,
and Surveyor/Respondent), as they are designed to manage the strict send/recv
state machine of those sockets. Thus they are not available on Pub/Sub, Push/Pull,
or Bus sockets.

    python client.py
"""

import asyncio
from collections.abc import Iterator

import nng

URL = "tcp://127.0.0.1:54322"
NUM_REQUESTS = 10        # total requests to send
NUM_PARALLEL_TASKS = 3   # number of concurrent tasks / contexts


async def handle_requests(
    req: nng.ReqSocket, task_id: int, request_ids: Iterator[int]
) -> None:
    """Open one Context on *req* and handle requests from the shared counter.

    The context is opened before the loop and closed in a ``finally`` block so
    its lifetime is explicit: it stays open for exactly as long as this task is
    processing requests, and closes only after the last ``arecv`` has returned.
    """
    # Create an independant channel to make requests on the shared socket.
    ctx = req.open_context()

    # Send a fixed number of requests, then exit.
    for i in request_ids:
        # Prepare and send request
        msg = f"task-{task_id}/req-{i}"
        print(f"  [task-{task_id}] send  → '{msg}'")
        await ctx.asend(msg)

        # Receive and handle reply
        reply = await ctx.arecv()
        print(f"  [task-{task_id}] recv  <= '{reply}'")

    # Note: we could close the context here with ctx.close().
    # It will be done automatically by the GC.


async def main() -> None:
    """Start a REQ client that sends NUM_REQUESTS messages to the server across NUM_PARALLEL_TASKS concurrent tasks."""
    print(f"Client connecting to {URL}")
    print(f"  {NUM_REQUESTS} request(s) across {NUM_PARALLEL_TASKS} parallel task(s)\n")

    with nng.ReqSocket() as req:
        # In this example we do start with block=False
        # to demonstrate we can queue requests without
        # a server connected yet. When the connection
        # is established, the queued requests will be sent
        req.add_dialer(URL).start(block=False)

        # Shared counter: each task calls next() only between awaits, so there
        # is no race condition despite the iterator being shared.
        request_ids = iter(range(1, NUM_REQUESTS + 1))

        # Launch multiple request-handling tasks to run concurrently.  Each task owns one Context,
        # so they can all send and receive independently without blocking each other.
        async with asyncio.TaskGroup() as tg:
            for task_id in range(1, NUM_PARALLEL_TASKS + 1):
                tg.create_task(handle_requests(req, task_id, request_ids))

    print("\nClient done.")


if __name__ == "__main__":
    asyncio.run(main())
