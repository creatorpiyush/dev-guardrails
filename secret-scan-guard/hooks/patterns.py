"""Detection rules for secret_scan.py, kept apart from the hook logic.

A rule's `confidence` sets its action: "high" blocks, "medium" asks.
`show` is how many leading characters a redacted preview may reveal: the
fixed provider prefix at most, never more than 4. `value`, when set, takes
(match, lines, index) and returns the secret text, or None to drop the match.

Standard library only. Python 3.9+.
"""
import math
import re
from collections import Counter, namedtuple

HIGH, MEDIUM = "high", "medium"

Rule = namedtuple("Rule", "id regex confidence show value")
Rule.__new__.__defaults__ = (0, None)  # show, value

ALLOW_MARKER = "secret-scan:allow"


def entropy(s):
    """Shannon entropy in bits per character."""
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


ASSIGNMENT = re.compile(
    r"""(?i)[\w.-]*(?:password|passwd|secret|token|api_?key|auth)\w*["']?\s*(?:=>|=|:)\s*"""
    r"""(?:"([^"\s]{16,})"|'([^'\s]{16,})'|([A-Za-z0-9+/=_.~-]{16,})\s*$)""")


def assignment_value(m, lines, i):
    quoted = m.group(1) or m.group(2)
    value = quoted or m.group(3)
    # ponytail: unquoted values need a digit and a letter, so `token = self.token_value`
    # doesn't ask; a real parser per language would do better.
    if not quoted and not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
        return None
    return value if entropy(value) >= 3.5 else None


AWS_CONTEXT = re.compile(r"(?i)aws|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def aws_secret_value(m, lines, i):
    value = m.group(0)
    # Real secret keys are base64-ish (~4.5+ bits/char); 40-char hex git SHAs stay under 4.
    if entropy(value) < 4.2:
        return None
    near = lines[max(0, i - 3):i + 4]
    return value if any(AWS_CONTEXT.search(text) for _, text, _ in near) else None


RULES = [
    # High confidence: provider-specific formats. Block.
    Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), HIGH, 4),
    Rule("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), HIGH, 4),
    Rule("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"), HIGH, 4),
    Rule("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), HIGH, 4),
    Rule("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), HIGH, 4),
    Rule("slack-webhook", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"), HIGH, 4),
    Rule("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}"), HIGH, 4),
    Rule("openai-api-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"), HIGH, 3),
    Rule("stripe-live-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}"), HIGH, 4),
    Rule("sendgrid-api-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"), HIGH, 3),
    Rule("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), HIGH, 4),
    Rule("twilio-api-key", re.compile(r"\bSK[0-9a-f]{32}\b"), HIGH, 2),
    Rule("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"), HIGH, 4),
    # Medium confidence: often real, sometimes public or illustrative. Ask.
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MEDIUM, 4),
    Rule("url-credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]+@"), MEDIUM),
    Rule("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), MEDIUM, 3),
    Rule("generic-assignment", ASSIGNMENT, MEDIUM, 0, assignment_value),
    Rule("aws-secret-key", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"),
         MEDIUM, 0, aws_secret_value),
]

# Values that are obviously not real secrets.
PLACEHOLDER = re.compile(
    r"example|dummy|changeme|your_|xxxx|<[^>]*>|\$\{[^}]*\}|^\$[A-Za-z_]|"
    r"process\.env\.|os\.environ|os\.getenv|^(.)\1*$", re.I)

# Subresource-integrity and lockfile hashes (sha512-...) are blanked before scanning.
INTEGRITY = re.compile(r"\bsha(?:1|256|384|512)-[A-Za-z0-9+/=]+")

# Generated files that are full of hashes and never hold hand-written secrets.
SKIP_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock", "go.sum", "composer.lock",
    "Gemfile.lock", "packages.lock.json",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map")

# Staging these asks first. Templates like .env.example are fine.
SENSITIVE_FILES = [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"]
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")
