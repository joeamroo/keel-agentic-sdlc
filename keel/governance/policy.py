"""Policy guardrails for security, compliance, and change control.

Every node in the plan graph passes this engine twice: once at its entry gate
and once at its exit gate. The engine holds no state beyond its rule set, so
one instance can be shared across a run and its decisions stay reproducible
under replay.

Three choices worth defending in review:

* Rules return `PolicyViolation` objects instead of raising, so a single pass
  reports every finding rather than stopping at the first one.
* Severity alone decides whether a gate closes (`Severity.blocks`), which
  keeps the blocking policy in the frozen contract instead of scattering it
  across rule implementations.
* A rule that raises is converted into a blocking violation. A governance
  plane that fails open is worse than no governance plane at all.

Rules that inspect produced artifacts naturally pass at the entry gate, where
the node has produced nothing yet, and bite at the exit gate. That asymmetry
is deliberate and is called out per rule below.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from keel.models import (
    Artifact,
    GateDecision,
    ImpactLevel,
    NodeSpec,
    PolicyRule,
    PolicyViolation,
    Severity,
)

__all__ = [
    "ChangeControlRule",
    "DestructiveOperationRule",
    "OpenRedirectRule",
    "PIIInUrlRule",
    "PolicyEngine",
    "SecretScanRule",
    "TestEvidenceRule",
    "default_engine",
]

_SNIPPET_CHARS = 90


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _location(artifact: Artifact, line_no: int) -> str:
    """Point a reviewer at exactly one line of exactly one artifact."""
    base = artifact.name if not artifact.path else f"{artifact.name}[{artifact.path}]"
    return f"{base}:{line_no}"


def _line_of(content: str, index: int) -> int:
    """1-based line number for a character offset into `content`."""
    return content.count("\n", 0, index) + 1


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _SNIPPET_CHARS else flat[: _SNIPPET_CHARS - 3] + "..."


def _produced_by(artifacts: list[Artifact], node: NodeSpec) -> list[Artifact]:
    return [a for a in artifacts if a.produced_by == node.id]


def _shannon_entropy(value: str) -> float:
    """Bits per character. Random credentials sit near 5, prose near 4, filler near 0."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


# --------------------------------------------------------------------------
# Rule: secret scanning
# --------------------------------------------------------------------------

# Ordered most specific first so the most informative label wins per location.
_PLACEHOLDER_MARKERS = re.compile(
    r"""(?ix)
    your[-_\s]?(?:api[-_\s]?)?(?:key|token|secret|creds?)   # sk-ant-your-key-here
    | (?:change[-_\s]?me|replace[-_\s]?(?:me|this|with))
    | placeholder | redacted | example | sample | dummy | \bfake\b
    | \bnot[-_]?a[-_]?real\b | \bhere\b | insert[-_\s]?your
    | x{3,} | \.{3,} | \*{3,}
    | <[^>]{0,64}> | \{\{[^}]{0,64}\}\} | \$\{[^}]{0,64}\} | \$[A-Z_]{4,}
    """
)
# A bare SCREAMING_SNAKE value is the name of a variable holding the secret,
# not the secret. `{"api_key": "ANTHROPIC_API_KEY"}` is config, not a leak. The
# underscore is required so that an AWS key id, which is also all upper case,
# still trips the shape-specific pattern.
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


@dataclass(frozen=True, slots=True)
class _SecretPattern:
    label: str
    pattern: re.Pattern[str]
    value_group: int = 0
    min_entropy: float = 0.0


_SECRET_PATTERNS: tuple[_SecretPattern, ...] = (
    _SecretPattern(
        "Anthropic API key",
        re.compile(r"sk-ant-(?:api\d{2}-)?[A-Za-z0-9_\-]{24,}"),
    ),
    _SecretPattern(
        "AWS access key id",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    ),
    _SecretPattern(
        "private key PEM header",
        re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    ),
    _SecretPattern(
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
    ),
    _SecretPattern(
        "Slack token",
        re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}"),
    ),
    _SecretPattern(
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ),
    _SecretPattern(
        "OpenAI API key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ),
    _SecretPattern(
        "hardcoded api key assignment",
        re.compile(r"""(?i)(?<![A-Za-z])api[_-]?key\s*[=:]\s*['"]([^'"]{16,})['"]"""),
        value_group=1,
        min_entropy=2.5,
    ),
    _SecretPattern(
        "hardcoded secret assignment",
        re.compile(
            r"""(?i)(?<![A-Za-z])(?:secret|password|passwd|token|access[_-]?key)"""
            r"""\s*[=:]\s*['"]([^'"]{12,})['"]"""
        ),
        value_group=1,
        min_entropy=3.0,
    ),
)


