"""tests/test_errors.py
~~~~~~~~~~~~~~~~~~~~
Unit tests verifying that documented error raises in the nng API are correct
and complete.  Each section exercises one exception type or a closely related
group of error-path scenarios.

Organisation
------------
1.  Exception class hierarchy and NngError base
2.  NngClosed  – all documented operations that raise after close
3.  NngAgain   – non-blocking recv/send when not ready
4.  NngTimeout – recv_timeout and send_timeout across sync recv/send,
    async arecv/asend, and submit_recv/submit_send
5.  NngNotSupported – recv/send on one-directional sockets
6.  NngAddressInUse – duplicate listener binding
7.  NngConnectionRefused – blocking dial to a non-existent listener
8.  NngState   – protocol state machine violations + double-start
9.  NngNotSupported from bad URL – unrecognized transport scheme in add_dialer / add_listener
10. Context error paths (NngClosed from closed context, NngState violations)
11. submit_recv / submit_send future error propagation

Naming convention: every URL uses a unique suffix so tests are fully
independent and run in any order (including in parallel).
"""
import asyncio

import pytest
import nng
from nng import (
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
)

_BASE = "inproc://test_errors"


def _url(suffix: str) -> str:
    return f"{_BASE}_{suffix}"


# =============================================================================
# 1.  Exception class hierarchy and NngError base
# =============================================================================

class TestNngErrorBase:
    """NngError is the documented base class for everything nng raises."""

    # -- construction ----------------------------------------------------------

    def test_is_exception_subclass(self):
        assert issubclass(NngError, Exception)

    def test_code_attribute_stored(self):
        err = NngError(7, "test message")
        assert err.code == 7

    def test_auto_message_from_nng_strerror(self):
        """When no message is supplied, nng_strerror fills it in."""
        err = NngError(0)  # 0 == NNG_OK → "Successful" or equivalent
        assert len(str(err)) > 0  # non-empty

    def test_custom_message_in_str(self):
        err = NngError(99, "custom text")
        assert "custom text" in str(err)
        assert "99" in str(err)

    def test_str_starts_with_bracket_code(self):
        err = NngError(1, "boom")
        assert str(err).startswith("[NNG-1]")

    def test_code_propagated_from_subclass(self):
        err = NngTimeout(11, "timed out")
        assert err.code == 11

    # -- inheritance: documented Python built-in co-base ----------------------

    def test_NngTimeout_is_TimeoutError(self):
        assert issubclass(NngTimeout, TimeoutError)
        assert issubclass(NngTimeout, NngError)

    def test_NngConnectionRefused_is_ConnectionRefusedError(self):
        assert issubclass(NngConnectionRefused, ConnectionRefusedError)
        assert issubclass(NngConnectionRefused, NngError)

    def test_NngClosed_is_NngError(self):
        assert issubclass(NngClosed, NngError)

    def test_NngAgain_is_NngError(self):
        assert issubclass(NngAgain, NngError)

    def test_NngNotSupported_is_NotImplementedError(self):
        assert issubclass(NngNotSupported, NotImplementedError)
        assert issubclass(NngNotSupported, NngError)

    def test_NngAddressInUse_is_OSError(self):
        assert issubclass(NngAddressInUse, OSError)
        assert issubclass(NngAddressInUse, NngError)

    def test_NngPermission_is_PermissionError(self):
        assert issubclass(NngPermission, PermissionError)
        assert issubclass(NngPermission, NngError)

    def test_NngCanceled_is_NngError(self):
        assert issubclass(NngCanceled, NngError)

    def test_NngConnectionReset_is_ConnectionResetError(self):
        assert issubclass(NngConnectionReset, ConnectionResetError)
        assert issubclass(NngConnectionReset, NngError)

    def test_NngConnectionAborted_is_ConnectionAbortedError(self):
        assert issubclass(NngConnectionAborted, ConnectionAbortedError)
        assert issubclass(NngConnectionAborted, NngError)

    def test_NngNoMemory_is_MemoryError(self):
        assert issubclass(NngNoMemory, MemoryError)
        assert issubclass(NngNoMemory, NngError)

    def test_NngInvalidArgument_is_ValueError(self):
        assert issubclass(NngInvalidArgument, ValueError)
        assert issubclass(NngInvalidArgument, NngError)

    def test_NngState_is_NngError(self):
        assert issubclass(NngState, NngError)

    def test_NngAuthError_is_NngError(self):
        assert issubclass(NngAuthError, NngError)

    def test_NngCryptoError_is_NngError(self):
        assert issubclass(NngCryptoError, NngError)

    def test_all_subclasses_catchable_as_NngError(self):
        """Every documented subclass is catch-able as plain NngError."""
        subclasses = [
            NngTimeout, NngConnectionRefused, NngClosed, NngAgain,
            NngNotSupported, NngAddressInUse, NngPermission, NngCanceled,
            NngConnectionReset, NngConnectionAborted, NngNoMemory,
            NngInvalidArgument, NngState, NngAuthError, NngCryptoError,
        ]
        for cls in subclasses:
            inst = cls(1)
            assert isinstance(inst, NngError), f"{cls.__name__} not catchable as NngError"

    def test_all_subclasses_have_code(self):
        subclasses = [
            NngTimeout, NngConnectionRefused, NngClosed, NngAgain,
            NngNotSupported, NngAddressInUse, NngPermission, NngCanceled,
            NngConnectionReset, NngConnectionAborted, NngNoMemory,
            NngInvalidArgument, NngState, NngAuthError, NngCryptoError,
        ]
        for cls in subclasses:
            inst = cls(42)
            assert inst.code == 42, f"{cls.__name__}.code not 42"


