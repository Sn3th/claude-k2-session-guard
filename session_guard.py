#!/usr/bin/env python3
"""
claude-k2-session-guard — Session Behavioral Profiler for Claude Code

Analyzes Claude Code session data to produce a behavioral fingerprint that
distinguishes legitimate developers from CLI wrapper abuse (OpenClaw, etc).

The profiles expose clear behavioral differences:
  - Developers have human typing cadence, tool diversity, idle time, corrections
  - Wrappers have instant responses, low diversity, zero idle, no corrections

Usage:
    python3 session_guard.py                    # Profile all recent sessions
    python3 session_guard.py --session <id>     # Profile a specific session
    python3 session_guard.py --baseline         # Generate baseline from all sessions
    python3 session_guard.py --score            # Score sessions against baseline
    python3 session_guard.py --json             # Output as JSON

Authors: @Sn3th, K2
License: MIT
Related: https://github.com/anthropics/claude-code/issues/42542
"""

import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_DEBUG_DIR = os.path.expanduser("~/.claude/debug")

# Correction patterns — things real humans type
CORRECTION_PATTERNS = re.compile(
    r"\b(no|nope|wait|actually|wrong|oops|sorry|undo|revert|never\s?mind|scratch\s+that|not\s+that|stop)\b",
    re.IGNORECASE,
)

# Prompt scaffold patterns — machine-generated wrapper artifacts
SCAFFOLD_PATTERNS = re.compile(
    r"(<tool_result>|<tool_use>|<function_call>|<result>[\s\S]*?</result>|"
    r"You are Claude Code, Anthropic's official CLI|"
    r"\{\"role\":\s*\"(user|assistant)\"|"
    r"\"messages\":\s*\[|\"model\":\s*\"claude)",
    re.IGNORECASE,
)

# Slash commands — interactive CLI features wrappers never use
SLASH_COMMAND_PATTERN = re.compile(r"^/(help|clear|compact|status|review|init|login|doctor|config|memory)\b")

# ---------------------------------------------------------------------------
# JSONL Session Parser
# ---------------------------------------------------------------------------

def find_session_files(project_dir=None, limit=50):
    """Find JSONL session files, newest first."""
    if project_dir:
        search_dir = project_dir
    else:
        search_dir = CLAUDE_PROJECTS_DIR

    jsonl_files = []
    for root, dirs, files in os.walk(search_dir):
        for f in files:
            if f.endswith(".jsonl"):
                full_path = os.path.join(root, f)
                jsonl_files.append((os.path.getmtime(full_path), full_path))

    jsonl_files.sort(reverse=True)
    return [path for _, path in jsonl_files[:limit]]


def parse_session(filepath):
    """Parse a JSONL session file into structured events."""
    events = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        return []
    return events


# ---------------------------------------------------------------------------
# Signal Extraction
# ---------------------------------------------------------------------------

def extract_signals(events):
    """Extract behavioral signals from session events."""
    signals = {
        "session_id": None,
        "event_count": len(events),
        "duration_seconds": 0,
        "first_timestamp": None,
        "last_timestamp": None,

        # Message signals
        "human_messages": 0,
        "assistant_messages": 0,
        "system_messages": 0,
        "human_message_lengths": [],
        "human_message_texts": [],

        # Tool signals
        "tool_calls": Counter(),
        "tool_call_count": 0,
        "tool_call_timestamps": [],
        "unique_tools": set(),

        # File signals
        "files_read": set(),
        "files_written": set(),
        "files_edited": set(),
        "unique_file_paths": set(),

        # Timing signals
        "message_gaps": [],  # seconds between consecutive human messages
        "human_timestamps": [],

        # Interaction signals
        "corrections": 0,
        "slash_commands": 0,
        "questions": 0,  # messages ending with ?
        "scaffold_hits": 0,  # machine-generated wrapper artifacts in prompts

        # Turn structure signals
        "single_turn_sessions": False,  # exactly 1 user turn (subprocess shim signature)

        # Session signals
        "model": None,
        "permission_mode": None,
    }

    timestamps = []

    for event in events:
        ts_str = event.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                timestamps.append(ts)
            except (ValueError, TypeError):
                pass

        event_type = event.get("type", "")
        session_id = event.get("sessionId")
        if session_id and not signals["session_id"]:
            signals["session_id"] = session_id

        # Message structure: event.message.role + event.message.content
        msg = event.get("message", {})
        msg_role = msg.get("role") if isinstance(msg, dict) else None

        # Message counting — JSONL uses type="user"/"assistant"/"system"
        if event_type == "user" or msg_role == "user":
            signals["human_messages"] += 1
            content = _extract_text_from_message(msg) if isinstance(msg, dict) else _extract_text(event)
            if content:
                signals["human_message_lengths"].append(len(content))
                signals["human_message_texts"].append(content)

                # Corrections
                if CORRECTION_PATTERNS.search(content):
                    signals["corrections"] += 1

                # Slash commands
                if SLASH_COMMAND_PATTERN.match(content.strip()):
                    signals["slash_commands"] += 1

                # Questions
                if content.strip().endswith("?"):
                    signals["questions"] += 1

                # Scaffold artifacts (wrapper signature)
                if SCAFFOLD_PATTERNS.search(content):
                    signals["scaffold_hits"] += 1

            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    signals["human_timestamps"].append(ts)
                except (ValueError, TypeError):
                    pass

        elif event_type == "assistant" or msg_role == "assistant":
            signals["assistant_messages"] += 1

            # Tool call extraction from assistant message content blocks
            msg_content = msg.get("content", []) if isinstance(msg, dict) else []
            if isinstance(msg_content, list):
                for block in msg_content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        if tool_name:
                            signals["tool_calls"][tool_name] += 1
                            signals["tool_call_count"] += 1
                            signals["unique_tools"].add(tool_name)

                            tool_input = block.get("input", {})
                            if isinstance(tool_input, dict):
                                for key in ("file_path", "path", "filePath", "command"):
                                    fpath = tool_input.get(key)
                                    if fpath and isinstance(fpath, str) and "/" in fpath:
                                        signals["unique_file_paths"].add(fpath)

                                if tool_name in ("Read",):
                                    fpath = tool_input.get("file_path", "")
                                    if fpath:
                                        signals["files_read"].add(fpath)
                                elif tool_name in ("Write",):
                                    fpath = tool_input.get("file_path", "")
                                    if fpath:
                                        signals["files_written"].add(fpath)
                                elif tool_name in ("Edit",):
                                    fpath = tool_input.get("file_path", "")
                                    if fpath:
                                        signals["files_edited"].add(fpath)

        elif event_type == "system" or msg_role == "system":
            signals["system_messages"] += 1

    # Compute timing
    if timestamps:
        timestamps.sort()
        signals["first_timestamp"] = timestamps[0].isoformat()
        signals["last_timestamp"] = timestamps[-1].isoformat()
        signals["duration_seconds"] = (timestamps[-1] - timestamps[0]).total_seconds()

    # Compute message gaps (idle time between human messages)
    human_ts = sorted(signals["human_timestamps"])
    for i in range(1, len(human_ts)):
        gap = (human_ts[i] - human_ts[i - 1]).total_seconds()
        signals["message_gaps"].append(gap)

    # Turn structure — single-turn detection (exactly 1 = subprocess shim)
    signals["single_turn_sessions"] = signals["human_messages"] == 1

    return signals


