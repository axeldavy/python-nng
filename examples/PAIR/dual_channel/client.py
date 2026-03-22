#!/usr/bin/env python3
"""PAIR client with independent reads and sends

Connects to the PAIR server, sends and receive NUM_MESSAGES in parallel

    python client.py
"""

import asyncio
import nng

URL = "tcp://127.0.0.1:54321"
NUM_OUTBOUND_MESSAGES = 5

def run_tasks(pair: nng.PairSocket) -> None:
    """In this demo the code for the server and client are the same"""
    # The PAIR protocol uses a single bidirectional connection, and thus allows for
    # independent sends and receives. Both send and recv operations have a
    # recv and send queues, which can be configured like this:
    pair.recv_buf = 3  # 3 pending messages max
    pair.send_buf = 0  # No pending unsent messages allowed

    # Parallel task 1: receiving messages
    async def receive_messages() -> None:
        for i in range(NUM_OUTBOUND_MESSAGES):
            # Block until a message arrives.
            request = await pair.arecv()
            print(f"  recv [{i + 1}/{NUM_OUTBOUND_MESSAGES}]  <= '{request}'")

    # Parallel task 2: sending messages
    async def send_messages() -> None:
        for i in range(NUM_OUTBOUND_MESSAGES):
            request = f"request-{i + 1}"
            await pair.asend(request)
            print(f"  sent [{i + 1}/{NUM_OUTBOUND_MESSAGES}]  => '{request}'")

    # Run both tasks in parallel until they are done.
    async def run_until_done() -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(receive_messages())
            tg.create_task(send_messages())

    # Start the tasks
    asyncio.run(run_until_done())

def main_client() -> None:
    with nng.PairSocket() as pair:
        pair.recv_timeout = 15000  # ms
        pair.send_timeout = 15000  # ms

        # Start connecting to the server
        dialer = pair.add_dialer(URL)
        dialer.start(block=True)

        # The pipe represents the connection to the server.
        # Here we know it is not None because we used block=True.
        pipe = dialer.pipe
        assert pipe is not None

        print(f"Client started on {pipe.get_self_addr()}, "
              f"connected to {pipe.get_peer_addr()} (expecting {NUM_OUTBOUND_MESSAGES} message(s))\n")

        run_tasks(pair)

    print("\nClient done.\n")

def main_server() -> None:
    with nng.PairSocket() as pair:
        pair.recv_timeout = 15000  # ms
        pair.send_timeout = 15000  # ms

        # Start listening for clients
        pair.add_listener(URL).start()

        run_tasks(pair)

    print("\nServer done.\n")

if __name__ == "__main__":
    main_client()