# =============================================================================
# 2.  NngClosed
#     Documented: socket/dialer/listener operations after close raise NngClosed.
# =============================================================================

class TestNngClosed:

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _closed():
        s = nng.PairSocket()
        s.close()
        return s

    # -- socket operations after close ----------------------------------------

    def test_send_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().send(b"x")

    def test_recv_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().recv()

    def test_submit_send_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().submit_send(b"x")

    def test_submit_recv_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().submit_recv()

    def test_add_dialer_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().add_dialer(_url("add_dial_ac"))

    def test_add_listener_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().add_listener(_url("add_lst_ac"))

    def test_on_new_pipe_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().on_new_pipe(None)

    def test_pipes_property_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().pipes

    def test_proto_name_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().proto_name

    def test_peer_name_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().peer_name

    def test_recv_timeout_get_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().recv_timeout

    def test_recv_timeout_set_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().recv_timeout = 1000

    def test_send_timeout_get_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().send_timeout

    def test_send_timeout_set_after_close(self):
        with pytest.raises(NngClosed):
            self._closed().send_timeout = 1000

    def test_recv_buf_get_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().recv_buf

    def test_send_buf_get_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().send_buf

    def test_recv_max_size_get_after_close(self):
        with pytest.raises(NngClosed):
            _ = self._closed().recv_max_size

    # -- socket id returns 0, not NngClosed (documented) ----------------------

    def test_socket_id_zero_after_close(self):
        """Socket.id returns 0 when closed — it does NOT raise."""
        s = nng.PairSocket()
        s.close()
        assert s.id == 0  # documented: 0 when closed

    # -- context manager closes cleanly and id becomes 0 ----------------------

    def test_context_manager_closes(self):
        with nng.PairSocket() as s:
            s.add_listener(_url("cm_close")).start()
        assert s.id == 0

    # -- double close is idempotent --------------------------------------------

    def test_double_close_does_not_raise(self):
        s = nng.PairSocket()
        s.close()
        s.close()  # must not raise

    # -- dialer operations after parent socket is closed ----------------------

    def test_dialer_start_after_socket_close(self):
        """When the parent socket is closed, nng invalidates the dialer handle
        and returns NNG_ENOENT (not NNG_ECLOSED), so the base NngError fires."""
        s = nng.PairSocket()
        d = s.add_dialer(_url("dial_start_sc"))
        s.close()
        with pytest.raises(NngError):
            d.start()

    def test_dialer_reconnect_min_ms_after_close(self):
        """Documented: Dialer.reconnect_min_ms raises NngClosed if dialer is closed."""
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("dial_rmin"))
            d.close()
            with pytest.raises(NngClosed):
                _ = d.reconnect_min_ms

    def test_dialer_reconnect_max_ms_after_close(self):
        """Documented: Dialer.reconnect_max_ms raises NngClosed if dialer is closed."""
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("dial_rmax"))
            d.close()
            with pytest.raises(NngClosed):
                _ = d.reconnect_max_ms

    def test_dialer_recv_timeout_after_close(self):
        """Documented: Dialer.recv_timeout raises NngClosed if dialer is closed."""
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("dial_rtmo"))
            d.close()
            with pytest.raises(NngClosed):
                _ = d.recv_timeout

    def test_dialer_send_timeout_after_close(self):
        """Documented: Dialer.send_timeout raises NngClosed if dialer is closed."""
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("dial_stmo"))
            d.close()
            with pytest.raises(NngClosed):
                _ = d.send_timeout

    def test_dialer_close_idempotent(self):
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("dial_idem"))
            d.close()
            d.close()  # second close must not raise

    # -- listener operations after parent socket is closed --------------------

    def test_listener_start_after_socket_close(self):
        """When the parent socket is closed, nng invalidates the listener handle
        and returns NNG_ENOENT (not NNG_ECLOSED), so the base NngError fires."""
        s = nng.PairSocket()
        lst = s.add_listener(_url("lst_start_sc"))
        s.close()
        with pytest.raises(NngError):
            lst.start()

    def test_listener_close_idempotent(self):
        with nng.PairSocket() as s:
            lst = s.add_listener(_url("lst_idem"))
            lst.close()
            lst.close()  # second close must not raise

    # -- NngClosed is still NngError ------------------------------------------

    def test_NngClosed_catchable_as_NngError(self):
        with pytest.raises(NngError):
            self._closed().send(b"x")


