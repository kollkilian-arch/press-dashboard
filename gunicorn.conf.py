"""Conservative defaults for the 512 MB Render service.

Loaded automatically by the existing `gunicorn app:app --timeout 120` command.
One process avoids duplicating the app, AI SDKs and background scheduler. Two
threads keep navigation available while one request waits for an AI response.
"""
import faulthandler
import os
import sys
import threading


workers = 1
worker_class = "gthread"
threads = 2
preload_app = False
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s %(t)s "%(r)s" %(s)s %(b)s duration_us=%(D)s'

# Heartbeat metadata on a disk-backed /tmp can stall Gunicorn's event loop.
# /dev/shm is tmpfs on Linux; keep the portable default on local macOS.
if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
    worker_tmp_dir = "/dev/shm"


def post_worker_init(worker):
    worker.log.info("[Startup] HTTP worker ready pid=%s threads=%s heartbeat_dir=%s",
                    worker.pid, worker.cfg.threads, worker.cfg.worker_tmp_dir)


def _log_slow_request(worker, method, path):
    worker.log.warning("[Slow request] %s %s still running after 30s; thread stacks follow",
                       method, path)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def pre_request(worker, req):
    # gthread keeps sending heartbeats even when all request threads are stuck:
    # --timeout alone will not diagnose that case. Never log bodies or query strings.
    req._diagnostic_timer = threading.Timer(30, _log_slow_request,
                                            args=(worker, req.method, req.path))
    req._diagnostic_timer.daemon = True
    req._diagnostic_timer.start()


def post_request(worker, req, environ, resp):
    timer = getattr(req, "_diagnostic_timer", None)
    if timer is not None:
        timer.cancel()


def worker_abort(worker):
    worker.log.error("[Worker timeout] pid=%s; thread stacks follow", worker.pid)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
