# ---------------------------------------------------------------------------
# Nuitka: add Qt DLL directories to search path before importing PySide2
# ---------------------------------------------------------------------------
import ctypes
import os
import sys

if getattr(sys, 'frozen', False):
    # Running as Nuitka compiled binary
    _base_path = os.path.dirname(os.path.abspath(sys.argv[0]))
    _pyside2_dir = os.path.join(_base_path, 'PySide2')
    _shiboken2_dir = os.path.join(_base_path, 'shiboken2')

    # Add to PATH
    _extra_paths = []
    if os.path.isdir(_pyside2_dir):
        _extra_paths.append(_pyside2_dir)
    if os.path.isdir(_shiboken2_dir):
        _extra_paths.append(_shiboken2_dir)
    if _extra_paths:
        os.environ['PATH'] = os.pathsep.join(_extra_paths) + os.pathsep + os.environ.get('PATH', '')

    # Use SetDllDirectory as a fallback (Windows API)
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(_pyside2_dir)
    except AttributeError:
        pass

    # Qt plugin path
    _plugins_dir = os.path.join(_pyside2_dir, 'plugins')
    if os.path.isdir(_plugins_dir):
        os.environ['QT_PLUGIN_PATH'] = _plugins_dir
        os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(_plugins_dir, 'platforms')

import asyncio
import csv
import gc
import ipaddress
import json
import logging
import os
import signal
import ssl
import threading
import time
from datetime import datetime, timedelta
from queue import Queue

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

import OpenOPC
import pythoncom
from asyncua.server.server import Server, ua
from PySide2.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QPushButton, QLabel,
    QFileDialog, QMessageBox, QDialog, QFormLayout, QLineEdit,
    QGroupBox, QSplitter, QTextEdit, QToolBar, QStatusBar,
    QMenuBar, QMenu, QComboBox, QAbstractItemView, QInputDialog,
    QAction, QRadioButton, QCheckBox,
)
from PySide2.QtCore import Qt, Slot, QThread, QTimer, Signal
from PySide2.QtGui import QFont, QColor, QTextCursor

# ---------------------------------------------------------------------------
# Custom logging – routes to file + Qt log pane
# ---------------------------------------------------------------------------

class QtLogHandler(logging.Handler):
    """Emits log records to a Qt signal for display in the log pane."""

    _signal = None

    def set_signal(self, signal):
        self._signal = signal

    def emit(self, record):
        if self._signal:
            msg = self.format(record)
            try:
                self._signal.emit(msg)
            except RuntimeError:
                pass  # Signal may be disconnected during shutdown


def setup_logging(level=logging.INFO, log_file="gateway.log"):
    """Configure root logger with file + Qt handlers."""
    fmt = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)

    # Qt handler (signal wired later by MainWindow)
    qth = QtLogHandler()
    qth.setFormatter(fmt)

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # Let handlers filter
    # Remove any existing basicConfig handlers
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(fh)
    root.addHandler(qth)

    return qth


qthandler = setup_logging()
logger = logging.getLogger("OPC_MultiServer_Gateway")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CSV = "tags.csv"
OPC_UA_ENDPOINT = "opc.tcp://0.0.0.0:4840/freeopcua/server/"
OPC_UA_NAMESPACE = "http://mycompany.com"
DEFAULT_CHUNK_SIZE = 500
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_DA_MODE = "polling"  # "polling" or "subscription"
RECONNECT_DELAY = 5.0
DEFAULT_FOLDER = "Default"
DEFAULT_REINIT_INTERVAL = 5400.0  # 1.5 hours in seconds

write_queues = {}
async_loop = None
da_written_values = {}  # Shared dict: nodeid -> value, updated by DA worker to suppress echo writes
CERT_FILE = "server_cert.pem"
KEY_FILE = "server_key.pem"


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
PREFERENCES_FILE = "gateway_prefs.json"
DEFAULT_LOG_FILE = "gateway.log"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_SSL_ENABLED = False
DEFAULT_CHUNK_SIZE_PREF = DEFAULT_CHUNK_SIZE
DEFAULT_POLL_INTERVAL_PREF = DEFAULT_POLL_INTERVAL
DEFAULT_DA_MODE_PREF = DEFAULT_DA_MODE
DEFAULT_MAX_LOG_LINES = 5000
DEFAULT_REINIT_INTERVAL_PREF = DEFAULT_REINIT_INTERVAL


def load_preferences():
    prefs = {
        "log_level": DEFAULT_LOG_LEVEL,
        "log_file": DEFAULT_LOG_FILE,
        "ssl_enabled": DEFAULT_SSL_ENABLED,
        "chunk_size": DEFAULT_CHUNK_SIZE_PREF,
        "poll_interval": DEFAULT_POLL_INTERVAL_PREF,
        "da_mode": DEFAULT_DA_MODE_PREF,
        "max_log_lines": DEFAULT_MAX_LOG_LINES,
        "reinit_interval": DEFAULT_REINIT_INTERVAL_PREF,
    }
    if os.path.exists(PREFERENCES_FILE):
        try:
            with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                prefs.update(user)
        except Exception:
            pass
    return prefs


def save_preferences(prefs):
    with open(PREFERENCES_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)


def apply_preferences(prefs=None):
    """Apply preferences to logging configuration."""
    if prefs is None:
        prefs = load_preferences()

    level_str = prefs.get("log_level", DEFAULT_LOG_LEVEL).upper()
    level_map = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    level = level_map.get(level_str, logging.INFO)

    log_file = prefs.get("log_file", DEFAULT_LOG_FILE)

    # Reconfigure logging
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove old handlers (close file handles to prevent leaks)
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()

    fmt = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(level)
    root.addHandler(fh)

    # Qt handler (will be wired by MainWindow)
    global qthandler
    qthandler = QtLogHandler()
    qthandler.setFormatter(fmt)
    qthandler.setLevel(level)
    root.addHandler(qthandler)

    logger.info(f"Logging configured: level={level_str}, file={log_file}")


