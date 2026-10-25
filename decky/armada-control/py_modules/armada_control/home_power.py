"""Backend for the "Steam UI at up to 60FPS/60Hz" home power-saving option.

Only the compositor's own Steam UI refresh request is written. Game FPS caps,
global dynamic refresh and game profile properties stay untouched. The request
is an upper bound of 60Hz: a lower reported feedback rate still counts as
active, and the compositor's native refresh policy is left alone.
"""

import json
import os
import pwd
import re
import subprocess
import tempfile
import threading
from collections import namedtuple
from pathlib import Path

from .proc import clean_env

SETTINGS_FILE = "home-power.json"
SETTINGS_DIR_ENV = "DECKY_PLUGIN_SETTINGS_DIR"

PROP_SUPPORTED = "GAMESCOPE_STEAMUI_REFRESH_RATE_SUPPORTED"
PROP_REQUEST = "GAMESCOPE_STEAMUI_REFRESH_RATE"
PROP_FEEDBACK = "GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK"
PROP_FOCUSED = "GAMESCOPE_FOCUSED_APP"

# Upper bound for the Steam UI request; lower reported feedback stays active.
TARGET_HZ = 60
STEAM_APPID = 769

COMPOSITOR_NAMES = ("gamescope", "gamescope-wl")
DISPLAY = ":0"
XPROP = "/usr/bin/xprop"
RUNUSER = "/usr/bin/runuser"
PROC_ROOT = Path("/proc")
CALL_TIMEOUT = 2

Session = namedtuple("Session", "user uid display key")
Property = namedtuple("Property", "present value")

# xprop prints one root property per line, e.g. ``NAME(CARDINAL) = 120``.
# Only that exact shape is an exactly-restorable 32-bit request value.
_CARDINAL_LINE = re.compile(r"\s*[^\s=]+\(CARDINAL\)\s*=\s*([0-9]+)\s*")
CARDINAL_MAX = 0xFFFFFFFF


def _cardinal_value(text):
    """Return the single unsigned 32-bit CARDINAL payload, or None."""
    match = _CARDINAL_LINE.fullmatch(text)
    if match is None:
        return None
    number = int(match.group(1), 10)
    if number > CARDINAL_MAX:
        return None
    return number


class HomePowerError(RuntimeError):
    """Carries a stable, untranslated reason code for the caller."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def current_uid():
    return getattr(os, "geteuid", lambda: 0)()


def _starttime(stat_text):
    # Field 22 of /proc/<pid>/stat, counted after the parenthesised comm.
    fields = stat_text.rsplit(")", 1)[-1].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _compositor_pids(uid, proc_root=PROC_ROOT):
    root = Path(proc_root)
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            comm = (root / entry.name / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            if comm not in COMPOSITOR_NAMES:
                continue
            start = _starttime(
                (root / entry.name / "stat").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
        except OSError:
            continue
        if start is not None:
            found.append((int(entry.name), start))
    return found


def default_session(proc_root=PROC_ROOT):
    """Locate the active user's own gamescope session, never a random one."""
    user = os.environ.get("DECKY_USER") or "armada"
    try:
        entry = pwd.getpwnam(user)
    except KeyError as exc:
        raise HomePowerError("no-user") from exc

    pids = _compositor_pids(entry.pw_uid, proc_root)
    if not pids:
        raise HomePowerError("no-compositor")
    if len(pids) > 1:
        raise HomePowerError("ambiguous-compositor")

    pid, start = pids[0]
    return Session(entry.pw_name, entry.pw_uid, DISPLAY, f"{pid}:{start}")


