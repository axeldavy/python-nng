#!/usr/bin/env python3
"""Simple REQ client — run after server.py is started.

Connects to the REP server, sends NUM_REQUESTS messages, and prints each
reply.  The REQ protocol enforces a strict send → recv → send → …  cycle:
you must receive a reply before sending the next request.

    python client.py
"""

import asyncio
import nng

URL = "tcp://127.0.0.1:54321"
NUM_REQUESTS = 5


def main() -> None:
    """Start a simple REQ client that sends NUM_REQUESTS messages to the server"""
    print(f"Client connecting to {URL}\n")

    with nng.ReqSocket() as req:
        # block=True waits until the TCP handshake completes
        req.add_dialer(URL).start(block=True)
        print("Connected.\n")

        # Prepare an async task to run the conversation.
        async def conversation():
            # Send NUM_REQUESTS messages
            for i in range(1, NUM_REQUESTS + 1):
                # Prepare and send request
                msg = f"request-{i}"
                print(f"  send [{i}/{NUM_REQUESTS}]  => '{msg}'")
                await req.asend(msg)

                # Receive and handle reply
                reply = await req.arecv()
                print(f"  recv [{i}/{NUM_REQUESTS}]  <= '{reply}'")

        asyncio.run(conversation())

    print("\nClient done.")


if __name__ == "__main__":
    main()
