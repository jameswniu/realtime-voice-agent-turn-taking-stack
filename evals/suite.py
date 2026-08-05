#!/usr/bin/env python3
"""Run the whole AJ regression suite OFFLINE. No ElevenLabs test platform.

Replaces the cloud suite ( ~60-70k credits per pull, opaque
judge). Roughly $0.03 of ElevenLabs credits per probe conversation and $0 of
judging: tier 1 is code, tier 2 runs on subscription models.

    suite.py --dry-run          # replay stored transcripts, no calls
    suite.py --repeat 5         # live run, 5 repeats per probe
    suite.py --compare          # diff the newest run vs the baseline
    suite.py --baseline         # also save this run as the baseline

WHAT RUNS WHERE
  caller   the probe TEXT itself -- no model, the scenarios are pre-written
  agent    the live EL agent (prod Gemini) over the text WebSocket
  tier 0/1 grade.py, pure code, decides ~54 of 55 tests
  tier 2   judge.py, two probes (Claude structural + GPT vibes), ~9% of runs

MAJORITY, NOT ALL-MUST-PASS. Aggregation across repeats is majority, matching
the existing cloud runner. The red-team pass proved all-must-pass goes red on
day one over ordinary agent nondeterminism (R17 3/5, R31 4/5 in the stored
corpus, both TOOL tests with no judge involved). A gate that reds on known-good
behaviour gets muted, and a muted gate is the silent failure.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.environ.get("AJ_OUT") or f"{HOME}/.aj-voice-agent/runs"
# A fresh clone has no data dir, and the checkpoint writer assumes one exists
# -- found by running this exact tree, when test 1 passed and the save crashed.
os.makedirs(OUTDIR, exist_ok=True)
BASELINE = f"{OUTDIR}/local-baseline.json"
TALK = os.path.join(SCRIPTS, "..", "harness", "talk-to-her.js")
TALK_VOICE = os.path.join(SCRIPTS, "..", "harness", "talk-to-her-voice.js")

# VOICE-ONLY PROBES (2026-08-01). Almost the whole suite runs on text: a paired
# text-vs-voice comparison agreed 6/6 on routing, both mishear probes (ASR
# CORRECTED the mangled speech rather than mishearing it), and the call-close
# probe. Voice costs 5x per conversation, so re-running agreeing tests in it
# buys nothing.
# These three are the exception, and the reason is structural rather than a
# preference: their assertion IS the audio. "can you slow down", "talk a bit
# slower", "say that again" are about delivery rate and turn-taking, which a
# text WebSocket cannot produce or observe at all -- graded on text they are
# vacuous passes. They run through the owner's clone (PROBE_VOICE_ID) so the ASR
# hears a real human timbre, per the owner's standing rule.
VOICE_PROBES = {"R40", "R41", "R42"}

# DO NOT put a probe that EXPECTS a call-control tool in VOICE_PROBES. The voice
# harness speaks its phrases then goes quiet, and AJ correctly hangs up on a dead
# line -- so end_call fires from the harness's own silence and would be scored as
# the probe passing. R57 (second thanks -> end_call) therefore runs on TEXT,
# where there is no silence to trigger it. Enforced, not just documented.
assert not (VOICE_PROBES & {"R57", "R60", "R61"}), (
    "a call-control-expecting probe in VOICE_PROBES would false-pass on silence")
CKPT = f"{OUTDIR}/local-checkpoint.json"

# CHECKPOINTING (2026-07-31). The runner used to hold every result in RAM and
# write once at the end, so anything that killed the process destroyed the whole
# run. That is not hypothetical here: this machine kernel-panicked SIX times in two days,
# three of them mid-suite, and a 22-of-56 run cost ~2,000 credits and
# produced nothing (memory exhaustion kills WindowServer, a display pipeline
# blocked its restart, watchdogd panics). We cannot remove either half of the
# workload, so instead make a panic CHEAP: write
# after every test, resume where we stopped. A panic now costs the one test in
# flight (~35 credits) instead of the entire run.


# OUTCOME PROBES (2026-08-02). R58/R59 assert what EXISTS AFTERWARDS, not just
# which tool fired. Motivating failure: she told the user she had rescheduled a
# call and cancelled the old one; no cron add, no registry write, no cancel was
# ever logged -- and the suite scored green, because every probe graded the
# DECISION and none graded the OUTCOME. The grader stays transcript-only; the
# runner owns side-effect checks because it owns the machine they land on.
#
# SAFETY: these probes create REAL wake-up jobs on the REAL cron. Each repeat
# snapshots wake-* job IDs before the turn, diffs after, and deletes only what
# it created -- never the user's own jobs, even same-named ones. The probe time is
# 23:58 so a failed cleanup leaves hours of margin; if any created job survives
# deletion, the runner prints a loud manual-action line rather than exiting
# quietly green.

def _cron_wake_jobs():
    """{job_id: name} for wake-* jobs. Empty dict on any failure -- callers
    treat that as 'cannot verify', never as 'verified absent'."""
    try:
        r = subprocess.run(["openclaw", "cron", "list", "--json"],
                           capture_output=True, text=True, timeout=120)
        d = json.loads(r.stdout)
        jobs = d if isinstance(d, list) else d.get("jobs", [])
        return {j["id"]: j.get("name", "") for j in jobs
                if str(j.get("name", "")).startswith("wake-")}
    except Exception:
        return {}


def _registry_wake_names():
    try:
        with open(f"{HOME}/.openclaw/state/wake-jobs.json") as f:
            return {j.get("name", "") for j in json.load(f)}
    except Exception:
        return set()


def _outcome_settle(pre_ids, want_new, timeout_s=90):
    """Poll cron until the wanted state holds or time runs out.

    The shim creates the job on a BACKGROUND thread and the gateway lane has
    been measured to 44s under load, so one immediate look would fail honest
    runs. want_new=True waits for a new wake-* job to appear (R58);
    want_new=False waits for the settled state to show none (R59: the schedule
    from the context turn must have been cancelled again).
    """
    deadline = time.time() + timeout_s
    new = {}
    while time.time() < deadline:
        now = _cron_wake_jobs()
        new = {i: n for i, n in now.items() if i not in pre_ids}
        if want_new and new:
            return new
        if not want_new and not new and now is not None:
            # for the no-new case, insist the state holds across two looks:
            # the add thread may simply not have landed YET
            time.sleep(8)
            now2 = _cron_wake_jobs()
            new = {i: n for i, n in now2.items() if i not in pre_ids}
            if not new:
                return {}
        time.sleep(5)
    return new


def _outcome_cleanup(created, pre_registry):
    """Delete every job this probe created; verify; scream if any survive."""
    for jid in created:
        for _ in range(3):
            with contextlib.suppress(Exception):
                subprocess.run(["openclaw", "cron", "delete", jid],
                               capture_output=True, text=True, timeout=120)
            if jid not in _cron_wake_jobs():
                break
    survivors = {i: n for i, n in _cron_wake_jobs().items() if i in created}
    if survivors:
        print(f"\n  *** OUTCOME CLEANUP FAILED: {survivors} still scheduled and "
              f"WILL RING -- delete manually with `openclaw cron delete <id>` ***")
    # prune registry entries the probe added, so list_calls does not tell the user
    # about a call the runner already deleted
    try:
        reg_path = f"{HOME}/.openclaw/state/wake-jobs.json"
        with open(reg_path) as f:
            entries = json.load(f)
        keep = [e for e in entries if e.get("name", "") in pre_registry]
        if len(keep) != len(entries):
            with open(reg_path + ".tmp", "w") as f:
                json.dump(keep, f)
            os.replace(reg_path + ".tmp", reg_path)
    except Exception:
        pass
    return not survivors


def _outcome_schedule(pre_ids, pre_reg):
    """R58: a new wake-* job must actually EXIST in cron and the registry."""
    new = _outcome_settle(pre_ids, want_new=True)
    in_reg = bool(_registry_wake_names() - pre_reg)
    ok = bool(new) and in_reg
    detail = (f"outcome: cron={'created ' + ','.join(new.values()) if new else 'NO JOB CREATED'}"
              f", registry={'written' if in_reg else 'NOT WRITTEN'}")
    clean = _outcome_cleanup(new, pre_reg)
    return ok, detail + ("" if clean else " [CLEANUP FAILED]")


def _outcome_cancel(pre_ids, pre_reg):
    """R59: after schedule-then-cancel, NO new wake-* job may remain."""
    remaining = _outcome_settle(pre_ids, want_new=False)
    ok = not remaining
    detail = ("outcome: cancelled cleanly, nothing remains" if ok else
              f"outcome: job(s) SURVIVED the cancel: {','.join(remaining.values())}")
    clean = _outcome_cleanup(remaining, pre_reg)
    return ok, detail + ("" if clean else " [CLEANUP FAILED]")


OUTCOME_PROBES = {"R58": _outcome_schedule, "R59": _outcome_cancel}


# COST, MEASURED (2026-08-03). This used to print `n * repeat * 0.003`, a
# hardcoded guess that predated any measurement -- it under-reported by ~13x and
# I quoted it to the user as fact ("$0.18 for a full run") until he showed me the
# billing page. Never restore a made-up constant here.
#
# Sources of truth: the user's ElevenLabs top-up dialog reads $1.66 = 10,000
# credits. Charge per conversation pulled from the account itself
# (metadata.cost): 25 real probe conversations averaged 192 credits; real calls
# bill ~9.7 credits/second, so a ~25s voice probe is ~242.
CREDIT_USD = 1.66 / 10000        # $0.000166 per credit
TEXT_CREDITS = 192               # measured, n=25
VOICE_CREDITS = 242              # measured, 9.7 cr/s x ~25s


def _est_cost(n_tests, repeat):
    """Dollars for a run. VOICE_PROBES cost ~25% more than a text probe."""
    n_voice = len(VOICE_PROBES) * repeat
    n_text = max(n_tests * repeat - n_voice, 0)
    return (n_text * TEXT_CREDITS + n_voice * VOICE_CREDITS) * CREDIT_USD


def _suite_id(tests):
    """Fingerprint of WHICH tests these are, so an edited suite invalidates the
    checkpoint instead of silently mixing old answers with new definitions."""
    return hashlib.sha256("|".join(t["id"] for t in tests).encode()).hexdigest()[:12]


def save_ckpt(results, repeat, suite_id):
    """Atomic: write a temp file then rename. A panic can land mid-write, and a
    half-written JSON checkpoint is worse than none -- os.replace guarantees the
    file on disk is always either the previous complete state or the new one."""
    tmp = CKPT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"repeat": repeat, "suite": suite_id, "results": results}, f)
    os.replace(tmp, CKPT)


def load_ckpt(repeat, suite_id):
    """Return prior results only if they belong to THIS suite and repeat count."""
    try:
        with open(CKPT) as f:
            ck = json.load(f)
    except Exception:
        return {}
    if ck.get("suite") != suite_id or ck.get("repeat") != repeat:
        return {}
    return ck.get("results") or {}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


grade_mod = _load("aj_grade", f"{SCRIPTS}/grade.py")
judge_mod = _load("aj_judge", f"{SCRIPTS}/judge.py")


def load_suite():
    """The 55 probes, from the SAME source the cloud suite syncs from.

    One definition of the tests, two runners. If these drifted apart, a local
    green would say nothing about a cloud green.
    """
    sync = _load("aj_sync", f"{SCRIPTS}/sync.py")
    probes, text_rules = sync.load_probes()
    tests = []
    for pid, _kind, exp, ctx, utt in probes:
        # text_rules maps probe id -> whether it is graded on WORDS not tools.
        # to_test takes that per-probe boolean, not the whole table; passing the
        # dict made every probe truthy and sent tool probes down the text branch.
        payload = sync.to_test(pid, exp, ctx, utt, bool(text_rules.get(pid)))
        tests.append({
            "id": pid,
            # WORD-GRADED means this probe has a TEXT_RULE -- its assertion is
            # about what she SAYS. Do NOT infer this from type=="llm": 25 probes
            # are llm-typed but only 2 have word rules, and routing all 25 to the
            # tier-2 judge made every no-tool probe pay a judge call (2026-08-01,
            # slowed the suite ~60% and then crashed it when `claude` was not on
            # launchd's PATH).
            "word_graded": bool(text_rules.get(pid)),
            "name": payload["name"],
            "type": payload["type"],
            "expected_tool": None if payload["type"] == "llm" else exp,
            "utterance": utt,
            "context": list(ctx),
            "chat_history": payload["chat_history"],
            "condition": payload.get("success_condition", ""),
        })
    return tests


def _parse_transcript(stdout):
    """Turn harness stdout into aj-grade's shape. Shared by the normal path and
    the timeout path so a partial transcript is read EXACTLY as a whole one."""
    lines = [re.sub(r"^\[\d+\.\d+s\]\s*", "", ln) for ln in (stdout or "").splitlines()]
    last_me = max((i for i, line in enumerate(lines)
                   if line.startswith("ME:") or line.startswith("ME (speaking):")),
                  default=-1)
    calls, reply = [], ""
    for line in lines[last_me + 1:]:
        if line.startswith("HER: "):
            # voice appends a latency note; it is not part of what she said
            reply = re.sub(r"\s*\(\+[\d.]+s after I stopped\)\s*$", "", line[5:]).strip()
        elif "[tool:" in line:
            name = line.split("[tool:", 1)[1].split("->", 1)[0].strip()
            calls.append({"tool_name": name, "tool_has_been_called": True})
    # Her replies to the CONTEXT turns, kept apart from the graded turn. Tool
    # grading must never see them (the R12 trap: crediting the graded turn with
    # the context turns' tools), but the R20 judge NEEDS the earlier joke text
    # to check "the joke" was relayed rather than replaced -- without it,
    # "same joke" is unverifiable and the condition can only be written loose.
    prior = [re.sub(r"\s*\(\+[\d.]+s after I stopped\)\s*$", "", ln[5:]).strip()
             for ln in lines[:max(last_me, 0)] if ln.startswith("HER: ")]
    return [{"role": "agent", "message": reply, "tool_calls": calls,
             "prior_replies": prior}]


def run_probe(test, timeout=120):
    """One live turn against the agent. Returns turns in aj-grade's shape."""
    turns = test["context"] + [test["utterance"]]
    voice = test["id"] in VOICE_PROBES
    harness = TALK_VOICE if voice else TALK
    env = {**os.environ, "PROBE_TTS": "clone"} if voice else None
    try:
        # Voice turns run in real time (TTS + ASR + her audio), so they need a
        # far longer wall-clock budget than a text turn.
        r = subprocess.run(["node", harness] + turns, env=env,
                           capture_output=True, text=True,
                           timeout=300 if voice else timeout)
        stdout = r.stdout
    except subprocess.TimeoutExpired as e:
        # EVIDENCE CAPTURE ON TIMEOUT (2026-08-02). This used to `return None,
        # "probe timed out"` and drop e.stdout on the floor -- discarding the one
        # piece of evidence that says WHICH SIDE failed. On 08-01 five probes
        # errored this way and attributing them cost 29 replay conversations,
        # every one of which passed. subprocess.run kills the child and drains
        # the pipes into the exception, so the partial transcript is right there.
        #
        # "timed out" is a fact about the HARNESS PROCESS not exiting. It is
        # silent on whether she answered -- and that distinction is the whole
        # question. A reply in the partial output means the agent did its job
        # and the harness hung after; no reply means the turn never landed.
        partial = e.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        got = _parse_transcript(partial)
        reply = got[0]["message"] if got else ""
        calls = [c["tool_name"] for c in (got[0]["tool_calls"] if got else [])]
        os.makedirs(f"{OUTDIR}/timeouts", exist_ok=True)
        path = f"{OUTDIR}/timeouts/{test['id']}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        with open(path, "w") as f:
            f.write(partial)
        if reply:
            return None, (f'timed out AFTER she replied ("{reply[:70]}"'
                          f'{", tools=" + ",".join(calls) if calls else ""}) '
                          f'-- harness did not exit; see {path}')
        return None, f"timed out with NO reply captured -- see {path}"
    # GRADE ONLY THE LAST TURN. talk-to-her.js asks every turn in sequence and
    # prints ME:/HER:/[tool:] for all of them, so a naive sweep of the whole
    # transcript credits the GRADED turn with tools the CONTEXT turns called.
    # That is not a small error: it failed 14 of 17 tests on the first full run,
    # R12 being the textbook case (context "how far away is X" legitimately calls
    # get_distance, then the graded "how long by car?" must call nothing -- and
    # the context call got counted against it). sync.py documents this
    # exact trap, and the judge prompt itself says "grade only [new] turns"; the
    # runner was the one place that ignored it.
    # The two harnesses print DIFFERENT shapes and the parser must handle both,
    # or voice probes silently mis-grade: text emits bare "ME:"/"HER: " while
    # voice prefixes every line with a timestamp -- "[5.2s] ME (speaking): ..",
    # "[7.2s] HER: .. (+1.0s after I stopped)". Matching only the text shape
    # finds no "ME:" line at all, so last_me stays -1 and the whole transcript
    # gets swept -- crediting the graded turn with the CONTEXT turns' tools,
    # the exact bug that failed 14 of 17 tests on the first full run.
    return _parse_transcript(stdout), None