def _redact(value: str) -> str:
    """Never echo a live credential into the audit log."""
    head = value[:8]
    return f"{head}...({len(value)} chars)"


def _looks_like_placeholder(value: str, line: str) -> bool:
    """True when the match is documentation, not a credential."""
    if _PLACEHOLDER_MARKERS.search(value):
        return True
    if _ENV_VAR_NAME.match(value):
        return True
    # `<sk-ant-...>` or `${API_KEY}` wrapping means the value is a template slot.
    idx = line.find(value)
    if idx > 0 and line[idx - 1] in "<{$" and idx + len(value) < len(line):
        return True
    return False


class SecretScanRule:
    """Stops credentials that a model invented or echoed from ever reaching disk.

    This is the rule that protects the public repository this orchestrator
    ships in, so it reports the artifact and line number and redacts the match
    itself rather than copying a live credential into the audit trail.
    """

    rule_id = "security.secret_scan"
    severity = Severity.CRITICAL

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        violations: list[PolicyViolation] = []
        seen: set[tuple[str, str]] = set()
        for artifact in artifacts:
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                for spec in _SECRET_PATTERNS:
                    for match in spec.pattern.finditer(line):
                        value = match.group(spec.value_group)
                        if _looks_like_placeholder(value, line):
                            continue
                        if spec.min_entropy and _shannon_entropy(value) < spec.min_entropy:
                            continue
                        location = _location(artifact, line_no)
                        key = (location, value)
                        if key in seen:
                            continue
                        seen.add(key)
                        violations.append(
                            PolicyViolation(
                                rule_id=self.rule_id,
                                severity=self.severity,
                                message=(
                                    f"{spec.label} found in generated content "
                                    f"({_redact(value)}). Rotate the credential and load it "
                                    f"from the environment instead."
                                ),
                                location=location,
                            )
                        )
        return violations


# --------------------------------------------------------------------------
# Rule: open redirect and SSRF in the URL shortener under construction
# --------------------------------------------------------------------------

# Deliberately call-shaped. A requirement string that says "redirect visitors
# to the original URL" is not redirect handling, and a dataclass field named
# `location:` is not a Location header.
_REDIRECT_MARKER = re.compile(
    r"""(?ix)
    \bredirect\w*\s*\(                 # redirect(...), redirect_to(...)
    | RedirectResponse | HttpResponseRedirect | send_redirect
    | (?-i:['"]Location['"])           # the header name, quoted and capitalized
    | (?-i:\bLocation\s*:\s*https?)    # a raw header line
    | status_code\s*=\s*30[12378]
    """
)
_SCHEME_ALLOWLIST_NAME = re.compile(
    r"""(?ix)
    \b(?:allowed?_schemes|safe_schemes|permitted_schemes|scheme_allowlist
    | is_safe_url | validate_(?:url|redirect|destination|target))\b
    """
)
_SCHEME_TOKEN = re.compile(r"(?i)\bschemes?\b")
_HTTP_PREFIX_CHECK = re.compile(r"""(?i)startswith\s*\(\s*\(?\s*['"]https?://""")
_GUARD_ACTION = re.compile(
    r"""(?ix)
    \bnot\s+in\b | \bin\b | == | != | \braise\b | \babort\b | \breject\b | \bblock\w*\b
    | \bdeny\b | \binvalid\b | \breturn\s+(?:None|False|null)\b | \b40[0-9]\b
    | startswith | \bassert\b | \bif\b
    """
)
_INTERNAL_HOST_TOKEN = re.compile(
    r"""(?ix)
    localhost | 127\.0\.0\. | 0\.0\.0\.0 | 169\.254\.169\.254 | \b10\.\d+\.
    | 192\.168\. | 172\.(?:1[6-9]|2\d|3[01])\. | \b(?:::1|\[::1\])
    | is_private | is_loopback | is_link_local | private_network
    | \bmetadata\b | internal_host
    """
)
_IP_INSPECTION = re.compile(r"(?i)\b(?:is_private|is_loopback|is_link_local|ip_address\s*\()")
# Anchored so that a comment like `# file: app.py` is not read as a `file://` URL.
_DANGEROUS_SCHEME_URL = re.compile(
    r"""(?ix)
    (?: ^ | ['"(=,\[\s] )
    (javascript | vbscript | data | file )
    (?: \s*:(?!\s|//) | \s*:[a-z]+/ | \s*:// )
    """
)
_DANGEROUS_SCHEME_LISTED = re.compile(r"""(?i)['"](javascript|vbscript|data|file):?['"]""")
_BLOCK_CONTEXT = re.compile(
    r"""(?ix)
    block\w* | deny\w* | forbid\w* | unsafe | dangerous | reject\w* | disallow\w*
    | \bbanned\b | \bbad_ | \bnot\s+in\b | \bstrip\b | \bsanitiz | \bescape\b
    """
)
_CODE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".java",
    ".php",
    ".rs",
    ".cs",
)
_CODE_MARKER = re.compile(
    r"""(?x)
    ^\s*(?:def|class|async\s+def|function|const|let|var|func|import|from|package|public)\b
    | =>\s*\{ | \breturn\b
    """,
    re.MULTILINE,
)


