#!/usr/bin/env python3
"""
stubs/generate_stubs.py
=======================
Auto-generate ``nng/_nng.pyi`` from the compiled Cython extension.

Usage (from the repository root, after building / installing the package)::

    python stubs/generate_stubs.py

The script introspects the live ``nng._nng`` extension module to extract
docstrings and callable signatures, then merges them with the type-override
tables defined here to produce a fully-typed ``.pyi`` stub.

When the API changes, update the override tables in this script and re-run it.
New methods/properties whose names match entries in the override tables are
typed automatically; others fall back to ``Any``.
"""

import inspect
import os
import sys
import textwrap
import types


import nng as _mod  # noqa: E402

REPO_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "nng", "_nng.pyi")

# ============================================================================
# Type-override tables
# ============================================================================

# On Python >= 3.12 the generated stub uses collections.abc.Buffer;
# on older versions it falls back to memoryview (defined by the conditional
# import block emitted in _HEADER).
_DATA_TYPE = "bytes | Message | Buffer | bytearray | str"

# Global parameter-name → type mapping.  Applied to every method whose
# parameter has the matching name (unless overridden by METHOD_PARAM_TYPES).
PARAM_TYPES: dict[str, str] = {
    "data":               _DATA_TYPE,
    "url":                "str | bytes",
    "tls":                "TlsConfig | None",
    "callback":           "Callable[[Pipe], None] | None",
    "prefix":             "bytes",
    "capacity":           "int",
    "new_size":           "int",
    "nonblock":           "bool",
    "block":              "bool",
    "server":             "bool",
    "v0":                 "bool",
    "poly":               "bool",
    "raw":                "bool",
    "size":               "int",
    "n":                  "int",
    "ms":                 "int",
    "mode":               "int",
    "min_ver":            "int",
    "max_ver":            "int",
    "v":                  "int",
    # TlsConfig-specific
    "cert_pem":           "str",
    "crl_pem":            "str | None",
    "key_pem":            "str",
    "password":           "str | None",
    "path":               "str",
    "name":               "str",
    "raw_key":            "bytes",
    "identity":           "str",
    "psks":               "dict[str, bytes]",
    "min_version":        "int | None",
    "max_version":        "int | None",
    # Socket.add_dialer / add_listener kwonly opts
    "reconnect_min_ms":   "int | None",
    "reconnect_max_ms":   "int | None",
    "recv_timeout":       "int | None",
    "send_timeout":       "int | None",
    "tcp_keepalive":    "bool | None",
    "tcp_nodelay":      "bool | None",
    "tcp_local_addr":   "str | None",
    # initialize() params
    "num_task_threads":   "int",
    "max_task_threads":   "int",
    "num_expire_threads": "int",
    "max_expire_threads": "int",
    "num_poller_threads": "int",
    "max_poller_threads": "int",
    "num_resolver_threads": "int",
}

# Method-level param overrides: (class_name_or_None, method_name, param_name)
# Takes priority over PARAM_TYPES.
METHOD_PARAM_TYPES: dict[tuple[str | None, str, str], str] = {
    # Context / Socket send/recv timeouts are plain int (not Optional)
    ("Context", "recv_timeout", "ms"): "int",
    ("Context", "send_timeout", "ms"): "int",
    ("Socket", "recv_timeout", "ms"):  "int",
    ("Socket", "send_timeout", "ms"):  "int",
    ("Socket", "recv_buf", "n"):       "int",
    ("Socket", "send_buf", "n"):       "int",
    ("Socket", "recv_max_size", "n"):  "int",
    # poll-fd setters don't exist but guard against future additions
    # PairSocket polyamorous setter
    ("PairSocket", "polyamorous", "v"): "bool",
    ("ReqSocket", "resend_time", "ms"): "int",
    ("SurveyorSocket", "survey_time", "ms"): "int",
    # NngError constructor
    ("NngError", "__init__", "code"):    "int",
    ("NngError", "__init__", "message"): "str | None",
}

