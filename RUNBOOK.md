# Runbook

Operating notes for a deployment built from this repo's templates. The repo ships the agent config, the system prompt, the probe harness, and the eval suite; the tool server behind the 16 webhook tools is yours. Where a number appears below, it is a measured value from the reference deployment, quoted as a reference point, not a promise about yours.

## Symptoms

| symptom | first check | fix |
|---|---|---|
| gateway not answering (tool calls failing across the board) | confirm the tool server at your `{{TOOL_SERVER}}` URL is up and reachable; when it is down, every webhook tool fails at once, which is visible in the conversation history and in the tool-failure panel fed by `evals/metrics.py` | restart the tool server; the agent config needs no change, tools resume on the next call. If the config itself drifted, redeploy from `agent/agent-config.template.json` |
| tool timeouts rising | run `evals/metrics.py` and read tool latency by tool; the slow consult tool `check_notes` measures 10-22s end to end by design, so a slow `check_notes` alone is not a regression | size the deferral grace to the measured latency, never the other way around; if a fast-lane tool crossed its budget, suspect the server behind it before the agent |
| ASR mishears rising on the phone lane | reproduce on the lane where confusions live: place a real PSTN call, or run `node harness/talk-to-her-voice.js` with the failing phrase; the voice harness prints what ASR actually heard next to what was said | do not reach for prompt wording first; put absolute rules in the tool contract for confusable pairs (an acknowledgement never hangs up), then re-run the acknowledgement probes to confirm the rule holds |
| credit exhaustion | the daily model watch reports burn and days left; the account's own billing page is the ground truth | top up if usage is legitimate; otherwise check billed models against the allowlist and burn against the pinned baseline, and if an unapproved model is billing, restore the pinned fallback order and redeploy the config |
| backup brain firing more than usual | the governance panel counts backup activations; the reference window reads 17% of conversations, so treat a sustained climb from your own baseline as the signal | check primary model latency against the 4.0s cascade window, a slow or degraded primary hands every turn to the backup; confirm the fallback order is still the pinned one, not the vendor default |

## Alerting

What pages, and when:

- **Bridge watchdog, every 30 minutes.** The transport bridge is either up, or someone hears about it within the half hour.
- **Metrics sweep, every 6 hours.** Refreshes the production panels from the conversation history, with harness traffic excluded so the instrument never grades itself.
- **Daily model watch.** Credit burn and days left, every billed model against the allowlist, and goodbyes that did not hang up.

Alerts page loudly. Silence is never treated as evidence: a quiet dashboard means the watchdogs ran and found nothing, and the watchdogs themselves are cron jobs whose absence would surface as a stale panel, not a green one.

## Restore

- **Config is templated.** `agent/agent-config.template.json` and `agent/system-prompt.template.txt` are the source of truth; a live agent is those files with the `{{PLACEHOLDER}}` values filled. There is no config that exists only in the vendor dashboard.
- **Secrets live in the OS keychain, never the repo.** `.env.example` names every variable a deployment needs; the filled `.env` stays out of version control, and keys are held in the operating system's keychain and exported at launch. Nothing account-shaped is committed.
- **Roll back by reverting the commit and redeploying the agent config.** Because the config is in git, a bad change is undone the same way it shipped: revert, fill placeholders, redeploy. Then prove the rollback the same way any change is proven, run the suite; `python3 evals/suite.py --dry-run` confirms the tree loads and lists what would run, and a live pass gates the redeploy.