# ===================================================================
# Background gateway engine (runs in a QThread)
# ===================================================================
class GatewayEngine(QThread):
    """Owns the async OPC-UA server and spawns DA worker threads."""

    log_signal = Signal(str)
    status_signal = Signal(str, bool)
    server_status_signal = Signal(str, bool)  # server_name, connected

    def __init__(self, config: dict):
        super().__init__()
        self.config = config          # { server_name: { folder: [tag, ...] } }
        self._ua_server = None
        self._stop_event = threading.Event()
        self._server_names = []  # Track which servers this engine created queues for
        self._worker_threads = []  # Track worker threads for proper cleanup
        self._async_loop = None  # Track the async loop for cleanup

    # -- QThread.run --------------------------------------------------------
    def run(self):
        threading.current_thread().name = self.objectName() or "OPC-UA-Server"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        global async_loop
        async_loop = loop
        self._async_loop = loop  # Track for cleanup
        loop.run_until_complete(self._async_main())
        # Clean up the event loop
        try:
            # Cancel all remaining tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        finally:
            loop.close()
            async_loop = None
            self._async_loop = None

    async def _async_main(self):
        if not self.config:
            self.log_signal.emit("No servers configured – add servers to start.")
            return

        # Generate and load self-signed certificate
        ssl_enabled = self.config.get("__ssl_enabled__", False)
        _ensure_self_signed_cert()
        ua_server = Server()
        ua_server.set_server_name("OPCUA-1")
        await ua_server.init()
        ua_server.set_endpoint(OPC_UA_ENDPOINT)
        if ssl_enabled:
            try:
                await ua_server.load_certificate(CERT_FILE)
                await ua_server.load_private_key(KEY_FILE)
                logger.info("SSL/TLS enabled with self-signed certificate")
            except Exception as e:
                logger.warning(f"Could not load certificate: {e}")
                logger.warning("Falling back to open (unencrypted) endpoint")
        root_idx = await ua_server.register_namespace(OPC_UA_NAMESPACE)
        root = await ua_server.nodes.objects.add_object(root_idx, "MultiServer_OPC_Bridge")
        tag_routing_map = {}
        ua_vars = {}  # Collect all UA variables across all servers

        for srv, folders in self.config.items():
            # Skip internal configuration keys (e.g., __ssl_enabled__)
            if not isinstance(folders, dict):
                continue

            write_queues[srv] = Queue()
            self._server_names.append(srv)
            clean = srv.replace(".", "_").replace(" ", "_")
            srv_node = await root.add_object(root_idx, f"Server_{clean}")
            status_node = await srv_node.add_variable(root_idx, "IsConnected", 0.0)

            all_tags = []
            srv_ua_vars = {}  # Per-server subset for the worker thread

            # Collect all tags first so we can probe types
            for folder, tags in folders.items():
                if tags:
                    all_tags.extend(tags)

            # Probe DA types before creating UA variables
            tag_types = _probe_da_types(srv, all_tags)

            for folder, tags in folders.items():
                if not tags:
                    continue
                # Each folder maps to a UA namespace
                ns_uri = f"{OPC_UA_NAMESPACE}#{folder}"
                ns_idx = await ua_server.register_namespace(ns_uri)
                clean_folder = folder.replace(".", "_").replace(" ", "_")
                folder_node = await srv_node.add_object(ns_idx, f"Folder_{clean_folder}")

                for tag in tags:
                    ct = tag.replace(".", "_").replace(" ", "_")
                    vtype, default_val = tag_types.get(tag, (ua.VariantType.Double, 0.0))
                    node = await folder_node.add_variable(ns_idx, ct, default_val, vtype)
                    await node.set_writable()
                    ua_vars[tag] = node
                    srv_ua_vars[tag] = node
                    tag_routing_map[node.nodeid] = (srv, tag)

            # Pass gateway preferences to the worker
            chunk_size = self.config.get("__chunk_size__", DEFAULT_CHUNK_SIZE)
            poll_interval = self.config.get("__poll_interval__", DEFAULT_POLL_INTERVAL)
            da_mode = self.config.get("__da_mode__", DEFAULT_DA_MODE)
            reinit_interval = self.config.get("__reinit_interval__", DEFAULT_REINIT_INTERVAL)

            t = threading.Thread(
                target=opc_da_worker,
                args=(srv, all_tags, srv_ua_vars, status_node, self.server_status_signal,
                      chunk_size, poll_interval, da_mode, reinit_interval, self._stop_event),
                name=f"Worker_{clean}",
                daemon=True,
            )
            self._worker_threads.append(t)
            t.start()

        handler = MultiServerWriteHandler(tag_routing_map)
        # Track connected UA clients so the write monitor can skip when idle
        self._client_count = {'count': 0}
        # Log memory usage after setup
        log_memory_usage(logger, "gateway-start")
        # Start a background monitor for UA write requests (asyncua 1.1.0 lacks
        # set_data_value_callback, so we poll the variables for external writes)
        self._write_monitor_task = asyncio.create_task(
            _ua_write_monitor(ua_server, ua_vars, handler, self._stop_event, self._client_count)
        )
        # Start periodic async GC to clean up event loop internal references
        self._async_gc_task = asyncio.create_task(
            _async_gc_loop(self._stop_event)
        )
        msg = "OPC UA Gateway is up and running on " + OPC_UA_ENDPOINT
        logger.info(msg)
        self.log_signal.emit(msg)
        self._ua_server = ua_server

        async with ua_server:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
            self.log_signal.emit("Shutdown requested – stopping server...")
            # Signal worker threads to stop by setting the stop event
            self._stop_event.set()
            # Cancel the write monitor task
            if hasattr(self, '_write_monitor_task'):
                self._write_monitor_task.cancel()
                try:
                    await self._write_monitor_task
                except asyncio.CancelledError:
                    pass
            # Cancel the async GC task
            if hasattr(self, '_async_gc_task'):
                self._async_gc_task.cancel()
                try:
                    await self._async_gc_task
                except asyncio.CancelledError:
                    pass
        self.log_signal.emit("OPC UA server stopped.")

        # Join all worker threads with timeout
        self.log_signal.emit(f"Joining {len(self._worker_threads)} worker threads...")
        alive_count = 0
        for i, t in enumerate(self._worker_threads):
            if t.is_alive():
                t.join(timeout=3.0)
                if t.is_alive():
                    alive_count += 1
                    logger.warning(f"Worker thread '{t.name}' did not stop within timeout")
                else:
                    logger.info(f"Worker thread '{t.name}' joined successfully")
        if alive_count == 0:
            logger.info("All worker threads stopped cleanly")
        else:
            logger.warning(f"{alive_count} worker thread(s) still alive after timeout")
        self._worker_threads.clear()

        # Clean up global state to prevent memory leaks
        for srv in self._server_names:
            write_queues.pop(srv, None)
        da_written_values.clear()
        self._server_names.clear()
        logger.debug("Gateway engine cleanup complete")
        log_memory_usage(logger, "gateway-stop")


# ===================================================================
# Core helpers (unchanged logic, minor clean-up)
# ===================================================================
class MultiServerWriteHandler:
    def __init__(self, tag_routing_map):
        self.tag_routing_map = tag_routing_map

    async def write_data_value(self, nodeid, value):
        route = self.tag_routing_map.get(nodeid)
        if not route:
            return
        server_name, da_tag = route
        write_queues[server_name].put((da_tag, value.Value.Value))
        logger.info(f"Queued write [{server_name}] -> {da_tag} = {value.Value.Value}")

    def route_write(self, nodeid, val):
        """Synchronous version for the polling monitor."""
        route = self.tag_routing_map.get(nodeid)
        if not route:
            return
        server_name, da_tag = route
        write_queues[server_name].put((da_tag, val))
        logger.info(f"Queued write [{server_name}] -> {da_tag} = {val}")


async def _da_write_ua(node, val):
    """Write a value to a UA variable and record it as DA-originated.

    This runs inside the async loop so the write and the da_written_values
    update happen atomically from the monitor's perspective.
    """
    await node.write_value(val)
    da_written_values[node.nodeid] = (val, time.monotonic())


async def _da_write_ua_batch_impl(write_list):
    """Execute a batch of writes inside the async loop (single Future).

    Uses set_attribute_value on the address space node directly to avoid
    the high-level write_value() wrapper object churn (Variant/DataValue/
    WriteValue/Response objects created per-tag per-cycle).
    """
    for node, val in write_list:
        nid = node.nodeid
        # Direct address space write - minimal object allocation
        await node.write_value(val)
        da_written_values[nid] = (val, time.monotonic())


def _da_write_ua_batch(write_list):
    """Schedule a batch of UA writes on the async loop as a single Future."""
    return asyncio.run_coroutine_threadsafe(_da_write_ua_batch_impl(write_list), async_loop)