def grade_one(test, turns):
    """Tier 0/1, then tier 2 only if tier 1 cannot decide.

    WORD-GRADED PROBES SKIP TIER 1 ENTIRELY (bug fixed 2026-08-01). R20 and R22
    assert something about what she SAYS, not which tool she calls -- R22's real
    defect was answering "I can certainly ask AJ... what should I ask HIM",
    treating her own name as a third person. load_suite gives those probes
    expected_tool=None because there is no tool to expect, but tier 1 reads None
    as "she may call NOTHING", so calling check_notes -- which is literally how
    she reaches her backend -- was scored a failure on a test that was never about
    tools. Same bug failed R20 for telling the joke its own SHAPE_C carve-out
    calls correct. Both were false failures in the 08-01 baseline.
    """
    if test.get("word_graded"):
        reply = " ".join((t.get("message") or "") for t in turns).strip()
        prior = [r for t in turns for r in (t.get("prior_replies") or []) if r]
        facts = (f"New business tool calls by the agent: "
                 f"{len(grade_mod.new_business_calls(turns))} (verified by code). "
                 f"Tool choice alone does not decide this test; apply the "
                 f"success condition as written.")
        if prior:
            facts += (" What the agent said EARLIER in this same conversation "
                      "(context turns, verbatim): " +
                      " | ".join(f'"{r}"' for r in prior))
        j = judge_mod.judge(test["condition"], test["utterance"], reply, facts)
        return {"verdict": j["outcome"], "detail": j, "tier": 2}

    verdict, detail = grade_mod.grade(test["expected_tool"], turns)
    if verdict != "undecided":
        return {"verdict": verdict, "detail": detail, "tier": 1}
    reply = " ".join((t.get("message") or "") for t in turns).strip()
    facts = (f"New business tool calls by the agent: "
             f"{len(grade_mod.new_business_calls(turns))} (verified by code).")
    j = judge_mod.judge(test["condition"], test["utterance"], reply, facts)
    return {"verdict": j["outcome"], "detail": j, "tier": 2}


