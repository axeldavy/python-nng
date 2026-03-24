#!/usr/bin/env python3
"""Async REP server subprocess for the async REQ-REP demo.

Like the client, spawns NUM_PARALLEL_TASKS tasks inside a TaskGroup.  Each
task owns one nng Context (an independent REP state machine on the shared
socket), so multiple requests can be received, processed, and replied to
concurrently.

Without contexts a bare RepSocket is strictly sequential: one recv must be
followed by one send before the next recv is allowed.  Contexts lift that
restriction, letting each task block independently in arecv() while the
others are processing or awaiting their own replies.

IMPORTANT PERFORMANCE NOTES:
NNG's implementation of contexts are designed to be efficient. Having
thousands of contexts enables to potentially have up to thousands of concurrent
requests in-flight, without adding overhead. Thus you can spawn many
more contexts than needed without performance penalty. However to avoid
any penalty you must avoid having a receive timeout, in which case
the arecv will periodically check for cancellation, which adds overhead.

    python server.py
"""

import asyncio

import nng

URL = "tcp://127.0.0.1:54322"
NUM_REQUESTS = 10        # total requests to serve before exiting
NUM_PARALLEL_TASKS = 3   # number of concurrent tasks / contexts

class Counter:
    """A simple counter object to share state between tasks."""
    def __init__(self, start: int = 1) -> None:
        self.value = start

    def next(self) -> int:
        """Return the current value and increment the counter."""
        current = self.value
        self.value += 1
        return current

async def process(request: str) -> str:
    """Simulate async work required to build a reply.

    In a real application this might be a database query, an HTTP call,
    or any other awaitable I/O.  While this coroutine is suspended, other
    server tasks — and the event loop as a whole — remain unblocked.
    """
    await asyncio.sleep(0.05)   # stand-in for real async work
    return f"echo: {request}"


async def handle_requests(
    rep: nng.RepSocket, task_id: int, counter: Counter, done_event: asyncio.Event
) -> None:
    """Open one Context on *rep* and serve requests from the shared counter."""
    # Create a context
    ctx = rep.open_context()
    while True:
        # Wait the context receives a task
        # NOTE: clients get different contexts on each request.
        request = await ctx.arecv()

        # Track number of requests served across all tasks with the shared counter.
        current_request_id = counter.next()
        print(f"  [task-{task_id}] recv ({current_request_id}/{NUM_REQUESTS})  <= '{request}'")

        # Prepare the reply
        reply = await process(request)

        # Send the reply back to the client.
        # The context ensures that this reply goes to the correct client,
        # even if other tasks are receiving and replying concurrently on the same socket.
        await ctx.asend(reply)
        print(f"  [task-{task_id}] sent ({current_request_id}/{NUM_REQUESTS})  => '{reply}'")

        # Stop when enough requests have been served.
        if current_request_id >= NUM_REQUESTS:
            done_event.set()  # signal that all requests have been served
            print(f"  [task-{task_id}] reached request limit, exiting")
            break
    # NOTE: the context is automatically closed as it gets released by the GC
    # when this function closes. It is also closed automatically when
    # the socket is closed. It could be manually closed here with ctx.close().


async def main() -> None:
    """Start a REP server that serves NUM_REQUESTS messages across NUM_PARALLEL_TASKS concurrent tasks."""
    print(f"Server listening on {URL}")
    print(f"  {NUM_REQUESTS} request(s) across {NUM_PARALLEL_TASKS} parallel task(s)\n")

    with nng.RepSocket() as rep:
        rep.add_listener(URL).start()

        # Shared counter
        done_event = asyncio.Event()  # signals when all requests have been served
        counter = Counter()

        # Start multiple request-handling tasks to run concurrently. Each task can process
        # one request at a time.
        async with asyncio.TaskGroup() as tg:
            tasks = []
            for task_id in range(1, NUM_PARALLEL_TASKS + 1):
                tasks.append(
                    tg.create_task(handle_requests(rep, task_id, counter, done_event))
                )

            # Wait until all requests have been served
            await done_event.wait() 

            # Cancel any still-running context
            for task in tasks:
                task.cancel()

    print("\nServer done.\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer interrupted by user, shutting down") 