# Return-type overrides: (class_name_or_None, method_name) → return type.
# class_name=None means "any class".
RETURN_TYPES: dict[tuple[str | None, str], str] = {
    (None, "__init__"):     "None",
    (None, "__enter__"):    "Self",
    (None, "__exit__"):     "None",
    (None, "__repr__"):     "str",
    (None, "__str__"):      "str",
    (None, "__len__"):      "int",
    (None, "__hash__"):     "int",
    (None, "__index__"):    "int",
    (None, "__bool__"):     "bool",
    (None, "__eq__"):       "bool",
    (None, "__ne__"):       "bool",
    (None, "close"):        "None",
    (None, "start"):        "None",
    (None, "send"):         "None",
    (None, "recv"):         "Message",
    (None, "asend"):        "None",
    (None, "arecv"):        "Message",
    (None, "submit_send"):  "concurrent.futures.Future[None]",
    (None, "submit_recv"):  "concurrent.futures.Future[Message]",
    (None, "arecv_ready"):  "AsyncGenerator[None, None]",
    (None, "asend_ready"):  "AsyncGenerator[None, None]",
    (None, "open_context"): "Context",
    (None, "dup"):          "Message",
    (None, "to_bytes"):     "bytes",
    (None, "clear"):        "None",
    (None, "reserve"):      "None",
    (None, "resize"):       "None",
    (None, "append"):       "None",
    (None, "insert"):       "None",
    (None, "trim"):         "None",
    (None, "chop"):         "None",
    (None, "append_u16"):   "None",
    (None, "append_u32"):   "None",
    (None, "append_u64"):   "None",
    (None, "chop_u16"):     "int",
    (None, "chop_u32"):     "int",
    (None, "chop_u64"):     "int",
    (None, "trim_u16"):     "int",
    (None, "trim_u32"):     "int",
    (None, "trim_u64"):     "int",
    (None, "on_new_pipe"):  "None",
    (None, "subscribe"):    "None",
    (None, "unsubscribe"):  "None",
    (None, "get_peer_addr"): "SocketAddr",
    (None, "get_self_addr"): "SocketAddr",
    # TlsConfig factories
    ("TlsConfig", "for_client"):     "TlsConfig",
    ("TlsConfig", "for_server"):     "TlsConfig",
    ("TlsConfig", "for_psk_client"): "TlsConfig",
    ("TlsConfig", "for_psk_server"): "TlsConfig",
    # TlsCert factories and methods
    ("TlsCert", "from_pem"):        "TlsCert",
    ("TlsCert", "from_der"):        "TlsCert",
    ("TlsCert", "to_der"):          "bytes",
    # Pipe TLS methods
    ("Pipe", "get_peer_cert"):      "TlsCert | None",
    # Socket factory methods
    ("Socket", "add_dialer"):   "Dialer",
    ("Socket", "add_listener"): "Listener",
    ("SubSocket",      "open_context"): "SubContext",
    ("ReqSocket",      "open_context"): "ReqContext",
    ("SurveyorSocket", "open_context"): "SurveyorContext",
}

# Property type overrides: (class_name_or_None, prop_name) → type string.
PROP_TYPES: dict[tuple[str | None, str], str] = {
    (None, "id"):             "int",
    (None, "body"):           "memoryview",
    (None, "header"):         "bytes",
    (None, "pipe_id"):        "int",
    (None, "pipe"):           "Pipe | None",
    (None, "status"):         "PipeStatus",
    (None, "socket"):         "Socket | None",
    (None, "dialer"):         "Dialer | None",
    (None, "listener"):       "Listener | None",
    (None, "on_status_change"): "Callable[[], None] | None",
    (None, "recv_timeout"):   "int",
    (None, "send_timeout"):   "int",
    (None, "recv_buf"):       "int",
    (None, "send_buf"):       "int",
    (None, "recv_max_size"):  "int",
    (None, "recv_fd"):        "int",
    (None, "send_fd"):        "int",
    (None, "pipes"):          "list[Pipe]",
    (None, "proto_name"):     "str",
    (None, "peer_name"):      "str",
    (None, "polyamorous"):    "bool",
    (None, "resend_time"):    "int",
    (None, "resend_tick"):    "int",
    (None, "skip_older_on_full_queue"): "bool",
    (None, "survey_time"):    "int",
    (None, "port"):           "int",
    (None, "tcp_keepalive"): "bool",
    (None, "tcp_nodelay"): "bool",
    # TlsCert certificate properties
    (None, "subject"):       "str",
    (None, "issuer"):        "str",
    (None, "serial_number"): "str",
    (None, "subject_cn"):    "str",
    (None, "alt_names"):     "list[str]",
    (None, "not_before"):    "datetime.datetime",
    (None, "not_after"):     "datetime.datetime",
    # Pipe TLS properties
    (None, "tls_verified"): "bool",
    (None, "tls_peer_cn"):  "str | None",
}

