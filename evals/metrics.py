#!/usr/bin/env python3
"""AJ voice-call metrics, computed from the ElevenLabs call history.

WHY / WHAT
----------
"How good is she" was being answered by eyeballing transcripts. The industry
voice-agent frameworks (Hamming, Cekura, EVA-Bench) converge on two layers,
which map to the dashboard's two columns -- personal-agent context, so the
call-center metrics (containment, transfer, WER) are dropped:

  RESPONSIVENESS & RELIABILITY (does it work, is it fast)
    latency p50/p95 (target p50<1.5s, p95<5s)   tool fail %
    silence / dead-air events                   task success

  CONVERSATION & USAGE (how it feels, what it's for)
    friction / reprompt rate     interruptions / barge-in
    turns per call               call duration
    call volume                  tool usage mix

Watchdog calls (the 20s "where am I" bridge checks) are excluded so they do not
flatter the numbers. Read-only against the EL API; never touches the agent.
Writes state/aj-metrics.json for the dashboard to render.

usage: metrics.py [--hours 72] [--print]   # default 3 days
"""
import argparse
import contextlib
import json
import os
import sys
import time
import urllib.request

AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "")
OUT = (os.environ.get("AJ_OUT") or os.path.expanduser("~/.aj-voice-agent")) + "/metrics.json"
FAIL_MARK = ("did not come back", "lookups are failing", "could not get that",
             "lookups are struggling", "lookups are broken", "could not compute")
REPEAT_MARK = ("no, i mean", "no i mean", "no, no", "what i mean", "i said",
               "stop sending", "read it out", "i approve", "not what i")


def _key():
    return os.environ["ELEVENLABS_API_KEY"]


def _get(url, key):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"xi-api-key": key}), timeout=25))


def _p(vals, q):
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def _ttf(turn):
    """Real end-to-end response latency: time from the user going silent to the
    agent's first audio byte (ElevenLabs' own turn metric, a float in seconds)."""
    m = ((turn.get("conversation_turn_metrics") or {}).get("metrics")) or {}
    v = m.get("convai_ttf_audio_since_silence")
    return v.get("elapsed_time") if v else None


RESET_FILE = (os.environ.get("AJ_OUT") or os.path.expanduser("~/.aj-voice-agent")) + "/metrics-reset"


def _reset_epoch():
    """A hard floor set by --reset, so the numbers start from a moment the user
    chose rather than only from a rolling window. The window still applies as a
    ceiling: whichever cutoff is LATER wins, so an old reset never resurrects
    calls the window has already dropped."""
    try:
        return float(open(RESET_FILE).read().strip())
    except Exception:
        return 0.0


# PRODUCTION CALLS ONLY (2026-08-02, the user). These cards claim to measure AJ's
# call quality, but every harness run is also a "conversation" on this agent --
# and the harness vastly outnumbers him: a 700-stub scan on 08-02 found just 7
# real calls, so ~99% of what these numbers described was my own test traffic.
# "calls: 144" over 72h was reporting the suite, labelled as his call quality.
#
# The discriminator is exact, not heuristic:
#   real call    initiation_source="twilio", phone_call.external_number=owner's, text_only=False
#   harness run  initiation_source="unknown", phone_call=null, text_only=True
# Both DIRECTIONS count: external_number is his number whether he called her
# (inbound) or she called him (outbound). Filtering on direction would drop half
# of production.
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")   # the owner's own cell, E.164

