#!/usr/bin/env python3
"""
deep_diagnostics.py
Comprehensive diagnostics for Sara AI Voice Bot:
 - env checks
 - service health checks
 - Twilio webhook test (non-destructive)
 - ElevenLabs TTS fetch test (optional)
 - local ffmpeg convert to µ-law test on ElevenLabs TTS
 - WebSocket simulation of Twilio Media Streams: send 'start', collect server outbound 'media'/'mark', analyze first chunk bytes
Outputs: deep_diag_report.json and printed summary
Usage:
  pip install requests websockets python-dotenv
  python deep_diagnostics.py
  python deep_diagnostics.py --no-remote-apis   # skip ElevenLabs/OpenAI calls
"""
import os
import sys
import time
import json
import base64
import ssl
import shutil
import logging
import argparse
import subprocess
import asyncio
import uuid
from datetime import datetime
from typing import Optional

# optional libs
try:
    import requests
except Exception:
    requests = None

try:
    import websockets
except Exception:
    websockets = None

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deepdiag")

REPORT_FILE = "deep_diag_report.json"
HTTP_TIMEOUT = 12
WS_TIMEOUT = 20
TTS_TEST_TEXT = "Hello. This is a diagnostics test."

# Helpers
def load_env_from_dotenv(path=".env"):
    if not os.path.exists(path):
        return {}
    loaded = {}
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            loaded[k] = v
            os.environ.setdefault(k, v)
    return loaded

def mask(v):
    if not v: return "<MISSING>"
    return v[:4] + "..." + v[-4:]

def check_exe(name):
    path = shutil.which(name)
    return {"found": bool(path), "path": path}

