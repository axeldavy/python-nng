#!/usr/bin/env python3
"""Simple REP server — start this before running client.py.

Listens for incoming requests on a fixed TCP address, echoes each one back
with a short acknowledgement, then exits after NUM_REQUESTS replies.

    python server.py
"""

import nng

URL = "tcp://127.0.0.1:54321"
NUM_REQUESTS = 5


def main() -> None:
    print(f"Server listening on {URL}  (expecting {NUM_REQUESTS} request(s))\n")

    with nng.RepSocket() as rep:
        rep.add_listener(URL).start()

        for i in range(1, NUM_REQUESTS + 1):
            # Block until a request arrives.
            data = rep.recv()
            request = data.decode()
            print(f"  recv [{i}/{NUM_REQUESTS}]  ← {request!r}")

            # The REP protocol requires exactly one send per recv.
            reply = f"echo: {request}"
            rep.send(reply.encode())
            print(f"  sent [{i}/{NUM_REQUESTS}]  → {reply!r}")

    print("\nServer done.")


if __name__ == "__main__":
    main()
