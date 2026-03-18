/*
 * bench_common.h — shared utilities for nng benchmark binaries.
 *
 * Provides:
 *   - now_us()              portable monotonic clock in microseconds
 *   - cmp_double()          qsort comparator for double
 *   - percentile()          interpolated percentile on a sorted array
 *   - print_latency_json()  emit latency stats as JSON to stdout
 *   - print_ops_json()      emit ops/sec result as JSON to stdout
 *   - run_warmup()          perform a fixed number of REQ/REP warmup round-trips
 *   - server_state          synchronisation state for the inproc server thread
 *   - server_fn()           inproc REP echo server thread entry point
 *   - start_inproc_server() start server_fn in a nng_thread and wait until ready
 *   - stop_inproc_server()  signal the server thread to stop and join it
 *
 * All functions are declared static inline to prevent unused-symbol warnings
 * when only a subset is used by a given translation unit.
 */

#ifndef BENCH_COMMON_H
#define BENCH_COMMON_H

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nng/nng.h>

/* ---- Portable monotonic clock (µs) ------------------------------------- */

#if defined(_WIN32)
#  include <windows.h>
static double _freq_us = 0.0; /* one copy per TU — fine, each binary is one TU */
static inline double now_us(void)
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
static inline double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec / 1e3;
}
#endif

/* ---- Stats helpers ----------------------------------------------------- */

static inline int cmp_double(const void *a, const void *b)
{
    double da = *(const double *)a;
    double db = *(const double *)b;
    return (da > db) - (da < db);
}

static inline double percentile(const double *sorted, size_t n, double p)
{
    if (n == 0) return 0.0;
    double idx  = p / 100.0 * (double)(n - 1);
    size_t lo   = (size_t)idx;
    size_t hi   = lo + 1 < n ? lo + 1 : lo;
    double frac = idx - (double)lo;
    return sorted[lo] * (1.0 - frac) + sorted[hi] * frac;
}

/* Sort samples in-place, compute summary statistics, and print JSON. */
static inline void print_latency_json(double *samples, int n, double elapsed_s)
{
    qsort(samples, (size_t)n, sizeof(double), cmp_double);

    double sum = 0.0, sum2 = 0.0;
    for (int i = 0; i < n; i++) { sum += samples[i]; sum2 += samples[i] * samples[i]; }
    double mean     = sum / (double)n;
    double variance = sum2 / (double)n - mean * mean;
    double std_dev  = variance > 0.0 ? sqrt(variance) : 0.0;

    printf("{\n"
           "  \"min_us\":      %.3f,\n"
           "  \"p50_us\":      %.3f,\n"
           "  \"p95_us\":      %.3f,\n"
           "  \"p99_us\":      %.3f,\n"
           "  \"max_us\":      %.3f,\n"
           "  \"mean_us\":     %.3f,\n"
           "  \"std_us\":      %.3f,\n"
           "  \"n\":           %d,\n"
           "  \"ops_per_sec\": %.1f\n"
           "}\n",
           samples[0],
           percentile(samples, (size_t)n, 50.0),
           percentile(samples, (size_t)n, 95.0),
           percentile(samples, (size_t)n, 99.0),
           samples[n - 1],
           mean, std_dev, n,
           elapsed_s > 0.0 ? (double)n / elapsed_s : 0.0);
}

static inline void print_ops_json(long count, double elapsed_s)
{
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
           count,
           elapsed_s > 0.0 ? (double)count / elapsed_s : 0.0);
}

/* ---- Warmup helper ----------------------------------------------------- */

/*
 * Perform n_warmup REQ/REP round-trips on an already-dialed socket.
 * Returns 0 on success, or a non-zero nng error code on failure.
 * All message allocations are freed before returning.
 */
static inline int run_warmup(
    nng_socket req, const unsigned char *payload, size_t msg_size, int n_warmup)
{
    nng_msg *smsg = NULL;
    nng_msg *rmsg = NULL;
    int rv;

    for (int i = 0; i < n_warmup; i++) {
        if ((rv = nng_msg_alloc(&smsg, msg_size)) != 0) return rv;
        memcpy(nng_msg_body(smsg), payload, msg_size);
        if ((rv = nng_sendmsg(req, smsg, 0)) != 0) { nng_msg_free(smsg); return rv; }
        smsg = NULL;
        if ((rv = nng_recvmsg(req, &rmsg, 0)) != 0) return rv;
        nng_msg_free(rmsg);
    }
    return 0;
}

/* ---- Inproc server thread ---------------------------------------------- */

typedef struct {
    const char  *url;
    nng_mtx     *mtx;
    nng_cv      *cv;
    int          ready;      /* set to 1 once listener is up */
    volatile int stop;       /* set to 1 by main thread when done */
} server_state;

static inline void server_fn(void *arg)
{
    server_state *s = (server_state *)arg;
    nng_socket    rep;
    int           rv;

    if ((rv = nng_rep0_open(&rep)) != 0) {
        fprintf(stderr, "server: nng_rep0_open: %s\n", nng_strerror(rv));
        goto signal_ready;
    }

    /* Short receive timeout so the loop can poll the stop flag. */
    nng_socket_set_ms(rep, NNG_OPT_RECVTIMEO, 20);

    if ((rv = nng_listen(rep, s->url, NULL, 0)) != 0) {
        fprintf(stderr, "server: nng_listen(%s): %s\n", s->url, nng_strerror(rv));
        nng_socket_close(rep);
        goto signal_ready;
    }

signal_ready:
    nng_mtx_lock(s->mtx);
    s->ready = 1;
    nng_cv_wake(s->cv);
    nng_mtx_unlock(s->mtx);

    if (rv != 0) return;

    nng_msg *msg = NULL;
    while (!s->stop) {
        rv = nng_recvmsg(rep, &msg, 0);
        if (rv == NNG_ETIMEDOUT) continue;
        if (rv != 0) break;
        if ((rv = nng_sendmsg(rep, msg, 0)) != 0) { nng_msg_free(msg); break; }
        msg = NULL;
    }

    if (msg) nng_msg_free(msg);
    nng_socket_close(rep);
}

/*
 * Allocate synchronisation objects, spawn server_fn in a new thread, and
 * block until the listener is up.  Returns 0 on success, -1 on failure.
 * On success the caller must eventually call stop_inproc_server().
 */
static inline int start_inproc_server(
    const char *url, server_state *s, nng_thread **srv_out)
{
    s->url   = url;
    s->ready = 0;
    s->stop  = 0;

    if (nng_mtx_alloc(&s->mtx) != 0) return -1;
    if (nng_cv_alloc(&s->cv, s->mtx) != 0) {
        nng_mtx_free(s->mtx);
        return -1;
    }
    if (nng_thread_create(srv_out, server_fn, s) != 0) {
        nng_cv_free(s->cv);
        nng_mtx_free(s->mtx);
        return -1;
    }

    nng_mtx_lock(s->mtx);
    while (!s->ready)
        nng_cv_wait(s->cv);
    nng_mtx_unlock(s->mtx);

    return 0;
}

/*
 * Signal the server thread to stop, join it, and free synchronisation objects.
 * The caller must close the REQ socket before calling this function so that
 * the server thread can drain and exit cleanly.
 */
static inline void stop_inproc_server(server_state *s, nng_thread *srv)
{
    s->stop = 1;
    nng_thread_destroy(srv); /* blocks until server_fn returns */
    nng_cv_free(s->cv);
    nng_mtx_free(s->mtx);
}

#endif /* BENCH_COMMON_H */
