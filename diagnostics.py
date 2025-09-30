#!/usr/bin/env python3
"""
Diagnostics script for Sara AI Bot (Flask + Streaming).
Save as diagnostics.py and run from project root:
  python diagnostics.py
Options:
  --no-ws   : skip WebSocket simulation (safe if you don't want to exercise TTS/OpenAI)
Outputs:
  diagnostics_report.json (summary)
  diagnostics.log (verbose)
"""
import os
import sys
import time
import json
import uuid
import ssl
import shutil
import base64
import socket
import logging
import argparse
import subprocess
from datetime import datetime
from typing import Optional

# try to import optional libs; we'll report if missing
try:
    import requests
except Exception:
    requests = None

try:
    import websockets
    from websockets import client as wsclient
except Exception:
    websockets = None

# ---- Configuration ----
REQUIRED_ENVS = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "SERVER_URL",
    "PUBLIC_STREAMING_URL",
    # AI keys optional but important to test:
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
]

REPORT_JSON = "diagnostics_report.json"
LOG_FILE = "diagnostics.log"
WS_READ_TIMEOUT = 18  # seconds to wait while listening to streaming server
HTTP_TIMEOUT = 10

# ---- Logging ----
logger = logging.getLogger("diag")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
fh.setFormatter(fmt)
logger.addHandler(fh)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(ch)

# ---- Helpers ----
def load_dotenv(dotenv_path=".env"):
    """Simple .env loader: overwrite only missing envs."""
    if not os.path.exists(dotenv_path):
        return {}
    found = {}
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            found[k] = v
            if k not in os.environ:
                os.environ[k] = v
    return found

def mask_key(v: Optional[str]) -> str:
    if not v:
        return "<MISSING>"
    if len(v) <= 8:
        return v[0:2] + "..." + v[-2:]
    return v[0:4] + "..." + v[-4:]

def check_executable(name: str) -> bool:
    return shutil.which(name) is not None

def check_python_package(name: str):
    try:
        m = __import__(name)
        ver = getattr(m, "__version__", "unknown")
        return True, str(ver)
    except Exception as e:
        return False, str(e)

