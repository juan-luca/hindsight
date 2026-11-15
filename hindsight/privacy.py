"""Hindsight privacy pass: scrub and audit the recorded history before it is
published.

The recorder already redacts secrets and machine paths as it writes, but logs
recorded by older recorders, on other machines, or before a new pattern was
added deserve a second look before a push. `hindsight scrub` rewrites
`.hindsight/events/*.jsonl` and `.hindsight/usage/*.json` in place with the
current rules (plus the roots each legacy envelope reveals about the machine
that wrote it), then reports anything that still looks personal. With
`--check` it changes nothing and exits 1 on findings: a pre-push gate.
"""
import glob
import json
import os
import re
import sys

if __package__:
    from .record import PATH_KEYS
    from .record import REDACT_PATTERNS
    from .record import TEXT_KEYS
    from .record import Scrubber
    from .record import default_scrubber
    from .record import private_patterns
    from .record import redact
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from record import PATH_KEYS
    from record import REDACT_PATTERNS
    from record import TEXT_KEYS
    from record import Scrubber
    from record import default_scrubber
    from record import private_patterns
    from record import redact

FREE_TEXT_KEYS = TEXT_KEYS + ("error", "answer")

SUSPECTS = [
    # Public no-reply addresses (commit trailers) are not personal data.
    (
        "email",
        re.compile(
            r"(?<![\w.+-])(?!git@|noreply@|[\w.+-]*@users\.noreply\.)"
            r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
        ),
    ),
    (
        "home-path",
        re.compile(
            r"(?i)(?:/Users/|/home/|[a-z]:[\\/]+Users[\\/]+|/[a-z]/Users/|/mnt/[a-z]/Users/)"
            r"(?!Shared\b|Public\b)[A-Za-z0-9._-]+"
        ),
    ),
    ("project-slug", re.compile(r"(?i)\b[a-z]?-{1,2}Users-[A-Za-z0-9._]+-")),
    ("phone", re.compile(r"(?<![\w.:/-])\+\d{1,3}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}\b")),
]


ALLOW_FILE = os.path.join(".hindsight", "privacy-allow.txt")


def allow_patterns(repo):
    """Committed regexes for known-safe matches (synthetic test fixtures such
    as /Users/ana): a finding whose text matches one is not reported."""
    try:
        with open(os.path.join(repo, ALLOW_FILE), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    patterns = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                patterns.append(re.compile(line))
            except re.error:
                continue
    return patterns


def _legacy_roots(envelope):
    """(project_dir, home) an old envelope reveals about its machine."""
    project = envelope.get("cwd") or ""
    if not (project.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", project)):
        project = ""
    home = ""
    transcript = envelope.get("transcript_path") or ""
    for marker in ("/.claude/", "\\.claude\\"):
        if marker in transcript:
            home = transcript.split(marker)[0]
            break
    return project, home


def _scrub_value(scrubbers, value, is_path=False):
    if not isinstance(value, str):
        return value
    if is_path:
        for scrubber in scrubbers:
            relative = scrubber.relative(value)
            if relative is not None:
                return relative
    value = redact(value)
    for scrubber in scrubbers:
        value = scrubber.text(value)
    return value


def scrub_envelope(envelope, scrubbers):
    """A copy of one event envelope with the current privacy rules applied."""
    legacy_project, legacy_home = _legacy_roots(envelope)
    local = list(scrubbers)
    if legacy_project or legacy_home:
        local.insert(0, Scrubber(legacy_project, legacy_home))
    out = dict(envelope)
    out.pop("transcript_path", None)
    cwd = envelope.get("cwd") or "."
    for scrubber in local:
        relative = scrubber.relative(cwd)
        if relative is not None:
            cwd = relative
            break
    else:
        cwd = _scrub_value(local, cwd)
    out["cwd"] = cwd
    data = dict(envelope.get("data") or {})
    for key in PATH_KEYS:
        if key in data:
            data[key] = _scrub_value(local, data[key], is_path=True)
    for key in FREE_TEXT_KEYS:
        if key in data:
            data[key] = _scrub_value(local, data[key])
    out["data"] = data
    return out, local


def scrub_summary(summary, scrubbers):
    out = json.loads(json.dumps(summary))
    for episode in out.get("episodes") or []:
        for key in ("text", "fix_detail"):
            episode[key] = _scrub_value(scrubbers, episode.get(key, ""))
    return out


def find_suspects(text, private=()):
    hits = []
    for name, pattern in SUSPECTS:
        for match in pattern.finditer(text):
            hits.append((name, match.group(0)))
    for term in private:
        try:
            if re.search(term, text, re.IGNORECASE):
                hits.append(("private-term", "(.hindsight/private.local)"))
        except re.error:
            continue
    for pattern, label in REDACT_PATTERNS:
        for match in pattern.finditer(text):
            if "[REDACTED" not in match.group(0):
                hits.append(("secret", label))
    return hits


def _mask(value):
    return value if len(value) <= 6 else value[:3] + "…" + value[-2:]


def run(repo, check=False, out=sys.stdout):
    """Scrub (or with check=True, only audit) the recorded history.

    Returns (files_changed, findings) where findings are (path, line, kind,
    masked sample) still suspicious after scrubbing.
    """
    base = [default_scrubber(repo)]
    private = private_patterns(repo)
    allow = allow_patterns(repo)

    def report(path, number, line):
        for kind, sample in find_suspects(line, private):
            if kind != "private-term" and any(a.search(sample) for a in allow):
                continue
            findings.append((path, number, kind, _mask(sample)))

    seen_roots = []
    changed = 0
    findings = []
    event_files = sorted(glob.glob(os.path.join(repo, ".hindsight", "events", "*.jsonl")))
    for path in event_files:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        rewritten = []
        for line in lines:
            try:
                envelope = json.loads(line)
            except ValueError:
                rewritten.append(line)
                continue
            clean, local = scrub_envelope(envelope, base)
            seen_roots.extend(s for s in local if s not in base and s not in seen_roots)
            rewritten.append(json.dumps(clean, ensure_ascii=False))
        text = "\n".join(rewritten) + ("\n" if rewritten else "")
        if text != "\n".join(lines) + ("\n" if lines else ""):
            changed += 1
            if not check:
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
        final = text if not check else "\n".join(lines)
        for number, line in enumerate(final.splitlines(), 1):
            report(path, number, line)
    scrubbers = seen_roots + base
    for path in sorted(glob.glob(os.path.join(repo, ".hindsight", "usage", "*.json"))):
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        try:
            summary = json.loads(original)
        except ValueError:
            continue
        text = json.dumps(scrub_summary(summary, scrubbers), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        if text != original:
            changed += 1
            if not check:
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
        final = text if not check else original
        for number, line in enumerate(final.splitlines(), 1):
            report(path, number, line)
    verb = "would rewrite" if check else "rewrote"
    print("hindsight scrub: %s %d file(s)" % (verb, changed), file=out)
    for path, number, kind, sample in findings:
        print(
            "  %s:%d  %s  %s" % (os.path.relpath(path, repo).replace(os.sep, "/"), number, kind, sample),
            file=out,
        )
    if not findings:
        print("  no personal data patterns left (emails, home paths, phones, secrets)", file=out)
    return changed, findings
