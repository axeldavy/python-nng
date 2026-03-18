#!/usr/bin/env python3
"""Simple REP server — start this before running client.py.

Listens for incoming requests on a fixed TCP address, echoes each one back
with a short acknowledgement, then exits after NUM_REQUESTS replies.

    python server.py
"""

import asyncio
import nng

URL = "tcp://127.0.0.1:54321"
NUM_REQUESTS = 5


def main() -> None:
    print(f"Server listening on {URL}  (expecting {NUM_REQUESTS} request(s))\n")

    with nng.RepSocket() as rep:
        # Start accepting client connections.
        rep.add_listener(URL).start()

        # Prepare an async task to serve requests.  The REP protocol requires that
        # each request is fully processed and replied to before the next request is
        # received (use contexts to bypass this restriction. See parallel_requests_async/server.py).
        async def serve():
            for i in range(1, NUM_REQUESTS + 1):
                # Block until a request arrives.
                request = await rep.arecv()
                print(f"  recv [{i}/{NUM_REQUESTS}]  <= '{request}'")

                # The REP protocol requires exactly one send per recv.
                reply = f"echo: {request}"
                await rep.asend(reply)
                print(f"  sent [{i}/{NUM_REQUESTS}]  => '{reply}'")

        asyncio.run(serve())

    print("\nServer done.")


if __name__ == "__main__":
    main()