def http_get(url: str, headers=None, timeout=HTTP_TIMEOUT):
    if not requests:
        return {"ok": False, "error": "requests not installed"}
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        dt = time.time() - t0
        return {"ok": r.status_code == 200, "status": r.status_code, "elapsed": dt, "text_snip": (r.text or "")[:800], "headers": dict(r.headers)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def http_post(url: str, data: dict, headers=None, timeout=HTTP_TIMEOUT):
    if not requests:
        return {"ok": False, "error": "requests not installed"}
    try:
        t0 = time.time()
        r = requests.post(url, data=data, headers=headers or {}, timeout=timeout)
        dt = time.time() - t0
        ct = r.headers.get("content-type","")
        return {"ok": r.status_code in (200,201,202), "status": r.status_code, "elapsed": dt, "content_type": ct, "text_snip": (r.text or "")[:1000], "headers": dict(r.headers)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_twilio_creds(sid: Optional[str], token: Optional[str]):
    if not sid or not token:
        return {"ok": False, "error": "Twilio SID or token missing"}
    if not requests:
        return {"ok": False, "error": "requests not installed"}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
    try:
        t0 = time.time()
        r = requests.get(url, auth=(sid, token), timeout=HTTP_TIMEOUT)
        dt = time.time() - t0
        return {"ok": r.status_code == 200, "status": r.status_code, "elapsed": dt, "text_snip": (r.text or "")[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_openai_key(key: Optional[str]):
    if not key:
        return {"ok": False, "error": "OpenAI key missing"}
    if not requests:
        return {"ok": False, "error": "requests not installed"}
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        dt = time.time() - t0
        return {"ok": r.status_code == 200, "status": r.status_code, "elapsed": dt, "text_snip": (r.text or "")[:800]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_elevenlabs_key(key: Optional[str]):
    if not key:
        return {"ok": False, "error": "ElevenLabs key missing"}
    if not requests:
        return {"ok": False, "error": "requests not installed"}
    url = "https://api.elevenlabs.io/v1/voices"
    headers = {"xi-api-key": key}
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        dt = time.time() - t0
        return {"ok": r.status_code == 200, "status": r.status_code, "elapsed": dt, "text_snip": (r.text or "")[:800]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def websocket_simulation(ws_url: str, timeout: int = WS_READ_TIMEOUT):
    """Simulate Twilio as a client: connect, expect 'connected', send 'start', collect messages."""
    if not websockets:
        return {"ok": False, "error": "websockets package not installed"}
    result = {"ok": True, "events": [], "errors": []}
    try:
        # choose ssl context automatically for wss
        sslctx = None
        if ws_url.startswith("wss://"):
            sslctx = ssl.create_default_context()
        # connect
        async with websockets.connect(ws_url, ssl=sslctx) as ws:
            # wait for initial message (server should send connected)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                result["events"].append({"recv_initial": msg})
                logger.debug("WS initial message: %s", msg)
            except Exception as e:
                result["errors"].append(f"no initial recv: {e}")
            # send a safe start event: sample rate 8000 mu-law
            stream_sid = f"diag-{uuid.uuid4().hex[:8]}"
            start = {"event": "start", "start": {"streamSid": stream_sid, "callSid": f"diag-call-{uuid.uuid4().hex[:6]}", "sampleRate": 8000, "mediaFormat": {"encoding":"audio/x-mulaw","samplerate":8000,"channels":1}}}
            await ws.send(json.dumps(start))
            result["events"].append({"sent_start": start})
            logger.debug("WS sent start event")
            # collect inbound messages for `timeout` seconds
            t0 = time.time()
            media_count = 0
            mark_count = 0
            recv_messages = []
            while time.time() - t0 < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    recv_messages.append(msg)
                    # parse JSON if possible
                    try:
                        j = json.loads(msg)
                        et = j.get("event")
                        if et == "media":
                            media_count += 1
                            # inspect payload prefix if present
                            payload = j.get("media", {}).get("payload")
                            if payload:
                                prefix = payload[:12]
                            else:
                                prefix = None
                            result["events"].append({"media": {"prefix_b64": prefix}})
                        elif et == "mark":
                            mark_count += 1
                            result["events"].append({"mark": j.get("mark")})
                        else:
                            result["events"].append({et: j})
                    except Exception:
                        result["events"].append({"raw": msg})
                except asyncio.TimeoutError:
                    # no message in this slot - continue until timeout
                    continue
            # send stop
            try:
                await ws.send(json.dumps({"event":"stop"}))
                result["events"].append({"sent_stop": True})
            except Exception as e:
                result["errors"].append(f"failed send stop: {e}")
            result["media_count"] = media_count
            result["mark_count"] = mark_count
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return result

def safe_json_dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

# ---- Main diagnostic logic ----
def run_all_checks(do_ws: bool = True):
    logger.info("Starting diagnostics at %s", datetime.utcnow().isoformat() + "Z")
    load_dotenv(".env")
    report = {"timestamp": datetime.utcnow().isoformat() + "Z", "checks": {}}

    # 1) env vars
    envs = {}
    missing = []
    for k in REQUIRED_ENVS:
        v = os.environ.get(k)
        envs[k] = mask_key(v)
        if not v:
            missing.append(k)
    report["checks"]["env"] = {"present": envs, "missing": missing}
    if missing:
        logger.warning("Missing env vars: %s", missing)

    # 2) ffmpeg check
    ff = check_executable("ffmpeg")
    report["checks"]["ffmpeg"] = {"found": ff, "path": shutil.which("ffmpeg")}
    logger.info("ffmpeg on PATH: %s", ff)

    # 3) python packages
    pkgs = {}
    for pkg in ("requests", "websockets"):
        ok, ver = check_python_package(pkg)
        pkgs[pkg] = {"installed": ok, "info": ver}
    report["checks"]["python_packages"] = pkgs
    if not requests:
        logger.warning("Python 'requests' not installed; many HTTP checks will be skipped. Install: pip install requests")
    if not websockets:
        logger.warning("Python 'websockets' not installed; WS simulation will be skipped. Install: pip install websockets")

    # 4) service health checks
    server_url = os.environ.get("SERVER_URL", "").rstrip("/")
    stream_url = os.environ.get("PUBLIC_STREAMING_URL", "").rstrip("/")
    # normalize stream websocket/http mapping for GET health
    stream_health_http = stream_url
    if stream_health_http.startswith("wss://"):
        stream_health_http = "https://" + stream_health_http[len("wss://"):]
    if stream_health_http.startswith("ws://"):
        stream_health_http = "http://" + stream_health_http[len("ws://"):]
    # ensure /health
    srv_health_url = server_url + "/health" if server_url else None
    strm_health_url = stream_health_http + "/health" if stream_health_http else None
    srv_res = http_get(srv_health_url) if srv_health_url and requests else {"ok": False, "error": "no requests or SERVER_URL unset"}
    strm_res = http_get(strm_health_url) if strm_health_url and requests else {"ok": False, "error": "no requests or PUBLIC_STREAMING_URL unset"}
    report["checks"]["service_health"] = {"server_health": {"url": srv_health_url, "result": srv_res}, "streaming_health": {"url": strm_health_url, "result": strm_res}}
    logger.info("Server health: %s | Streaming health: %s", srv_res.get("status") if isinstance(srv_res, dict) else srv_res, strm_res.get("status") if isinstance(strm_res, dict) else strm_res)

    # 5) Twilio webhook endpoints (POST safe)
    outbound_url = server_url + "/outbound" if server_url else None
    inbound_url = server_url + "/inbound" if server_url else None
    tw_post_results = {}
    sample_form = {"CallSid": "diag-call-sid", "From": "+15005550006", "To": os.environ.get("TWILIO_PHONE_NUMBER", "+15555550000")}
    if outbound_url and requests:
        r = http_post(outbound_url, sample_form)
        tw_post_results["outbound"] = {"url": outbound_url, "result": r}
    else:
        tw_post_results["outbound"] = {"error": "no outbound url or requests missing"}
    if inbound_url and requests:
        r = http_post(inbound_url, sample_form)
        tw_post_results["inbound"] = {"url": inbound_url, "result": r}
    else:
        tw_post_results["inbound"] = {"error": "no inbound url or requests missing"}
    report["checks"]["twilio_webhooks"] = tw_post_results
    logger.info("Webhook outbound status: %s", tw_post_results.get("outbound"))

    # 6) API auth checks
    tw_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tw_token = os.environ.get("TWILIO_AUTH_TOKEN")
    report["checks"]["twilio_api"] = check_twilio_creds(tw_sid, tw_token) if requests else {"ok": False, "error": "requests missing"}

    report["checks"]["openai_api"] = check_openai_key(os.environ.get("OPENAI_API_KEY")) if requests else {"ok": False, "error": "requests missing"}
    report["checks"]["elevenlabs_api"] = check_elevenlabs_key(os.environ.get("ELEVENLABS_API_KEY")) if requests else {"ok": False, "error": "requests missing"}

    # 7) WebSocket simulation (optional)
    ws_report = {"enabled": do_ws, "ok": None}
    if do_ws:
        ws_target = os.environ.get("PUBLIC_STREAMING_URL", "").rstrip("/")
        # if it starts with https:// or http://, convert to ws:// or wss://
        if ws_target.startswith("https://"):
            ws_target = "wss://" + ws_target[len("https://"):]
        elif ws_target.startswith("http://"):
            ws_target = "ws://" + ws_target[len("http://"):]
        # ensure path ends with /ws
        if not ws_target.endswith("/ws"):
            ws_target = ws_target.rstrip("/") + "/ws"
        ws_report["ws_url"] = ws_target
        logger.info("Attempting WebSocket simulation to %s", ws_target)
        if websockets:
            try:
                res = asyncio.run(websocket_simulation(ws_target, timeout=WS_READ_TIMEOUT))
                ws_report["ok"] = res.get("ok", False)
                ws_report["detail"] = res
            except Exception as e:
                ws_report["ok"] = False
                ws_report["error"] = str(e)
        else:
            ws_report["ok"] = False
            ws_report["error"] = "websockets package not installed"
    report["checks"]["websocket_test"] = ws_report

    # 8) local host checks: port binding if your service is local (quick)
    try:
        host_check = {}
        for url in (server_url, stream_health_http):
            if not url:
                continue
            try:
                parsed = requests.utils.urlparse(url)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                s = socket.socket()
                s.settimeout(2.0)
                r = s.connect_ex((host, port))
                host_check[url] = {"host": host, "port": port, "connect_ok": (r == 0)}
                s.close()
            except Exception as e:
                host_check[url] = {"error": str(e)}
        report["checks"]["host_connectivity"] = host_check
    except Exception as e:
        report["checks"]["host_connectivity"] = {"error": str(e)}

    # Save and print summary
    safe_json_dump(REPORT_JSON, report)
    logger.info("\nDiagnostics complete. Summary written to %s", REPORT_JSON)
    logger.info("Key outcomes (quick):")
    # quick summary lines
    if report["checks"]["env"]["missing"]:
        logger.warning("Missing env vars: %s", report["checks"]["env"]["missing"])
    logger.info("ffmpeg found: %s", report["checks"]["ffmpeg"]["found"])
    logger.info("Server /health: %s", report["checks"]["service_health"]["server_health"].get("status") if isinstance(report["checks"]["service_health"]["server_health"], dict) else "n/a")
    logger.info("Streaming /health: %s", report["checks"]["service_health"]["streaming_health"].get("status") if isinstance(report["checks"]["service_health"]["streaming_health"], dict) else "n/a")
    logger.info("Twilio API check: %s", report["checks"]["twilio_api"].get("ok"))
    logger.info("OpenAI API check: %s", report["checks"]["openai_api"].get("ok"))
    logger.info("ElevenLabs API check: %s", report["checks"]["elevenlabs_api"].get("ok"))
    if do_ws:
        logger.info("WebSocket simulation OK: %s | media_count: %s", report["checks"]["websocket_test"].get("ok"), report["checks"]["websocket_test"].get("detail", {}).get("media_count"))
    logger.info("For full details, see %s and %s", REPORT_JSON, LOG_FILE)
    return report

# ---- CLI entrypoint ----
def main():
    parser = argparse.ArgumentParser(prog="Sara AI diagnostics", description="Run full diagnostics for Sara AI bot")
    parser.add_argument("--no-ws", action="store_true", help="Skip WebSocket simulation")
    args = parser.parse_args()
    do_ws = not args.no_ws
    report = run_all_checks(do_ws=do_ws)
    return 0

if __name__ == "__main__":
    sys.exit(main())