def _is_code(artifact: Artifact) -> bool:
    if artifact.path and artifact.path.endswith(_CODE_SUFFIXES):
        return True
    if artifact.name.endswith(_CODE_SUFFIXES):
        return True
    if "python" in artifact.media_type or "javascript" in artifact.media_type:
        return True
    return bool(_CODE_MARKER.search(artifact.content))


def _guarded(content: str, marker: re.Pattern[str], action: re.Pattern[str]) -> bool:
    """A guard is a marker and a decision on the same line, not a bare mention."""
    return any(
        marker.search(line) and action.search(line) for line in content.splitlines()
    )


class OpenRedirectRule:
    """An unvalidated shortener redirect is a phishing and SSRF weapon, not a bug.

    Fires on redirect handling that accepts `javascript:`, `data:`, `file:`, or
    `vbscript:` destinations, that never validates the destination scheme, or
    that never blocks loopback, RFC1918, and link-local hosts (including the
    169.254.169.254 cloud metadata endpoint). Only code artifacts are scanned,
    so prose describing a redirect is not mistaken for one.
    """

    rule_id = "security.open_redirect"
    severity = Severity.HIGH

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        violations: list[PolicyViolation] = []

        # The guard evidence is gathered across every code artifact the node
        # produced, not just the file holding the redirect handler.
        #
        # A live run made the reason concrete. The generated service put its
        # redirect in app/main.py and its SSRF validation in a dedicated
        # app/urls.py, with a scheme allowlist, RFC1918 and link-local network
        # checks, and the cloud metadata address called out by name. Scanning
        # per-file could not see it, so the rule denied genuinely correct code.
        #
        # That is the worst failure mode available to a guardrail: it punished
        # good modularity, and because the denial is fed into the retry it
        # pressured the next attempt to inline the check purely to satisfy the
        # gate. A gate that distorts the work is worse than no gate.
        corpus = "\n".join(a.content for a in artifacts if _is_code(a))

        for artifact in artifacts:
            if not _is_code(artifact):
                continue
            content = artifact.content
            marker = _REDIRECT_MARKER.search(content)
            if marker is None:
                continue
            marker_line = _line_of(content, marker.start())

            for line_no, line in enumerate(content.splitlines(), start=1):
                if _BLOCK_CONTEXT.search(line):
                    continue
                hits = {m.group(1).lower() for m in _DANGEROUS_SCHEME_URL.finditer(line)}
                if _SCHEME_ALLOWLIST_NAME.search(line):
                    hits |= {m.group(1).lower() for m in _DANGEROUS_SCHEME_LISTED.finditer(line)}
                for scheme in sorted(hits):
                    violations.append(
                        PolicyViolation(
                            rule_id=self.rule_id,
                            severity=self.severity,
                            message=(
                                f"redirect target accepts the '{scheme}:' scheme, which "
                                f"executes in the victim browser or reads local state: "
                                f"{_excerpt(line)}"
                            ),
                            location=_location(artifact, line_no),
                        )
                    )

            scheme_validated = bool(
                _SCHEME_ALLOWLIST_NAME.search(corpus)
                or _HTTP_PREFIX_CHECK.search(corpus)
                or _guarded(corpus, _SCHEME_TOKEN, _GUARD_ACTION)
            )
            if not scheme_validated:
                violations.append(
                    PolicyViolation(
                        rule_id=self.rule_id,
                        severity=self.severity,
                        message=(
                            "redirect handling never validates the destination scheme; "
                            "restrict it to an http/https allowlist before issuing the "
                            "Location header"
                        ),
                        location=_location(artifact, marker_line),
                    )
                )

            host_blocked = bool(
                _IP_INSPECTION.search(corpus)
                or _guarded(corpus, _INTERNAL_HOST_TOKEN, _GUARD_ACTION)
            )
            if not host_blocked:
                violations.append(
                    PolicyViolation(
                        rule_id=self.rule_id,
                        severity=self.severity,
                        message=(
                            "redirect handling never blocks internal hosts; localhost, "
                            "127.0.0.0/8, 10/8, 172.16/12, 192.168/16 and the "
                            "169.254.169.254 metadata endpoint stay reachable"
                        ),
                        location=_location(artifact, marker_line),
                    )
                )
        return violations


