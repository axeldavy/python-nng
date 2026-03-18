/*
 * bench_client_ops.c — nng REQ ops/sec client (external REP server).
 *
 * Usage: bench_client_ops <url> <msg_size> <n_warmup> <duration_s>
 *
 * Connects to a bench_server running at <url>, performs n_warmup warmup
 * round-trips, then loops until the wall-clock deadline and prints the
 * achieved ops/sec as JSON on stdout.
 *
 * Using time-based termination (rather than a fixed iteration count) prevents
 * hangs with large messages where a fixed-count run could take far longer than
 * intended.
 */

#include "bench_common.h"

int main(int argc, char *argv[])
{
    if (argc < 5) {
        fprintf(stderr,
                "Usage: bench_client_ops <url> <msg_size> <n_warmup> <duration_s>\n");
        return 1;
    }

    const char *url        = argv[1];
    size_t      msg_size   = (size_t)atol(argv[2]);
    int         n_warmup   = atoi(argv[3]);
    double      duration_s = atof(argv[4]);

    if (msg_size == 0 || duration_s <= 0.0) {
        fprintf(stderr, "msg_size must be > 0 and duration_s must be > 0\n");
        return 1;
    }

    if (nng_init(NULL) != 0) { fprintf(stderr, "nng_init failed\n"); return 1; }

    nng_socket req;
    int rv;

    if ((rv = nng_req0_open(&req)) != 0) {
        fprintf(stderr, "nng_req0_open: %s\n", nng_strerror(rv));
        nng_fini(); return 1;
    }

    nng_socket_set_ms(req, NNG_OPT_RECVTIMEO, 10000);
    nng_socket_set_ms(req, NNG_OPT_SENDTIMEO, 10000);

    if ((rv = nng_dial(req, url, NULL, 0)) != 0) {
        fprintf(stderr, "nng_dial(%s): %s\n", url, nng_strerror(rv));
        nng_socket_close(req); nng_fini(); return 1;
    }

    unsigned char *payload = (unsigned char *)calloc(1, msg_size);
    if (!payload) {
        fprintf(stderr, "allocation failed\n");
        nng_socket_close(req); nng_fini(); return 1;
    }

    if ((rv = run_warmup(req, payload, msg_size, n_warmup)) != 0) goto err;

    /* ---------- measured ---------- */
    nng_msg *smsg = NULL;
    nng_msg *rmsg = NULL;
    double t_start  = now_us();
    double deadline = t_start + duration_s * 1e6;
    long   count    = 0;

    while (now_us() < deadline) {
        if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) goto err;
        memcpy(nng_msg_body(smsg), payload, msg_size);
        if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); smsg = NULL; goto err; }
        smsg = NULL;
        if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) goto err;
        nng_msg_free(rmsg); rmsg = NULL;
        count++;
    }

    double elapsed_s = (now_us() - t_start) / 1e6;
    print_ops_json(count, elapsed_s);

    free(payload);
    nng_socket_close(req); nng_fini();
    return 0;

err:
    fprintf(stderr, "nng error: %s\n", nng_strerror(rv));
    if (smsg) nng_msg_free(smsg);
    if (rmsg) nng_msg_free(rmsg);
    free(payload);
    nng_socket_close(req); nng_fini();
    return 1;
}
