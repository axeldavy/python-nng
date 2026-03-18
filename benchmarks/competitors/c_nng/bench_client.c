/*
 * bench_client.c — nng REQ benchmark client.
 *
 * Usage (latency): bench_client <url> <msg_size> <n_warmup> <n_iters>
 * Usage (ops):     bench_client <url> <msg_size> <n_warmup> <n_iters> <duration_s>
 *
 * When <duration_s> is supplied the measured section loops until the wall-clock
 * deadline instead of for a fixed iteration count.  This prevents hangs with
 * large messages where a fixed-count run would take far longer than intended.
 * <n_iters> is still required as a placeholder but is ignored for the measured
 * loop; it is only used during warmup (capped at n_warmup).
 *
 * Outputs JSON with keys: min_us, p50_us, p95_us, p99_us, max_us, mean_us,
 * std_us, n, ops_per_sec.  In ops mode the per-sample latency fields are 0.
 */

#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nng/nng.h>

/* ---- portable monotonic clock (µs) ------------------------------------ */

#if defined(_WIN32)
#  include <windows.h>
static double _freq_us = 0.0;
static double now_us(void)
{
    if (_freq_us == 0.0) {
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        _freq_us = (double)f.QuadPart / 1e6;
    }
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart / _freq_us;
}
#else
#  include <time.h>
static double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec / 1e3;
}
#endif

/* ---- comparison for qsort --------------------------------------------- */

static int cmp_double(const void *a, const void *b)
{
    double da = *(const double *)a;
    double db = *(const double *)b;
    return (da > db) - (da < db);
}

/* ---- percentile (requires sorted array) -------------------------------- */

static double percentile(const double *sorted, size_t n, double p)
{
    if (n == 0) return 0.0;
    double idx = p / 100.0 * (double)(n - 1);
    size_t lo = (size_t)idx;
    size_t hi = lo + 1 < n ? lo + 1 : lo;
    double frac = idx - (double)lo;
    return sorted[lo] * (1.0 - frac) + sorted[hi] * frac;
}

/* ----------------------------------------------------------------------- */

