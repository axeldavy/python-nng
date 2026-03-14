/*
 * bench_server.c — nng REP echo server for benchmarking.
 *
 * Usage: bench_server <url>
 *
 * The server echoes every received message back to the sender until it
 * receives SIGTERM or SIGINT, at which point it exits cleanly.
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nng/nng.h>

static volatile int g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr, "Usage: bench_server <url>\n");
        return 1;
    }
    const char *url = argv[1];

    signal(SIGTERM, on_signal);
    signal(SIGINT,  on_signal);

    if (nng_init(NULL) != 0) {
        fprintf(stderr, "nng_init failed\n");
        return 1;
    }

    nng_socket rep;
    int rv;


    if ((rv = nng_rep0_open(&rep)) != 0) {
        fprintf(stderr, "nng_rep0_open: %s\n", nng_strerror(rv));
        return 1;
    }

    /* Short receive timeout so we can check the stop flag. */
    nng_socket_set_ms(rep, NNG_OPT_RECVTIMEO, 200);

    if ((rv = nng_listen(rep, url, NULL, 0)) != 0) {
        fprintf(stderr, "nng_listen(%s): %s\n", url, nng_strerror(rv));
        nng_socket_close(rep);
        return 1;
    }

    /* Signal that we are ready (parent process reads one line). */
    fprintf(stdout, "READY\n");
    fflush(stdout);

    nng_msg *msg = NULL;
    while (!g_stop) {
        if ((rv = nng_recvmsg(rep, &msg, 0)) != 0) {
            if (rv == NNG_ETIMEDOUT) {
                continue; /* check stop flag */
            }
            if (rv == NNG_ECLOSED) {
                break;
            }
            fprintf(stderr, "nng_recvmsg: %s\n", nng_strerror(rv));
            break;
        }

        /* Echo the message back.  nng_sendmsg takes ownership. */
        if ((rv = nng_sendmsg(rep, msg, 0)) != 0) {
            fprintf(stderr, "nng_sendmsg: %s\n", nng_strerror(rv));
            nng_msg_free(msg);
            break;
        }
        msg = NULL;
    }

    if (msg) {
        nng_msg_free(msg);
    }

    nng_socket_close(rep);
    nng_fini();
    return 0;
}