# Read-write properties: (class_name, prop_name).  All others are read-only.
WRITABLE_PROPS: set[tuple[str, str]] = {
    ("Socket",       "recv_timeout"),
    ("Socket",       "send_timeout"),
    ("Socket",       "recv_max_size"),
    ("Context",      "recv_timeout"),
    ("Context",      "send_timeout"),
    ("Context",      "resend_time"),
    ("Context",      "recv_buf"),
    ("Context",      "skip_older_on_full_queue"),
    ("Context",      "survey_time"),
    ("ReqContext",   "resend_time"),
    ("SubContext",   "recv_buf"),
    ("SubContext",   "skip_older_on_full_queue"),
    ("SurveyorContext", "survey_time"),
    ("Pipe",         "on_status_change"),
    ("PairSocket",   "polyamorous"),
    ("PairSocket",   "recv_buf"),
    ("PairSocket",   "send_buf"),
    ("PubSocket",    "send_buf"),
    ("SubSocket",    "recv_buf"),
    ("SubSocket",    "skip_older_on_full_queue"),
    ("ReqSocket",    "resend_time"),
    ("ReqSocket",    "resend_tick"),
    ("PushSocket",   "send_buf"),
    ("SurveyorSocket", "survey_time"),
    ("SurveyorSocket", "recv_buf"),
    ("SurveyorSocket", "send_buf"),
    ("BusSocket",    "recv_buf"),
    ("BusSocket",    "send_buf"),
}

# Full parameter-string overrides for methods where signature introspection
# cannot reconstruct the correct signature (e.g. Cython __init__ with defaults).
# Values are complete param strings (including 'self' or 'cls').
HARDCODED_PARAM_STRINGS: dict[tuple[str, str], str] = {
    ("Message", "__init__"): "self, data: bytes | Buffer | bytearray | str | None = None, *, size: int = 0",
    ("TlsConfig", "for_client"): (
        "cls, *, server_name: str | None = None, ca_pem: str | None = None,"
        " crl_pem: str | None = None, ca_file: str | None = None,"
        " cert_pem: str | None = None, key_pem: str | None = None,"
        " cert_file: str | None = None, password: str | None = None,"
        " auth_mode: int | None = None, min_version: int | None = None,"
        " max_version: int | None = None"
    ),
    ("TlsConfig", "for_server"): (
        "cls, *, cert_pem: str | None = None, key_pem: str | None = None,"
        " cert_file: str | None = None, password: str | None = None,"
        " ca_pem: str | None = None, crl_pem: str | None = None,"
        " ca_file: str | None = None, auth_mode: int | None = None,"
        " min_version: int | None = None, max_version: int | None = None"
    ),
    ("TlsConfig", "for_psk_client"): (
        "cls, identity: str, key: bytes, *,"
        " min_version: int | None = None, max_version: int | None = None"
    ),
    ("TlsConfig", "for_psk_server"): (
        "cls, psks: dict[str, bytes], *,"
        " min_version: int | None = None, max_version: int | None = None"
    ),
}


ASYNC_GENERATORS: set[str] = {"arecv_ready", "asend_ready"}

# Methods to suppress from the stub entirely (internal helpers or untypeable).
SKIP_METHODS: set[str] = {
    "__cinit__", "__dealloc__", "__getbuffer__", "__releasebuffer__",
    "__pyx_cython_api__", "__reduce__", "__reduce_ex__",
    "__class__", "__dict__", "__doc__", "__module__", "__weakref__",
    "__new__", "__subclasshook__", "__init_subclass__",
    "__getattr__", "__setattr__",
}