def compute(hours=72):
    key = _key()
    cutoff = max(time.time() - hours * 3600, _reset_epoch())
    # PAGINATE, OR A LONG WINDOW SILENTLY MEASURES A SHORT ONE. page_size caps
    # at 100 and the scheduled health pings fire every 30 minutes, so a single
    # page reaches back barely 27 hours. Asking for 5 days without paging would
    # have returned one day of data labelled "last 120h" -- wrong in the
    # direction that looks fine, which is the worst kind.
    convs, cursor = [], None
    while True:
        url = (f"https://api.elevenlabs.io/v1/convai/conversations"
               f"?agent_id={AGENT}&page_size=100")
        if cursor:
            url += f"&cursor={cursor}"
        page = _get(url, key)
        batch = page.get("conversations") or []
        convs.extend(batch)
        oldest = min((c.get("start_time_unix_secs") or 0) for c in batch) if batch else 0
        if not page.get("has_more") or not batch or oldest < cutoff or len(convs) >= 1200:
            break
        cursor = page.get("next_cursor")
        if not cursor:
            break

    base_gaps, tool_gaps = [], []       # real ttf latency floats, split by tool use
    tcount, tfail = {}, {}
    user_turns = repeats = 0
    silence = interruptions = 0
    turns_per, durations = [], []
    real = 0
    resolved = clean_end = 0            # EL success eval / clean end_call hangup

    excluded_tests = 0
    for c in convs:
        if (c.get("start_time_unix_secs") or 0) < cutoff:
            continue
        # STAGE 1, on the STUB so it costs no API call: anything not placed
        # through Twilio is a harness run or a widget session, never a call.
        # Doing this before the detail fetch also cuts the per-conversation
        # requests from ~144 to the handful that are real.
        if c.get("conversation_initiation_source") != "twilio":
            excluded_tests += 1
            continue
        if (c.get("call_duration_secs") or 0) <= 25:        # watchdog
            continue
        real += 1
        durations.append(c.get("call_duration_secs") or 0)
        try:
            d = _get(f"https://api.elevenlabs.io/v1/convai/conversations/{c['conversation_id']}", key)
        except Exception:
            continue
        # STAGE 2: a Twilio call from someone who is not the user is still not HIS
        # call quality. external_number is the far end in both directions.
        pc = (d.get("metadata") or {}).get("phone_call") or {}
        if pc.get("external_number") != OWNER_NUMBER:
            excluded_tests += 1
            real -= 1
            durations.pop()
            continue
        tr = d.get("transcript") or []
        spoken = [t for t in tr if (t.get("message") or "").strip() or t.get("tool_calls")]
        turns_per.append(len(spoken))

        # EL's own call-success evaluation, not a keyword guess
        if ((d.get("analysis") or {}).get("call_successful")) == "success":
            resolved += 1
        # A CALL ENDING BECAUSE THE USER HUNG UP IS NOT A FAILURE.
        #
        # This counted a call clean only when the end_call TOOL fired, so every
        # call he ended himself scored against her. Measured over 72h: 61 ended
        # via end_call and 131 ended with him closing the line (client
        # disconnect 1005/1000, "ended by remote party") -- all perfectly
        # normal. The gauge read 31% and looked like a serious defect while
        # describing ordinary behaviour.
        #
        # Only three endings are actually wrong, and they are the ones worth a
        # red number: an abnormal socket drop, hitting the duration cap
        # mid-conversation, and dying of silence.
        _term = ((d.get("metadata") or {}).get("termination_reason") or "").lower()
        _bad = ("1006" in _term                      # abnormal close, no close frame
                or "exceeded maximum duration" in _term
                or "seconds of silence" in _term)
        if not _bad:
            clean_end += 1

        for i, t in enumerate(tr):
            role, msg = t.get("role"), (t.get("message") or "")
            if role == "user":
                if t.get("ignored_as_backchannel"):
                    continue
                user_turns += 1
                if any(m in msg.lower() for m in REPEAT_MARK):
                    repeats += 1
            for tc in (t.get("tool_calls") or []):
                n = tc.get("tool_name") or tc.get("name")
                tcount[n] = tcount.get(n, 0) + 1
            if role == "agent":
                if t.get("interrupted"):            # real barge-in flag from EL
                    interruptions += 1
                if any(m in msg.lower() for m in FAIL_MARK):
                    for j in range(i, -1, -1):
                        tcs = tr[j].get("tool_calls") or []
                        if tcs:
                            n = tcs[0].get("tool_name") or tcs[0].get("name")
                            tfail[n] = tfail.get(n, 0) + 1
                            break
                lat = _ttf(t)
                if lat is not None:
                    (tool_gaps if t.get("tool_calls") else base_gaps).append(lat)
                    # dead-air: slow to respond with NO tool reason = real silence
                    if lat >= 5 and not t.get("tool_calls"):
                        silence += 1

    total_tool = sum(tcount.values())
    total_fail = sum(tfail.values())
    worst, worst_pct = "", 0
    for n, cnt in tcount.items():
        if cnt >= 3:
            fp = 100 * tfail.get(n, 0) // cnt
            if fp > worst_pct:
                worst, worst_pct = n, fp
    top = sorted(tcount.items(), key=lambda x: -x[1])[:3]
    cn = tcount.get("check_notes", 0)

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_epoch": int(time.time()),
        "hours": hours,
        "reset_epoch": _reset_epoch(),
        "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff)),
        "calls": real,
        # Auditable on purpose: a filter that silently drops 99% of the rows
        # looks identical to a broken fetch. Publishing what it removed means a
        # wrong filter shows up as a number, not as a quiet zero.
        "calls_excluded_nonprod": excluded_tests,
        # --- responsiveness & reliability (real EL turn telemetry) ---
        "latency_base_p50": round(_p(base_gaps, .5), 1),
        "latency_base_p95": round(_p(base_gaps, .95), 1),
        "latency_tool_p50": round(_p(tool_gaps, .5), 1),
        "latency_tool_p95": round(_p(tool_gaps, .95), 1),
        "tool_fail_pct": (100 * total_fail // total_tool) if total_tool else 0,
        "tool_calls": total_tool,
        "tool_fails": total_fail,
        "check_notes_calls": cn,
        "check_notes_fails": tfail.get("check_notes", 0),
        "check_notes_fail_pct": (100 * tfail.get("check_notes", 0) // cn) if cn else 0,
        "silence_events": silence,
        "task_success_pct": (100 * resolved // real) if real else 0,
        "clean_end_pct": (100 * clean_end // real) if real else 0,
        # --- conversation & usage ---
        "friction_pct": (100 * repeats // user_turns) if user_turns else 0,
        "interruptions": interruptions,
        "avg_turns": round(sum(turns_per) / len(turns_per), 1) if turns_per else 0,
        "avg_duration": int(sum(durations) / len(durations)) if durations else 0,
        "top_tools": [{"name": n, "n": c} for n, c in top],
        "worst_tool": worst,
        "worst_tool_fail_pct": worst_pct,
    }


def agent_config(key):
    """Live agent identity, cached alongside the metrics.

    The dashboard used to HARDCODE "gemini-2.5-flash" in its header (2026-07-25),
    which is only right until someone swaps the model -- the same silent-drift bug
    class as the line-vs-char gauge. Captured here because this refresher already
    holds the API key and runs at most every 50 min, so the dashboard reads it
    from cache for free instead of calling ElevenLabs on every render.
    """
    try:
        a = _get(f"https://api.elevenlabs.io/v1/convai/agents/{AGENT}", key)
        cc = a.get("conversation_config", {})
        return {
            "agent_llm": (cc.get("agent", {}).get("prompt") or {}).get("llm"),
            "agent_tts_model": (cc.get("tts") or {}).get("model_id"),
            "agent_voice_id": (cc.get("tts") or {}).get("voice_id"),
        }
    except Exception:
        return {}


def consult_model():
    """The OTHER half of AJ's brain: the model the shim consults for real answers.

    AJ runs two models -- the ElevenLabs agent LLM (turn-taking, tool choice) and
    this consult brain. Showing only one reads as "AJ runs on X" when half the
    stack is something else. Read from source; no network.
    """
    try:
        with open(os.environ.get("CONSULT_SHIM", "/nonexistent"), encoding="utf-8") as _src:
            for line in _src:
                if line.startswith("CONSULT_MODEL"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)   # 3 days
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="start counting from now; older calls are ignored")
    a = ap.parse_args()
    if a.reset:
        os.makedirs(os.path.dirname(RESET_FILE), exist_ok=True)
        with open(RESET_FILE, "w") as fh:
            fh.write(str(time.time()))
        print(f"metrics reset: counting from {time.strftime('%Y-%m-%d %H:%M')}")
    try:
        m = compute(a.hours)
    except Exception as exc:
        m = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "generated_epoch": int(time.time()), "error": str(exc)[:120], "calls": 0}
    with contextlib.suppress(Exception):
        m.update(agent_config(_key()))
    m["consult_model"] = consult_model()
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as _out:
            json.dump(m, _out, indent=1)
    except OSError:
        pass
    if a.print or "error" in m:
        print(json.dumps(m, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
