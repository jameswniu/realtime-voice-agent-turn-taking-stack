<div align="center">

<img src="assets/hero.svg" alt="realtime-voice-agent-turn-taking-stack: a white-glove voice concierge on a real phone number" width="100%">

<sub><em>AJ is a designed persona; her voice is a generated ElevenLabs voice, not a recording of a person.</em></sub>

<br><br>

[![suite](https://img.shields.io/badge/suite-61%2F61_green-A83E32?style=for-the-badge&labelColor=2B1B12)](#the-white-glove-pass-stage-by-stage)
[![graded by code](https://img.shields.io/badge/graded_by_code-~54_of_55-C6A664?style=for-the-badge&labelColor=2B1B12)](#the-white-glove-pass-stage-by-stage)
[![judge](https://img.shields.io/badge/judge-structural_%2B_vibes-C6A664?style=for-the-badge&labelColor=2B1B12)](#tier-2-judgepy)
[![probe](https://img.shields.io/badge/probe-192_credits_measured-C6A664?style=for-the-badge&labelColor=2B1B12)](#cost)
[![license](https://img.shields.io/badge/license-AGPL--3.0-C6A664?style=for-the-badge&labelColor=2B1B12)](LICENSE)

<br>

<code>ring -> she picks up -> it is handled</code>

</div>

## Hear her work

Nine real phone calls, placed from the owner's own number in his cloned voice, her answering live on an 8 kHz line. Press play. This is what she does for the person who calls; the engineering behind each one is dissected on the [technical receipts page](https://jameswniu.github.io/realtime-voice-agent-turn-taking-stack/receipts.html).

### Stays on top of your day

**Sweeps what actually needs you** across email, iMessage, and WhatsApp, and names the quiet channel instead of skipping it (real names and vendors silenced here).

https://github.com/user-attachments/assets/b50091ec-dcb5-4fec-b1ac-61c6eef17a57

**Reads back your week** from the calendar, with days and times (the private event details are bleeped for this demo).

https://github.com/user-attachments/assets/1002049f-a215-49b6-8680-d1ad0cb75f99

**Wakes you with a real call**, schedules it on the spot and confirms the time back to you.

https://github.com/user-attachments/assets/7fae02f1-4d23-4e24-83cc-1e6d01cd581e

### Answers on the spot

**Splits the check** with tip, out loud, in one turn.

https://github.com/user-attachments/assets/b10d354b-a413-4e31-b0cf-e6491199ef25

**Gives you the weekend weather**, real forecast, spoken like a person.

https://github.com/user-attachments/assets/f46a8caf-ad34-4536-9a40-38d3ad31c3f3

**Tells you how far and how to get there**, drive, scooter, and walk times in one answer.

https://github.com/user-attachments/assets/4e13ab3a-0145-413e-b6c7-b952e4a1576b

### Keeps you company

**Tells a joke, and keeps the line open**; a "thanks" never hangs up, only a real close does.

https://github.com/user-attachments/assets/7d2f8f26-d82a-4251-aa7b-e37302606df8

**Checks in warmly**, no tool, no script, just a person on the line.

https://github.com/user-attachments/assets/9a5b26e6-31c4-4e64-8f96-ef2029c0e096

**Tells you what she can do**, in her own words, when you ask.

https://github.com/user-attachments/assets/c30b4da2-8dc8-4518-9999-4a054b83c341

**AJ is a phone-native voice companion.** She lives on a real phone number, answers when called, rings back when scheduled, checks a calendar and an inbox, tells jokes she fetched rather than composed, and hangs up only on plain words of leaving. This repo is her skeleton: the agent configuration, the probe harness that talks to her like a person, and the eval suite that decides, with code first and models last, whether she is actually good.

## Start here

Three claims, each backed by something you can run or read:

1. **The interesting part of a voice agent is not the prompt, it is the referee.** The prompt here is one file. The machinery that catches her being wrong, mishears, premature hangups, dead-end replies, tools that fired but did nothing, is nine files, and every one of them exists because a real call failed in a specific way.
2. **A test can be wrong, and half of these were.** The probe comments keep the reversals on the record: R57 flipped twice before the transcript data settled it. When a green suite disagrees with a failing live call, the suite is what gets indicted.
3. **Production numbers must exclude the instrument.** The metrics module filters to calls placed through Twilio from the owner's own number, because without that filter, ~99% of "call quality" was the harness grading itself.

The interactive version of the architecture lives at **[jameswniu.github.io/realtime-voice-agent-turn-taking-stack](https://jameswniu.github.io/realtime-voice-agent-turn-taking-stack/architecture.html)**, standalone, no dependencies, no build step.

<img src="assets/band-stack.svg" alt="Back of house: one number, two transports, sixteen tools" width="100%">

## Architecture

<img src="assets/architecture.svg" alt="System map: two transports land in one ElevenLabs agent, sixteen webhook tools reach one server, and a chrome band of harness plus suite referees every claim" width="100%">

```mermaid
%%{init: {'flowchart': {'curve': 'linear'}}}%%
flowchart TD
    PSTN["iPhone · Twilio PSTN · 8 kHz<br/>the hard lane"]
    WS["browser widget · WebSocket · 16 kHz<br/>what the harness drives"]

    subgraph AGENT["ElevenLabs Agents platform · the router, not the brain"]
        direction LR
        ASR["ASR<br/>scribe_realtime"] --> TURN["turn_v3<br/>eager · 3.0s"] --> LLM["LLM<br/>temp 0 · cascade 4.0s"] --> TTS["TTS<br/>eleven_flash_v2"]
        SYS["end_call · voicemail_detection<br/>the hang-up doctrine"]
    end

    subgraph TOOLS["Tool server · an OpenClaw gateway"]
        direction LR
        FAST["fast lane · 10 tools<br/>calendar · calls · music"]
        SLOW["slow lane<br/>check_notes · 10-22s"]
    end

    subgraph REF["The referee · every claim names its layer"]
        direction LR
        HARN["harness<br/>text + voice probes"]
        SUITE["suite<br/>61 cases · 1x then 3x"]
        GJ["grade + judge<br/>code, then two probes"]
        MET["metrics<br/>production only"]
    end

    PSTN --> AGENT
    WS --> AGENT
    AGENT -->|"16 webhook tools"| TOOLS
    AGENT -.->|"audited by"| REF
    TOOLS -.->|"audited by"| REF

    classDef caller fill:#FFFDF8,stroke:#6B5340,color:#2B1B12
    classDef agent fill:#FBF2E2,stroke:#C6A664,color:#2B1B12
    classDef tool fill:#FFFDF8,stroke:#6B5340,color:#2B1B12
    classDef ref fill:#F7E9CC,stroke:#A98B4F,color:#2B1B12
    class PSTN,WS caller
    class ASR,TURN,LLM,TTS,SYS agent
    class FAST,SLOW tool
    class HARN,SUITE,GJ,MET ref
    style AGENT fill:#FBF6EC,stroke:#C6A664,color:#5C4A3D
    style TOOLS fill:#FBF6EC,stroke:#6B5340,color:#5C4A3D
    style REF fill:#F7EFDD,stroke:#A98B4F,color:#5C4A3D
```

Two transports land in one agent, and the difference between them is the whole testing story. The WebSocket lane is crisp 16 kHz and drivable from code; the PSTN lane is 8 kHz telephony audio where "next" and "thanks" become the same word. Cheap layers come back green while the real call fails, so the layers are explicit, and every claim names the layer it was proven on.

The agent itself is a router, not a brain: turn-taking, tool choice, and personality at temperature 0. Anything requiring real knowledge goes through `check_notes`, a consult brain behind the tool server, measured 10-22s end to end, and the entire deferral apparatus (holding lines, grace windows, never asking the caller to repeat) exists because of that latency.

## The code, in three pieces

```
agent/    agent-config.template.json   the full ElevenLabs agent config, templated
          system-prompt.template.txt   the behavioral rulebook (~24k chars)
harness/  talk-to-her.js               text probe: real agent, real tools, no audio
          talk-to-her-voice.js         voice probe: real TTS -> ASR -> VAD, real-time pacing
evals/    cases.py                     61 probes mined verbatim from live calls
          suite.py                     runner: checkpointed, majority, 1x-then-escalate
          grade.py                     tier 0/1: pure code, decides ~54 of 55
          judge.py                     tier 2: structural probe + vibes probe
          sync.py                      ports the same cases to ElevenLabs' native suite
          metrics.py                   production-call metrics, harness traffic excluded
docs/     architecture.html            the interactive diagram (GitHub Pages)
```

One suite definition, two runners: `suite.py` runs everything locally against the live agent; `sync.py` ports the identical cases to ElevenLabs' hosted testing so a branch can be gated before it ever reaches a caller. If those drifted apart, a local green would say nothing about a cloud green, so they load from the same table.

## What ships here, and what does not

**Ships:** the agent config as a template, the system prompt with placeholders, both harnesses, the full eval suite, the architecture assets. Every hard-won behavioral rule, the hang-up doctrine, the deferral grammar, the acknowledgement handling, ships verbatim.

**Does not ship, by design:**

- **A voice.** `{{VOICE_ID}}` is any voice in your ElevenLabs account. The reference persona's voice stays with its owner, and so should yours: probe with a clone of *your own* voice, because macOS `say` cannot catch an ASR mishearing of *you*.
- **Keys, numbers, or endpoints.** Everything account-shaped is an environment variable or a `{{PLACEHOLDER}}`, see `.env.example`.
- **The tool server's internals.** The agent config needs 15 URLs; the reference implementation answers them with an [OpenClaw](https://openclaw.ai) gateway plus thin shims, but anything that serves HTTP works. `get_weather` needs no server at all, it goes straight to open-meteo.
- **Anyone's personal data.** Probe utterances are real production speech with identifying details swapped for neutral stand-ins; the swaps preserve the routing shape (a name you cannot place is still the signal to look up, whoever the name is).

What a stranger needs to run her: an ElevenLabs account with an agents-platform agent, an API key, a voice, and optionally a Twilio number for the PSTN lane. Reading the suite costs nothing; every live probe is a real billed conversation.

<img src="assets/band-harness.svg" alt="The harness: text probes for routing, voice for hearing" width="100%">

## The harness

**`talk-to-her.js`** opens a real conversation over the text WebSocket: real agent, real tools, no audio. Its one hard-earned rule: *a turn is over when her answer has landed, not when a clock says so.* The first version advanced on a blind 14-second timer, which made every latency identical (the harness was timing itself) and truncated slow tools *unevenly*, biasing any A/B in exactly the direction the A/B was trying to measure.

**`talk-to-her-voice.js`** speaks. Each phrase is rendered to PCM, streamed at real-time pace as microphone frames, with continuous silence between utterances so server-side VAD sees a live mic. It exists because the text harness structurally cannot catch a mishearing or a turn-taking fault, the class of bug that keeps surfacing on live calls. It prints what ASR *actually heard* next to what was said, and measures answer latency from end-of-speech, the way a human experiences it.

Both harnesses share the deferral rule: when her reply is a holding line ("let me have a squiz") and no tool has landed yet, the grace window extends, because the consult tool measured 22-23s on the voice path, and a harness that gives up at 20 reports an agent failure that is actually an instrument failure. Both files carry the same regex, kept in sync deliberately.

<img src="assets/band-suite.svg" alt="The white-glove pass: sixty-one cases, one phone number" width="100%">

## The white-glove pass, stage by stage

Latest full run of this exact tree: **61/61 pass** (2026-08-05, 1x with reds escalated to 3x). Two probes graded as instrument errors on the first attempt, the judge's codex lane refused to run outside a git repo, and passed on retest the same day after the one-line fix; both the bug and the fix are in the commit history, which is where this repo keeps its mistakes.

The PSTN lane keeps receipts too: **[a real 8 kHz call you can listen to](https://jameswniu.github.io/realtime-voice-agent-turn-taking-stack/receipts.html)**, turn-by-turn players with redacted audio, plus the [written transcript](evals/transcripts/2026-08-05-pstn-important-comms.md), replaying a production miss from that same morning word for word and showing the routing fix holding on the hard lane.

### cases.py, 61 probes, mined not invented

Utterances come verbatim from live calls, disfluencies kept: *"Hey. Hey, um, what events do I have for the next three weeks on my calendar?"*, *"Uh, h- how long by car?"*. Each carries a hand-set expected tool, grading against what the agent *did* is useless when production data already shows real routing errors. READ probes are safe; WRITE probes really change the volume and really create playlists, and hide behind `--allow-actions`.

The comments are the changelog of being wrong. R48 expected no tool until it became clear that searching the inbox for a flight is the *correct* move. R57 asserted "second thanks hangs up," shipped, and cut off a real call the same night when the transcriber wrote "thanks" for "next" twice, so now the absolute rule stands, verified 5/5: thanks never ends a call, and the closers that cannot be confused with "next" carry the ending.

### suite.py, the runner

- **Checkpointed after every test.** Written after this machine kernel-panicked mid-suite and a 22-of-56 run produced nothing. A crash now costs the one test in flight.
- **Majority across repeats.** All-must-pass goes red on day one over ordinary nondeterminism; a gate that reds on known-good behaviour gets muted, and a muted gate is the silent failure.
- **1x, then escalate the reds to 3x.** A probe still earns its verdict from three runs, so confidence matches a flat 3x sweep, but the ones that pass first time never pay for repeats. Never 2x: majority at two repeats is as strict as one, at twice the price.
- **Outcome probes.** Born from a live failure the suite scored green: she *said* she rescheduled a call, and nothing landed in cron. R58/R59 assert what exists afterwards, create real jobs at 23:58 so a failed cleanup leaves hours of margin, and delete only what they created.
- **Timeout evidence capture.** "Timed out" is a fact about the harness process, silent on whether she answered. The partial transcript is saved and parsed, because a reply in the partial output means the agent did its job and the instrument hung.

### grade.py, tier 0/1

Pure code: no model, no credits, no nondeterminism. Business tools are graded; call-control tools are filtered so a legitimate goodbye never fails a no-tool test, and `!end_call` exists as an explicit assertion because that same filter once made a premature hangup invisible to every probe. It earned trust by replaying the vendor's stored corpus and diffing verdicts: agreement everywhere except the runs where their judge was demonstrably wrong (it overrode a written "only the tool call decides" condition, and narrated a joke that never existed in the transcript).

It also scans every reply for spoken scaffolding, a reply that opens with a bare "thought" means the model's reasoning reached the phone line. Seen once in production, rate ~1 in 10; the suite already collects every reply, so the detector rides for free and stays silent on clean runs.

### tier 2: judge.py

Two probes asking two *different* questions, not two votes on one:

| probe | question | style |
|---|---|---|
| structural | did the reply satisfy the condition **as written**? | evidence-bound, must quote verbatim, fabricated quotes rejected |
| vibes | would this **annoy** a real caller? | the human ear, ignores the spec entirely |

Structural fail blocks. Structural pass + vibes fail = **FLAG**: technically correct, feels wrong, the most valuable signal the harness produces, and it usually means the *test* needs fixing, not the agent. The two probes run on different model families from each other and from the agent, so nobody grades their own family's output.

### metrics.py, production only

Reads the ElevenLabs conversation history and computes the two-layer dashboard (latency p50/p95 split by tool use, tool-failure rate, dead-air, friction, barge-ins, clean-end rate). The filter is exact, not heuristic: placed through Twilio, far-end number is the owner's, both directions. Exclusions are *published beside the numbers*, because a filter that silently drops 99% of rows looks identical to a broken fetch. A call the owner ends by hanging up counts as clean, only abnormal socket drops, duration-cap hits, and dying of silence are failures.

<img src="assets/band-findings.svg" alt="Findings: what measuring a companion taught" width="100%">

## What measuring it taught

1. **Verify the instrument before the agent.** Three separate defects reported as agent failures were harness bugs: the blind turn timer, the too-short deferral grace, the timeout that discarded its own evidence. When a measurement contradicts a documented fact, suspect the checker first.
2. **Costs are asymmetric, so rules are absolute.** A misheard "next" costs one joke; a misheard "thanks" costs the call. Every conditional version of the hang-up rule measured worse than the absolute one, a rule that needs state the model does not reliably track is a rule that fails.
3. **Structural vs. vibes disagreement is signal, not noise.** A single judge silently resolves the conflict by inventing criteria. Two probes with different questions surface it as a FLAG, and the flag usually points at the test.
4. **The prompt is not always the lever.** Measured three ways on one defect: adding a clause made it worse, rewording made it worse, reordering changed nothing. What worked: removing a contradiction, or moving the decision into code (the tool description, the grader, the runner).
5. **Grade the decision *and* the outcome.** Every tool-choice probe passed while a scheduling bug shipped nothing to cron. If a tool claims a side effect, some probe must look at the world afterwards.

## The numbers

| what | value | how it is known |
|---|---|---|
| latest full run | 61/61 pass | measured 2026-08-05, 1x + reds escalated to 3x |
| probes shipped | 61 READ + 7 WRITE | `evals/cases.py`, counted by `--dry-run` |
| decided by pure code | ~54 of 55 | grader coverage over the suite |
| tier-2 judge involvement | ~9% of runs | measured across stored runs |
| text probe cost | 192 credits avg | measured, n=25 conversations |
| voice probe cost | ~242 credits | 9.7 credits/sec × ~25s, from account billing |
| credit price | $1.66 = 10,000 credits | the account's own top-up dialog |

## Cost

A full 1x pass prints its own estimate before running: **~$1.97** at current definitions. The escalation shape is the economics: a flat 3x sweep costs ~35,000 credits (~$5.82); 1x with only the reds escalated costs ~14,600 (~$2.42) for the same per-verdict confidence. The cost constants in `suite.py` are measured, and the comment above them records the time a made-up constant under-reported by 13x, never restore one.

<img src="assets/band-run.svg" alt="Running it: bring your own keys, voice, and number" width="100%">

## Running it

```bash
git clone https://github.com/jameswniu/realtime-voice-agent-turn-taking-stack && cd realtime-voice-agent-turn-taking-stack
cp .env.example .env            # fill in your ElevenLabs key + agent id
cd harness && npm install       # one dependency: ws

# free: list the suite, no calls, no cost
python3 evals/suite.py --dry-run

# paid: real conversations against your live agent
python3 evals/suite.py                    # 1x, reds escalate to 3x automatically
python3 evals/suite.py --only R32-R39     # just the acknowledgement probes
node harness/talk-to-her.js "what time is it right now?"
node harness/talk-to-her-voice.js "tell me a joke"   # real ASR, real VAD
```

Setting her up from scratch: create an agents-platform agent in ElevenLabs, paste `agent/system-prompt.template.txt` (placeholders filled) as the prompt, apply the settings from `agent/agent-config.template.json` (ASR/turn/TTS blocks matter most), point the 15 webhook tools at your own `{{TOOL_SERVER}}`, pick a voice, and, for the full experience, attach a Twilio number. The suite runs identically against any agent that carries the same tool names.

## Read next

- **[docs/architecture.html](https://jameswniu.github.io/realtime-voice-agent-turn-taking-stack/architecture.html)**, the interactive stack diagram; every row carries the doctrine behind it
- **`agent/system-prompt.template.txt`**, the rulebook itself; the sections on acknowledgements, jokes, and call-ending are where most of the failures lived
- **`evals/cases.py`**, read the comments top to bottom and you have the project's honest history

## Roadmap

- A scripted PSTN probe lane: real 8 kHz calls are already placed and [kept as receipts](evals/transcripts/2026-08-05-pstn-important-comms.md); the missing piece is packaging that flow as a harness script
- A fixture-capture helper, so multi-turn cases replay recorded tool turns instead of reconstructions
- A minimal reference tool server, so the webhook side runs without an OpenClaw install

---

<sub>AGPL-3.0. The persona, the doctrine, and the mistakes are all part of the work, fork accordingly.</sub>