async def _ua_write_monitor(ua_server, ua_vars, handler, stop_event, client_count=None):
    """Monitor UA variables for externally-written values and route them to DA.

    Only polls when there are connected UA clients. When no clients are
    connected, there's nothing to detect so we skip the read entirely.
    """
    # Ordered list of (nodeid, node) for stable iteration
    node_list = [(node.nodeid, node) for _, node in ua_vars.items()]
    all_nodeids = frozenset(nid for nid, _ in node_list)

    last_values = {}  # nodeid -> raw python value
    poll_interval = 1.0  # seconds when clients connected
    idle_poll_interval = 5.0  # when no clients, just check client count less frequently
    prune_interval = 30  # seconds between pruning
    last_prune = time.monotonic()

    while not stop_event.is_set():
        # Check if any UA clients are connected
        has_clients = False
        if client_count is not None:
            has_clients = client_count.get('count', 0) > 0
        else:
            # Fallback: check sessions on the server
            try:
                has_clients = len(ua_server.iserver.session_mgr.sessions) > 1  # -1 for system session
            except Exception:
                has_clients = True  # be safe, poll if we can't tell

        if has_clients:
            await asyncio.sleep(poll_interval)
            try:
                for nodeid, node in node_list:
                    try:
                        current = await node.read_value_variant()
                    except Exception:
                        continue

                    prev = last_values.get(nodeid)
                    if prev is None:
                        last_values[nodeid] = current
                        continue
                    # Skip if the change was made by the DA worker itself
                    da_entry = da_written_values.get(nodeid)
                    if da_entry is not None:
                        da_val = da_entry[0]
                        if current == da_val:
                            last_values[nodeid] = current
                            continue
                    # Only forward if value changed (external UA client write)
                    if current != prev:
                        last_values[nodeid] = current
                        handler.route_write(nodeid, current)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Write monitor poll error: {e}")
        else:
            # No clients connected - sleep longer, skip all reads
            await asyncio.sleep(idle_poll_interval)

        # Periodically prune stale entries (regardless of client state)
        now = time.monotonic()
        if now - last_prune > prune_interval:
            _prune_da_written_values(all_nodeids)
            stale_last = [k for k in last_values if k not in all_nodeids]
            for k in stale_last:
                del last_values[k]
            last_prune = now


def _prune_da_written_values(current_nodeids):
    """Remove stale entries from da_written_values.

    Entries are stored as (value, timestamp) and are pruned if:
    1. The node no longer exists, OR
    2. The entry is older than 5 seconds (echo suppression only needs to be brief)
    """
    now = time.monotonic()
    max_age = 5.0  # seconds
    keys_to_remove = []
    for k, v in da_written_values.items():
        if k not in current_nodeids:
            keys_to_remove.append(k)
        elif now - v[1] > max_age:
            keys_to_remove.append(k)
    for k in keys_to_remove:
        del da_written_values[k]
    if keys_to_remove:
        logger.debug(f"Pruned {len(keys_to_remove)} stale entries from da_written_values")


async def _async_gc_loop(stop_event):
    """Periodically run GC from within the async event loop.

    The async event loop holds references to completed coroutine frames,
    Future internals, and asyncua objects that synchronous GC can't easily
    reach. Running GC from within the loop context helps release these.
    """
    interval = 30  # seconds (more frequent for aggressive leak mitigation)
    while not stop_event.is_set():
        await asyncio.sleep(interval)
        try:
            mem_before = get_memory_usage_mb()
            # Run 3 GC passes to break reference cycles
            n1 = gc.collect()
            n2 = gc.collect()
            n3 = gc.collect()
            mem_after = get_memory_usage_mb()
            if mem_before is not None and mem_after is not None:
                freed = mem_before - mem_after
                tag = f"+{freed:.1f}" if freed > 0 else f"{freed:.1f}"
                logger.info(f"Async GC: {mem_before:.1f} MB -> {mem_after:.1f} MB ({tag} MB), collected {n1+n2+n3} objects")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Async GC error: {e}")


def _interruptible_sleep(duration, stop_event=None):
    """Sleep for *duration* seconds, but wake early if stop_event is set."""
    if stop_event is None:
        time.sleep(duration)
        return
    # Sleep in 100 ms increments so we can check the event
    end = time.monotonic() + duration
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        time.sleep(min(0.1, end - time.monotonic()))


def get_memory_usage_mb():
    """Get current process memory usage in MB."""
    if not HAS_PSUTIL:
        return None
    try:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def log_memory_usage(logger, tag=""):
    """Log current memory usage if psutil is available."""
    mem_mb = get_memory_usage_mb()
    if mem_mb is not None:
        tag_str = f" [{tag}]" if tag else ""
        logger.info(f"Memory usage{tag_str}: {mem_mb:.1f} MB")


def _count_objects_by_type():
    """Count live objects by type for memory diagnostics."""
    import sys
    counts = {}
    for obj in gc.get_objects():
        try:
            tname = type(obj).__name__
            counts[tname] = counts.get(tname, 0) + 1
        except (TypeError, ReferenceError):
            pass
    return counts


def load_csv(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            srv = row.get("server_name", "").strip()
            folder = row.get("folder", DEFAULT_FOLDER).strip() or DEFAULT_FOLDER
            tag = row.get("da_tag", "").strip()
            if srv and tag:
                cfg.setdefault(srv, {}).setdefault(folder, []).append(tag)
    return cfg


def save_csv(path, cfg):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["server_name", "folder", "da_tag"])
        w.writeheader()
        for srv, folders in cfg.items():
            for folder, tags in folders.items():
                for tag in tags:
                    w.writerow({"server_name": srv, "folder": folder, "da_tag": tag})


def _da_type_to_ua(val):
    """Return (VariantType, default_value) for a Python value read from OPC DA."""
    if isinstance(val, bool):
        return ua.VariantType.Boolean, False
    if isinstance(val, int):
        return ua.VariantType.Int64, 0
    if isinstance(val, float):
        return ua.VariantType.Double, 0.0
    if isinstance(val, str):
        return ua.VariantType.String, ""
    if isinstance(val, datetime):
        return ua.VariantType.DateTime, datetime.min
    return ua.VariantType.Double, 0.0