def http_get(url, headers=None, timeout=HTTP_TIMEOUT):
    if not requests:
        return {"ok": False, "error": "requests missing"}
    try:
        t0 = time.time()
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        return {"ok": r.status_code == 200, "status": r.status_code, "elapsed": time.time()-t0, "text": (r.text or "")[:1000], "headers": dict(r.headers)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def http_post(url, data, headers=None, timeout=HTTP_TIMEOUT):
    if not requests:
        return {"ok": False, "error": "requests missing"}
    try:
        t0 = time.time()
        r = requests.post(url, data=data, headers=headers or {}, timeout=timeout)
        return {"ok": r.status_code in (200,201,202), "status": r.status_code, "elapsed": time.time()-t0, "text": (r.text or "")[:200], "headers": dict(r.headers)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_twilio(sid, token):
    if not requests: return {"ok": False, "error": "requests missing"}
    if not sid or not token:
        return {"ok": False, "error": "missing credentials"}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json"
    try:
        t0=time.time()
        r = requests.get(url, auth=(sid, token), timeout=HTTP_TIMEOUT)
        return {"ok": r.status_code==200, "status": r.status_code, "elapsed": time.time()-t0, "text": (r.text or "")[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_openai(key):
    if not requests: return {"ok": False, "error": "requests missing"}
    if not key: return {"ok": False, "error": "missing key"}
    try:
        t0=time.time()
        r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT)
        return {"ok": r.status_code==200, "status": r.status_code, "elapsed": time.time()-t0, "text": (r.text or "")[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_elevenlabs(key):
    if not requests: return {"ok": False, "error": "requests missing"}
    if not key: return {"ok": False, "error": "missing key"}
    try:
        t0=time.time()
        r = requests.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}, timeout=HTTP_TIMEOUT)
        return {"ok": r.status_code==200, "status": r.status_code, "elapsed": time.time()-t0, "text": (r.text or "")[:400]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def elevenlabs_tts(text, key, voice_id, timeout=20):
    """Fetch TTS from ElevenLabs; return bytes or None and response info."""
    if not requests:
        return None, {"error":"requests missing"}
    if not key or not voice_id:
        return None, {"error":"missing key/voice"}
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {"xi-api-key": key, "Accept":"audio/mpeg", "Content-Type":"application/json"}
    payload = {"text": text, "voice_settings": {"stability":0.6,"similarity_boost":0.6}}
    try:
        t0=time.time()
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        info = {"status": r.status_code, "elapsed": time.time()-t0, "text_snip": (r.text or "")[:200]}
        if r.status_code == 200:
            return r.content, info
        else:
            return None, info
    except Exception as e:
        return None, {"error": str(e)}

def ffmpeg_to_mulaw_bytes(input_bytes: bytes, input_format_hint: Optional[str]=None, out_rate:int=8000):
    """
    Convert arbitrary input bytes (MP3/WAV/PCM) to raw mu-law bytes using ffmpeg subprocess.
    Returns raw bytes or raises RuntimeError.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if input_format_hint:
        cmd += ["-f", input_format_hint]
    cmd += ["-i", "pipe:0", "-ar", str(out_rate), "-ac", "1", "-f", "mulaw", "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(input_bytes)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg->mulaw failed: " + (err.decode(errors='ignore')[:400]))
    return out

def is_mostly_silence_mulaw(data: bytes, threshold_fraction=0.95):
    """Detect whether a mu-law buffer is mostly silence (0xFF repeated)."""
    if not data:
        return True
    count_ff = data.count(b"\xff")
    frac = count_ff / len(data)
    return frac >= threshold_fraction

# WebSocket simulation (acts as Twilio client)
async def ws_simulate(ws_url, timeout=WS_TIMEOUT):
    result = {"ok": True, "events": [], "errors": []}
    if not websockets:
        return {"ok": False, "error":"websockets missing"}
    sslctx = None
    if ws_url.startswith("wss://"):
        sslctx = ssl.create_default_context()
    try:
        async with websockets.connect(ws_url, ssl=sslctx) as ws:
            # expect initial connected from server
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                result["events"].append({"initial": msg})
            except Exception as e:
                result["errors"].append(f"no initial: {e}")
            streamSid = f"diag-{uuid.uuid4().hex[:8]}"
            start = {"event":"start","start":{"streamSid":streamSid,"callSid":f"diagcall-{uuid.uuid4().hex[:6]}","sampleRate":8000,"mediaFormat":{"encoding":"audio/x-mulaw","samplerate":8000,"channels":1}}}
            await ws.send(json.dumps(start))
            result["events"].append({"sent_start": start})
            t0 = time.time()
            first_media_b64 = None
            first_media_raw=None
            media_count=0
            mark_count=0
            while time.time()-t0 < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    # try parse
                    try:
                        j = json.loads(msg)
                    except Exception:
                        j = {"raw": msg}
                    ev = j.get("event")
                    if ev == "media":
                        media_count += 1
                        payload = j.get("media", {}).get("payload")
                        if payload and not first_media_b64:
                            first_media_b64 = payload
                            try:
                                first_media_raw = base64.b64decode(payload)
                            except Exception:
                                first_media_raw = None
                        result["events"].append({"media_seen": True})
                    elif ev == "mark":
                        mark_count += 1
                        result["events"].append({"mark": j.get("mark")})
                    else:
                        result["events"].append({ev: j})
                except asyncio.TimeoutError:
                    continue
            # stop
            try:
                await ws.send(json.dumps({"event":"stop"}))
            except Exception:
                pass
            result.update({"media_count": media_count, "mark_count": mark_count, "first_media_b64": first_media_b64})
            if first_media_raw:
                result["first_media_len"] = len(first_media_raw)
                result["first_media_is_silence"] = is_mostly_silence_mulaw(first_media_raw)
            return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

def main(no_remote_apis=False):
    load_env_from_dotenv()
    report = {"timestamp": datetime.utcnow().isoformat()+"Z", "checks": {}}
    # Env
    keys = ["TWILIO_ACCOUNT_SID","TWILIO_AUTH_TOKEN","TWILIO_PHONE_NUMBER","SERVER_URL","PUBLIC_STREAMING_URL","OPENAI_API_KEY","ELEVENLABS_API_KEY","ELEVENLABS_VOICE_ID"]
    envs = {k: mask(os.environ.get(k)) for k in keys}
    missing = [k for k in keys if not os.environ.get(k)]
    report["checks"]["env"] = {"present": envs, "missing": missing}
    # ffmpeg
    report["checks"]["ffmpeg"] = check_exe("ffmpeg")
    # packages
    report["checks"]["packages"] = {"requests": bool(requests), "websockets": bool(websockets)}
    # services health
    server_url = (os.environ.get("SERVER_URL") or "").rstrip("/")
    stream_url = (os.environ.get("PUBLIC_STREAMING_URL") or "").rstrip("/")
    # normalize streaming health endpoint
    stream_health = stream_url
    if stream_health.startswith("wss://"): stream_health = "https://"+stream_health[len("wss://"):]
    if stream_health.startswith("ws://"): stream_health = "http://"+stream_health[len("ws://"):]
    if stream_health.endswith("/ws"): stream_health = stream_health[:-3]
    if not stream_health.endswith("/health"): stream_health = stream_health.rstrip("/") + "/health"
    server_health_res = http_get(server_url + "/health") if server_url and requests else {"ok": False, "error":"requests missing or SERVER_URL unset"}
    stream_health_res = http_get(stream_health) if stream_health and requests else {"ok": False, "error":"requests missing or PUBLIC_STREAMING_URL unset"}
    report["checks"]["server_health"] = server_health_res
    report["checks"]["stream_health"] = stream_health_res
    # twilio webhook tests
    if server_url and requests:
        sample = {"CallSid":"diag","From":"+15005550006","To": os.environ.get("TWILIO_PHONE_NUMBER","")}
        report["checks"]["webhooks"] = {"outbound": http_post(server_url + "/outbound", sample), "inbound": http_post(server_url + "/inbound", sample)}
    else:
        report["checks"]["webhooks"] = {"error": "no requests or SERVER_URL unset"}
    # API auth checks
    report["checks"]["twilio_api"] = check_twilio(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")) if requests else {"ok":False,"error":"requests missing"}
    report["checks"]["openai_api"] = check_openai(os.environ.get("OPENAI_API_KEY")) if (requests and not no_remote_apis) else {"skipped": no_remote_apis}
    report["checks"]["elevenlabs_api"] = check_elevenlabs(os.environ.get("ELEVENLABS_API_KEY")) if (requests and not no_remote_apis) else {"skipped": no_remote_apis}
    # ElevenLabs TTS fetch + ffmpeg convert (local) - only if remote APIs enabled
    tts_info = {"performed": False}
    if not no_remote_apis and requests:
        tts_bytes, info = elevenlabs_tts(TTS_TEST_TEXT, os.environ.get("ELEVENLABS_API_KEY"), os.environ.get("ELEVENLABS_VOICE_ID"))
        tts_info["elevenlabs_fetch_info"] = info
        if tts_bytes:
            tts_info["performed"] = True
            tts_info["original_size"] = len(tts_bytes)
            # convert to mulaw
            try:
                mulaw = ffmpeg_to_mulaw_bytes(tts_bytes, input_format_hint=None, out_rate=8000)
                tts_info["mulaw_len"] = len(mulaw)
                tts_info["mulaw_silence_detected"] = is_mostly_silence_mulaw(mulaw)
                # store first bytes sample as base64 for inspection
                tts_info["mulaw_prefix_b64"] = base64.b64encode(mulaw[:12]).decode("ascii")
            except Exception as e:
                tts_info["mulaw_error"] = str(e)
        else:
            tts_info["error_fetching_tts"] = info
    else:
        tts_info["skipped"] = True
    report["checks"]["tts_local_test"] = tts_info
    # WebSocket simulation (simulate Twilio connecting to your streaming server)
    ws_report = {"performed": False}
    if websockets:
        # Normalize WS URL
        ws_target = stream_url
        if ws_target.startswith("https://"): ws_target = "wss://" + ws_target[len("https://"):]
        if ws_target.startswith("http://"): ws_target = "ws://" + ws_target[len("http://"):]
        if not ws_target.endswith("/ws"): ws_target = ws_target.rstrip("/") + "/ws"
        ws_report["ws_url"] = ws_target
        ws_report["performed"] = True
        try:
            res = asyncio.run(ws_simulate(ws_target, timeout=WS_TIMEOUT))
            ws_report["result"] = res
            # if server sent a first outbound media chunk, decode and analyze for silence
            if res.get("first_media_b64"):
                try:
                    raw = base64.b64decode(res["first_media_b64"])
                    ws_report["first_raw_len"] = len(raw)
                    ws_report["first_raw_silence"] = is_mostly_silence_mulaw(raw)
                    ws_report["first_raw_prefix_b64"] = res["first_media_b64"][:24]
                except Exception as e:
                    ws_report["decode_error"] = str(e)
        except Exception as e:
            ws_report["error"] = str(e)
    else:
        ws_report["error"] = "websockets not installed"
    report["checks"]["websocket_simulation"] = ws_report
    # Quick heuristics and verdicts
    verdicts = []
    # If ElevenLabs mulaw found and is mostly silence -> immediate red flag
    if report["checks"]["tts_local_test"].get("performed"):
        if report["checks"]["tts_local_test"].get("mulaw_silence_detected"):
            verdicts.append({"issue":"TTS->mulaw produced mostly silence (ElevenLabs->ffmpeg conversion)", "severity":"high", "hint":"ffmpeg conversion flags or input format detection may be wrong; check content-type and mp3/wav bytes returned by ElevenLabs"})
    # If websocket simulation shows first outbound raw is silence -> red flag
    ws_res = report["checks"]["websocket_simulation"]
    if ws_res.get("performed") and ws_res.get("result"):
        r = ws_res["result"]
        if r.get("first_media_b64"):
            if r.get("first_media_raw") is None:
                # already added decode results, but we compute above; fallback:
                pass
        if isinstance(r, dict):
            # check if first_media_b64 decoded to silence (we set first_raw_silence earlier)
            if ws_res.get("first_raw_silence"):
                verdicts.append({"issue":"Server outbound media chunk appears to be silent (mu-law 0xFF)", "severity":"high", "hint":"Server is streaming silent bytes. Investigate that TTS->mp3 was converted properly and that ffmpeg uses correct input format and sample rate."})
            # check media_count and mark_count
            if r.get("media_count",0) == 0:
                verdicts.append({"issue":"Server did not emit outbound media in WS simulation","severity":"medium","hint":"Server may have skipped TTS path; check server logs for TTS conversion errors."})
            if r.get("mark_count",0) == 0:
                verdicts.append({"issue":"No mark events received from server in simulation","severity":"medium","hint":"Server may have attempted to send mark but Twilio handshake not simulated exactly, or server didn't send mark."})
    # If no major verdicts so far, say nominal
    if not verdicts:
        verdicts.append({"ok":"no immediate audio-silence faults detected; if caller still silent, check Twilio console media delivery or network path."})
    report["verdicts"] = verdicts
    # Save
    with open(REPORT_FILE,"w",encoding="utf-8") as f:
        json.dump(report,f,indent=2)
    log.info("Diagnostic complete. Saved to %s", REPORT_FILE)
    # Print short summary
    for v in verdicts:
        if "issue" in v:
            log.warning("ISSUE: %s | severity: %s | hint: %s", v["issue"], v["severity"], v["hint"])
        else:
            log.info("OK: %s", v.get("ok"))
    return report

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-remote-apis", action="store_true", help="skip calls to ElevenLabs/OpenAI APIs")
    args = parser.parse_args()
    main(no_remote_apis=args.no_remote_apis)