# ============================================================================
# Helpers
# ============================================================================

I1 = "    "
I2 = "        "


def _getdoc(obj: object) -> str | None:
    """Return a cleaned docstring, or None."""
    doc = inspect.getdoc(obj)
    if not doc:
        return None
    # Strip Cython-generated trivial docstrings
    trivial = {"Initialize self.  See help(type(self)) for accurate signature."}
    if doc.strip() in trivial:
        return None
    return doc


def _format_doc(doc: str, indent: str = I1) -> list[str]:
    """Return lines for a triple-quoted docstring block."""
    lines = doc.splitlines()
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    parts = [f'{indent}"""']
    for line in lines:
        parts.append(f"{indent}{line}" if line else "")
    parts.append(f'{indent}"""')
    return parts


def _prop_type(class_name: str, prop_name: str) -> str:
    return (
        PROP_TYPES.get((class_name, prop_name))
        or PROP_TYPES.get((None, prop_name))
        or "Any"
    )


def _return_type(class_name: str, method_name: str) -> str:
    return (
        RETURN_TYPES.get((class_name, method_name))
        or RETURN_TYPES.get((None, method_name))
        or "Any"
    )


def _param_type(class_name: str, method_name: str, param_name: str) -> str:
    return (
        METHOD_PARAM_TYPES.get((class_name, method_name, param_name))
        or METHOD_PARAM_TYPES.get((None, method_name, param_name))
        or PARAM_TYPES.get(param_name)
        or "Any"
    )


def _is_property(cls: type, name: str) -> bool:
    """Return True if *name* on *cls* is a property-like descriptor."""
    desc = cls.__dict__.get(name)
    if desc is None:
        return False
    return isinstance(desc, (property, types.GetSetDescriptorType, types.MemberDescriptorType))


def _is_writable(class_name: str, prop_name: str) -> bool:
    return (class_name, prop_name) in WRITABLE_PROPS


def _build_param_str(
    class_name: str,
    method_name: str,
    sig: inspect.Signature,
) -> str:
    """Build the parameters string for a stub (excluding return annotation)."""
    parts: list[str] = []
    prev_kind = None
    for i, (pname, param) in enumerate(sig.parameters.items()):
        kind = param.kind

        # Insert '*' separator before keyword-only params when previous was positional
        if (
            kind == inspect.Parameter.KEYWORD_ONLY
            and prev_kind
            not in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
            )
        ):
            parts.append("*")

        # Insert '/' separator before positional-only when next is not positional-only
        # (handled at end – check ahead)

        if pname == "self":
            parts.append("self")
            prev_kind = kind
            continue

        # Type annotation
        if pname.startswith("**"):
            type_str = "Any"
        else:
            type_str = _param_type(class_name, method_name, pname)

        # Default value
        if param.default is inspect.Parameter.empty:
            default_str = ""
        else:
            dv = param.default
            if dv is None:
                default_str = " = None"
            elif isinstance(dv, bool):
                default_str = f" = {dv}"
            elif isinstance(dv, (int, float)):
                default_str = f" = {dv}"
            elif isinstance(dv, bytes):
                default_str = f" = {dv!r}"
            elif isinstance(dv, str):
                default_str = f' = "{dv}"'
            else:
                default_str = " = ..."

        # Handle VAR_POSITIONAL / VAR_KEYWORD
        if kind == inspect.Parameter.VAR_POSITIONAL:
            parts.append(f"*{pname}: {type_str}")
        elif kind == inspect.Parameter.VAR_KEYWORD:
            parts.append(f"**{pname}: {type_str}")
        else:
            parts.append(f"{pname}: {type_str}{default_str}")

        prev_kind = kind

    return ", ".join(parts)