# =============================================================================
# 3.  NngAgain – non-blocking operations when the queue is empty
# =============================================================================

class TestNngAgain:

    def test_nonblock_recv_no_message(self):
        """recv(nonblock=True) with empty recv queue → NngAgain."""
        with nng.PairSocket() as s:
            s.add_listener(_url("again_recv")).start()
            with pytest.raises(NngAgain):
                s.recv(nonblock=True)

    def test_NngAgain_is_NngError(self):
        with nng.PairSocket() as s:
            s.add_listener(_url("again_nng")).start()
            with pytest.raises(NngError):
                s.recv(nonblock=True)

    def test_NngAgain_code_is_set(self):
        with nng.PairSocket() as s:
            s.add_listener(_url("again_code")).start()
            exc = pytest.raises(NngAgain, s.recv, nonblock=True)
            assert exc.value.code != 0

    def test_nonblock_context_recv_no_message(self):
        """Sub context recv(nonblock=True) with no published message → NngAgain.

        Note: a REQ context enforces send-before-recv protocol order and raises
        NngState rather than NngAgain when recv is called without a prior send.
        SubSocket has no such ordering requirement, so NngAgain is returned when
        no message is available.
        """
        with nng.SubSocket() as s:
            s.add_listener(_url("again_ctx")).start()
            s.subscribe(b"")
            ctx = s.open_context()
            try:
                with pytest.raises(NngAgain):
                    ctx.recv(nonblock=True)
            finally:
                ctx.close()


# =============================================================================
# 4.  NngTimeout – blocking operations with a very short deadline
# =============================================================================