# --------------------------------------------------------------------------
# Rule: destructive operations
# --------------------------------------------------------------------------

_RM_FLAGS = re.compile(r"\brm\s+((?:-{1,2}[A-Za-z][\w-]*\s*)+)")
_RMTREE = re.compile(r"\bshutil\.rmtree\s*\(\s*([^)\n]*)")
_DROP_OBJECT = re.compile(r"(?i)\bDROP\s+(TABLE|DATABASE|SCHEMA)\b[^\n;]*")
# The `TABLE` keyword or a statement terminator keeps "truncate the message"
# in an English sentence from reading as SQL.
_TRUNCATE = re.compile(
    r"""(?ix) \bTRUNCATE\s+ (?: TABLE\s+[\w."`\[\]]+ | [\w."`\[\]]+\s*; )"""
)
_DELETE_FROM = re.compile(
    r"""(?ix)
    \bDELETE\s+FROM\s+
    (?! (?:the|a|an|this|that|these|those|our|your|it|them|here|any|all)\b )
    [\w."`\[\]]+
    """
)
_WHERE = re.compile(r"(?i)\bWHERE\b")
_OS_SYSTEM = re.compile(r"\bos\.system\s*\(")
# Argument position required, so a docstring saying "never `shell=True`" is
# not read as a call that does.
_SHELL_TRUE = re.compile(r"(?i)[,(]\s*shell\s*=\s*True\b")
_EVAL_CALL = re.compile(r"(?<![\w.])eval\s*\(")
_EXEC_CALL = re.compile(r"(?<![\w.])exec\s*\(")
_WORKSPACE_TOKEN = re.compile(
    r"""(?ix)
    workspace | sandbox | scratch | \btmp\b | tempfile | temp_dir | tmpdir | tmp_path
    | \.cache\b | build_dir | dist_dir | staging_dir | run_dir | artifacts?_dir
    """
)


def _statement(content: str, start: int, span: int = 400) -> str:
    """The SQL statement beginning at `start`, up to its terminator."""
    window = content[start : start + span]
    return window.split(";", 1)[0]


