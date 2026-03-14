# nng/_errors.pxi – included into _nng.pyx
#
# Exception hierarchy mirroring nng_err values.
# Every nng C function that returns an int error code passes through check_err().

class NngError(Exception):
    """Base exception for all nng errors."""

    def __init__(self, int code, str message=None):
        self.code = code
        if message is None:
            message = nng_strerror(<nng_err>code).decode("utf-8", errors="replace")
        super().__init__(message)

    def __str__(self) -> str:
        return f"[NNG-{self.code}] {self.args[0]}"


class NngTimeout(NngError, TimeoutError):
    """NNG_ETIMEDOUT – operation timed out."""

class NngConnectionRefused(NngError, ConnectionRefusedError):
    """NNG_ECONNREFUSED – connection refused."""

class NngClosed(NngError):
    """NNG_ECLOSED – socket / pipe / aio is closed."""

class NngAgain(NngError):
    """NNG_EAGAIN – operation would block in non-blocking mode."""

class NngNotSupported(NngError, NotImplementedError):
    """NNG_ENOTSUP – operation not supported by this protocol."""

class NngAddressInUse(NngError, OSError):
    """NNG_EADDRINUSE – address already in use."""

class NngPermission(NngError, PermissionError):
    """NNG_EPERM – permission denied."""

class NngCanceled(NngError):
    """NNG_ECANCELED – operation was cancelled."""

class NngConnectionReset(NngError, ConnectionResetError):
    """NNG_ECONNRESET – connection reset by peer."""

class NngConnectionAborted(NngError, ConnectionAbortedError):
    """NNG_ECONNABORTED – connection aborted."""

class NngNoMemory(NngError, MemoryError):
    """NNG_ENOMEM – out of memory."""

class NngInvalidArgument(NngError, ValueError):
    """NNG_EINVAL – invalid argument."""

class NngState(NngError):
    """NNG_ESTATE – incorrect state for this operation."""

class NngAuthError(NngError):
    """NNG_EPEERAUTH – peer authentication failed."""

class NngCryptoError(NngError):
    """NNG_ECRYPTO – cryptographic error."""


# Map from int error code to the most-specific exception subclass.
_ERR_MAP: dict = {}

def _build_err_map():
    _ERR_MAP.update({
        NNG_ETIMEDOUT:    NngTimeout,
        NNG_ECONNREFUSED: NngConnectionRefused,
        NNG_ECLOSED:      NngClosed,
        NNG_EAGAIN:       NngAgain,
        NNG_ENOTSUP:      NngNotSupported,
        NNG_EADDRINUSE:   NngAddressInUse,
        NNG_EPERM:        NngPermission,
        NNG_ECANCELED:    NngCanceled,
        NNG_ECONNRESET:   NngConnectionReset,
        NNG_ECONNABORTED: NngConnectionAborted,
        NNG_ENOMEM:       NngNoMemory,
        NNG_EINVAL:       NngInvalidArgument,
        NNG_ESTATE:       NngState,
        NNG_EPEERAUTH:    NngAuthError,
        NNG_ECRYPTO:      NngCryptoError,
    })

_build_err_map()


# ── Module-level C helpers ────────────────────────────────────────────────────

cdef object _err_from_code(int code):
    """Create the most-specific NngError subclass for *code*."""
    cdef str msg = nng_strerror(<nng_err>code).decode("utf-8", errors="replace")
    cls = _ERR_MAP.get(code, NngError)
    return cls(code, msg)


cdef inline void check_err(int rv) except *:
    """Raise the appropriate NngError subclass when rv != NNG_OK."""
    if rv != 0:
        raise _err_from_code(rv)