class TestNngTimeout:

    def test_recv_timeout(self):
        """recv() with short timeout and no sender → NngTimeout."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_recv")).start()
            with pytest.raises(NngTimeout):
                s.recv()

    def test_NngTimeout_is_TimeoutError(self):
        """NngTimeout must also be catchable as built-in TimeoutError."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_te")).start()
            with pytest.raises(TimeoutError):
                s.recv()

    def test_NngTimeout_is_NngError(self):
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_ne")).start()
            with pytest.raises(NngError):
                s.recv()

    def test_NngTimeout_code_is_set(self):
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_code")).start()
            exc = pytest.raises(NngTimeout, s.recv)
            assert exc.value.code != 0

    def test_submit_recv_timeout_via_future(self):
        """submit_recv().result() raises NngTimeout when the socket times out."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_sub")).start()
            fut = s.submit_recv()
            with pytest.raises(NngTimeout):
                fut.result(timeout=2.0)

    @pytest.mark.asyncio
    async def test_arecv_timeout(self):
        """arecv() with short timeout and no sender → NngTimeout."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_arecv")).start()
            with pytest.raises(NngTimeout):
                await s.arecv()

    @pytest.mark.asyncio
    async def test_arecv_timeout_is_TimeoutError(self):
        """arecv() NngTimeout is catchable as built-in TimeoutError."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("tmo_arecv_te")).start()
            with pytest.raises(TimeoutError):
                await s.arecv()

    def test_send_timeout(self):
        """send() with send_timeout and no available pipe → NngTimeout.

        PushSocket with send_buf=0 has no local queue; with no PULL receiver
        connected the first send blocks until send_timeout expires.
        """
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_send")).start()
            with pytest.raises(NngTimeout):
                s.send(b"x")

    def test_send_timeout_is_TimeoutError(self):
        """send() NngTimeout is catchable as built-in TimeoutError."""
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_send_te")).start()
            with pytest.raises(TimeoutError):
                s.send(b"x")

    def test_send_timeout_is_NngError(self):
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_send_ne")).start()
            with pytest.raises(NngError):
                s.send(b"x")

    def test_send_timeout_code_is_set(self):
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_send_code")).start()
            exc = pytest.raises(NngTimeout, s.send, b"x")
            assert exc.value.code != 0

    @pytest.mark.asyncio
    async def test_asend_timeout(self):
        """asend() with send_timeout and no available pipe → NngTimeout."""
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_asend")).start()
            with pytest.raises(NngTimeout):
                await s.asend(b"x")

    @pytest.mark.asyncio
    async def test_asend_timeout_is_TimeoutError(self):
        """asend() NngTimeout is catchable as built-in TimeoutError."""
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_asend_te")).start()
            with pytest.raises(TimeoutError):
                await s.asend(b"x")

    def test_submit_send_timeout_via_future(self):
        """submit_send().result() raises NngTimeout when send_timeout expires."""
        with nng.PushSocket() as s:
            s.send_timeout = 50
            s.send_buf = 0
            s.add_listener(_url("tmo_sub_send")).start()
            fut = s.submit_send(b"x")
            with pytest.raises(NngTimeout):
                fut.result(timeout=2.0)


# =============================================================================
# 5.  NngNotSupported – recv / send on one-directional sockets
# =============================================================================

class TestNngNotSupported:

    def test_pub_recv_raises(self):
        """Documented: PubSocket cannot receive; recv() → NngNotSupported."""
        with nng.PubSocket() as s:
            s.add_listener(_url("notsup_pub")).start()
            with pytest.raises(NngNotSupported):
                s.recv()

    def test_push_recv_raises(self):
        """Documented: PushSocket cannot receive; recv() → NngNotSupported."""
        with nng.PushSocket() as s:
            s.add_listener(_url("notsup_push")).start()
            with pytest.raises(NngNotSupported):
                s.recv()

    def test_pull_send_raises(self):
        """PullSocket is receive-only; send() → NngNotSupported."""
        with nng.PullSocket() as s:
            s.add_listener(_url("notsup_pull")).start()
            with pytest.raises(NngNotSupported):
                s.send(b"x")

    def test_sub_send_raises(self):
        """SubSocket is receive-only; send() → NngNotSupported."""
        with nng.SubSocket() as s:
            s.add_listener(_url("notsup_sub")).start()
            with pytest.raises(NngNotSupported):
                s.send(b"x")

    def test_NngNotSupported_is_NotImplementedError(self):
        """NngNotSupported must be catchable as built-in NotImplementedError."""
        with nng.PubSocket() as s:
            s.add_listener(_url("notsup_ni")).start()
            with pytest.raises(NotImplementedError):
                s.recv()

    def test_NngNotSupported_is_NngError(self):
        with nng.PushSocket() as s:
            s.add_listener(_url("notsup_ne")).start()
            with pytest.raises(NngError):
                s.recv()


# =============================================================================
# 6.  NngAddressInUse – two listeners bound to the same address
# =============================================================================

class TestNngAddressInUse:

    def test_duplicate_listener_inproc(self):
        """Second listener on the same inproc URL → NngAddressInUse."""
        u = _url("addruse")
        with nng.PairSocket() as s1, nng.PairSocket() as s2:
            s1.add_listener(u).start()
            with pytest.raises(NngAddressInUse):
                s2.add_listener(u).start()

    def test_NngAddressInUse_is_OSError(self):
        """Documented co-base is OSError."""
        u = _url("addruse_os")
        with nng.PairSocket() as s1, nng.PairSocket() as s2:
            s1.add_listener(u).start()
            with pytest.raises(OSError):
                s2.add_listener(u).start()

    def test_NngAddressInUse_is_NngError(self):
        u = _url("addruse_ne")
        with nng.PairSocket() as s1, nng.PairSocket() as s2:
            s1.add_listener(u).start()
            with pytest.raises(NngError):
                s2.add_listener(u).start()

    def test_NngAddressInUse_code_is_set(self):
        u = _url("addruse_code")
        with nng.PairSocket() as s1, nng.PairSocket() as s2:
            s1.add_listener(u).start()
            exc = pytest.raises(NngAddressInUse, s2.add_listener(u).start)
            assert exc.value.code != 0

    def test_original_listener_still_works_after_failure(self):
        """The first listener remains functional even after the duplicate attempt."""
        u = _url("addruse_ok")
        with nng.PairSocket() as srv, nng.PairSocket() as srv2, nng.PairSocket() as cli:
            srv.recv_timeout = 500
            srv.add_listener(u).start()
            with pytest.raises(NngAddressInUse):
                srv2.add_listener(u).start()
            # First listener is unaffected — a client can still connect.
            cli.add_dialer(u).start()
            cli.send(b"ok")
            assert srv.recv() == b"ok"


# =============================================================================
# 7.  NngConnectionRefused – blocking dial to a non-existent listener
# =============================================================================

class TestNngConnectionRefused:

    def test_blocking_dial_no_listener(self):
        """Documented: Dialer.start(block=True) with no listener →
        NngConnectionRefused."""
        with nng.PairSocket() as s:
            with pytest.raises(NngConnectionRefused):
                s.add_dialer(_url("connref")).start(block=True)

    def test_NngConnectionRefused_is_ConnectionRefusedError(self):
        """Documented co-base is ConnectionRefusedError."""
        with nng.PairSocket() as s:
            with pytest.raises(ConnectionRefusedError):
                s.add_dialer(_url("connref_cre")).start(block=True)

    def test_NngConnectionRefused_is_NngError(self):
        with nng.PairSocket() as s:
            with pytest.raises(NngError):
                s.add_dialer(_url("connref_ne")).start(block=True)

    def test_NngConnectionRefused_code_is_set(self):
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("connref_code"))
            exc = pytest.raises(NngConnectionRefused, d.start, block=True)
            assert exc.value.code != 0

    def test_nonblocking_dial_no_listener_does_not_raise(self):
        """Documented: start(block=False) returns immediately even if no listener;
        NngConnectionRefused is NOT raised."""
        with nng.PairSocket() as s:
            # Must not raise — nng retries in the background.
            s.add_dialer(_url("connref_nobk")).start(block=False)


# =============================================================================
# 8.  NngState – protocol state-machine violations and double-start
# =============================================================================

class TestNngState:

    def test_rep_send_before_recv(self):
        """Documented: REP socket send before recv → NngState.

        A connected REQ peer is required: without any active request in
        flight nng cannot enforce the REP state machine against thin air.
        """
        with nng.RepSocket() as rep, nng.ReqSocket() as req:
            u = _url("state_rep_sbr")
            rep.add_listener(u).start()
            req.add_dialer(u).start()
            # REP has a pipe but no pending request → must raise immediately.
            with pytest.raises(NngState):
                rep.send(b"reply without request", nonblock=True)

    def test_req_recv_before_send(self):
        """Documented: REQ socket recv before send → NngState."""
        with nng.ReqSocket() as s:
            s.add_listener(_url("state_req_rbs")).start()
            with pytest.raises(NngState):
                s.recv(nonblock=True)

    def test_NngState_is_NngError(self):
        with nng.RepSocket() as rep, nng.ReqSocket() as req:
            u = _url("state_ne")
            rep.add_listener(u).start()
            req.add_dialer(u).start()
            with pytest.raises(NngError):
                rep.send(b"x", nonblock=True)

    def test_NngState_code_is_set(self):
        with nng.RepSocket() as rep, nng.ReqSocket() as req:
            u = _url("state_code")
            rep.add_listener(u).start()
            req.add_dialer(u).start()
            exc = pytest.raises(NngState, rep.send, b"x", nonblock=True)
            assert exc.value.code != 0

    def test_dialer_start_twice_raises_NngState(self):
        """Documented: Dialer.start() already-started → NngState."""
        with nng.PairSocket() as srv, nng.PairSocket() as cli:
            srv.add_listener(_url("state_d2")).start()
            d = cli.add_dialer(_url("state_d2"))
            d.start()
            with pytest.raises(NngState):
                d.start()

    def test_listener_start_twice_raises_NngState(self):
        """Documented: Listener.start() already-started → NngState."""
        with nng.PairSocket() as s:
            lst = s.add_listener(_url("state_l2"))
            lst.start()
            with pytest.raises(NngState):
                lst.start()


# =============================================================================
# 9.  NngNotSupported from bad URL – unrecognized transport in add_dialer / add_listener
#     (nng returns NNG_ENOTSUP, not NNG_EINVAL, for unknown transport schemes)
# =============================================================================

class TestBadUrlErrors:
    """add_dialer / add_listener with an unrecognized transport scheme.

    nng returns NNG_ENOTSUP (NngNotSupported) rather than NNG_EINVAL
    (NngInvalidArgument) when the URL has an unknown transport scheme such as
    ``"foo://..."`` or ``"not-a-valid-url"``.  The docstrings have been
    corrected to document this.
    """

    def test_add_dialer_bad_url(self):
        """add_dialer with an unrecognized transport scheme → NngNotSupported.

        nng returns NNG_ENOTSUP (not NNG_EINVAL) for URLs whose transport
        scheme is unknown.  The docstring has been corrected.
        """
        with nng.PairSocket() as s:
            with pytest.raises(NngNotSupported):
                s.add_dialer("not-a-valid-url")

    def test_add_listener_bad_url(self):
        """add_listener with an unrecognized transport scheme → NngNotSupported."""
        with nng.PairSocket() as s:
            with pytest.raises(NngNotSupported):
                s.add_listener("not-a-valid-url")

    def test_bad_url_is_NotImplementedError(self):
        """NngNotSupported is a NotImplementedError (not ValueError)."""
        with nng.PairSocket() as s:
            with pytest.raises(NotImplementedError):
                s.add_dialer("garbage://!!")

    def test_bad_url_is_NngError(self):
        with nng.PairSocket() as s:
            with pytest.raises(NngError):
                s.add_dialer("garbage://!!")

    def test_bad_url_code_is_set(self):
        with nng.PairSocket() as s:
            exc = pytest.raises(NngNotSupported, s.add_dialer, "bad://url")
            assert exc.value.code != 0


# =============================================================================
# 10.  Context error paths
# =============================================================================

class TestContextErrors:

    def test_rep_context_send_before_recv_is_state(self):
        """Documented: REP context send before recv → NngState.

        A connected REQ peer is required so that nng has an active request
        state machine to enforce against.
        """
        with nng.RepSocket() as rep, nng.ReqSocket() as req:
            u = _url("ctx_rep_sbr")
            rep.add_listener(u).start()
            req.add_dialer(u).start()
            ctx = rep.open_context()
            try:
                with pytest.raises(NngState):
                    ctx.send(b"reply without request", nonblock=True)
            finally:
                ctx.close()

    def test_req_context_recv_before_send_is_state(self):
        """Documented: REQ context recv before send → NngState."""
        with nng.ReqSocket() as s:
            s.add_listener(_url("ctx_req_rbs")).start()
            ctx = s.open_context()
            try:
                with pytest.raises(NngState):
                    ctx.recv(nonblock=True)
            finally:
                ctx.close()

    def test_context_recv_after_close_is_NngClosed(self):
        """Documented: Context methods after close → NngClosed."""
        with nng.ReqSocket() as s:
            s.add_listener(_url("ctx_rc")).start()
            ctx = s.open_context()
            ctx.close()
            with pytest.raises(NngClosed):
                ctx.recv()

    def test_context_send_after_close_is_NngClosed(self):
        with nng.RepSocket() as s:
            s.add_listener(_url("ctx_sc")).start()
            ctx = s.open_context()
            ctx.close()
            with pytest.raises(NngClosed):
                ctx.send(b"x")

    def test_context_manager_close(self):
        """Context used as context manager closes cleanly."""
        with nng.ReqSocket() as s:
            s.add_listener(_url("ctx_cm")).start()
            with s.open_context() as ctx:
                pass
            with pytest.raises(NngClosed):
                ctx.recv()

    def test_sub_context_recv_timeout(self):
        """Sub context recv with timeout → NngTimeout when no publisher."""
        with nng.SubSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("ctx_sub_tmo")).start()
            s.subscribe(b"")
            ctx = s.open_context()
            try:
                with pytest.raises(NngTimeout):
                    ctx.recv()
            finally:
                ctx.close()

    @pytest.mark.asyncio
    async def test_context_arecv_timeout(self):
        """Context arecv() obeys the socket-level recv_timeout."""
        with nng.RepSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("ctx_arecv_atmo")).start()
            ctx = s.open_context()
            try:
                with pytest.raises(NngTimeout):
                    await ctx.arecv()
            finally:
                ctx.close()


# =============================================================================
# 11.  submit_recv / submit_send – future-based error propagation
# =============================================================================

class TestFutureErrors:

    def test_submit_recv_raises_closed_on_socket_close(self):
        """submit_recv() future raises NngClosed when the socket is closed."""
        with nng.PairSocket() as s:
            s.add_listener(_url("fut_rc")).start()
            fut = s.submit_recv()
            s.close()
            with pytest.raises(NngClosed):
                fut.result(timeout=2.0)

    def test_submit_recv_raises_timeout(self):
        """submit_recv() propagates NngTimeout from a timed-out socket."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("fut_tmo")).start()
            fut = s.submit_recv()
            with pytest.raises(NngTimeout):
                fut.result(timeout=2.0)

    def test_submit_send_raises_closed_immediately(self):
        """submit_send() on a closed socket raises NngClosed synchronously."""
        s = nng.PairSocket()
        s.close()
        with pytest.raises(NngClosed):
            s.submit_send(b"x")

    def test_submit_recv_raises_closed_immediately(self):
        """submit_recv() on a closed socket raises NngClosed synchronously."""
        s = nng.PairSocket()
        s.close()
        with pytest.raises(NngClosed):
            s.submit_recv()

    def test_future_error_catchable_as_NngError(self):
        """Errors from futures are NngError subclasses."""
        with nng.PairSocket() as s:
            s.recv_timeout = 50
            s.add_listener(_url("fut_ne")).start()
            fut = s.submit_recv()
            with pytest.raises(NngError):
                fut.result(timeout=2.0)

    @pytest.mark.asyncio
    async def test_arecv_raises_closed_when_socket_closed_concurrently(self):
        """arecv() raises NngClosed when another task closes the socket mid-await.

        Simulates the common asyncio pattern where a background task is blocked
        on arecv() and the socket is closed from the main task, e.g. on shutdown.
        """
        s = nng.RepSocket()
        s.add_listener(_url("fut_conc_close")).start()

        async def _recv_task() -> None:
            with pytest.raises(NngClosed):
                await s.arecv()

        task = asyncio.create_task(_recv_task())
        # Yield to let _recv_task reach its await before we close.
        await asyncio.sleep(0)
        s.close()
        await task