def _emit_method(
    class_name: str,
    method_name: str,
    method_obj: object,
    indent: str = I1,
    *,
    is_classmethod: bool = False,
) -> list[str]:
    """Return stub lines for one method."""
    lines: list[str] = []

    # Unwrap classmethod descriptor before introspection
    raw_obj = method_obj.__func__ if is_classmethod and hasattr(method_obj, "__func__") else method_obj

    # Signature
    try:
        sig = inspect.signature(raw_obj)
    except (ValueError, TypeError):
        sig = None

    is_async = inspect.iscoroutinefunction(raw_obj)
    is_async_gen = (
        inspect.isasyncgenfunction(raw_obj)
        or method_name in ASYNC_GENERATORS
    )
    # Async generators are 'def' returning AsyncGenerator, not 'async def'
    if is_async_gen:
        is_async = False

    prefix = "async " if is_async else ""
    ret = _return_type(class_name, method_name)

    if (class_name, method_name) in HARDCODED_PARAM_STRINGS:
        param_str = HARDCODED_PARAM_STRINGS[(class_name, method_name)]
    elif sig is not None:
        param_str = _build_param_str(class_name, method_name, sig)
    else:
        first = "cls" if is_classmethod else "self"
        param_str = f"{first}, *args: Any, **kwargs: Any"

    if is_classmethod:
        lines.append(f"{indent}@classmethod")
    lines.append(f"{indent}{prefix}def {method_name}({param_str}) -> {ret}:")

    # Use the docstring from the underlying Cython function, not 'classmethod' descriptor
    doc = _getdoc(raw_obj)
    if doc:
        lines.extend(_format_doc(doc, indent + I1))
    lines.append(f"{indent}{I1}...")

    return lines


def _emit_property(
    class_name: str,
    prop_name: str,
    prop_obj: object,
    indent: str = I1,
) -> list[str]:
    """Return stub lines for one property (getter + optional setter)."""
    lines: list[str] = []
    ptype = _prop_type(class_name, prop_name)

    doc = _getdoc(prop_obj)
    lines.append(f"{indent}@property")
    lines.append(f"{indent}def {prop_name}(self) -> {ptype}:")
    if doc:
        lines.extend(_format_doc(doc, indent + I1))
    lines.append(f"{indent}{I1}...")

    if _is_writable(class_name, prop_name):
        # Determine setter parameter type (strip Optional for plain numeric types)
        # Use the setter param name 'value' generically
        setter_type = ptype.replace(" | None", "")
        lines.append(f"{indent}@{prop_name}.setter")
        lines.append(f"{indent}def {prop_name}(self, value: {setter_type}) -> None: ...")

    return lines


# ============================================================================
# Class classification helpers
# ============================================================================

def _class_members(cls: type) -> tuple[list[str], list[str]]:
    """Return (prop_names, method_names) defined directly on *cls*."""
    props: list[str] = []
    methods: list[str] = []

    for name in sorted(cls.__dict__):
        if name in SKIP_METHODS:
            continue
        desc = cls.__dict__[name]
        if _is_property(cls, name):
            props.append(name)
        elif callable(desc) or isinstance(desc, classmethod):
            methods.append(name)

    return props, methods


def _method_order_key(name: str) -> tuple[int, str]:
    """Sort key: __init__ first, then public, then dunder."""
    if name == "__init__":
        return (0, name)
    if not name.startswith("__"):
        return (1, name)
    return (2, name)


def _property_order_key(name: str) -> tuple[int, str]:
    if not name.startswith("_"):
        return (0, name)
    return (1, name)


# ============================================================================
# Main class emitter
# ============================================================================

def _bases_str(cls: type) -> str:
    """Format the base-class list for the class declaration."""
    skip = {object}
    bases = [b for b in cls.__bases__ if b not in skip]
    if not bases:
        return ""
    return "(" + ", ".join(b.__name__ for b in bases) + ")"


