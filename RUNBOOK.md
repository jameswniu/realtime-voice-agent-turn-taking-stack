# Runbook

Operating notes for a deployment of this stack. Everything here stays generic to what the repo ships: the templated agent config, the two harnesses, the eval suite, and the metrics module. Nothing below assumes infrastructure this repo does not contain.

## Symptoms

| Symptom | First check | Fix |
|---|---|---|
| Gateway not answering (calls ring out or the agent picks up with no tools) | The bridge watchdog log; it probes every 30 minutes, so the last entry brackets the outage. Then hit one webhook URL from `agent/agent-config.template.json` directly over HTTPS. | Restart whatever serves the webhook URLs (the reference deployment uses an OpenClaw gateway; yours is whatever you pointed `{{TOOL_SERVER}}` at). The platform side needs no restart; tools recover on the next call. |
| Tool timeouts rising | The metrics sweep output for per-tool latency. Know the baseline before judging: the slow consult tool `check_notes` measures 10-22s end to end by design, and the deferral protocol (holding lines, grace windows) exists for exactly that. | Fix the slow dependency behind the tool, not the timeout constant. If a grace window must move, size it to measured tool latency, never to a guess; a grace shorter than the tool it waits for reports agent failures that are instrument failures. |
| ASR mishears rising on the phone lane | Run the voice probe (`harness/talk-to-her-voice.js`); it prints what ASR actually heard next to what was said. Confirm on the PSTN lane, not the WebSocket lane: 8 kHz is where "next" and "thanks" collapse into the same word. | Keep destructive decisions out of confusable word classes: an acknowledgement never hangs up, only plain words of leaving do. Re-run the acknowledgement probes after any change, then verify on a real PSTN call. |
| Credit exhaustion | The daily model watch reading: burn and days left. Then compare LLM credit burn against the pinned baseline on the governance panel (1.82x is the currently flagged state in the reference deployment). | Top up, but treat elevated burn as a routing regression until shown otherwise: check which models billed and how often the fallback fired before topping up a second time. |
| Backup brain firing more than usual | The model governance panel. The reference window shows the backup firing in 11 of 64 conversations (17%); a sustained climb above your own baseline is the signal, not any single call. | Check the primary model's status with the vendor. Keep the pinned fallback order and the allowlist as they are; widening the allowlist to quiet the alarm trades a paging alert for silent unapproved billing. |

## Alerting

What pages, and when:

- **Bridge watchdog, every 30 minutes.** Pages when the tool bridge stops answering. This is the outer bound on how long a dead gateway can go unnoticed.
- **Metrics sweep, every 6 hours.** Recomputes the dashboard panels from production calls (harness traffic excluded) and pages when a reading crosses the SLO thresholds in the README table.
- **Daily model watch, once a day.** Pages on three things: credit burn and days left, any billed model outside the allowlist, and goodbyes that did not hang up.
- **Silence is never treated as evidence.** A watchdog that stops reporting is itself an alert condition; check the watchdog before trusting a quiet dashboard.

## Restore

- **Config is templated.** `agent/agent-config.template.json` and `agent/system-prompt.template.txt` are the source of truth; the live agent is a rendered copy of them with `{{PLACEHOLDER}}` values filled.
- **Secrets live in the OS keychain, never the repo.** `.env` is gitignored and holds only local pointers; nothing account-shaped is committed, so a checkout is always safe to share and a restore never round-trips a credential through git.
- **Roll back by reverting the commit and redeploying the agent config.** A bad behavior change is undone the same way it shipped: revert, re-render the templates, apply to the agent. There is no separate rollback lane to drift out of date.
- **A rollback rides the same gate as a rollout.** Run `python3 evals/suite.py --dry-run` to confirm the tree is intact, then a live pass if the change touched behavior; behavior changes ship only through the suite, in both directions.
