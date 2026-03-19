#!/usr/bin/env python3
"""Simple REQ client — run after server.py is started.

Connects to the REP server, sends NUM_REQUESTS messages, and prints each
reply.  The REQ protocol enforces a strict send → recv → send → …  cycle:
you must receive a reply before sending the next request.

    python client.py
"""

import nng

URL = "tcp://127.0.0.1:54321"
NUM_REQUESTS = 5

def main() -> None:
    print(f"Client connecting to {URL}\n")

    with nng.ReqSocket() as req:
        # block=True waits until the TCP handshake completes
        req.add_dialer(URL).start(block=True)
        print("Connected.\n")

        for i in range(1, NUM_REQUESTS + 1):
            # Prepare and send request
            msg = f"request-{i}"
            print(f"  send [{i}/{NUM_REQUESTS}]  => '{msg}'")
            req.send(msg)

            # Receive and handle reply
            reply = req.recv()
            print(f"  recv [{i}/{NUM_REQUESTS}]  <= '{reply}'")

    print("\nClient done.")


if __name__ == "__main__":
    main()
