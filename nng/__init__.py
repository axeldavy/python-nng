"""nng – high-performance Python bindings for the nng (nanomsg-next-gen) library.

All public symbols are re-exported from this package::

    from nng import (
        PairSocket, ReqSocket, RepSocket,
        PubSocket, SubSocket,
        PushSocket, PullSocket,
        SurveyorSocket, RespondentSocket,
        BusSocket,
        Message, Context, Dialer, Listener,
        TlsConfig,
        NngError, NngTimeout, NngConnectionRefused,
        PIPE_EV_ADD_PRE, PIPE_EV_ADD_POST, PIPE_EV_REM_POST,
        TLS_AUTH_NONE, TLS_AUTH_OPTIONAL, TLS_AUTH_REQUIRED,
        TLS_VERSION_1_2, TLS_VERSION_1_3,
        version, nng_init,
    )

Initialisation
--------------
nng is initialised automatically on first use (lazy singleton).  To supply
custom threading parameters, call :func:`initialize` **before** creating any
socket, dialer, listener, or other nng object::

    import nng
    nng.initialize(num_task_threads=4, max_task_threads=8)
    sock = nng.ReqSocket()
    ...
"""

from nng._nng import (
    # ── Sockets ──────────────────────────────────────────────────────────
    Socket,
    PairSocket,
    PubSocket,
    SubSocket,
    ReqSocket,
    RepSocket,
    PushSocket,
    PullSocket,
    SurveyorSocket,
    RespondentSocket,
    BusSocket,

    # ── Supporting types ─────────────────────────────────────────────────
    Message,
    Context,
    Dialer,
    Listener,
    Pipe,    PipeStatus,    TlsConfig,

    # ── Errors ───────────────────────────────────────────────────────────
    NngError,
    NngTimeout,
    NngConnectionRefused,
    NngClosed,
    NngAgain,
    NngNotSupported,
    NngAddressInUse,
    NngPermission,
    NngCanceled,
    NngConnectionReset,
    NngConnectionAborted,
    NngNoMemory,
    NngInvalidArgument,
    NngState,
    NngAuthError,
    NngCryptoError,

    # ── TLS constants ─────────────────────────────────────────────────────
    TLS_AUTH_NONE,
    TLS_AUTH_OPTIONAL,
    TLS_AUTH_REQUIRED,
    TLS_VERSION_1_2,
    TLS_VERSION_1_3,

    # ── Utility functions ─────────────────────────────────────────────────
    version,
    random,
    initialize,
)

__all__ = [
    # Sockets
    "Socket",
    "PairSocket", "PubSocket", "SubSocket",
    "ReqSocket", "RepSocket",
    "PushSocket", "PullSocket",
    "SurveyorSocket", "RespondentSocket",
    "BusSocket",
    # Types
    "Message", "Context", "Dialer", "Listener", "Pipe", "PipeStatus", "TlsConfig",
    # Errors
    "NngError", "NngTimeout", "NngConnectionRefused", "NngClosed",
    "NngAgain", "NngNotSupported", "NngAddressInUse", "NngPermission",
    "NngCanceled", "NngConnectionReset", "NngConnectionAborted",
    "NngNoMemory", "NngInvalidArgument", "NngState",
    "NngAuthError", "NngCryptoError",
    # Pipe events (backward compat)
    "PIPE_EV_ADD_PRE", "PIPE_EV_ADD_POST", "PIPE_EV_REM_POST",
    # TLS
    "TLS_AUTH_NONE", "TLS_AUTH_OPTIONAL", "TLS_AUTH_REQUIRED",
    "TLS_VERSION_1_2", "TLS_VERSION_1_3",
    # Utilities
    "version", "random", "initialize",
]
