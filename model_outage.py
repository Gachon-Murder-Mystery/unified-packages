"""[FIX-I11] Pause the game while the model is unavailable, resume when it is back (textbook II
ch12 'Protections from Internal Failures', ch10 'Self-Healing'; user decision 2026-11-09).

Before: 3 consecutive Codex failures raised CodexUnavailable, the game was aborted, and a shared
pool campaign had to be rolled back by hand (runs/v8-main-30c seed 5, I10).

Now one gate per process:
  * the first thread that sees the outage becomes the prober. It PAUSEs the game through the
    operator (the engine stops its clocks; RESUME shifts question/trade deadlines and leases),
    then retries its own request with growing delays (30 s .. 5 min);
  * every other model call waits at the gate instead of failing, so no decision is replaced by a
    rule-based stand-in during the outage (the game stays one the model played);
  * engine calls refused with `not_running` while the game is paused by the gate wait for RESUME
    and are sent again (PauseAwareClient). A refused call changed nothing, so resending is safe;
  * when the probe succeeds: RESUME, open the gate, carry on. If the outage outlasts
    CODEX_OUTAGE_MAX_SECONDS (default 3 h) the old behaviour applies: abort, campaign pauses.

Each outage is recorded (start, end, seconds, probes, first error) for the game summary and the
campaign's regression traces (GAME_TRACE_FILE).
"""
import json
import os
import threading
import time

DELAYS = (30, 60, 120, 300)


def trace(record):
    """[FIX-C16] Append one fault record to the campaign's regression traces (if configured)."""
    path = os.environ.get("GAME_TRACE_FILE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict({"source": "model"}, **record, at=round(time.time(), 1)), ensure_ascii=False) + "\n")
    except OSError:
        pass   # a trace must never break a game


class ModelCallPolicy:
    """[FIX-C20] Bridge from the model client to the GM's memory (same process). The GM binds its memory;
    without a GM the client keeps its fixed cap and the old single-failure behaviour (stand-in)."""

    def __init__(self):
        self.memory = None
        self.lock = threading.Lock()

    def bind(self, memory):
        self.memory = memory

    def deadline(self, default):
        with self.lock:
            return self.memory.model_deadline() if self.memory is not None else default

    def observe(self, seconds):
        with self.lock:
            if self.memory is not None:
                self.memory.model_call(seconds)

    def remedy(self):
        with self.lock:
            return self.memory.choose("model_timeout") if self.memory is not None else "stand_in"

    def learn(self, remedy, resolved, seconds):
        with self.lock:
            if self.memory is not None:
                self.memory.learn("model_timeout", remedy, resolved, seconds)


MODEL_POLICY = ModelCallPolicy()


class OutageGate:
    def __init__(self, delays=None, max_seconds=None, sleep=time.sleep):
        env = os.environ.get("MODEL_OUTAGE_DELAYS")      # e.g. "2,2" for offline checks
        self.delays = tuple(delays or (tuple(float(x) for x in env.split(",")) if env else DELAYS))
        self.max_seconds = float(max_seconds if max_seconds is not None
                                 else os.environ.get("CODEX_OUTAGE_MAX_SECONDS", 3 * 3600))
        self.sleep = sleep
        self.lock = threading.Lock()
        self.healthy = threading.Event()
        self.healthy.set()
        self.resumed = threading.Event()
        self.resumed.set()
        self.active = False
        self.failed = None          # message once an outage outlasted max_seconds (sticky)
        self.engine_paused = False
        self.events = []
        self.paused_seconds = 0.0
        self.on_pause = self.on_resume = None

    def bind(self, on_pause, on_resume):
        """The runner gives the gate its operator PAUSE/RESUME for the current game."""
        self.on_pause, self.on_resume = on_pause, on_resume

    def wait_healthy(self, exc_cls):
        if not self.healthy.is_set():
            self.healthy.wait()
        if self.failed:
            raise exc_cls(self.failed)

    def _call_hook(self, hook, name, event):
        if hook is None:
            return False
        try:
            hook()
            return True
        except Exception as exc:          # e.g. the game already ended: keep waiting for the model
            event.setdefault("hook_errors", []).append(f"{name}: {type(exc).__name__}: {exc}"[:200])
            return False

    def outage(self, probe, retry, error, exc_cls):
        """probe() -> (text, usage, error) is a tiny health check; retry() is the caller's own
        request. Once the health check passes, the caller's request is sent once more and its
        result returned; if only that request keeps failing it is a single failure (RuntimeError,
        B falls back once), not an outage."""
        with self.lock:
            leader = not self.active
            if leader:
                self.active = True
                self.healthy.clear()
        if not leader:
            self.wait_healthy(exc_cls)
            return self._again(retry)
        started = time.time()
        event = {"kind": "model_outage", "started": round(started, 1), "first_error": str(error)[:300],
                 "probes": 0, "game_paused": False}
        self.events.append(event)
        # mark paused before PAUSE so a call refused right after it already waits for RESUME
        self.engine_paused = True
        self.resumed.clear()
        if self._call_hook(self.on_pause, "pause", event):
            event["game_paused"] = True
        else:
            self.engine_paused = False
            self.resumed.set()
        trace(dict(event, phase="start"))
        attempt = 0
        try:
            while True:
                self.sleep(self.delays[min(attempt, len(self.delays) - 1)])
                attempt += 1
                event["probes"] = attempt
                _, _, err = probe()
                if not err:
                    break
                event["last_error"] = str(err)[:300]
                if time.time() - started >= self.max_seconds:
                    self.failed = (f"model unavailable for {round(time.time() - started)} s "
                                   f"({attempt} probes); last: {err}")
                    event.update(ended=round(time.time(), 1), seconds=round(time.time() - started, 1),
                                 outcome="gave_up")
                    trace(dict(event, phase="end"))
                    raise exc_cls(self.failed)
            event.update(ended=round(time.time(), 1), seconds=round(time.time() - started, 1), outcome="recovered")
            trace(dict(event, phase="end"))
        finally:
            if self.engine_paused:
                self._call_hook(self.on_resume, "resume", event)
                self.paused_seconds += time.time() - started
                self.engine_paused = False
                self.resumed.set()
            with self.lock:
                self.active = False
            self.healthy.set()      # waiters re-check self.failed
        return self._again(retry)

    @staticmethod
    def _again(retry):
        text, usage, err = retry()   # the model is back: my own request once more
        if err:
            raise RuntimeError(err)  # a single failure: B falls back once (counted)
        return text, usage

    def summary(self):
        return {"outages": len(self.events), "paused_seconds": round(self.paused_seconds, 1),
                "events": self.events}


GATE = OutageGate()


class PauseAwareClient:
    """Engine client wrapper: a call refused with `not_running` while the gate has the game paused
    waits for RESUME and is sent again (it changed nothing, so resending is safe)."""

    def __init__(self, client, gate=GATE):
        self.client, self.gate = client, gate

    def __getattr__(self, name):
        return getattr(self.client, name)

    def bind(self, **credentials):
        return PauseAwareClient(self.client.bind(**credentials), self.gate)

    def call(self, route, **data):
        while True:
            try:
                return self.client.call(route, **data)
            except Exception as exc:
                if getattr(exc, "code", None) != "not_running" or not (
                        self.gate.engine_paused or not self.gate.resumed.is_set()):
                    raise
                self.gate.resumed.wait()
