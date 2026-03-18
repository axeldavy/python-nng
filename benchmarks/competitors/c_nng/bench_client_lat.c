/*
 * bench_client_lat.c — nng REQ latency client (external REP server).
 *
 * Usage: bench_client_lat <url> <msg_size> <n_warmup> <n_iters>
 *
 * Connects to a bench_server running at <url>, performs n_warmup + n_iters
 * round-trips, and prints per-RTT latency statistics as JSON on stdout.
 */

#include "bench_common.h"

int main(int argc, char *argv[])
{
    if (argc < 5) {
        fprintf(stderr,
                "Usage: bench_client_lat <url> <msg_size> <n_warmup> <n_iters>\n");
        return 1;
    }

    const char *url      = argv[1];
    size_t      msg_size = (size_t)atol(argv[2]);
    int         n_warmup = atoi(argv[3]);
    int         n_iters  = atoi(argv[4]);

    if (msg_size == 0 || n_iters <= 0) {
        fprintf(stderr, "msg_size and n_iters must be > 0\n");
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
    double        *samples = (double *)malloc((size_t)n_iters * sizeof(double));
    if (!payload || !samples) {
        fprintf(stderr, "allocation failed\n");
        free(payload); free(samples);
        nng_socket_close(req); nng_fini(); return 1;
    }

    if ((rv = run_warmup(req, payload, msg_size, n_warmup)) != 0) goto err;

    /* ---------- measured ---------- */
    nng_msg *smsg = NULL;
    nng_msg *rmsg = NULL;
    double t_start = now_us();

    for (int i = 0; i < n_iters; i++) {
        if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) goto err;
        memcpy(nng_msg_body(smsg), payload, msg_size);
        double t0 = now_us();
        if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); smsg = NULL; goto err; }
        smsg = NULL;
        if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) goto err;
        samples[i] = now_us() - t0;
        nng_msg_free(rmsg); rmsg = NULL;
    }

    double elapsed_s = (now_us() - t_start) / 1e6;
    print_latency_json(samples, n_iters, elapsed_s);

    free(payload); free(samples);
    nng_socket_close(req); nng_fini();
    return 0;

err:
    fprintf(stderr, "nng error: %s\n", nng_strerror(rv));
    if (smsg) nng_msg_free(smsg);
    if (rmsg) nng_msg_free(rmsg);
    free(payload); free(samples);
    nng_socket_close(req); nng_fini();
    return 1;
}