class DestructiveOperationRule:
    """Model-produced content gets executed, so irreversible operations need a human.

    Catches recursive force deletes, `shutil.rmtree` aimed outside the managed
    workspace, `DROP TABLE`/`TRUNCATE`, unbounded `DELETE FROM`, and the
    arbitrary-execution trio `os.system`, `eval(`, and `exec(`.
    """

    rule_id = "safety.destructive_operation"
    severity = Severity.CRITICAL

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        violations: list[PolicyViolation] = []
        for artifact in artifacts:
            content = artifact.content

            # Both loop variables are bound as defaults. `_artifact` alone was
            # bound before, which works only because flag() is called
            # synchronously; binding one and not the other means a later
            # refactor that defers the call would report the right file with a
            # line number computed from a different one.
            def flag(
                index: int,
                message: str,
                _artifact: Artifact = artifact,
                _content: str = content,
            ) -> None:
                violations.append(
                    PolicyViolation(
                        rule_id=self.rule_id,
                        severity=self.severity,
                        message=message,
                        location=_location(_artifact, _line_of(_content, index)),
                    )
                )

            for match in _RM_FLAGS.finditer(content):
                flags = match.group(1)
                short = "".join(f.lstrip("-") for f in flags.split() if not f.startswith("--"))
                recursive = "r" in short.lower() or "--recursive" in flags
                force = "f" in short or "--force" in flags
                if recursive and force:
                    flag(
                        match.start(),
                        f"recursive force delete: {_excerpt(content[match.start():match.start() + 60])}",
                    )

            for match in _RMTREE.finditer(content):
                target = match.group(1).strip()
                if _WORKSPACE_TOKEN.search(target):
                    continue
                flag(
                    match.start(),
                    f"shutil.rmtree targets a path outside the managed workspace: "
                    f"rmtree({_excerpt(target)})",
                )

            for match in _DROP_OBJECT.finditer(content):
                flag(match.start(), f"schema destruction: {_excerpt(match.group(0))}")

            for match in _TRUNCATE.finditer(content):
                flag(match.start(), f"table truncation: {_excerpt(match.group(0))}")

            for match in _DELETE_FROM.finditer(content):
                if _WHERE.search(_statement(content, match.start())):
                    continue
                flag(
                    match.start(),
                    f"unbounded delete with no WHERE clause: {_excerpt(match.group(0))}",
                )

            for match in _OS_SYSTEM.finditer(content):
                flag(match.start(), "os.system passes model-produced text to a shell")

            for match in _SHELL_TRUE.finditer(content):
                flag(match.start(), "subprocess call with shell=True on model-produced text")

            for match in _EVAL_CALL.finditer(content):
                flag(match.start(), "eval() executes model-produced text as code")

            for match in _EXEC_CALL.finditer(content):
                flag(match.start(), "exec() executes model-produced text as code")

        return violations


# --------------------------------------------------------------------------
# Rule: change control
# --------------------------------------------------------------------------


class ChangeControlRule:
    """High-impact work needs recorded human sign-off before its output is accepted.

    The approved set is the evidence: a node whose `impact` is HIGH and whose
    id is absent from that set is denied at its exit gate, once it has actually
    produced something. Entry gates see no produced artifacts and therefore
    pass, which is what lets the orchestrator run the node and then hold its
    output pending approval.
    """

    rule_id = "change_control.approval_required"
    severity = Severity.HIGH

    def __init__(self, approved_nodes: set[str] | None = None) -> None:
        self.approved_nodes: set[str] = set(approved_nodes or ())

    def approve(self, node_id: str) -> None:
        """Record a human decision so later gates on the same node pass."""
        self.approved_nodes.add(node_id)

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        if not node.needs_approval:
            return []
        if node.id in self.approved_nodes:
            return []
        produced = _produced_by(artifacts, node)
        if not produced:
            return []
        names = ", ".join(sorted(a.name for a in produced))
        return [
            PolicyViolation(
                rule_id=self.rule_id,
                severity=self.severity,
                message=(
                    f"node '{node.id}' is {ImpactLevel.HIGH.value} impact and produced "
                    f"{len(produced)} artifact(s) ({names}) without recorded approval"
                ),
                location=f"node:{node.id}",
            )
        ]


# --------------------------------------------------------------------------
# Rule: test evidence
# --------------------------------------------------------------------------

_RUN_EVIDENCE = (
    re.compile(r"(?i)\b\d+\s+(?:passed|failed|error|errors|skipped|xfailed)\b"),
    re.compile(r"(?i)\bRan\s+\d+\s+tests?\b"),
    re.compile(r"(?i)\bTests?\s+run:\s*\d+"),
    re.compile(r"(?i)<testsuite\b[^>]*\btests\s*="),
    re.compile(r"(?i)\bshort test summary info\b|\btest session starts\b"),
    re.compile(r"(?i)\bOK\s*\(\s*\d+\s+tests?"),
    re.compile(r"(?i)\bexit\s*(?:code|status)\s*[:=]\s*\d+"),
    re.compile(r"(?i)\b(?:PASSED|FAILED)\b\s+\S+::\S+"),
    re.compile(r"(?i)\bcoverage\b[^\n]*\b\d{1,3}%"),
)
_CLAIMS_PASS = re.compile(
    r"""(?ix)
    all\s+(?:the\s+)?tests?\s+pass\w* | tests?\s+(?:are\s+)?passing | suite\s+is\s+green
    | 100\s*%\s+pass\w* | everything\s+passes | no\s+(?:test\s+)?failures
    | \bgreen\s+across\s+the\s+board\b
    """
)