class HomePower:
    """Reads and applies the upper-bound-60 Steam UI compositor request.

    ``run``/``session``/``settings_dir`` are injectable for tests; production
    code uses xprop through runuser and the Decky settings directory.
    """

    def __init__(self, run=None, session=None, settings_dir=None):
        self._run = run
        self._session = session
        self._settings_dir = settings_dir
        # Decky calls us through asyncio.to_thread, so every entry point locks.
        self._lock = threading.RLock()
        self._snapshot = None

    # ---- public API -----------------------------------------------------

    def get_state(self):
        with self._lock:
            return self._state()

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        with self._lock:
            path = self._settings_path()
            if path is None:
                raise HomePowerError("settings-missing")

            session, reason = self._try_session()
            if not enabled:
                return self._turn_off(path, session, reason)
            if session is None:
                raise HomePowerError(reason)

            self._apply(session)
            try:
                self._write_preference(path, True)
            except HomePowerError as exc:
                if not self._rollback(session):
                    # Recovery itself failed; the snapshot stays for a retry.
                    raise HomePowerError("restore-failed") from exc
                raise
            return self._state(path)

    def sync(self):
        """Reapply the stored preference to whatever compositor exists now."""
        with self._lock:
            path = self._settings_path()
            if path is None:
                return self._missing_settings()
            try:
                session = self._require_session()
                if self._read_preference(path):
                    self._apply(session)
                else:
                    self._release(session)
            except HomePowerError:
                # A stale or unsupported session is reported through _state.
                pass
            return self._state(path)

    def restore(self):
        """Undo our property change but keep the stored user preference."""
        with self._lock:
            path = self._settings_path()
            session, _ = self._try_session()
            if session is not None:
                # A failed lookup keeps the snapshot; so does a failed write,
                # which surfaces as restore-failed and stays retryable.
                self._release(session)
            return self._state(path)

    # ---- session --------------------------------------------------------

    def _require_session(self):
        if self._session is None:
            return default_session()
        session = self._session()
        if session is None:
            raise HomePowerError("no-compositor")
        return session

    def _try_session(self):
        try:
            return self._require_session(), None
        except HomePowerError as exc:
            return None, exc.reason

    # ---- compositor properties -----------------------------------------

    def _execute(self, session, argv):
        cmd = list(argv)
        if current_uid() != session.uid:
            cmd = [RUNUSER, "-u", session.user, "--", *cmd]
        if self._run is not None:
            return self._run(cmd, timeout=CALL_TIMEOUT)
        try:
            return subprocess.run(
                cmd,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=CALL_TIMEOUT,
                env=clean_env({"DISPLAY": session.display}),
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _xprop(self, session, *args):
        return self._execute(
            session, [XPROP, "-display", session.display, "-root", *args]
        )

    def _read(self, session, name, strict=False):
        proc = self._xprop(session, name)
        if proc is None:
            return None
        text = proc.stdout or ""
        if "not found" in text:
            return Property(False, None)
        if proc.returncode != 0:
            return None
        if strict:
            # A present value we cannot write back verbatim must not pass as
            # a captured original, so anything but one CARDINAL reads as None.
            return Property(True, _cardinal_value(text))
        if "=" not in text:
            return Property(True, None)
        raw = text.split("=", 1)[1].strip().strip('"')
        try:
            return Property(True, int(raw, 10))
        except ValueError:
            return Property(True, None)

    def _supported(self, session):
        prop = self._read(session, PROP_SUPPORTED)
        if prop is None:
            raise HomePowerError("no-x-access")
        if not prop.present or prop.value != TARGET_HZ:
            raise HomePowerError("unsupported")

    def _set_request(self, session, value):
        proc = self._xprop(
            session, "-f", PROP_REQUEST, "32c", "-set", PROP_REQUEST, str(value)
        )
        return proc is not None and proc.returncode == 0

    def _remove_request(self, session):
        proc = self._xprop(session, "-remove", PROP_REQUEST)
        return proc is not None and proc.returncode == 0

    # ---- preference + snapshot -----------------------------------------

    def _settings_path(self):
        base = self._settings_dir
        if base is None:
            base = os.environ.get(SETTINGS_DIR_ENV) or ""
            if not base:
                return None
        return Path(base) / SETTINGS_FILE

    def _read_preference(self, path):
        # A genuinely absent preference file means the user never turned this
        # off, so the default is on; anything unreadable is a safe "off".
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and data.get("enabled") is True

    def _write_preference(self, path, enabled):
        path = Path(path)
        payload = json.dumps({"enabled": bool(enabled)}, indent=2) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), prefix=".home-power-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise HomePowerError("settings-write-failed") from exc

    def _forget_stale(self, session):
        # A restarted compositor has a fresh property set; never replay ours.
        if self._snapshot is not None and self._snapshot["key"] != session.key:
            self._snapshot = None

    def _apply(self, session):
        self._supported(session)
        self._forget_stale(session)

        current = self._read(session, PROP_REQUEST, strict=True)
        if current is None:
            raise HomePowerError("no-x-access")
        if current.present and current.value is None:
            # Our change would not be exactly reversible, so do nothing.
            raise HomePowerError("invalid-request-property")

        fresh = self._snapshot is None
        if fresh:
            self._snapshot = {
                "key": session.key,
                "present": current.present,
                "value": current.value,
            }
        if current.present and current.value == TARGET_HZ:
            return
        if not self._set_request(session, TARGET_HZ):
            if fresh:
                self._snapshot = None
            raise HomePowerError("request-failed")

    def _release(self, session):
        if session is None:
            # An unresolved lookup is not proof that the compositor changed,
            # so the owned snapshot stays available for a later retry.
            return
        self._forget_stale(session)
        snapshot = self._snapshot
        if snapshot is None:
            return
        if snapshot["present"]:
            if not self._set_request(session, snapshot["value"]):
                raise HomePowerError("restore-failed")
        elif not self._remove_request(session):
            raise HomePowerError("restore-failed")
        self._snapshot = None

    def _rollback(self, session):
        try:
            self._release(session)
        except HomePowerError:
            return False
        return True

    def _turn_off(self, path, session, reason):
        if session is None:
            if self._snapshot is not None:
                # We still own the live request; persisting "off" here would
                # forget it without restoring anything.
                raise HomePowerError(reason)
        else:
            self._release(session)
        self._write_preference(path, False)
        return self._state(path)

    def _owned_snapshot(self, session):
        return (
            self._snapshot is not None
            and session is not None
            and self._snapshot["key"] == session.key
        )

    # ---- state ----------------------------------------------------------

    def _missing_settings(self):
        return {
            "enabled": False,
            "supported": False,
            "active": False,
            "reason": "settings-missing",
        }

    def _state(self, path=None):
        if path is None:
            path = self._settings_path()
        if path is None:
            return self._missing_settings()

        enabled = self._read_preference(path)
        session, reason = self._try_session()
        if session is None:
            return {
                "enabled": enabled,
                "supported": False,
                "active": False,
                "reason": reason,
            }

        supported = self._read(session, PROP_SUPPORTED)
        if supported is None:
            return {
                "enabled": enabled,
                "supported": False,
                "active": False,
                "reason": "no-x-access",
            }
        supported_ok = supported.present and supported.value == TARGET_HZ

        if not enabled and self._owned_snapshot(session):
            # The preference is off but our request is still live because a
            # restore failed; never report that as a clean "off".
            return {
                "enabled": False,
                "supported": supported_ok,
                "active": False,
                "reason": "restore-failed",
            }
        if not enabled:
            return {
                "enabled": False,
                "supported": supported_ok,
                "active": False,
                "reason": "off",
            }
        if not supported_ok:
            return {
                "enabled": True,
                "supported": False,
                "active": False,
                "reason": "unsupported",
            }

        request = self._read(session, PROP_REQUEST)
        feedback = self._read(session, PROP_FEEDBACK)
        focus = self._read(session, PROP_FOCUSED)
        if request is None or feedback is None or focus is None:
            return {
                "enabled": True,
                "supported": True,
                "active": False,
                "reason": "no-x-access",
            }
        if not (request.present and request.value == TARGET_HZ):
            return {
                "enabled": True,
                "supported": True,
                "active": False,
                "reason": "not-requested",
            }
        if not (
            feedback.present
            and feedback.value is not None
            and 0 < feedback.value <= TARGET_HZ
        ):
            return {
                "enabled": True,
                "supported": True,
                "active": False,
                "reason": "feedback-mismatch",
            }
        if not (focus.present and focus.value == STEAM_APPID):
            return {
                "enabled": True,
                "supported": True,
                "active": False,
                "reason": "focus-mismatch",
            }
        return {
            "enabled": True,
            "supported": True,
            "active": True,
            "reason": "active",
        }