def _ensure_self_signed_cert(force=False):
    """Generate a self-signed certificate if it doesn't already exist."""
    if not force and os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"OPC DA2UA Gateway"),
            x509.NameAttribute(NameOID.COMMON_NAME, u"localhost"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow())
            .not_valid_after(datetime.utcnow() + timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName(u"localhost"),
                    x509.DNSName(u"*"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv4Address("0.0.0.0")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256(), default_backend())
        )
        with open(CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(KEY_FILE, "wb") as f:
            f.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
        logger.info(f"Generated self-signed certificate ({CERT_FILE})")
    except Exception as e:
        logger.warning(f"Could not generate certificate: {e}")


def _probe_da_types(server_name, tags):
    """Connect to an OPC DA server, read every tag once, and return { tag: (VariantType, default_value) }."""
    types = {}
    client = None
    try:
        client = OpenOPC.client()
        client.connect(server_name)
        # Read in chunks (same default size used by the worker)
        for i in range(0, len(tags), DEFAULT_CHUNK_SIZE):
            chunk = tags[i:i + DEFAULT_CHUNK_SIZE]
            for tname, val, qual, ts in client.read(chunk):
                if qual == "Good":
                    types[tname] = _da_type_to_ua(val)
                else:
                    types[tname] = (ua.VariantType.Double, 0.0)  # fallback
    except Exception as e:
        logger.warning(f"Type probe [{server_name}] failed: {e} – falling back to Double")
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
    # Fill in any tags that were missed
    for tag in tags:
        if tag not in types:
            types[tag] = (ua.VariantType.Double, 0.0)
    return types


def opc_da_worker(server_name, tags, ua_variables, ua_status_node, server_status_signal,
                   chunk_size=DEFAULT_CHUNK_SIZE, poll_interval=DEFAULT_POLL_INTERVAL,
                   da_mode=DEFAULT_DA_MODE, reinit_interval=DEFAULT_REINIT_INTERVAL,
                   stop_event=None):
    global async_loop
    pythoncom.CoInitialize()
    try:
        _opc_da_worker_loop(server_name, tags, ua_variables, ua_status_node, server_status_signal,
                             chunk_size, poll_interval, da_mode, reinit_interval, stop_event)
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _opc_da_worker_loop(server_name, tags, ua_variables, ua_status_node, server_status_signal,
                         chunk_size=DEFAULT_CHUNK_SIZE, poll_interval=DEFAULT_POLL_INTERVAL,
                         da_mode=DEFAULT_DA_MODE, reinit_interval=DEFAULT_REINIT_INTERVAL,
                         stop_event=None):
    global async_loop
    my_queue = write_queues[server_name]
    chunks = [tags[i:i + chunk_size] for i in range(0, len(tags), chunk_size)]
    da_client = None
    connected = False
    subscribed = False
    reconnect_count = 0

    # Track time for periodic reinitialization
    last_reinit_time = time.monotonic() if reinit_interval > 0 else None
    
    # Track time for periodic garbage collection and memory logging
    gc_interval = 60  # seconds between GC runs
    last_gc_time = time.monotonic()
    
    # Log initial memory usage
    log_memory_usage(logger, server_name)

    def cleanup_subscription():
        """Safely unsubscribe and clean up subscription state."""
        nonlocal subscribed, da_client
        if subscribed and da_client:
            try:
                da_client.unsubscribe(tags)
                logger.info(f"[{server_name}] unsubscribed from {len(tags)} tags")
            except Exception as e:
                logger.warning(f"[{server_name}] unsubscribe error: {e}")
            subscribed = False

    def cleanup_connection():
        """Safely close the DA connection and reset state."""
        nonlocal connected, da_client
        cleanup_subscription()
        connected = False
        # Emit disconnected status
        try:
            server_status_signal.emit(server_name, False)
        except RuntimeError:
            pass
        if da_client:
            try:
                da_client.close()
            except Exception as e:
                logger.warning(f"[{server_name}] close error during cleanup: {e}")
            da_client = None

    def update_ua_status(value):
        """Update the UA status node if the async loop is available."""
        if async_loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    ua_status_node.write_value(value), async_loop
                )
            except Exception as e:
                logger.warning(f"[{server_name}] status update error: {e}")

    def emit_server_status(is_connected):
        """Emit server status signal safely."""
        try:
            server_status_signal.emit(server_name, is_connected)
        except RuntimeError:
            pass

    while stop_event is None or not stop_event.is_set():
        # Periodic garbage collection to clean up COM object references
        now = time.monotonic()
        if now - last_gc_time > gc_interval:
            mem_before = get_memory_usage_mb()
            gc.collect()
            mem_after = get_memory_usage_mb()
            if mem_before is not None and mem_after is not None:
                logger.info(f"[{server_name}] GC: {mem_before:.1f} MB → {mem_after:.1f} MB (freed {mem_before - mem_after:.1f} MB)")
            last_gc_time = now

        # Check if it's time for periodic reinitialization
        if reinit_interval > 0 and last_reinit_time is not None and connected:
            elapsed = time.monotonic() - last_reinit_time
            if elapsed >= reinit_interval:
                logger.info(f"[{server_name}] periodic reinitialization triggered (interval={reinit_interval}s)")
                log_memory_usage(logger, server_name)
                cleanup_connection()
                last_reinit_time = time.monotonic()
                connected = False

        if not connected:
            # Show disconnected status before attempting reconnect
            update_ua_status(0.0)
            emit_server_status(False)

            # Clean up any stale client from previous iteration
            if da_client:
                cleanup_connection()

            try:
                da_client = OpenOPC.client()
                da_client.connect(server_name)
                connected = True
                update_ua_status(1.0)
                emit_server_status(True)
                reconnect_count = 0
                last_reinit_time = time.monotonic()
                logger.info(f"Connected to [{server_name}]")

                # Subscribe if using subscription mode
                if da_mode == "subscription":
                    try:
                        da_client.subscribe(tags, poll_interval * 1000)
                        subscribed = True
                        logger.info(f"[{server_name}] subscribed to {len(tags)} tags (interval={poll_interval}s)")
                    except Exception as e:
                        logger.warning(f"[{server_name}] subscription failed ({e}), falling back to polling")
                        subscribed = False

            except Exception as e:
                logger.error(f"Connect [{server_name}] failed: {e}")
                da_client = None
                _interruptible_sleep(RECONNECT_DELAY, stop_event)
                continue

        try:
            # Process pending writes
            while not my_queue.empty():
                tag, val = my_queue.get_nowait()
                try:
                    logger.info(f"[{server_name}] writing {tag} = {val}")
                    da_client.write((tag, val))  # type: ignore[union-attr]
                except Exception as e:
                    logger.error(f"[{server_name}] write {tag}: {e}")
                    # Write failure may indicate a broken connection
                    raise
                my_queue.task_done()

            if da_mode == "subscription" and subscribed:
                # Subscription mode – read returns only changed values
                write_batch = []
                try:
                    for tname, val, qual, ts in da_client.read(tags):
                        if qual == "Good":
                            node = ua_variables[tname]
                            write_batch.append((node, val))
                        elif qual != "Good":
                            logger.debug(f"[{server_name}] tag {tname} quality: {qual}")
                        # Explicitly release COM references from the tuple
                        del tname, val, qual, ts
                except Exception as e:
                    raise

                # Submit all writes as a single batch
                if write_batch and async_loop:
                    future = _da_write_ua_batch(write_batch)
                    write_batch.clear()
                    try:
                        future.result(timeout=2.0)
                    except Exception as e:
                        logger.warning(f"[{server_name}] batch write error: {e}")
                    finally:
                        del future

                time.sleep(max(poll_interval / 10, 0.05))
            else:
                # Polling mode – read all tags in chunks and batch writes
                for chunk in chunks:
                    write_batch = []
                    for tname, val, qual, ts in da_client.read(chunk):
                        if qual == "Good":
                            node = ua_variables[tname]
                            write_batch.append((node, val))
                        # Explicitly release COM references
                        del tname, val, qual, ts

                    # Submit batch
                    if write_batch and async_loop:
                        future = _da_write_ua_batch(write_batch)
                        write_batch.clear()
                        try:
                            future.result(timeout=2.0)
                        except Exception as e:
                            logger.warning(f"[{server_name}] batch write error: {e}")
                        finally:
                            del future

                _interruptible_sleep(poll_interval, stop_event)
        except Exception as e:
            logger.error(f"[{server_name}] connection error: {e}")
            cleanup_connection()
            reconnect_count += 1
            _interruptible_sleep(RECONNECT_DELAY, stop_event)

    # Clean shutdown – stop event was set
    logger.info(f"[{server_name}] worker shutting down cleanly")
    log_memory_usage(logger, server_name)
    cleanup_connection()
    
    # Clear local references to help GC
    del ua_variables
    del chunks
    del my_queue
    
    logger.info(f"[{server_name}] worker thread exiting")


