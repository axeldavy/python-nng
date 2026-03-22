#!/usr/bin/env python3
"""Simple PAIR client

Connects to the PAIR server, sends NUM_OUTBOUND_MESSAGES messages, and prints each
reply.  The PAIR protocol allows any arbitrary send / recv pattern. In this
demo we will assume a simple request-response pattern with two replies per request.

    python client.py
"""

import nng

URL = "tcp://127.0.0.1:54321"
NUM_OUTBOUND_MESSAGES = 5

def main() -> None:
    with nng.PairSocket() as pair:
        pair.recv_timeout = 5000  # ms
        pair.send_timeout = 5000  # ms

        # Start connecting to the server
        dialer = pair.add_dialer(URL)
        dialer.start(block=True)

        # The pipe represents the connection to the server.
        # Here we know it is not None because we used block=True.
        pipe = dialer.pipe
        assert pipe is not None

        print(f"Client started on {pipe.get_self_addr()}, "
              f"connected to {pipe.get_peer_addr()} (expecting {NUM_OUTBOUND_MESSAGES} message(s))\n")

        for i in range(NUM_OUTBOUND_MESSAGES):
            # Block until a message arrives.
            request = f"request-{i + 1}"
            pair.send(request)
            print(f"  sent [{i + 1}/{NUM_OUTBOUND_MESSAGES}]  => '{request}'")

            # The PAIR protocol enable zero, one or any arbitrary number of sends per recv.
            # Here we assume we get two replies per request.
            # The conversation is not able to resume if the server ends early, unlike
            # the REQ/REP pattern which enforces a strict send-recv alternation, and
            # thus enables better recovery
            reply1 = pair.recv()
            print(f"  recv [{i + 1}/{NUM_OUTBOUND_MESSAGES} part 1]  <= '{reply1}'")

            reply2 = pair.recv()
            print(f"  recv [{i + 1}/{NUM_OUTBOUND_MESSAGES} part 2]  <= '{reply2}'")

    print("\nClient done.\n")

if __name__ == "__main__":
    main()