def aggregate(verdicts):
    """Majority across repeats; ERROR is an instrument fault and never a fail."""
    real = [v for v in verdicts if v != "error"]
    if not real:
        return "error"
    passes = sum(1 for v in real if v == "pass")
    if passes * 2 > len(real):
        return "pass"
    return "flag" if "flag" in real else "fail"


def _only_match(spec, t):
    """Does this test match --only?

    RANGES AND LISTS, not just a substring (2026-08-04). The sibling cloud
    runner documents `--only R32-R37,R42`, and the same syntax is what makes a
    targeted re-run affordable: the eight acknowledgement probes are R32-R39 and
    no substring selects exactly those (`R3` also drags in R30 and R31). Plain
    substrings still work, so every existing invocation is unchanged.
    """
    hay = (t["name"] + t["id"]).lower()
    tid = str(t["id"]).upper()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([A-Za-z]+)(\d+)-([A-Za-z]*)(\d+)", part)
        if m:
            pre, lo, pre2, hi = m.group(1).upper(), int(m.group(2)), m.group(3).upper(), int(m.group(4))
            if pre2 and pre2 != pre:
                continue
            mm = re.fullmatch(r"([A-Za-z]+)(\d+)", tid)
            if mm and mm.group(1).upper() == pre and lo <= int(mm.group(2)) <= hi:
                return True
            continue
        if part.lower() in hay:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    # DEFAULTS ARE 1 + ESCALATE 3 (the user, 2026-08-03, standing). A flat x3 sweep
    # costs ~35,000 EL credits (~$5.82); 1x with only the reds escalated costs
    # ~14,600 (~$2.42) for the SAME confidence, because a probe still earns its
    # verdict from 3 runs. Never default to a flat repeat again, and never use
    # x2: aggregate() takes a strict majority, so x2 is as strict as x1 at twice
    # the price. Enforced by ~/.claude/hooks/block-suite-flat-repeat.sh.
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would run; makes no calls and costs nothing")
    ap.add_argument("--baseline", action="store_true", help="save as baseline")
    ap.add_argument("--compare", action="store_true", help="diff vs baseline")
    ap.add_argument("--only", help="substring filter on test name/id")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any checkpoint and start the run from test 1")
    ap.add_argument("--escalate", type=int, default=3, metavar="N",
                    help="after the main pass, re-run ONLY non-passing probes at N "
                         "repeats and take that as their verdict (use with --repeat 1)")
    a = ap.parse_args()

    tests = load_suite()
    if a.only:
        tests = [t for t in tests if _only_match(a.only, t)]
    print(f"{len(tests)} tests x {a.repeat} repeats "
          f"= {len(tests) * a.repeat} conversations "
          f"(~${_est_cost(len(tests), a.repeat):.2f} of ElevenLabs credits)")

    if a.dry_run:
        for t in tests:
            kind = ("tool:" + t["expected_tool"]) if t["expected_tool"] else "no-tool"
            print(f"  {t['id']:6} {kind:22} {t['utterance'][:50]}")
        return 0

    # A FILTERED RUN NEVER TOUCHES THE CHECKPOINT (bug caught by executing the
    # regression test, 2026-07-31). All runs share one checkpoint path, so a
    # short `--only` run was overwriting an interrupted FULL run's state test by
    # test -- destroying the resume data this feature exists to protect, long
    # before the cleanup guard could matter. Checkpointing is for the 2-hour
    # full run; a filtered run is short enough to just redo.
    checkpointing = not a.only
    suite_id = _suite_id(tests)
    results = load_ckpt(a.repeat, suite_id) if (checkpointing and not a.fresh) else {}
    if results:
        done = [t["id"] for t in tests if t["id"] in results]
        tests = [t for t in tests if t["id"] not in results]
        print(f"resuming: {len(done)} test(s) already done, {len(tests)} to go "
              f"(--fresh to ignore the checkpoint)")
    t0 = time.time()
    for i, t in enumerate(tests, 1):
        verdicts, details, replies = [], [], []
        for _ in range(a.repeat):
            outcome_fn = OUTCOME_PROBES.get(t["id"])
            if outcome_fn:
                pre_ids = set(_cron_wake_jobs())
                pre_reg = _registry_wake_names()
            turns, err = run_probe(t)
            if err:
                verdicts.append("error")
                details.append(err)
                if outcome_fn:
                    # she may have scheduled before the harness died; never
                    # leave a probe-created job armed because the probe errored
                    _outcome_cleanup(
                        {i: n for i, n in _cron_wake_jobs().items()
                         if i not in pre_ids}, pre_reg)
                continue
            g = grade_one(t, turns)
            verdict, detail = g["verdict"], g["detail"]
            if outcome_fn:
                # The tool firing is necessary, not sufficient: the motivating
                # defect was schedule_call firing and NOTHING landing in cron.
                # Outcome failure downgrades a tool-pass to fail; it never
                # upgrades a tool-fail, and both facts land in the detail.
                ok, odetail = outcome_fn(pre_ids, pre_reg)
                detail = f"{detail} | {odetail}"
                if verdict == "pass" and not ok:
                    verdict = "fail"
            verdicts.append(verdict)
            details.append(detail)
            replies.append(" ".join((x.get("message") or "") for x in turns).strip())
        final = aggregate(verdicts)
        # Quality is reported BESIDE the verdict, never instead of it: a
        # dead-end is a conversational defect, not a routing bug, and folding
        # them together would make the tool-choice metric mean two things.
        distinct, total = grade_mod.variety(replies)
        midrun = any("joke" in c.lower() for c in t["context"])
        results[t["id"]] = {"name": t["name"], "verdict": final,
                            "runs": verdicts, "details": details,
                            # WHAT SHE ACTUALLY SAID (2026-08-04). Without this the
                            # run file recorded "dead_ends: 3" and nothing else, so
                            # there was no way to tell whether she offered "anything
                            # else I can help with?" -- which ENDS a joke run -- or
                            # merely said "no worries". Those need opposite fixes,
                            # and acting on the merged number once already pushed
                            # her toward the forbidden phrase and had to be
                            # reverted. Truncated: enough to judge the beat, not a
                            # transcript.
                            "replies": [r[:400] for r in replies],
                            "quality": {
                                # mid-joke-run inverts the rule -- see dead_ends()
                                "dead_ends": sum(
                                    grade_mod.dead_ends(r, mid_joke_run=midrun)
                                    for r in replies),
                                # ...and WHICH branch fired, so the two opposite
                                # defects stop being one number.
                                "dead_end_kinds": [
                                    k for k in (grade_mod.dead_end_kind(r, mid_joke_run=midrun)
                                                for r in replies) if k],
                                "mid_joke_run": midrun,
                                "distinct_replies": distinct,
                                "repeats": total,
                                # Reasoning spoken aloud. Kept as the offending
                                # TOKENS, not a count, so a hit names what leaked.
                                "leaks": [x for x in
                                          (grade_mod.leaked_reasoning(r) for r in replies)
                                          if x],
                            }}
        if checkpointing:
            save_ckpt(results, a.repeat, suite_id)   # survive a panic mid-run
        mark = {"pass": "ok", "fail": "FAIL", "flag": "FLAG", "error": "ERR"}[final]
        print(f"  [{i:2}/{len(tests)}] {mark:4} {t['name'][:44]:46} {verdicts}")

    # Acknowledgement probes are where her NEVER DEAD-END and NEVER SAY THE
    # SAME OFFER TWICE rules live, and where the suite was previously blind.
    # ESCALATION (2026-08-02, the user: standing default). Run everything once, then
    # give ONLY the reds their 3 chances. A probe still earns its verdict from 3
    # runs, so confidence matches a full x3 sweep, but the ~57 that pass first
    # time never pay for repeats: 60 + 3-per-red instead of 180 conversations.
    #
    # This is not an optimisation, it is the correct shape. aggregate() takes a
    # STRICT majority, so at 1 repeat a single stochastic miss reds a test
    # outright -- and those are routinely false: R09 failed its only 1x run and
    # passed 3/3 minutes later; R37 and R48 both went 2/3 fail then 5/5 clean.
    # Escalating makes a 1x red mean "unproven" rather than "broken".
    if a.escalate and not a.dry_run:
        reds = [t for t in load_suite()
                if a.only is None or _only_match(a.only, t)]
        reds = [t for t in reds
                if results.get(t["id"], {}).get("verdict") in ("fail", "flag", "error")]
        if reds:
            print(f"\nescalating {len(reds)} non-passing probe(s) to {a.escalate} repeats: "
                  f"{', '.join(t['id'] for t in reds)}")
            for t in reds:
                before = results[t["id"]]["verdict"]
                verdicts, details, replies = [], [], []
                for _ in range(a.escalate):
                    turns, err = run_probe(t)
                    if err:
                        verdicts.append("error")
                        details.append(err)
                        continue
                    g = grade_one(t, turns)
                    verdicts.append(g["verdict"])
                    details.append(g["detail"])
                    replies.append(" ".join((x.get("message") or "") for x in turns).strip())
                final = aggregate(verdicts)
                distinct, total = grade_mod.variety(replies)
                # mid_joke_run WAS MISSING HERE. The first-pass scorer above
                # passes it; this escalation path did not, so a re-run ack probe
                # was graded by the GENERAL rule while its first pass used the
                # inverted mid-run rule -- the same probe scored two ways
                # depending on whether it escalated. Passed now so both paths
                # agree.
                midrun = any("joke" in c.lower() for c in t["context"])
                results[t["id"]].update(
                    verdict=final, runs=verdicts, details=details, escalated_from=before,
                    replies=[r[:400] for r in replies],
                    quality={"dead_ends": sum(grade_mod.dead_ends(r, mid_joke_run=midrun)
                                              for r in replies),
                             "dead_end_kinds": [
                                 k for k in (grade_mod.dead_end_kind(r, mid_joke_run=midrun)
                                             for r in replies) if k],
                             "mid_joke_run": midrun,
                             "distinct_replies": distinct, "repeats": total,
                             "leaks": [x for x in
                                       (grade_mod.leaked_reasoning(r) for r in replies)
                                       if x]})
                mark = {"pass": "ok", "fail": "FAIL", "flag": "FLAG", "error": "ERR"}[final]
                print(f"    {t['id']:5} {before} -> {mark:4} {verdicts}")
            if checkpointing:
                save_ckpt(results, a.repeat, suite_id)

    # .get, not [], on purpose: a resumed run mixes results written by THIS
    # version with results loaded from a checkpoint an OLDER version wrote,
    # which have no "quality" key at all. Indexing crashed the summary and
    # would have thrown away a completed 2-hour run at the last line.
    ack = {k: v for k, v in results.items() if k in grade_mod.ACK_PROBES}
    if ack:
        q = {k: (v.get("quality") or {}) for k, v in ack.items()}
        de = sum(x.get("dead_ends", 0) for x in q.values())
        tot = sum(x.get("repeats", 0) for x in q.values())
        worst = sorted((k for k in q if q[k].get("dead_ends")),
                       key=lambda k: -q[k]["dead_ends"])
        # SPLIT BY KIND, because the two are opposite defects: 'offer' ends the
        # joke run he was working through, 'bare-closer' merely hands him
        # silence. Reporting only the total is what let a fix aimed at one of
        # them get judged against a number driven by the other.
        kinds = {}
        for v in q.values():
            for k in (v.get("dead_end_kinds") or []):
                kinds[k] = kinds.get(k, 0) + 1
        by_kind = ("  [" + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) + "]"
                   if kinds else "")
        print(f"\nacknowledgement quality: {de}/{tot} replies DEAD-ENDED"
              + (f" ({', '.join(worst)})" if worst else "") + by_kind)

    # REASONING LEAKS ARE SCANNED ACROSS EVERY PROBE, not just the ack subset:
    # she can speak her scaffolding on any turn, and the one real sighting
    # (a real PSTN call opening with a bare "thought") was on a recall
    # answer. Rate is about 1 in 10 with no known mechanism, which is why this is
    # a free standing detector rather than something anyone hunts by hand.
    leaks = {k: (v.get("quality") or {}).get("leaks") or [] for k, v in results.items()}
    n_leak = sum(len(v) for v in leaks.values())
    if n_leak:
        who = ", ".join(f"{k}:{'/'.join(sorted(set(v)))}"
                        for k, v in leaks.items() if v)
        print(f"\nSPOKEN REASONING LEAK: {n_leak} repl{'y' if n_leak == 1 else 'ies'} "
              f"began with scaffolding ({who})")
    # NO LINE ON A CLEAN RUN. A detector for something seen once in ten calls
    # would otherwise print "none" on every single run forever, which is a
    # non-finding dressed as a result. Silence is the pass.

    counts = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"\n{time.time() - t0:.0f}s  " +
          "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = f"{OUTDIR}/local-run-{stamp}.json"
    payload = {"stamp": stamp, "repeat": a.repeat, "results": results}
    with open(out, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"saved: {out}")
    # Run completed, so the checkpoint has done its job. Leaving it would make
    # the NEXT run think it was already finished and skip every test.
    # ONLY clear a checkpoint belonging to THIS suite (bug caught by executing
    # the resume test, 2026-07-31): `--only` filters the test list first, so a
    # small filtered run has a different suite id -- and used to delete the
    # checkpoint of an interrupted FULL run on its way out, destroying exactly
    # the resume state this feature exists to protect.
    if checkpointing:
        try:
            with open(CKPT) as f:
                if json.load(f).get("suite") == suite_id:
                    os.remove(CKPT)
        except (FileNotFoundError, ValueError):
            pass

    if a.baseline:
        with open(BASELINE, "w") as f:
            json.dump(payload, f, indent=1)
        print(f"baseline updated: {BASELINE}")

    if a.compare and os.path.isfile(BASELINE):
        with open(BASELINE) as f:
            base = json.load(f)["results"]
        moved = [(k, base[k]["verdict"], v["verdict"])
                 for k, v in results.items()
                 if k in base and base[k]["verdict"] != v["verdict"]]
        if moved:
            print(f"\n{len(moved)} test(s) MOVED vs baseline:")
            for k, was, now in moved:
                print(f"  {k}: {was} -> {now}")
        else:
            print("\nno movement vs baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