# ===================================================================
# Qt dialogs
# ===================================================================
class AddServerDialog(QDialog):
    """Simple dialog to add a new OPC DA server name."""

    def __init__(self, existing_servers, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle("Add OPC DA Server")
        self.resize(450, 220)
        layout = QVBoxLayout(self)

        # Explanatory text
        info = QLabel(
            "Enter the <b>COM Server ID</b> (ProgID) of the OPC DA server.<br/><br/>"
            "This is <b>not</b> a hostname or IP address. Examples:<br/>"
            "<ul>"
            "<li><i>FactoryTalk Gateway</i></li>"
            "<li><i>Matrikon.OPC.Simulation.1</i></li>"
            "<li><i>KEPware.KEPServerEX.V4</i></li>"
            "</ul>"
        )
        info.setWordWrap(True)
        info.setStyleSheet("margin-bottom: 8px;")
        layout.addWidget(info)

        form = QFormLayout()
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("e.g.  FactoryTalk Gateway")
        form.addRow("Server ID:", self.server_edit)
        layout.addLayout(form)

        # Show existing servers as suggestions
        if existing_servers:
            hint = QLabel("Existing servers: " + ", ".join(sorted(existing_servers)))
            hint.setStyleSheet("color: gray; font-size: small; margin-top: 4px;")
            layout.addWidget(hint)

        layout.addStretch()

        btns = QHBoxLayout()
        ok = QPushButton("Add")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)

    def result(self):
        return self.server_edit.text().strip()