def emit_class(cls: type) -> list[str]:
    """Return all stub lines for one class."""
    class_name = cls.__name__
    lines: list[str] = []
    lines.append(f"class {class_name}{_bases_str(cls)}:")

    doc = _getdoc(cls)
    if doc:
        lines.extend(_format_doc(doc, I1))
    else:
        lines.append(f"{I1}...")
        return lines

    prop_names, method_names = _class_members(cls)

    # Order: __init__ → public methods/props → dunders
    method_names.sort(key=_method_order_key)
    prop_names.sort(key=_property_order_key)

    # Emit __init__ (methods list already sorted)
    has_content = False
    for mname in method_names:
        if mname != "__init__":
            continue
        mobj = cls.__dict__[mname]
        lines.extend(_emit_method(class_name, mname, mobj))
        lines.append("")
        has_content = True

    # Emit properties
    for pname in prop_names:
        pobj = cls.__dict__.get(pname)
        lines.extend(_emit_property(class_name, pname, pobj))
        lines.append("")
        has_content = True

    # Emit remaining methods (skip __init__ already done)
    for mname in method_names:
        if mname == "__init__":
            continue
        desc = cls.__dict__[mname]
        is_cm = isinstance(desc, classmethod)
        lines.extend(_emit_method(class_name, mname, desc, is_classmethod=is_cm))
        lines.append("")
        has_content = True

    if not has_content:
        lines.append(f"{I1}...")

    return lines


# ============================================================================
# Module-level constants and functions
# ============================================================================

_KNOWN_INT_CONSTANTS = {
    "TLS_AUTH_NONE", "TLS_AUTH_OPTIONAL", "TLS_AUTH_REQUIRED",
    "TLS_VERSION_1_2", "TLS_VERSION_1_3",
}


def emit_module_constants() -> list[str]:
    lines: list[str] = ["# Module-level constants", ""]
    for name in sorted(_KNOWN_INT_CONSTANTS):
        lines.append(f"{name}: int")
    lines.append("")
    return lines


def emit_module_functions() -> list[str]:
    lines: list[str] = ["# Module-level functions", ""]
    fn_names = ["version", "random", "initialize", "tls_engine_name", "tls_engine_description"]
    for fname in fn_names:
        fn = getattr(_mod, fname, None)
        if fn is None:
            continue
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            sig = None

        ret = _return_type("", fname) or "Any"
        if sig is not None:
            param_str = _build_param_str("", fname, sig)
        else:
            param_str = "*args: Any, **kwargs: Any"

        lines.append(f"def {fname}({param_str}) -> {ret}:")
        doc = _getdoc(fn)
        if doc:
            lines.extend(_format_doc(doc, I1))
        lines.append(f"{I1}...")
        lines.append("")
    return lines


# ============================================================================
# Exception hierarchy (hardcoded – simpler than generic introspection)
# ============================================================================

_EXCEPTIONS = [
    ("NngError", ["Exception"],
     "Base exception for all nng errors.\n\n"
     "``code`` is the raw nng error integer."),
    ("NngTimeout",           ["NngError", "TimeoutError"],       None),
    ("NngConnectionRefused", ["NngError", "ConnectionRefusedError"], None),
    ("NngClosed",            ["NngError"],                        None),
    ("NngAgain",             ["NngError"],                        None),
    ("NngNotSupported",      ["NngError", "NotImplementedError"], None),
    ("NngAddressInUse",      ["NngError", "OSError"],             None),
    ("NngPermission",        ["NngError", "PermissionError"],     None),
    ("NngCanceled",          ["NngError"],                        None),
    ("NngConnectionReset",   ["NngError", "ConnectionResetError"], None),
    ("NngConnectionAborted", ["NngError", "ConnectionAbortedError"], None),
    ("NngNoMemory",          ["NngError", "MemoryError"],         None),
    ("NngInvalidArgument",   ["NngError", "ValueError"],          None),
    ("NngState",             ["NngError"],                        None),
    ("NngAuthError",         ["NngError"],                        None),
    ("NngCryptoError",       ["NngError"],                        None),
]


def emit_exceptions() -> list[str]:
    lines: list[str] = []
    for cls_name, bases, forced_doc in _EXCEPTIONS:
        cls = getattr(_mod, cls_name, None)
        bases_str = "(" + ", ".join(bases) + ")"
        lines.append(f"class {cls_name}{bases_str}:")

        # NngError gets a rich stub; subclasses just inherit
        if cls_name == "NngError":
            doc = forced_doc or (cls and _getdoc(cls))
            if doc:
                lines.extend(_format_doc(doc, I1))
            lines.append(f"{I1}code: int")
            lines.append(f"{I1}def __init__(self, code: int, message: str | None = None) -> None: ...")
            lines.append(f"{I1}def __str__(self) -> str: ...")
        else:
            # Live docstring if available
            if cls:
                doc = _getdoc(cls)
                if doc:
                    lines.extend(_format_doc(doc, I1))
                    lines.append(f"{I1}...")
                else:
                    lines.append(f"{I1}...")
            else:
                lines.append(f"{I1}...")
        lines.append("")
    return lines