# =============================================================================
# 12.  recv_timeout / send_timeout property get/set correctness
# =============================================================================


class TestTimeoutProperties:
    """Verify recv_timeout and send_timeout property semantics."""

    def test_recv_timeout_default_is_minus_one(self):
        """recv_timeout is -1 (infinite) by default."""
        with nng.PairSocket() as s:
            assert s.recv_timeout == -1

    def test_recv_timeout_roundtrip(self):
        """recv_timeout set then read back returns the same value."""
        with nng.PairSocket() as s:
            s.recv_timeout = 250
            assert s.recv_timeout == 250

    def test_recv_timeout_zero_means_poll(self):
        """recv_timeout=0 causes recv() to return immediately with NngTimeout."""
        with nng.PairSocket() as s:
            s.recv_timeout = 0
            s.add_listener(_url("tmo_prop_zero")).start()
            with pytest.raises(NngTimeout):
                s.recv()

    def test_send_timeout_default_is_minus_one(self):
        """send_timeout is -1 (infinite) by default."""
        with nng.PairSocket() as s:
            assert s.send_timeout == -1

    def test_send_timeout_roundtrip(self):
        """send_timeout set then read back returns the same value."""
        with nng.PairSocket() as s:
            s.send_timeout = 300
            assert s.send_timeout == 300

    def test_dialer_recv_timeout_readable(self):
        """Dialer.recv_timeout is readable and defaults to -1 (inherit from socket).

        The per-dialer timeout is configured at construction time in
        ``Socket.add_dialer``; the property is read-only after creation.
        """
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("tmo_prop_dial_recv"))
            assert d.recv_timeout == -1

    def test_dialer_send_timeout_readable(self):
        """Dialer.send_timeout is readable and defaults to -1 (inherit from socket)."""
        with nng.PairSocket() as s:
            d = s.add_dialer(_url("tmo_prop_dial_send"))
            assert d.send_timeout == -1