# A node opts into this rule by declaring that it executes tests, rather than
# by being of a particular kind. A plan has two TEST-kind nodes with different
# jobs: one authors the suite, one runs it. Only the second can produce a
# transcript, and keying on kind made the first fail a rule it could never
# satisfy. Keying on the declared contract also means the rule enforces what
# the node promised rather than what its type implies.
EXECUTES_TESTS_RULE = "tests_executed"


class TestEvidenceRule:
    """A stage that claims tests ran without a transcript is a hallucination risk.

    A node that declares `tests_executed` among its exit rules must produce at
    least one artifact containing output only an executed run can produce: a
    pytest summary line, a JUnit report, a process exit status. Claiming a pass
    with no such transcript is the failure mode this rule exists to catch.

    Nodes that author tests without running them are out of scope, since they
    have nothing to show and demanding it would block honest work.
    """

    rule_id = "quality.test_evidence"
    severity = Severity.HIGH

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        if EXECUTES_TESTS_RULE not in node.exit_rules:
            return []
        produced = _produced_by(artifacts, node)
        if not produced:
            return []
        if any(self._has_run_evidence(a.content) for a in produced):
            return []

        claimers = [a for a in produced if _CLAIMS_PASS.search(a.content)]
        if claimers:
            return [
                PolicyViolation(
                    rule_id=self.rule_id,
                    severity=self.severity,
                    message=(
                        "test artifact claims a passing suite but carries no transcript of "
                        "an executed run (no pass/fail counts, JUnit report, or exit status)"
                    ),
                    location=_location(a, self._claim_line(a.content)),
                )
                for a in claimers
            ]
        names = ", ".join(sorted(a.name for a in produced))
        return [
            PolicyViolation(
                rule_id=self.rule_id,
                severity=self.severity,
                message=(
                    f"test node '{node.id}' produced no artifact containing real test "
                    f"results (saw: {names})"
                ),
                location=f"node:{node.id}",
            )
        ]

    @staticmethod
    def _has_run_evidence(content: str) -> bool:
        return any(pattern.search(content) for pattern in _RUN_EVIDENCE)

    @staticmethod
    def _claim_line(content: str) -> int:
        match = _CLAIMS_PASS.search(content)
        return _line_of(content, match.start()) if match else 1


# --------------------------------------------------------------------------
# Rule: PII in stored URLs
# --------------------------------------------------------------------------