# ============================================================================
# PipeStatus (IntEnum)
# ============================================================================

def emit_pipe_status() -> list[str]:
    cls = getattr(_mod, "PipeStatus", None)
    lines: list[str] = []
    lines.append("class PipeStatus(int):")
    if cls:
        doc = _getdoc(cls)
        if doc:
            lines.extend(_format_doc(doc, I1))
    # Emit enum members
    if cls:
        for member_name in ("ADDING", "ACTIVE", "REMOVED"):
            lines.append(f"{I1}{member_name}: ClassVar[PipeStatus]")
    else:
        lines.append(f"{I1}ADDING: ClassVar[PipeStatus]")
        lines.append(f"{I1}ACTIVE: ClassVar[PipeStatus]")
        lines.append(f"{I1}REMOVED: ClassVar[PipeStatus]")
    lines.append("")
    return lines


# ============================================================================
# Ordered class list
# ============================================================================

# Classes emitted in dependency order (referenced types appear before use)
_CLASS_ORDER = [
    "Message",
    "TlsCert",
    "TlsConfig",
    "SocketAddr",
    "Pipe",
    "Dialer",
    "Listener",
    "Context",
    "ReqContext",
    "SubContext",
    "SurveyorContext",
    "Socket",
    "PairSocket",
    "PubSocket",
    "SubSocket",
    "ReqSocket",
    "RepSocket",
    "PushSocket",
    "PullSocket",
    "SurveyorSocket",
    "RespondentSocket",
    "BusSocket",
]


# ============================================================================
# File header
# ============================================================================

_HEADER = '''\
# This file is auto-generated by stubs/generate_stubs.py – do not edit manually.
# Re-run the script after changing any .pxi / .pyx source.

import concurrent.futures
import datetime
import sys
from collections.abc import AsyncGenerator, Callable
from typing import Any, ClassVar, Self

if sys.version_info >= (3, 12):
    from collections.abc import Buffer
else:
    from typing import TypeAlias
    Buffer: TypeAlias = memoryview

'''


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    output_parts: list[str] = [_HEADER]

    # Exceptions
    output_parts.append("# ---------------------------------------------------------------------------\n")
    output_parts.append("# Exceptions\n")
    output_parts.append("# ---------------------------------------------------------------------------\n\n")
    output_parts.append("\n".join(emit_exceptions()))

    # PipeStatus
    output_parts.append("# ---------------------------------------------------------------------------\n")
    output_parts.append("# Pipe lifecycle enum\n")
    output_parts.append("# ---------------------------------------------------------------------------\n\n")
    output_parts.append("\n".join(emit_pipe_status()))
    output_parts.append("\n")

    # Classes
    output_parts.append("# ---------------------------------------------------------------------------\n")
    output_parts.append("# Core types\n")
    output_parts.append("# ---------------------------------------------------------------------------\n\n")
    for cls_name in _CLASS_ORDER:
        cls = getattr(_mod, cls_name, None)
        if cls is None:
            print(f"WARNING: class {cls_name!r} not found in nng._nng – skipping.")
            continue
        cls_lines = emit_class(cls)
        output_parts.append("\n".join(cls_lines))
        output_parts.append("\n\n")

    # Constants
    output_parts.append("# ---------------------------------------------------------------------------\n")
    output_parts.append("# Module-level constants\n")
    output_parts.append("# ---------------------------------------------------------------------------\n\n")
    output_parts.append("\n".join(emit_module_constants()))
    output_parts.append("\n")

    # Functions
    output_parts.append("# ---------------------------------------------------------------------------\n")
    output_parts.append("# Module-level functions\n")
    output_parts.append("# ---------------------------------------------------------------------------\n\n")
    output_parts.append("\n".join(emit_module_functions()))

    content = "".join(output_parts)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Wrote {OUT_PATH} ({len(content)} bytes, {content.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