def _extract_text_from_message(msg):
    """Extract text from a message object (event.message)."""
    if not isinstance(msg, dict):
        return None

    content = msg.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return " ".join(texts) if texts else None

    return None


def _extract_text(event):
    """Extract text content from an event (fallback)."""
    content = event.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return " ".join(texts) if texts else None

    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return _extract_text_from_message(message)

    return None


# ---------------------------------------------------------------------------
# Profile Generation
# ---------------------------------------------------------------------------

def compute_profile(signals):
    """Compute a behavioral profile from extracted signals."""
    profile = {
        "session_id": signals["session_id"],
        "duration_seconds": signals["duration_seconds"],
        "duration_human": _format_duration(signals["duration_seconds"]),
        "event_count": signals["event_count"],
    }

    # --- Message metrics ---
    profile["human_messages"] = signals["human_messages"]
    profile["assistant_messages"] = signals["assistant_messages"]
    profile["message_ratio"] = (
        round(signals["assistant_messages"] / max(signals["human_messages"], 1), 2)
    )

    # --- Human message entropy ---
    # High entropy = diverse phrasing (developer). Low entropy = templated (wrapper)
    texts = signals["human_message_texts"]
    if len(texts) >= 3:
        # Word-level entropy across all human messages
        all_words = []
        for t in texts:
            all_words.extend(t.lower().split())
        profile["prompt_entropy"] = round(_shannon_entropy(all_words), 3)
    else:
        profile["prompt_entropy"] = None

    # Average human message length
    lengths = signals["human_message_lengths"]
    profile["avg_human_msg_length"] = round(sum(lengths) / max(len(lengths), 1), 1)

    # Scaffold hits — machine-generated wrapper artifacts
    profile["scaffold_hits"] = signals["scaffold_hits"]
    profile["scaffold_rate"] = round(
        signals["scaffold_hits"] / max(signals["human_messages"], 1), 3
    )

    # Single-turn detection
    profile["single_turn"] = signals["single_turn_sessions"]

    # --- Tool metrics ---
    profile["tool_call_count"] = signals["tool_call_count"]
    profile["unique_tools"] = len(signals["unique_tools"])
    profile["tool_diversity"] = round(
        len(signals["unique_tools"]) / max(signals["tool_call_count"], 1), 3
    )
    profile["top_tools"] = dict(signals["tool_calls"].most_common(10))

    # --- File metrics ---
    profile["files_read"] = len(signals["files_read"])
    profile["files_written"] = len(signals["files_written"])
    profile["files_edited"] = len(signals["files_edited"])
    profile["unique_file_paths"] = len(signals["unique_file_paths"])

    # --- Timing metrics ---
    gaps = signals["message_gaps"]
    if gaps:
        profile["avg_gap_seconds"] = round(sum(gaps) / len(gaps), 1)
        profile["max_gap_seconds"] = round(max(gaps), 1)
        profile["min_gap_seconds"] = round(min(gaps), 1)
        sorted_gaps = sorted(gaps)
        mid = len(sorted_gaps) // 2
        profile["median_gap_seconds"] = round(
            (sorted_gaps[mid - 1] + sorted_gaps[mid]) / 2 if len(sorted_gaps) % 2 == 0
            else sorted_gaps[mid], 1
        )

        # Idle ratio: % of time in gaps > 30 seconds (human thinking/doing other things)
        idle_time = sum(g for g in gaps if g > 30)
        total_gap_time = sum(gaps)
        profile["idle_ratio"] = round(idle_time / max(total_gap_time, 1), 3)

        # Cadence variance — high variance = human (bursts + pauses), low = bot (uniform)
        if len(gaps) >= 3:
            mean_gap = sum(gaps) / len(gaps)
            variance = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
            profile["cadence_variance"] = round(math.sqrt(variance), 1)
        else:
            profile["cadence_variance"] = None
    else:
        profile["avg_gap_seconds"] = None
        profile["max_gap_seconds"] = None
        profile["min_gap_seconds"] = None
        profile["median_gap_seconds"] = None
        profile["idle_ratio"] = None
        profile["cadence_variance"] = None

    # --- Interaction metrics ---
    profile["corrections"] = signals["corrections"]
    profile["correction_rate"] = round(
        signals["corrections"] / max(signals["human_messages"], 1), 3
    )
    profile["slash_commands"] = signals["slash_commands"]
    profile["questions"] = signals["questions"]
    profile["question_rate"] = round(
        signals["questions"] / max(signals["human_messages"], 1), 3
    )

    return profile


