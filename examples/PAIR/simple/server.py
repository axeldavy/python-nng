#!/usr/bin/env python3
"""Simple PAIR server

Listens for incoming messages on a fixed TCP address, echoes each one back
with a short acknowledgement, then exits after 2 * NUM_INBOUND_MESSAGES replies.

    python server.py
"""

import nng

URL = "tcp://127.0.0.1:54321"
NUM_INBOUND_MESSAGES = 5

def main() -> None:
    print(f"Server listening on {URL}  (expecting {NUM_INBOUND_MESSAGES} message(s))\n")

    with nng.PairSocket() as pair:
        # Start accepting clients
        pair.add_listener(URL).start()

        for i in range(NUM_INBOUND_MESSAGES):
            # Block until a message arrives.
            request = pair.recv()
            print(f"  recv [{i + 1}/{NUM_INBOUND_MESSAGES}]  <= '{request}'")

            # The PAIR protocol enable zero, one or any arbitrary number of sends per recv.
            reply = f"echo 1: {request}"
            pair.send(reply)
            print(f"  sent [{i + 1}/{NUM_INBOUND_MESSAGES} part 1]  => '{reply}'")

            reply = f"echo 2: {request}"
            pair.send(reply)
            print(f"  sent [{i + 1}/{NUM_INBOUND_MESSAGES} part 2]  => '{reply}'")

    print("\nServer done.\n")

if __name__ == "__main__":
    main()