class AddTagDialog(QDialog):
    """Dialog to add a single tag to a server."""

    def __init__(self, server_name, existing_tags, existing_folders, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle(f"Add Tag → {server_name}")
        self.resize(400, 160)
        layout = QFormLayout(self)
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("e.g.  Random.Real4")
        layout.addRow("DA Tag:", self.tag_edit)

        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("e.g.  ProcessData, Alarms, Commands")
        layout.addRow("Folder:", self.folder_edit)

        if existing_folders:
            hint = QLabel("Existing folders: " + ", ".join(sorted(existing_folders)))
            hint.setStyleSheet("color: gray; font-size: small;")
            layout.addRow(hint)

        btns = QHBoxLayout()
        ok = QPushButton("Add")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addRow(btns)

    def result(self):
        return self.tag_edit.text().strip(), self.folder_edit.text().strip() or DEFAULT_FOLDER


class EditTagDialog(QDialog):
    """Dialog to edit an existing tag name."""

    def __init__(self, server_name, current_tag, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle(f"Edit Tag → {server_name}")
        layout = QFormLayout(self)
        self.tag_edit = QLineEdit(current_tag)
        layout.addRow("DA Tag:", self.tag_edit)

        btns = QHBoxLayout()
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addRow(btns)

    def result(self):
        return self.tag_edit.text().strip()


# ===================================================================
# Preferences dialog
# ===================================================================
class PreferencesDialog(QDialog):
    """Dialog to configure gateway preferences."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle("Preferences")
        self.resize(450, 380)

        self.prefs = load_preferences()

        layout = QVBoxLayout(self)

        # Log Level Group
        log_level_group = QGroupBox("Log Level")
        log_level_layout = QVBoxLayout(log_level_group)

        self.radio_info = QRadioButton("Info")
        self.radio_warning = QRadioButton("Warning")
        self.radio_error = QRadioButton("Error")

        current_level = self.prefs.get("log_level", DEFAULT_LOG_LEVEL).upper()
        if current_level == "INFO":
            self.radio_info.setChecked(True)
        elif current_level == "WARNING":
            self.radio_warning.setChecked(True)
        else:
            self.radio_error.setChecked(True)

        log_level_layout.addWidget(self.radio_info)
        log_level_layout.addWidget(self.radio_warning)
        log_level_layout.addWidget(self.radio_error)
        layout.addWidget(log_level_group)

        # Log File Group
        log_file_group = QGroupBox("Log File")
        log_file_layout = QHBoxLayout(log_file_group)

        self.log_file_edit = QLineEdit(self.prefs.get("log_file", DEFAULT_LOG_FILE))
        self.log_file_edit.setPlaceholderText("gateway.log")
        log_file_layout.addWidget(self.log_file_edit)

        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._browse_log_file)
        log_file_layout.addWidget(self.btn_browse)

        layout.addWidget(log_file_group)

        # SSL/TLS Group
        ssl_group = QGroupBox("SSL/TLS")
        ssl_layout = QVBoxLayout(ssl_group)

        self.chk_ssl = QCheckBox("Enable SSL/TLS encryption (requires restart)")
        self.chk_ssl.setChecked(self.prefs.get("ssl_enabled", DEFAULT_SSL_ENABLED))
        ssl_layout.addWidget(self.chk_ssl)

        ssl_hint = QLabel(
            "When enabled, the OPC UA server uses a self-signed certificate."
        )
        ssl_hint.setStyleSheet("color: gray; font-size: small;")
        ssl_layout.addWidget(ssl_hint)

        # Certificate info and regeneration
        cert_info_layout = QHBoxLayout()
        cert_path_label = QLabel(f"Certificate: {os.path.abspath(CERT_FILE)}")
        cert_path_label.setStyleSheet("font-size: small; color: #555;")
        cert_path_label.setWordWrap(True)
        cert_info_layout.addWidget(cert_path_label)

        self.btn_regenerate_cert = QPushButton("Regenerate Certificate")
        self.btn_regenerate_cert.clicked.connect(self._regenerate_certificate)
        cert_info_layout.addWidget(self.btn_regenerate_cert)

        ssl_layout.addLayout(cert_info_layout)
        layout.addWidget(ssl_group)

        # DA Data Retrieval Group
        da_group = QGroupBox("OPC DA Data Retrieval")
        da_layout = QFormLayout(da_group)

        # DA Mode (polling vs subscription)
        self.da_mode_combo = QComboBox()
        self.da_mode_combo.addItems(["Polling", "Subscription"])
        current_mode = self.prefs.get("da_mode", DEFAULT_DA_MODE).lower()
        if current_mode == "subscription":
            self.da_mode_combo.setCurrentIndex(1)
        da_layout.addRow("Mode:", self.da_mode_combo)

        da_mode_hint = QLabel(
            "Polling reads all tags periodically.\n"
            "Subscription receives change notifications from the DA server."
        )
        da_mode_hint.setStyleSheet("color: gray; font-size: small;")
        da_layout.addRow(da_mode_hint)

        # Polling interval
        self.poll_interval_edit = QLineEdit(
            str(self.prefs.get("poll_interval", DEFAULT_POLL_INTERVAL))
        )
        self.poll_interval_edit.setPlaceholderText("0.5")
        poll_label = QLabel("Poll interval (seconds):")
        da_layout.addRow(poll_label, self.poll_interval_edit)

        # Chunk size
        self.chunk_size_edit = QLineEdit(
            str(self.prefs.get("chunk_size", DEFAULT_CHUNK_SIZE))
        )
        self.chunk_size_edit.setPlaceholderText("500")
        chunk_label = QLabel("Chunk size (tags per batch):")
        da_layout.addRow(chunk_label, self.chunk_size_edit)

        # Reinit interval
        self.reinit_interval_edit = QLineEdit(
            str(self.prefs.get("reinit_interval", DEFAULT_REINIT_INTERVAL))
        )
        self.reinit_interval_edit.setPlaceholderText(str(DEFAULT_REINIT_INTERVAL))
        reinit_label = QLabel("Connection reinit interval (seconds, 0 to disable):")
        da_layout.addRow(reinit_label, self.reinit_interval_edit)

        reinit_hint = QLabel(
            "Periodically reconnects to the DA server to prevent stale connections.\n"
            "Default is 5400 (1.5 hours). Set to 0 to disable."
        )
        reinit_hint.setStyleSheet("color: gray; font-size: small;")
        da_layout.addRow(reinit_hint)

        layout.addWidget(da_group)

        # Log Display Group
        log_group = QGroupBox("Log Display")
        log_grp_layout = QFormLayout(log_group)

        self.max_log_lines_edit = QLineEdit(
            str(self.prefs.get("max_log_lines", DEFAULT_MAX_LOG_LINES))
        )
        self.max_log_lines_edit.setPlaceholderText(str(DEFAULT_MAX_LOG_LINES))
        max_lines_label = QLabel("Max log lines in GUI:")
        log_grp_layout.addRow(max_lines_label, self.max_log_lines_edit)

        max_lines_hint = QLabel(
            "Older lines are automatically removed to keep the UI responsive.\n"
            "Set to 0 to disable (not recommended for long runs)."
        )
        max_lines_hint.setStyleSheet("color: gray; font-size: small;")
        log_grp_layout.addRow(max_lines_hint)

        layout.addWidget(log_group)

        # Description
        desc = QLabel(
            "Log output is written to both the Gateway Log pane and the log file."
        )
        desc.setStyleSheet("color: gray; font-size: small; margin-top: 4px;")
        layout.addWidget(desc)

        layout.addStretch()

        # Buttons
        btns = QHBoxLayout()
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)

    def _browse_log_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Log File",
            self.log_file_edit.text() or DEFAULT_LOG_FILE,
            "Log Files (*.log);;All Files (*)"
        )
        if path:
            self.log_file_edit.setText(path)

    def _regenerate_certificate(self):
        """Regenerate the self-signed certificate."""
        reply = QMessageBox.question(
            self, "Regenerate Certificate",
            "This will regenerate the self-signed certificate. "
            "Connected clients will need to re-trust the new certificate.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            _ensure_self_signed_cert(force=True)
            QMessageBox.information(
                self, "Certificate Regenerated",
                f"New certificate generated:\n{os.path.abspath(CERT_FILE)}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to generate certificate: {e}")

    def get_prefs(self):
        if self.radio_info.isChecked():
            level = "INFO"
        elif self.radio_warning.isChecked():
            level = "WARNING"
        else:
            level = "ERROR"

        log_file = self.log_file_edit.text().strip() or DEFAULT_LOG_FILE
        ssl_enabled = self.chk_ssl.isChecked()

        # DA mode
        da_mode = "subscription" if self.da_mode_combo.currentIndex() == 1 else "polling"

        # Poll interval
        try:
            poll_interval = float(self.poll_interval_edit.text().strip())
            if poll_interval <= 0:
                poll_interval = DEFAULT_POLL_INTERVAL
        except ValueError:
            poll_interval = DEFAULT_POLL_INTERVAL

        # Chunk size
        try:
            chunk_size = int(self.chunk_size_edit.text().strip())
            if chunk_size <= 0:
                chunk_size = DEFAULT_CHUNK_SIZE
        except ValueError:
            chunk_size = DEFAULT_CHUNK_SIZE

        # Max log lines
        try:
            max_log_lines = int(self.max_log_lines_edit.text().strip())
            if max_log_lines < 0:
                max_log_lines = DEFAULT_MAX_LOG_LINES
        except ValueError:
            max_log_lines = DEFAULT_MAX_LOG_LINES

        # Reinit interval
        try:
            reinit_interval = float(self.reinit_interval_edit.text().strip())
            if reinit_interval < 0:
                reinit_interval = DEFAULT_REINIT_INTERVAL
        except ValueError:
            reinit_interval = DEFAULT_REINIT_INTERVAL

        return {
            "log_level": level,
            "log_file": log_file,
            "ssl_enabled": ssl_enabled,
            "da_mode": da_mode,
            "poll_interval": poll_interval,
            "chunk_size": chunk_size,
            "max_log_lines": max_log_lines,
            "reinit_interval": reinit_interval,
        }


# ===================================================================
# Main window
# ===================================================================


# ===================================================================
# Main window
# ===================================================================
class MainWindow(QMainWindow):
    _log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.config = {}          # { server: { folder: [tag, ...] } }
        self.engine = None
        self._csv_path = DEFAULT_CSV
        self.prefs = {}
        self._server_status = {}  # { server_name: bool }
        self._server_tree_items = {}  # { server_name: QTreeWidgetItem }

        self._build_ui()
        self._build_menu()
        self._build_toolbar()

        # Connect the log signal to the log view
        self._log_signal.connect(self._log)

        # Apply saved preferences (this creates the qthandler)
        self.prefs = load_preferences()
        apply_preferences(self.prefs)

        # Wire Qt log handler to log_view (after apply_preferences creates qthandler)
        qthandler.set_signal(self._log_signal)

        # Memory monitoring timer (updates status bar every 30 seconds)
        self._memory_timer = QTimer(self)
        self._memory_timer.timeout.connect(self._update_memory_status)
        self._memory_timer.start(30000)  # 30 seconds

        self._load_default_csv()

        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("OPC DA → OPC UA Gateway")
        self.resize(1100, 700)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # --- Splitter: tree (top) | log (bottom) ---
        splitter = QSplitter(Qt.Vertical)

        # Top: hierarchical tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Status"])
        self.tree.setHeaderHidden(False)
        self.tree.setAnimated(True)
        self.tree.setIndentation(20)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_click)
        splitter.addWidget(self.tree)

        # Bottom: log viewer
        log_group = QGroupBox("Gateway Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_group)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

        # --- Bottom toolbar (start / stop / save) ---
        btn_bar = QHBoxLayout()

        self.btn_start = QPushButton("▶  Start Gateway")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.clicked.connect(self._start_gateway)
        btn_bar.addWidget(self.btn_start)

        self.btn_stop = QPushButton("⏹  Stop Gateway")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_gateway)
        btn_bar.addWidget(self.btn_stop)

        self.btn_clear_log = QPushButton("🗑  Clear Log")
        self.btn_clear_log.setObjectName("clearLogBtn")
        self.btn_clear_log.clicked.connect(self._clear_log)
        btn_bar.addWidget(self.btn_clear_log)

        btn_bar.addStretch()

        main_layout.addLayout(btn_bar)

        # Style
        self.setStyleSheet("""
            #startBtn { background: #2e7d32; color: white; padding: 6px 16px; border-radius: 4px; font-weight: bold; }
            #startBtn:hover { background: #388e3c; }
            #startBtn:disabled { background: #a5d6a7; color: #616161; }
            #stopBtn { background: #c62828; color: white; padding: 6px 16px; border-radius: 4px; font-weight: bold; }
            #stopBtn:hover { background: #d32f2f; }
            #stopBtn:disabled { background: #ef9a9a; color: #616161; }
            #clearLogBtn { background: #546e7a; color: white; padding: 6px 16px; border-radius: 4px; font-weight: bold; }
            #clearLogBtn:hover { background: #607d8b; }
            QTreeWidget::item { height: 24px; }
            QGroupBox { font-weight: bold; border: 1px solid #ccc; margin-top: 8px; padding-top: 8px; }
        """)

    def _build_menu(self):
        menubar = self.menuBar()

        # File
        file_menu = menubar.addMenu("&File")
        act_open = QAction("&Load CSV…", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._load_csv_dialog)
        file_menu.addAction(act_open)

        act_save = QAction("&Save CSV", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._save_csv)
        file_menu.addAction(act_save)

        act_save_as = QAction("Save CSV &As…", self)
        act_save_as.triggered.connect(self._save_csv_as)
        file_menu.addAction(act_save_as)

        file_menu.addSeparator()
        act_exit = QAction("E&xit", self)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        # Edit
        edit_menu = menubar.addMenu("&Edit")
        act_add_srv = QAction("Add &Server…", self)
        act_add_srv.setShortcut("Ctrl+N")
        act_add_srv.triggered.connect(self._add_server)
        edit_menu.addAction(act_add_srv)

        act_add_tag = QAction("Add &Tag…", self)
        act_add_tag.setShortcut("Ctrl+T")
        act_add_tag.triggered.connect(self._add_tag)
        edit_menu.addAction(act_add_tag)

        act_edit_tag = QAction("&Edit Tag…", self)
        act_edit_tag.setShortcut("Ctrl+E")
        act_edit_tag.triggered.connect(self._edit_tag)
        edit_menu.addAction(act_edit_tag)

        act_del = QAction("&Delete Selected", self)
        act_del.setShortcut("Del")
        act_del.triggered.connect(self._delete_selected)
        edit_menu.addAction(act_del)

        edit_menu.addSeparator()
        act_prefs = QAction("Preferences…", self)
        act_prefs.setShortcut("Ctrl+,")
        act_prefs.triggered.connect(self._open_preferences)
        edit_menu.addAction(act_prefs)

        # Help
        help_menu = menubar.addMenu("&Help")
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._about)
        help_menu.addAction(act_about)

    def _build_toolbar(self):
        tb = self.addToolBar("Quick")
        tb.addAction("Add Server", lambda: self._add_server())
        tb.addAction("Add Tag", lambda: self._add_tag())
        tb.addAction("Delete", lambda: self._delete_selected())
        tb.addSeparator()
        tb.addAction("Load CSV", lambda: self._load_csv_dialog())
        tb.addAction("Save CSV", lambda: self._save_csv())
        tb.addSeparator()
        tb.addAction("🧹 Force GC", lambda: self._force_gc())

    def _force_gc(self):
        """Manually trigger garbage collection and log detailed memory diagnostics."""
        mem_before = get_memory_usage_mb()
        
        # Count objects of common leak-suspect types before GC
        type_counts_before = _count_objects_by_type()
        
        collected = gc.collect()
        collected_pass2 = gc.collect()
        collected_pass3 = gc.collect()
        total_collected = collected + collected_pass2 + collected_pass3
        
        mem_after = get_memory_usage_mb()
        
        # Count objects after GC
        type_counts_after = _count_objects_by_type()
        
        if mem_before is not None and mem_after is not None:
            self._log(f"Force GC: collected {total_collected} objects (pass1:{collected} pass2:{collected_pass2} pass3:{collected_pass3})")
            self._log(f"  Memory: {mem_before:.1f} MB → {mem_after:.1f} MB (freed {mem_before - mem_after:.1f} MB)")
            
            # Show thread status
            threads = threading.enumerate()
            self._log(f"  Active threads: {len(threads)}")
            for t in threads:
                self._log(f"    - {t.name} (daemon={t.daemon}, alive={t.is_alive()})")
            
            # Show object count changes for key types
            self._log(f"  Object counts (before → after [delta]):")
            for ttype in sorted(type_counts_before.keys()):
                before = type_counts_before[ttype]
                after = type_counts_after.get(ttype, 0)
                delta = after - before
                if before > 0 or after > 0:
                    self._log(f"    {ttype}: {before} → {after} [{delta:+d}]")
        else:
            self._log(f"Force GC: collected {total_collected} objects (psutil not available)")
        
        # Update memory status immediately
        self._update_memory_status()

    # ------------------------------------------------------------------
    # Tree management
    # ------------------------------------------------------------------
    def _refresh_tree(self):
        """Rebuild the tree widget from self.config."""
        self.tree.clear()
        self.tree.setHeaderLabels(["Name", "Type", "Status"])
        self._server_tree_items = {}

        for srv in sorted(self.config):
            status = "Good" if self._server_status.get(srv, False) else "Bad"
            srv_item = QTreeWidgetItem(self.tree, [srv, "OPC DA Server", status])
            srv_item.setFont(0, QFont("Segoe UI", 9, QFont.Bold))
            srv_item.setForeground(0, QColor("#1565c0"))
            self._server_tree_items[srv] = srv_item

            folders = self.config[srv]
            for folder in sorted(folders):
                folder_item = QTreeWidgetItem(srv_item, [folder, "Folder (UA Namespace)", ""])
                folder_item.setFont(0, QFont("Segoe UI", 9))
                folder_item.setForeground(0, QColor("#0d47a1"))

                tags = folders[folder]
                for tag in tags:
                    tag_item = QTreeWidgetItem(folder_item, [tag, "Tag", ""])
                    tag_item.setForeground(0, QColor("#333"))

        self.tree.collapseAll()
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)

    # ------------------------------------------------------------------
    # CSV I/O
    # ------------------------------------------------------------------
    def _load_default_csv(self):
        if os.path.exists(DEFAULT_CSV):
            self.config = load_csv(DEFAULT_CSV)
            self._refresh_tree()
            self._log(f"Loaded {DEFAULT_CSV}")

    def _load_csv_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load CSV Configuration", "", "CSV Files (*.csv);;All (*)"
        )
        if not path:
            return
        self._csv_path = path
        self.config = load_csv(path)
        self._refresh_tree()
        self._log(f"Loaded {path}")
        self.statusBar().showMessage(f"Loaded: {path}")

    def _save_csv(self):
        save_csv(self._csv_path, self.config)
        self._log(f"Saved configuration to {self._csv_path}")
        self.statusBar().showMessage(f"Saved: {self._csv_path}")

    def _save_csv_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV As", DEFAULT_CSV, "CSV Files (*.csv);;All (*)"
        )
        if not path:
            return
        self._csv_path = path
        self._save_csv()

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------
    def _add_server(self):
        dlg = AddServerDialog(list(self.config.keys()), self)
        if dlg.exec_() != QDialog.Accepted:
            return
        name = dlg.result()
        if not name:
            return
        if name in self.config:
            QMessageBox.warning(self, "Duplicate", f"Server '{name}' already exists.")
            return
        self.config[name] = {DEFAULT_FOLDER: []}
        self._refresh_tree()
        self._log(f"Added server: {name}")

    def _add_tag(self):
        item = self.tree.currentItem()
        if not item:
            QMessageBox.information(self, "Select Server", "Select a server or folder in the tree first.")
            return
        # Walk up to find server item (level 0)
        srv_item = item
        while srv_item and self.tree.indexOfTopLevelItem(srv_item) < 0:
            srv_item = srv_item.parent()
        if not srv_item:
            return
        srv_name = srv_item.text(0)

        # Determine folder from selection
        folder = DEFAULT_FOLDER
        if item != srv_item:
            # Check if selected item is a folder (level 1)
            if item.parent() == srv_item:
                folder = item.text(0)
            else:
                # Selected a tag, use its parent folder
                folder = item.parent().text(0)

        existing_folders = list(self.config.get(srv_name, {}).keys())
        dlg = AddTagDialog(srv_name, [], existing_folders, self)
        dlg.folder_edit.setText(folder)
        if dlg.exec_() != QDialog.Accepted:
            return
        tag, folder = dlg.result()
        if not tag:
            return
        if tag in self.config.get(srv_name, {}).get(folder, []):
            QMessageBox.warning(self, "Duplicate", f"Tag '{tag}' already exists in folder '{folder}'.")
            return
        self.config.setdefault(srv_name, {}).setdefault(folder, []).append(tag)
        self._refresh_tree()
        self._log(f"Added tag '{tag}' to folder '{folder}' on server '{srv_name}'")

    def _edit_tag(self):
        item = self.tree.currentItem()
        if not item or item.parent() is None:
            QMessageBox.information(self, "Select Tag", "Select a tag in the tree to edit.")
            return
        # Find server and folder
        folder_item = item.parent()
        srv_item = folder_item.parent()
        if not srv_item:
            return
        srv_name = srv_item.text(0)
        folder = folder_item.text(0)
        old_tag = item.text(0)
        dlg = EditTagDialog(srv_name, old_tag, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        new_tag = dlg.result()
        if not new_tag or new_tag == old_tag:
            return
        tags = self.config.get(srv_name, {}).get(folder, [])
        if new_tag in tags:
            QMessageBox.warning(self, "Duplicate", f"Tag '{new_tag}' already exists.")
            return
        tags[tags.index(old_tag)] = new_tag
        self._refresh_tree()
        self._log(f"Renamed tag '{old_tag}' → '{new_tag}' in folder '{folder}' on '{srv_name}'")

    def _delete_selected(self):
        item = self.tree.currentItem()
        if not item:
            return
        srv_idx = self.tree.indexOfTopLevelItem(item)

        if srv_idx >= 0:
            # Deleting a server
            name = item.text(0)
            reply = QMessageBox.question(
                self, "Delete Server",
                f"Delete server '{name}' and all its tags?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            del self.config[name]
            self._log(f"Deleted server: {name}")
        elif item.parent() and item.parent().parent():
            # Deleting a tag
            folder_item = item.parent()
            srv_item = folder_item.parent()
            srv_name = srv_item.text(0)
            folder = folder_item.text(0)
            tag = item.text(0)
            self.config[srv_name][folder].remove(tag)
            if not self.config[srv_name][folder]:
                del self.config[srv_name][folder]
            if not self.config[srv_name]:
                del self.config[srv_name]
            self._log(f"Deleted tag '{tag}' from folder '{folder}' on '{srv_name}'")
        elif item.parent():
            # Deleting a folder
            srv_item = item.parent()
            srv_name = srv_item.text(0)
            folder = item.text(0)
            reply = QMessageBox.question(
                self, "Delete Folder",
                f"Delete folder '{folder}' and all its tags from '{srv_name}'?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            del self.config[srv_name][folder]
            if not self.config[srv_name]:
                del self.config[srv_name]
            self._log(f"Deleted folder '{folder}' from '{srv_name}'")
        self._refresh_tree()

    def _on_tree_double_click(self, item, column):
        """Double-click a tag to edit it."""
        if item.parent():
            self.tree.setCurrentItem(item)
            self._edit_tag()

    # ------------------------------------------------------------------
    # Gateway start / stop
    # ------------------------------------------------------------------
    def _start_gateway(self):
        if not self.config:
            QMessageBox.warning(self, "No Config", "Add at least one server before starting.")
            return
        # Stop any existing engine before starting a new one
        if self.engine and self.engine.isRunning():
            self._stop_gateway()
        self.engine = GatewayEngine(dict(self.config))
        self.engine.setObjectName("OPCUA-1")
        self.engine.config["__ssl_enabled__"] = self.prefs.get("ssl_enabled", DEFAULT_SSL_ENABLED)
        self.engine.config["__da_mode__"] = self.prefs.get("da_mode", DEFAULT_DA_MODE)
        self.engine.config["__poll_interval__"] = self.prefs.get("poll_interval", DEFAULT_POLL_INTERVAL)
        self.engine.config["__chunk_size__"] = self.prefs.get("chunk_size", DEFAULT_CHUNK_SIZE)
        self.engine.config["__reinit_interval__"] = self.prefs.get("reinit_interval", DEFAULT_REINIT_INTERVAL)
        self.engine.log_signal.connect(self._log)
        self.engine.server_status_signal.connect(self._on_server_status_changed)
        self.engine.start()
        # Reset status for all servers when starting
        for srv in self.config:
            self._server_status[srv] = False
        self._update_server_status_colors()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.statusBar().showMessage("Gateway running…")

    def _stop_gateway(self):
        if self.engine:
            self._log("Stopping gateway...")
            # Disconnect signals first to prevent emissions during shutdown
            try:
                self.engine.log_signal.disconnect(self._log)
            except TypeError:
                pass
            try:
                self.engine.server_status_signal.disconnect(self._on_server_status_changed)
            except TypeError:
                pass
            
            # Signal stop and wait
            self.engine._stop_event.set()
            self.engine.wait(5000)
            if self.engine.isRunning():
                self._log("Gateway didn't stop in time, terminating...")
                self.engine.terminate()
                self.engine.wait(3000)
            
            # Clean up engine reference
            self.engine.deleteLater()
            self.engine = None
            
            # Force GC to clean up thread resources
            collected = gc.collect()
            mem = get_memory_usage_mb()
            if mem is not None:
                self._log(f"Gateway stopped. GC freed {collected} objects, memory: {mem:.1f} MB")
            else:
                self._log(f"Gateway stopped. GC freed {collected} objects")
        
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.statusBar().showMessage("Gateway stopped")
        self._update_memory_status()

    @Slot()
    def _clear_log(self):
        self.log_view.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, msg):
        # Cap log lines to prevent unbounded memory growth (0 = disabled)
        max_lines = self.prefs.get("max_log_lines", DEFAULT_MAX_LOG_LINES)
        doc = self.log_view.document()

        # If we're over the limit, trim before appending
        if max_lines > 0 and doc.lineCount() >= max_lines:
            # Get all blocks and find where to start keeping
            start_block_num = doc.blockCount() - max_lines + 1
            start_block = doc.findBlockByNumber(start_block_num)
            
            # Collect text from blocks we want to keep
            kept_lines = []
            block = start_block
            while block.isValid():
                kept_lines.append(block.text())
                block = block.next()
            
            # Clear document and disable undo to free memory
            self.log_view.setUndoRedoEnabled(False)
            self.log_view.clear()
            self.log_view.setUndoRedoEnabled(True)
            
            # Restore kept content and append new message
            for line in kept_lines:
                self.log_view.append(line)
            self.log_view.append(msg)
            return

        self.log_view.append(msg)

    def _on_server_status_changed(self, server_name, connected):
        """Update server status in the tree when connection state changes."""
        self._server_status[server_name] = connected
        self._update_server_status_colors()

    def _update_server_status_colors(self):
        """Update status column and colors for all server items in the tree."""
        for srv, item in self._server_tree_items.items():
            connected = self._server_status.get(srv, False)
            if connected:
                item.setText(2, "Good")
                item.setForeground(2, QColor("#2e7d32"))  # Green
            else:
                item.setText(2, "Bad")
                item.setForeground(2, QColor("#c62828"))  # Red

    def _update_memory_status(self):
        """Update status bar with current memory usage."""
        mem_mb = get_memory_usage_mb()
        if mem_mb is not None:
            # Show memory in status bar permanently
            if not hasattr(self, '_memory_label'):
                self._memory_label = QLabel()
                self.statusBar().addPermanentWidget(self._memory_label)
            self._memory_label.setText(f"Memory: {mem_mb:.1f} MB")

    def _open_preferences(self):
        dlg = PreferencesDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        new_prefs = dlg.get_prefs()
        save_preferences(new_prefs)
        self.prefs = new_prefs
        apply_preferences(new_prefs)
        # Re-wire the Qt log handler signal (apply_preferences creates a new handler)
        qthandler.set_signal(self._log_signal)
        self._log("Preferences saved.")
        self.statusBar().showMessage("Preferences saved")

    def _about(self):
        QMessageBox.about(
            self, "About",
            "<b>OPC DA → OPC UA Gateway</b><br/>"
            "Version 0.1<br/><br/>"
            "Bridge legacy OPC DA servers to modern OPC UA clients.<br/><br/>"
            "• Add servers and tags via the tree or CSV.<br/>"
            "• Start the gateway to expose tags over OPC UA.<br/>"
            "• Double-click a tag to edit it.",
        )

    def closeEvent(self, event):
        if self.engine and self.engine.isRunning():
            self._stop_gateway()
        event.accept()


# ===================================================================
# Entry point
# ===================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()

    # Handle Ctrl-C gracefully by triggering the window close event
    def _handle_sigint(signum, frame):
        win.close()
    signal.signal(signal.SIGINT, _handle_sigint)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()