def _shannon_entropy(words):
    """Compute Shannon entropy of word frequency distribution."""
    if not words:
        return 0.0
    counter = Counter(words)
    total = len(words)
    entropy = 0.0
    for count in counter.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def _format_duration(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

DEVELOPER_BASELINE = {
    "idle_ratio_min": 0.15,         # Developers have significant idle time
    "correction_rate_min": 0.02,     # Developers correct themselves
    "tool_diversity_min": 0.05,      # Developers use many different tools
    "unique_tools_min": 3,           # At least 3 different tool types
    "prompt_entropy_min": 4.0,       # Diverse vocabulary
    "cadence_variance_min": 20.0,    # Irregular timing (human)
    "question_rate_min": 0.01,       # Developers ask questions
    "files_min": 3,                  # Touch multiple files
    "duration_min": 300,             # At least 5 minutes
}

WRAPPER_INDICATORS = {
    "idle_ratio_max": 0.05,          # Almost no idle time
    "correction_rate_max": 0.0,      # Zero corrections
    "tool_diversity_max": 0.02,      # Minimal tool diversity
    "unique_tools_max": 2,           # 1-2 tools only
    "cadence_variance_max": 5.0,     # Very uniform timing
    "question_rate_max": 0.0,        # Never asks questions
    "slash_commands_max": 0,         # Never uses slash commands
    "duration_max": 180,             # Very short sessions
}


def score_session(profile):
    """Score a session profile. Returns 0-100 (0 = wrapper, 100 = developer).

    Human behavioral signals (idle, cadence, entropy, corrections) are weighted
    heavily because they're the hardest to fake. Structural signals (duration,
    tool count, file count) are secondary — a quick 5-minute fix is still a
    developer if the behavioral signals say human.
    """
    score = 50  # Start neutral
    reasons = []

    # --- PRIMARY: Human behavioral signals (high weight) ---

    # Idle ratio — strongest single signal. Humans think, read, do other things.
    idle = profile.get("idle_ratio")
    if idle is not None:
        if idle >= 0.30:
            score += 15
            reasons.append(f"+15 idle_ratio {idle:.2f} (significant human thinking time)")
        elif idle >= 0.10:
            score += 10
            reasons.append(f"+10 idle_ratio {idle:.2f} (some human pauses)")
        elif idle <= WRAPPER_INDICATORS["idle_ratio_max"]:
            score -= 15
            reasons.append(f"-15 idle_ratio {idle:.2f} (no idle = bot-like)")

    # Cadence variance — humans have irregular timing, bots are uniform
    cv = profile.get("cadence_variance")
    if cv is not None:
        if cv >= 50.0:
            score += 12
            reasons.append(f"+12 cadence_variance {cv:.1f} (irregular = human)")
        elif cv >= 15.0:
            score += 6
            reasons.append(f"+6 cadence_variance {cv:.1f} (some variation)")
        elif cv <= WRAPPER_INDICATORS["cadence_variance_max"]:
            score -= 12
            reasons.append(f"-12 cadence_variance {cv:.1f} (uniform = bot)")

    # Prompt entropy — diverse vocabulary = human, templated = bot
    pe = profile.get("prompt_entropy")
    if pe is not None:
        if pe >= 4.0:
            score += 10
            reasons.append(f"+10 prompt_entropy {pe:.1f} (diverse vocabulary)")
        elif pe >= 2.5:
            score += 5
            reasons.append(f"+5 prompt_entropy {pe:.1f} (moderate vocabulary)")
        elif pe < 2.0 and profile["human_messages"] > 5:
            score -= 10
            reasons.append(f"-10 prompt_entropy {pe:.1f} (templated/repetitive)")

    # Corrections — humans correct themselves, bots never do
    cr = profile.get("correction_rate", 0)
    if cr >= DEVELOPER_BASELINE["correction_rate_min"]:
        score += 8
        reasons.append(f"+8 corrections {profile['corrections']} (humans correct themselves)")
    elif cr <= 0 and profile["human_messages"] > 10:
        score -= 5
        reasons.append(f"-5 zero corrections in {profile['human_messages']} messages")

    # --- SECONDARY: Structural signals (lower weight) ---

    # Tool diversity
    ut = profile.get("unique_tools", 0)
    if ut >= 5:
        score += 5
        reasons.append(f"+5 {ut} unique tools (diverse development)")
    elif ut >= DEVELOPER_BASELINE["unique_tools_min"]:
        score += 3
        reasons.append(f"+3 {ut} unique tools")
    elif ut <= WRAPPER_INDICATORS["unique_tools_max"] and profile["tool_call_count"] > 10:
        score -= 8
        reasons.append(f"-8 only {ut} tools across {profile['tool_call_count']} calls")

    # Questions — interactive signal
    qr = profile.get("question_rate", 0)
    if qr >= DEVELOPER_BASELINE["question_rate_min"]:
        score += 4
        reasons.append(f"+4 questions {profile['questions']} (interactive)")

    # Slash commands
    sc = profile.get("slash_commands", 0)
    if sc > 0:
        score += 3
        reasons.append(f"+3 slash_commands {sc} (CLI interaction)")

    # File diversity
    fp = profile.get("unique_file_paths", 0)
    if fp >= DEVELOPER_BASELINE["files_min"]:
        score += 3
        reasons.append(f"+3 {fp} unique files (project work)")

    # Duration — light touch, short sessions aren't inherently suspicious
    dur = profile.get("duration_seconds", 0)
    if dur >= 600:
        score += 3
        reasons.append(f"+3 duration {profile['duration_human']} (sustained session)")
    elif dur <= 60 and profile["tool_call_count"] > 20:
        score -= 8
        reasons.append(f"-8 {profile['duration_human']} with {profile['tool_call_count']} tool calls (rapid-fire)")

    # --- WRAPPER ARTIFACT SIGNALS ---

    # Scaffold artifacts — machine-generated prompt patterns
    sh = profile.get("scaffold_hits", 0)
    sr = profile.get("scaffold_rate", 0)
    if sr >= 0.3 and profile["human_messages"] >= 5:
        score -= 15
        reasons.append(f"-15 scaffold_rate {sr:.2f} ({sh} wrapper artifacts in {profile['human_messages']} messages)")
    elif sh > 0:
        score -= 5
        reasons.append(f"-5 {sh} scaffold artifacts detected (possible wrapper)")

    # Single-turn session — one prompt, one response, done
    if profile.get("single_turn") and profile["tool_call_count"] > 0:
        score -= 5
        reasons.append(f"-5 single-turn session (one prompt in, one response out)")

    # --- HUMAN SIGNAL FLOOR ---
    # If ANY strong human behavioral signal is present, the session is not a wrapper.
    # A developer doing a quick 2-minute fix with clear human timing is still a developer.
    human_signals = 0
    if idle is not None and idle >= 0.10:
        human_signals += 1
    if cv is not None and cv >= 15.0:
        human_signals += 1
    if pe is not None and pe >= 2.5:
        human_signals += 1
    if profile.get("corrections", 0) > 0:
        human_signals += 1
    if profile.get("questions", 0) > 0:
        human_signals += 1

    if human_signals >= 2 and score < 65:
        old_score = score
        score = 65
        reasons.append(f"+{score - old_score} human signal floor ({human_signals} human signals detected)")
    elif human_signals >= 1 and score < 55:
        old_score = score
        score = 55
        reasons.append(f"+{score - old_score} human signal floor ({human_signals} human signal detected)")

    # Clamp
    score = max(0, min(100, score))

    return {
        "score": score,
        "verdict": _verdict(score, profile),
        "reasons": reasons,
    }


def _verdict(score, profile=None):
    # Sessions with <3 human messages don't have enough signal to classify
    if profile and profile.get("human_messages", 0) < 3:
        if score >= 60:
            return "LIKELY_DEVELOPER"
        return "INSUFFICIENT_DATA"

    if score >= 80:
        return "DEVELOPER"
    elif score >= 60:
        return "LIKELY_DEVELOPER"
    elif score >= 45:
        return "AMBIGUOUS"
    elif score >= 20:
        return "LIKELY_WRAPPER"
    else:
        return "WRAPPER"


# ---------------------------------------------------------------------------
# Git Analysis — Development Activity Fingerprint
# ---------------------------------------------------------------------------

def find_git_repos(search_paths=None):
    """Find git repos the user works in. Checks common locations."""
    if search_paths:
        candidates = search_paths
    else:
        home = os.path.expanduser("~")
        candidates = []
        # Check home directory children for .git
        try:
            for entry in os.scandir(home):
                if entry.is_dir() and not entry.name.startswith("."):
                    git_dir = os.path.join(entry.path, ".git")
                    if os.path.isdir(git_dir):
                        candidates.append(entry.path)
        except (OSError, PermissionError):
            pass
    return candidates


def analyze_git_repo(repo_path, since_date=None):
    """Extract development signals from a git repo."""
    if not since_date:
        since_date = "12 weeks ago"

    result = {
        "path": repo_path,
        "name": os.path.basename(repo_path),
        "commits": 0,
        "authors": Counter(),
        "co_authors": Counter(),
        "commits_per_week": {},
        "commits_per_hour": Counter(),
        "daily_counts": [],
        "files_changed_distribution": Counter(),
        "first_commit": None,
        "last_commit": None,
    }

    try:
        # Total commits
        out = _git(repo_path, ["log", f"--since={since_date}", "--oneline"])
        lines = [l for l in out.strip().split("\n") if l.strip()]
        result["commits"] = len(lines)

        if result["commits"] == 0:
            return result

        # Authors
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%an"])
        for line in out.strip().split("\n"):
            if line.strip():
                result["authors"][line.strip()] += 1

        # Co-authors
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%b"])
        for line in out.split("\n"):
            if "Co-Authored-By" in line or "co-authored-by" in line.lower():
                # Extract name/email
                match = re.search(r"Co-Authored-By:\s*(.+?)(?:\s*<|$)", line, re.IGNORECASE)
                if match:
                    result["co_authors"][match.group(1).strip()] += 1

        # Weekly distribution
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%ad", "--date=format:%Y-W%W"])
        for line in out.strip().split("\n"):
            if line.strip():
                result["commits_per_week"][line.strip()] = result["commits_per_week"].get(line.strip(), 0) + 1

        # Hourly distribution
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%ad", "--date=format:%H"])
        for line in out.strip().split("\n"):
            if line.strip():
                result["commits_per_hour"][line.strip()] += 1

        # Daily counts (for variance calculation)
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%ad", "--date=format:%Y-%m-%d"])
        day_counts = Counter()
        for line in out.strip().split("\n"):
            if line.strip():
                day_counts[line.strip()] += 1
        result["daily_counts"] = sorted(day_counts.values())

        # Date range
        out = _git(repo_path, ["log", f"--since={since_date}", "--format=%aI", "--reverse"])
        dates = [l.strip() for l in out.strip().split("\n") if l.strip()]
        if dates:
            result["first_commit"] = dates[0][:10]
            result["last_commit"] = dates[-1][:10]

        # Files per commit distribution
        out = _git(repo_path, ["log", f"--since={since_date}", "--pretty=format:%h", "--name-only"])
        current_files = 0
        for line in out.split("\n"):
            if not line.strip():
                if current_files > 0:
                    if current_files == 1:
                        result["files_changed_distribution"]["1"] += 1
                    elif current_files <= 3:
                        result["files_changed_distribution"]["2-3"] += 1
                    elif current_files <= 5:
                        result["files_changed_distribution"]["4-5"] += 1
                    elif current_files <= 10:
                        result["files_changed_distribution"]["6-10"] += 1
                    elif current_files <= 20:
                        result["files_changed_distribution"]["11-20"] += 1
                    else:
                        result["files_changed_distribution"]["21+"] += 1
                    current_files = 0
            elif re.fullmatch(r"[0-9a-f]{7,12}", line.strip()):
                # This is a commit hash line (hex only)
                current_files = 0
            else:
                current_files += 1

    except (OSError, subprocess.SubprocessError):
        pass

    return result


def compute_account_report(session_results, git_repos=None, since_date=None):
    """Compute account-level report combining sessions + git activity."""
    report = {
        "sessions": {},
        "git": {},
        "account_verdict": None,
        "account_score": 0,
        "reasons": [],
    }

    # --- Session summary ---
    verdicts = Counter()
    total_human_msgs = 0
    total_tool_calls = 0
    total_duration = 0
    single_turn_count = 0
    total_scaffold_hits = 0
    for r in session_results:
        verdicts[r["scoring"]["verdict"]] += 1
        total_human_msgs += r["profile"]["human_messages"]
        total_tool_calls += r["profile"]["tool_call_count"]
        total_duration += r["profile"]["duration_seconds"]
        if r["profile"].get("single_turn"):
            single_turn_count += 1
        total_scaffold_hits += r["profile"].get("scaffold_hits", 0)

    report["sessions"] = {
        "total": len(session_results),
        "verdicts": dict(verdicts),
        "developer_sessions": verdicts.get("DEVELOPER", 0) + verdicts.get("LIKELY_DEVELOPER", 0),
        "automated_sessions": verdicts.get("WRAPPER", 0) + verdicts.get("LIKELY_WRAPPER", 0),
        "ambiguous": verdicts.get("AMBIGUOUS", 0),
        "insufficient_data": verdicts.get("INSUFFICIENT_DATA", 0),
        "total_human_messages": total_human_msgs,
        "total_tool_calls": total_tool_calls,
        "total_duration_hours": round(total_duration / 3600, 1),
        "single_turn_sessions": single_turn_count,
        "single_turn_rate": round(single_turn_count / max(len(session_results), 1), 3),
        "total_scaffold_hits": total_scaffold_hits,
    }

    dev_count = report["sessions"]["developer_sessions"]
    auto_count = report["sessions"]["automated_sessions"]

    # --- Git analysis ---
    repos = []
    total_commits = 0
    all_daily_counts = []
    all_hours = Counter()
    all_co_authors = Counter()
    all_weekly = {}

    if git_repos:
        for repo_path in git_repos:
            analysis = analyze_git_repo(repo_path, since_date)
            if analysis["commits"] > 0:
                repos.append(analysis)
                total_commits += analysis["commits"]
                all_daily_counts.extend(analysis["daily_counts"])
                all_hours.update(analysis["commits_per_hour"])
                all_co_authors.update(analysis["co_authors"])
                for week, count in analysis["commits_per_week"].items():
                    all_weekly[week] = all_weekly.get(week, 0) + count

    # Date span
    date_span_days = 0
    if repos:
        first = min(r["first_commit"] for r in repos if r["first_commit"])
        last = max(r["last_commit"] for r in repos if r["last_commit"])
        try:
            d1 = datetime.strptime(first, "%Y-%m-%d")
            d2 = datetime.strptime(last, "%Y-%m-%d")
            date_span_days = (d2 - d1).days + 1
        except ValueError:
            pass

    commits_per_day = total_commits / max(date_span_days, 1)

    # Daily variance
    daily_variance = 0
    if all_daily_counts:
        mean = sum(all_daily_counts) / len(all_daily_counts)
        daily_variance = math.sqrt(sum((c - mean) ** 2 for c in all_daily_counts) / len(all_daily_counts))

    # Peak hours
    peak_hours = [h for h, _ in all_hours.most_common(6)]

    # Weeks
    weekly_counts = sorted(all_weekly.values()) if all_weekly else []
    weekly_variance = 0
    if weekly_counts:
        mean = sum(weekly_counts) / len(weekly_counts)
        weekly_variance = math.sqrt(sum((c - mean) ** 2 for c in weekly_counts) / len(weekly_counts))

    report["git"] = {
        "repos": [{
            "name": r["name"],
            "path": r["path"],
            "commits": r["commits"],
            "authors": dict(r["authors"].most_common(5)),
            "co_authors": dict(r["co_authors"].most_common(5)),
            "first_commit": r["first_commit"],
            "last_commit": r["last_commit"],
        } for r in repos],
        "total_commits": total_commits,
        "date_span_days": date_span_days,
        "commits_per_day": round(commits_per_day, 1),
        "daily_variance": round(daily_variance, 1),
        "weekly_variance": round(weekly_variance, 1),
        "peak_hours": peak_hours,
        "weeks_active": len(all_weekly),
    }

    # --- Account-level scoring ---
    score = 50
    reasons = []

    # Developer sessions exist
    if dev_count >= 10:
        score += 15
        reasons.append(f"+15 {dev_count} developer-scored sessions (human presence confirmed)")
    elif dev_count >= 3:
        score += 8
        reasons.append(f"+8 {dev_count} developer-scored sessions")
    elif dev_count == 0 and auto_count > 10:
        score -= 25
        reasons.append(f"-25 zero developer sessions with {auto_count} automated (wrapper signature)")

    # Proportionality — automated:developer ratio
    if dev_count > 0 and auto_count > 0:
        ratio = auto_count / dev_count
        if ratio <= 5:
            score += 10
            reasons.append(f"+10 auto:dev ratio {ratio:.1f}:1 (proportional — hooks, reviews, workers)")
        elif ratio <= 15:
            score += 3
            reasons.append(f"+3 auto:dev ratio {ratio:.1f}:1 (moderate automation)")
        else:
            score -= 15
            reasons.append(f"-15 auto:dev ratio {ratio:.1f}:1 (disproportionate automation)")

    # Git activity
    if total_commits > 0:
        score += 10
        reasons.append(f"+10 {total_commits} git commits across {len(repos)} repos (real development)")

        # Commits per day
        if commits_per_day >= 5:
            score += 5
            reasons.append(f"+5 {commits_per_day:.1f} commits/day (active development)")

        # Daily variance — humans have busy and quiet days
        if daily_variance >= 5:
            score += 5
            reasons.append(f"+5 daily commit variance {daily_variance:.1f} (human work pattern)")

        # Weekly variance — same
        if weekly_variance >= 10:
            score += 3
            reasons.append(f"+3 weekly commit variance {weekly_variance:.1f} (natural cadence)")

        # Co-authors — development workflow with code review
        if all_co_authors:
            score += 5
            names = ", ".join(all_co_authors.keys())
            reasons.append(f"+5 co-authored commits ({names})")

        # Session:commit ratio
        if len(session_results) > 0:
            sessions_per_commit = len(session_results) / max(total_commits, 1)
            if 1.5 <= sessions_per_commit <= 8:
                score += 5
                reasons.append(f"+5 {sessions_per_commit:.1f} sessions/commit (consistent with dev + review + fingerprint cycle)")
    else:
        if auto_count > 50:
            score -= 10
            reasons.append(f"-10 {auto_count} sessions with zero git commits (no development output)")

    score = max(0, min(100, score))

    if score >= 80:
        report["account_verdict"] = "LEGITIMATE_DEVELOPER"
    elif score >= 60:
        report["account_verdict"] = "LIKELY_LEGITIMATE"
    elif score >= 40:
        report["account_verdict"] = "INCONCLUSIVE"
    elif score >= 20:
        report["account_verdict"] = "LIKELY_ABUSE"
    else:
        report["account_verdict"] = "ABUSE"

    report["account_score"] = score
    report["reasons"] = reasons

    return report


def print_account_report(report, use_json=False):
    """Print the account-level report."""
    if use_json:
        print(json.dumps(report, indent=2, default=str))
        return

    s = report["sessions"]
    g = report["git"]

    print(f"\n{'=' * 70}")
    print(f"  ACCOUNT-LEVEL DEVELOPMENT FINGERPRINT")
    print(f"{'=' * 70}")

    print(f"\n  Sessions ({s['total']} total)")
    print(f"    DEVELOPER:          {s['verdicts'].get('DEVELOPER', 0):>5d}")
    print(f"    LIKELY_DEVELOPER:   {s['verdicts'].get('LIKELY_DEVELOPER', 0):>5d}")
    print(f"    AMBIGUOUS:          {s['verdicts'].get('AMBIGUOUS', 0):>5d}")
    print(f"    INSUFFICIENT_DATA:  {s['verdicts'].get('INSUFFICIENT_DATA', 0):>5d}")
    print(f"    LIKELY_WRAPPER:     {s['verdicts'].get('LIKELY_WRAPPER', 0):>5d}")
    print(f"    WRAPPER:            {s['verdicts'].get('WRAPPER', 0):>5d}")
    print(f"    {'─' * 40}")
    print(f"    Developer-side:     {s['developer_sessions']:>5d}  ({s['developer_sessions']*100//max(s['total'],1)}%)")
    print(f"    Automated:          {s['automated_sessions']:>5d}  ({s['automated_sessions']*100//max(s['total'],1)}%)")
    if s["developer_sessions"] > 0 and s["automated_sessions"] > 0:
        ratio = s["automated_sessions"] / s["developer_sessions"]
        print(f"    Ratio:              {ratio:.1f}:1 auto:dev")
    print(f"    Total human msgs:   {s['total_human_messages']:>5d}")
    print(f"    Total tool calls:   {s['total_tool_calls']:>5d}")
    print(f"    Total hours:        {s['total_duration_hours']:>7.1f}")

    if g["total_commits"] > 0:
        print(f"\n  Git Activity ({g['total_commits']} commits, {g['date_span_days']} days, {len(g['repos'])} repos)")
        print(f"    Commits/day:     {g['commits_per_day']}")
        print(f"    Daily variance:  {g['daily_variance']}")
        print(f"    Weekly variance: {g['weekly_variance']}")
        print(f"    Weeks active:    {g['weeks_active']}")
        if g["peak_hours"]:
            print(f"    Peak hours:      {', '.join(h + ':00' for h in g['peak_hours'])}")
        print(f"\n    Repos:")
        for r in g["repos"]:
            authors = ", ".join(f"{n} ({c})" for n, c in r["authors"].items())
            print(f"      {r['name']:30s}  {r['commits']:>5d} commits  [{r['first_commit']} to {r['last_commit']}]")
            if r["co_authors"]:
                co = ", ".join(f"{n} ({c})" for n, c in r["co_authors"].items())
                print(f"        Co-authors: {co}")
    else:
        print(f"\n  Git Activity: none detected")

    print(f"\n  {'─' * 66}")
    print(f"  ACCOUNT SCORE: {report['account_score']}/100 {report['account_verdict']}")
    print(f"  {'─' * 66}")
    for reason in report["reasons"]:
        print(f"    {reason}")

    print()


def _git(repo_path, args):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", "-C", repo_path] + args,
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_profile(profile, scoring=None, use_json=False):
    """Print a session profile."""
    if use_json:
        output = {"profile": profile}
        if scoring:
            output["scoring"] = scoring
        print(json.dumps(output, indent=2, default=str))
        return

    print(f"\n{'=' * 70}")
    print(f"  Session: {profile['session_id'] or 'unknown'}")
    print(f"  Duration: {profile['duration_human']}  |  Events: {profile['event_count']}")
    print(f"{'=' * 70}")

    print(f"\n  Messages")
    print(f"    Human:     {profile['human_messages']}")
    print(f"    Assistant: {profile['assistant_messages']}")
    print(f"    Ratio:     {profile['message_ratio']}:1 (assistant:human)")
    print(f"    Avg len:   {profile['avg_human_msg_length']} chars")

    print(f"\n  Tools")
    print(f"    Total calls:  {profile['tool_call_count']}")
    print(f"    Unique tools: {profile['unique_tools']}")
    print(f"    Diversity:    {profile['tool_diversity']}")
    if profile["top_tools"]:
        print(f"    Top tools:")
        for tool, count in list(profile["top_tools"].items())[:5]:
            bar = "#" * min(count, 40)
            print(f"      {tool:20s} {bar} {count}")

    print(f"\n  Files")
    print(f"    Read:    {profile['files_read']}")
    print(f"    Written: {profile['files_written']}")
    print(f"    Edited:  {profile['files_edited']}")
    print(f"    Unique:  {profile['unique_file_paths']}")

    print(f"\n  Timing")
    if profile["avg_gap_seconds"] is not None:
        print(f"    Avg gap:     {profile['avg_gap_seconds']}s")
        print(f"    Median gap:  {profile['median_gap_seconds']}s")
        print(f"    Max gap:     {profile['max_gap_seconds']}s")
        print(f"    Idle ratio:  {profile['idle_ratio']}")
        if profile["cadence_variance"] is not None:
            print(f"    Cadence var:  {profile['cadence_variance']}")
    else:
        print(f"    (insufficient data)")

    print(f"\n  Interaction")
    print(f"    Corrections:    {profile['corrections']} ({profile['correction_rate']} rate)")
    print(f"    Slash commands: {profile['slash_commands']}")
    print(f"    Questions:      {profile['questions']} ({profile['question_rate']} rate)")
    if profile["prompt_entropy"] is not None:
        print(f"    Prompt entropy: {profile['prompt_entropy']}")
    if profile.get("scaffold_hits", 0) > 0:
        print(f"    Scaffold hits:  {profile['scaffold_hits']} ({profile['scaffold_rate']} rate)")
    if profile.get("single_turn"):
        print(f"    Single-turn:    yes")

    if scoring:
        print(f"\n  {'─' * 66}")
        print(f"  SCORE: {scoring['score']}/100 {scoring['verdict']}")
        print(f"  {'─' * 66}")
        for reason in scoring["reasons"]:
            print(f"    {reason}")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Session Guard — behavioral profiler for abuse detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 session_guard.py                  # Profile recent sessions with scores
  python3 session_guard.py --limit 5        # Profile last 5 sessions
  python3 session_guard.py --session <id>   # Profile specific session
  python3 session_guard.py --json           # JSON output
  python3 session_guard.py --score          # Show scores only (summary)

Related: https://github.com/anthropics/claude-code/issues/42542
        """,
    )
    parser.add_argument("--session", help="Profile a specific session ID")
    parser.add_argument("--project", help="Path to project directory under ~/.claude/projects/")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent sessions to profile (default: 10)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--score", action="store_true", help="Show scores only (summary table)")
    parser.add_argument("--account", action="store_true", help="Full account-level report with git analysis")
    parser.add_argument("--git-repos", nargs="*", help="Git repo paths to analyze (auto-detected if omitted)")
    parser.add_argument("--since", default="12 weeks ago", help="Git history window (default: '12 weeks ago')")
    parser.add_argument("--min-messages", type=int, default=3, help="Minimum human messages to include (default: 3)")

    args = parser.parse_args()

    # Find session files
    project_dir = args.project or CLAUDE_PROJECTS_DIR
    if not os.path.isdir(project_dir):
        print(f"Error: Claude projects directory not found: {project_dir}", file=sys.stderr)
        print(f"Is Claude Code installed? Expected at ~/.claude/projects/", file=sys.stderr)
        sys.exit(1)

    if args.session:
        # Find specific session
        matches = glob.glob(os.path.join(project_dir, "**", f"{args.session}*"), recursive=True)
        jsonl_matches = [m for m in matches if m.endswith(".jsonl")]
        if not jsonl_matches:
            print(f"Error: Session {args.session} not found", file=sys.stderr)
            sys.exit(1)
        session_files = jsonl_matches[:1]
    else:
        session_files = find_session_files(project_dir, limit=args.limit)

    if not session_files:
        print("No session files found.", file=sys.stderr)
        sys.exit(1)

    # Process sessions
    results = []
    for filepath in session_files:
        events = parse_session(filepath)
        if not events:
            continue

        signals = extract_signals(events)
        if signals["human_messages"] < args.min_messages:
            continue

        profile = compute_profile(signals)
        scoring = score_session(profile)
        results.append({"profile": profile, "scoring": scoring, "file": filepath})

    if not results:
        print("No sessions with enough data to profile.", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.account:
        # Full account report — all sessions + git
        git_repos = args.git_repos or find_git_repos()
        report = compute_account_report(results, git_repos, args.since)
        print_account_report(report, use_json=args.json)
    elif args.json:
        print(json.dumps(results, indent=2, default=str))
    elif args.score:
        print(f"\n{'Session':>40s}  {'Score':>5s}  {'Verdict':>16s}  {'Msgs':>5s}  {'Tools':>5s}  Duration")
        print(f"{'─' * 100}")
        for r in results:
            p = r["profile"]
            s = r["scoring"]
            sid = (p["session_id"] or "unknown")[-36:]
            print(f"{sid:>40s}  {s['score']:>5d}  {s['verdict']:>16s}  {p['human_messages']:>5d}  {p['tool_call_count']:>5d}  {p['duration_human']}")
    else:
        for r in results:
            print_profile(r["profile"], r["scoring"])


if __name__ == "__main__":
    main()