int main(int argc, char *argv[])
{
    if (argc < 5) {
        fprintf(stderr,
                "Usage: bench_client <url> <msg_size> <n_warmup> <n_iters> [duration_s]\n");
        return 1;
    }

    const char *url      = argv[1];
    size_t      msg_size = (size_t)atol(argv[2]);
    int         n_warmup = atoi(argv[3]);
    int         n_iters  = atoi(argv[4]);
    int         ops_mode = (argc >= 6);
    double      duration_s = ops_mode ? atof(argv[5]) : 0.0;

    if (msg_size == 0 || (!ops_mode && n_iters <= 0)) {
        fprintf(stderr, "msg_size must be > 0; n_iters must be > 0 in latency mode\n");
        return 1;
    }

    if (nng_init(NULL) != 0) {
        fprintf(stderr, "nng_init failed\n");
        return 1;
    }

    nng_socket req;
    int rv;

    if ((rv = nng_req0_open(&req)) != 0) {
        fprintf(stderr, "nng_req0_open: %s\n", nng_strerror(rv));
        return 1;
    }

    /* Wait up to 10 s for the server to become available. */
    nng_socket_set_ms(req, NNG_OPT_RECVTIMEO, 10000);
    nng_socket_set_ms(req, NNG_OPT_SENDTIMEO, 10000);

    if ((rv = nng_dial(req, url, NULL, 0)) != 0) {
        fprintf(stderr, "nng_dial(%s): %s\n", url, nng_strerror(rv));
        nng_socket_close(req);
        return 1;
    }

    /* Allocate payload once; we reuse the allocation every send. */
    unsigned char *payload = (unsigned char *)calloc(1, msg_size);
    if (!payload) {
        fprintf(stderr, "calloc failed\n");
        nng_socket_close(req);
        return 1;
    }

    /* In ops mode we only need a counter — no per-sample array. */
    double *samples = NULL;
    if (!ops_mode) {
        samples = (double *)malloc((size_t)n_iters * sizeof(double));
        if (!samples) {
            fprintf(stderr, "malloc failed for samples\n");
            free(payload);
            nng_socket_close(req);
            return 1;
        }
    }

    nng_msg *smsg = NULL;
    nng_msg *rmsg = NULL;

    /* ---------- warmup ---------- */
    for (int i = 0; i < n_warmup; i++) {
        if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) goto err;
        memcpy(nng_msg_body(smsg), payload, msg_size);
        if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); goto err; }
        smsg = NULL;
        if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) goto err;
        nng_msg_free(rmsg); rmsg = NULL;
    }

    /* ---------- measured ---------- */
    double t_start = now_us();
    long   ops_count = 0;

    if (ops_mode) {
        double deadline = t_start + duration_s * 1e6;
        while (now_us() < deadline) {
            if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) goto err;
            memcpy(nng_msg_body(smsg), payload, msg_size);
            if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); smsg = NULL; goto err; }
            smsg = NULL;
            if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) goto err;
            nng_msg_free(rmsg); rmsg = NULL;
            ops_count++;
        }
    } else {
        for (int i = 0; i < n_iters; i++) {
            if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) goto err;
            memcpy(nng_msg_body(smsg), payload, msg_size);

            double t0 = now_us();
            if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); goto err; }
            smsg = NULL;
            if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) goto err;
            double t1 = now_us();

            nng_msg_free(rmsg); rmsg = NULL;
            samples[i] = t1 - t0;
        }
    }

    double t_end = now_us();
    double elapsed_s = (t_end - t_start) / 1e6;

    /* ---------- statistics ---------- */
    if (ops_mode) {
        printf("{\n"
               "  \"min_us\":      0,\n"
               "  \"p50_us\":      0,\n"
               "  \"p95_us\":      0,\n"
               "  \"p99_us\":      0,\n"
               "  \"max_us\":      0,\n"
               "  \"mean_us\":     0,\n"
               "  \"std_us\":      0,\n"
               "  \"n\":           %ld,\n"
               "  \"ops_per_sec\": %.1f\n"
               "}\n",
               ops_count,
               elapsed_s > 0.0 ? (double)ops_count / elapsed_s : 0.0);
    } else {
        qsort(samples, (size_t)n_iters, sizeof(double), cmp_double);

        double sum = 0.0, sum2 = 0.0;
        for (int i = 0; i < n_iters; i++) { sum += samples[i]; sum2 += samples[i] * samples[i]; }
        double mean = sum / (double)n_iters;
        double variance = sum2 / (double)n_iters - mean * mean;
        double std_dev = variance > 0.0 ? sqrt(variance) : 0.0;

        printf("{\n"
               "  \"min_us\":     %.3f,\n"
               "  \"p50_us\":     %.3f,\n"
               "  \"p95_us\":     %.3f,\n"
               "  \"p99_us\":     %.3f,\n"
               "  \"max_us\":     %.3f,\n"
               "  \"mean_us\":    %.3f,\n"
               "  \"std_us\":     %.3f,\n"
               "  \"n\":          %d,\n"
               "  \"ops_per_sec\": %.1f\n"
               "}\n",
               samples[0],
               percentile(samples, (size_t)n_iters, 50.0),
               percentile(samples, (size_t)n_iters, 95.0),
               percentile(samples, (size_t)n_iters, 99.0),
               samples[n_iters - 1],
               mean,
               std_dev,
               n_iters,
               elapsed_s > 0 ? (double)n_iters / elapsed_s : 0.0);
    }

    free(payload);
    free(samples);
    nng_socket_close(req);
    nng_fini();
    return 0;

err:
    fprintf(stderr, "nng error: %s\n", nng_strerror(rv));
    if (smsg) nng_msg_free(smsg);
    if (rmsg) nng_msg_free(rmsg);
    free(payload);
    free(samples);
    nng_socket_close(req);
    nng_fini();
    return 1;
}