_ABSOLUTE_URL = re.compile(r"""(?i)\b(?:https?|ftp)://[^\s'"<>)\]}\\]+""")
# The `/` in the lookbehind keeps the path of an absolute URL from being
# rediscovered as a second, relative URL on the same line.
_RELATIVE_URL = re.compile(r"""(?<![\w:/])/[\w/\-.%]*\?[^\s'"<>)\]}\\]+""")
_EMAIL_IN_URL = re.compile(r"(?i)[A-Za-z0-9._%+\-]+(?:@|%40)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SSN_SHAPED = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_RESERVED_DOMAIN = re.compile(r"(?i)(?:example\.(?:com|org|net)|\.invalid|\.test|localhost)")


class PIIInUrlRule:
    """Shortened URLs get logged, shared, and mined, so identifiers must not ride in them.

    Flags email addresses (plain or percent-encoded) and SSN-shaped digits
    inside absolute or query-bearing relative URLs. Reserved documentation
    domains are exempt because example.com addresses are not real people.
    """

    rule_id = "privacy.pii_in_url"
    severity = Severity.MEDIUM

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        violations: list[PolicyViolation] = []
        for artifact in artifacts:
            for line_no, line in enumerate(artifact.content.splitlines(), start=1):
                for url_match in (*_ABSOLUTE_URL.finditer(line), *_RELATIVE_URL.finditer(line)):
                    url = url_match.group(0)
                    email = _EMAIL_IN_URL.search(url)
                    if email and not _RESERVED_DOMAIN.search(email.group(0)):
                        violations.append(
                            PolicyViolation(
                                rule_id=self.rule_id,
                                severity=self.severity,
                                message=(
                                    f"email address embedded in a stored URL: "
                                    f"{_excerpt(url)}"
                                ),
                                location=_location(artifact, line_no),
                            )
                        )
                    ssn = _SSN_SHAPED.search(url)
                    if ssn:
                        violations.append(
                            PolicyViolation(
                                rule_id=self.rule_id,
                                severity=self.severity,
                                message=(
                                    f"SSN-shaped value embedded in a stored URL: "
                                    f"{_excerpt(url)}"
                                ),
                                location=_location(artifact, line_no),
                            )
                        )
        return violations


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class PolicyEngine:
    """Evaluates the registered rules at one gate and returns an auditable decision."""

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self._rules: list[PolicyRule] = []
        for rule in rules or []:
            self.register(rule)

    # -- rule set ---------------------------------------------------------

    def record_approval(self, node_id: str) -> None:
        """Tell the rules a human approved this node.

        Without this the change-control rule holds whatever approval set it was
        constructed with, which in a live run is empty, so an approved node
        fails its own exit gate for not being approved. A live brownfield run
        did exactly that: the broker recorded approved=true and the gate denied
        the same node moments later.

        Forwarding by capability rather than by type keeps the engine from
        knowing which rules care.
        """
        for rule in self._rules:
            approve = getattr(rule, "approve", None)
            if callable(approve):
                approve(node_id)

    def register(self, rule: PolicyRule) -> PolicyEngine:
        """Add a rule. Returns self so a rule set can be built in one expression."""
        if not isinstance(rule, PolicyRule):
            raise TypeError(f"{rule!r} does not satisfy the PolicyRule protocol")
        if rule.rule_id in self.rule_ids:
            raise ValueError(f"duplicate rule_id: {rule.rule_id}")
        self._rules.append(rule)
        return self

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._rules)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(r.rule_id for r in self._rules)

    # -- evaluation -------------------------------------------------------

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        """Run every rule and return findings, most severe first.

        A rule that raises becomes a blocking violation of its own rather than
        being swallowed, so a broken guardrail cannot quietly open a gate.
        """
        findings: list[PolicyViolation] = []
        for rule in self._rules:
            try:
                findings.extend(rule.evaluate(artifacts, node))
            except Exception as exc:  # noqa: BLE001 - deliberate fail-closed boundary
                findings.append(
                    PolicyViolation(
                        rule_id=rule.rule_id,
                        severity=Severity.HIGH,
                        message=(
                            f"policy rule raised {type(exc).__name__}: {exc}. "
                            f"Treating as blocking because an unevaluated rule is not a pass."
                        ),
                        location=f"node:{node.id}",
                    )
                )
        findings.sort(key=lambda v: _SEVERITY_ORDER.get(v.severity, 9))
        return findings

    def decide(self, gate: str, node: NodeSpec, artifacts: list[Artifact]) -> GateDecision:
        """Close the gate if anything blocking fired, naming the rules in the reason."""
        violations = self.evaluate(artifacts, node)
        blocking = [v for v in violations if v.blocks]
        if blocking:
            return GateDecision.deny(
                gate=gate,
                node_id=node.id,
                reason=f"{gate} gate blocked by {_summarize(blocking)}",
                violations=violations,
            )
        if violations:
            reason = f"allowed with advisory findings: {_summarize(violations)}"
        else:
            reason = f"no policy violations across {len(self._rules)} rule(s)"
        return GateDecision(
            gate=gate,
            node_id=node.id,
            allowed=True,
            reason=reason,
            violations=violations,
        )


def _summarize(violations: list[PolicyViolation]) -> str:
    """Render `rule_id (severity xN)` in severity order for the decision reason."""
    counts: dict[tuple[str, Severity], int] = {}
    for violation in violations:
        counts[(violation.rule_id, violation.severity)] = (
            counts.get((violation.rule_id, violation.severity), 0) + 1
        )
    parts = []
    for (rule_id, severity), count in sorted(
        counts.items(), key=lambda kv: (_SEVERITY_ORDER.get(kv[0][1], 9), kv[0][0])
    ):
        suffix = f" x{count}" if count > 1 else ""
        parts.append(f"{rule_id} ({severity.value}{suffix})")
    return ", ".join(parts)


def default_engine(approved_nodes: set[str] | None = None) -> PolicyEngine:
    """The standard guardrail set every run starts with.

    Ordered by severity so the audit log reads worst-first even before the
    findings are sorted.
    """
    return PolicyEngine(
        [
            SecretScanRule(),
            DestructiveOperationRule(),
            OpenRedirectRule(),
            ChangeControlRule(approved_nodes),
            TestEvidenceRule(),
            PIIInUrlRule(),
        ]
    )
