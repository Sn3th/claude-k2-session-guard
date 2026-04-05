# claude-k2-session-guard

**Session behavioral profiler for Claude Code.** Distinguishes legitimate developers from CLI wrapper abuse by analyzing session data and git activity that Claude Code already collects.

Built because Anthropic applies server-side context management that silently degrades Claude Code sessions for all users, including legitimate developers, as a blunt response to CLI wrapper abuse. This tool proves there's a better way.

**Related:** [anthropics/claude-code#42542](https://github.com/anthropics/claude-code/issues/42542)

---

## The Problem

Claude Code sessions degrade mid-session in ways that aren't explained by the advertised context limits. Agents lose coherence, forget earlier context, and produce lower quality output as sessions progress. This affects paying Max plan subscribers doing real development work.

The degradation appears to be a response to tools that wrap the Claude Code CLI as a free API proxy, costing Anthropic compute they never intended to subsidise. Rather than detecting and blocking the abusers, the mitigation is applied broadly, punishing the developer doing real work alongside the wrapper farm extracting free compute.

**We think there's a better way.** The behavioral difference between a real developer and a CLI wrapper is massive. This tool measures it.

---

## What It Does

Reads Claude Code's own session data (`~/.claude/projects/`) and git history to produce:

1. **Session profiles:** behavioral fingerprint per session (tool diversity, idle time, prompt entropy, cadence variance, corrections, questions, scaffold detection)
2. **Session scores:** 0-100 legitimacy score per session (DEVELOPER, LIKELY_DEVELOPER, AMBIGUOUS, LIKELY_WRAPPER, WRAPPER)
3. **Account-level report:** combines all sessions + git activity into one fingerprint (commit rate, daily variance, auto:dev ratio, co-authors, peak hours, single-turn rate)

---

## Quick Start

```bash
git clone https://github.com/Sn3th/claude-k2-session-guard.git
cd claude-k2-session-guard

# Profile your last 10 sessions
python3 session_guard.py

# Score summary of your last 50 sessions
python3 session_guard.py --score --limit 50

# Full account report with git analysis
python3 session_guard.py --account --limit 10000 --min-messages 1

# Specify git repos explicitly
python3 session_guard.py --account --limit 10000 --min-messages 1 \
  --git-repos ~/my-project ~/other-project

# JSON output for piping
python3 session_guard.py --account --limit 10000 --min-messages 1 --json
```

No dependencies. Python 3.8+ standard library only.

---

## The Wrapper Ecosystem: Know Your Enemy

Research into actual wrapper tools (OpenClaw, claude-code-provider, horselock, AIDotNet, kiyo) reveals three distinct families:

### Family 1: Direct HTTP Impersonators

Tools like `horselock/claude-code-proxy` and `AIDotNet/ClaudeCodeProxy` bypass Claude Code entirely. They reuse subscription credentials and call Anthropic's API directly, spoofing Claude Code headers. **No Claude Code process runs. No session JSONL is created.**

### Family 2: CLI Subprocess Shims

Tools like `sonami-tech/claude-code-provider` spawn `claude -p --no-session-persistence --tools ""` as a subprocess per API request. Each request is one Claude invocation. Tools are disabled. Sessions are not persisted. **JSONL may exist but is typically empty or suppressed.**

### Family 3: Base-URL Rerouters

Tools like `kiyo-e/claude-code-proxy` reroute Claude Code's upstream traffic to a different model provider. The local Claude Code client still runs normally. **Sessions look mostly legitimate because a real user may still be driving them.**

### What This Means for Detection

- JSONL-based detection catches Family 2 (when persistence isn't disabled) and validates legitimacy for everyone else
- Families 1 and 2 without persistence require **server-side detection** (Anthropic's responsibility)
- Family 3 is not inherently abusive. The behavioral signals still determine legitimacy.
- The account-level pattern (developer sessions driving automated sessions, correlated with git commits) catches abuse across all families that leave any trace

---

## Session Scoring: What It Measures

### Primary signals (high weight, hardest to fake)

| Signal | Developer | Wrapper |
|--------|-----------|---------|
| **Idle ratio** | 30-80% (thinking, reading, doing other things) | <5% (relentless throughput) |
| **Cadence variance** | High (bursts + pauses) | Near zero (uniform timing) |
| **Prompt entropy** | High (diverse vocabulary, natural language) | Low or zero (templated) |
| **Corrections** | Regular ("no", "wait", "actually", "wrong") | Zero (bots don't make mistakes) |

### Secondary signals (lower weight)

| Signal | Developer | Wrapper |
|--------|-----------|---------|
| Tool diversity | Many tools (Read, Edit, Bash, Grep, Glob...) | 1-2 tools or zero |
| Questions | Regular (interactive problem-solving) | Zero |
| Slash commands | Occasional (/help, /clear, /compact) | Zero |
| File path diversity | Wide (real project, many files) | Narrow or none |
| Session duration | 5 min to 8+ hours | Seconds to low minutes |

### Wrapper artifact signals

| Signal | What It Catches |
|--------|----------------|
| **Scaffold detection** | Machine-generated prompt patterns: `<tool_result>` XML, serialized JSON schemas, injected system prefixes |
| **Single-turn sessions** | One prompt in, one response out. Per-request subprocess shim signature. |

### Human signal floor

If **any** strong human behavioral signal is present (idle time, cadence variance, entropy, corrections, questions), the score floors at 55-65 regardless of other factors. A developer doing a quick 2-minute fix with clear human timing is still a developer.

---

## Account-Level Scoring: The Kill Signal

Individual session scoring catches obvious cases. But the real discriminator is the **account-level pattern**:

### What a legitimate developer looks like

```
Sessions:  6,830 total
  Developer-side:   1,805 (26%)
  Automated:        2,459 (36%)
  Ratio:            1.4:1 auto:dev

Git: 1,824 commits across 4 repos in 64 days
  28.5 commits/day
  Peak hours: 10:00-15:00 (working hours)
  Daily variance: 16.3 (busy days + quiet days)
  3.7 sessions per commit (code + review + fingerprint)

ACCOUNT SCORE: 100/100 LEGITIMATE_DEVELOPER
```

The automated sessions (scoring as "wrapper") are **code review hooks and CI tooling** that fire on every commit. The ratio is proportional to development activity. More commits means more automated sessions. This is one person, working on real products, producing real code.

### What a CLI wrapper looks like

```
Sessions:  50,000 total
  Developer-side:       0 (0%)
  Automated:       50,000 (100%)
  Ratio:            N/A (no developer sessions)

Git: 0 commits across 0 repos

ACCOUNT SCORE: 0/100 ABUSE
```

Zero developer sessions. Zero git commits. Zero human behavioral signals. Just raw automated API throughput with no development output.

### The 5 Kill Signals

Based on analysis of actual wrapper codebases:

1. **Zero developer sessions + zero git + sustained volume.** The account-level backbone. Legitimate automation is downstream of development. Wrapper automation is independent of it.

2. **Sustained absence of ALL human signals** across 50+ sessions. Idle near zero, cadence uniform, entropy low, no corrections, no questions, no slash commands. Any one alone is noisy. All together are impossible to fake.

3. **No real Claude tool loops** on accounts claiming coding usage. Wrappers often disable tools (`--tools ""`) or fake them via prompt injection. Zero `tool_use`/`tool_result` events across many sessions is a strong negative signal.

4. **One-shot session fragmentation.** Extremely high new-session rate, seconds-long durations, one user turn per session. This is exactly what a per-request subprocess shim creates.

5. **Machine-converted prompt scaffolding.** Serialized tool schemas in plain text, XML-ish `<tool_result>` blocks, identical preambles across sessions. Catches the "API translation layer stuffed into one prompt" family.

### The Proportionality Signal

Legitimate automation is **proportional to and driven by** developer sessions:
- Post-commit code review hooks fire per commit
- Skill indexers fire per doc change
- One-shot workers fire per ticket
- The auto:dev ratio stays between 1:1 and 5:1

Wrapper abuse is **independent of** any developer activity:
- Thousands of sessions with no developer sessions driving them
- No git commits correlating with session activity
- No proportionality. Just volume.

---

## Honest Caveat

> Some wrapper-abuse tools will not show up in `~/.claude/projects/` at all.
> Direct HTTP impersonators bypass Claude Code completely, and some CLI shims
> explicitly use `--no-session-persistence`. Session JSONL is therefore strong
> evidence when present, but not a complete sensor for every abuse path.
>
> Server-side detection (request patterns, header analysis, session spawn rates
> at the API level) is needed to catch wrappers that never touch the local CLI.
> This tool proves the detection model works. Anthropic has the data to apply
> it server-side.

---

## Example Output

### Single session profile

```
======================================================================
  Session: 3470fb59-5db2-426f-8440-4046b0bba6bc
  Duration: 3h 9m  |  Events: 857
======================================================================

  Messages
    Human:     296
    Assistant: 445
    Ratio:     1.5:1 (assistant:human)
    Avg len:   161.7 chars

  Tools
    Total calls:  252
    Unique tools: 19
    Diversity:    0.075
    Top tools:
      Bash                 ################################# 76
      Read                 ############################# 67
      Edit                 ###################### 49
      Grep                 ######### 20

  Timing
    Avg gap:     38.6s
    Median gap:  7.9s
    Max gap:     2418.6s
    Idle ratio:  0.814
    Cadence var: 167.2

  Interaction
    Corrections:    1 (0.003 rate)
    Questions:      9 (0.03 rate)
    Prompt entropy: 8.245

  SCORE: 100/100 DEVELOPER
```

### Account report

```
======================================================================
  ACCOUNT-LEVEL DEVELOPMENT FINGERPRINT
======================================================================

  Sessions (6,830 total)
    DEVELOPER:            725
    LIKELY_DEVELOPER:    1,080
    AMBIGUOUS:             800
    INSUFFICIENT_DATA:   1,766
    LIKELY_WRAPPER:      1,917
    WRAPPER:               542
    Developer-side:      1,805 (26%)
    Automated:           2,459 (36%)
    Ratio:               1.4:1 auto:dev

  Git Activity (1,824 commits, 64 days, 4 repos)
    Commits/day:     28.5
    Daily variance:  16.3
    Peak hours:      10:00-15:00

  ACCOUNT SCORE: 100/100 LEGITIMATE_DEVELOPER
```

---

## Why This Exists

I'm a developer. Production runs on the Claude API where it belongs. Development happens on Claude Code where it belongs. That's the intended split. You don't develop on the API at Opus token prices, that's madness.

When my Claude Code sessions started losing coherence mid-session, I dug in and found server-side context management silently degrading the sessions I'm paying for. The degradation exists because wrapper tools abuse the CLI as a free API proxy. I get it, that's a real problem costing Anthropic real money. But the response punishes everyone equally, and developers who are using the product exactly as intended shouldn't be collateral damage.

This tool is a constructive proposal: **detect the abuse pattern, not the product usage.** The behavioral fingerprint of a real developer is fundamentally different from a CLI wrapper. Measure it, score it, act on the score. Don't degrade everyone's sessions because some people are taking the piss.

---

## Data Sources

All data comes from files Claude Code already creates locally:

| Source | Location | What it contains |
|--------|----------|-----------------|
| Session JSONL | `~/.claude/projects/<project>/<session>.jsonl` | Full conversation: messages, tool calls, timestamps |
| Git history | Any git repo on the machine | Commits, authors, co-authors, timestamps |

No data is sent anywhere. Everything runs locally. The tool reads your files and prints a report.

---

## Contributing

Issues and PRs welcome. If you find new behavioral signals that improve detection, if Anthropic changes the session data format, or if new wrapper tools emerge with different signatures, open an issue.

If you're from Anthropic: this tool exists because blanket degradation hurts your best customers. We'd rather you used targeted detection like this. Happy to discuss.

---

## License

MIT. See [LICENSE](LICENSE).

---

**Authors:** [@Sn3th](https://github.com/Sn3th), K2
