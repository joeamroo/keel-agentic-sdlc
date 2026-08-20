"""Tests for the governance policy engine.

Each rule gets a positive case (the finding it exists to catch) and a negative
case (the thing that must not be mistaken for it). The negative cases matter
more than the positive ones: a guardrail that cries wolf gets switched off.
"""

from __future__ import annotations

import pytest

from keel.governance.policy import (
    ChangeControlRule,
    DestructiveOperationRule,
    OpenRedirectRule,
    PIIInUrlRule,
    PolicyEngine,
    SecretScanRule,
    TestEvidenceRule,
    default_engine,
)
from keel.models import (
    Artifact,
    ImpactLevel,
    NodeSpec,
    PolicyRule,
    PolicyViolation,
    Severity,
    StageKind,
)

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def art(
    content: str,
    name: str = "app.py",
    produced_by: str = "n1",
    path: str | None = None,
) -> Artifact:
    return Artifact(name=name, content=content, produced_by=produced_by, path=path)


def node(
    node_id: str = "n1",
    kind: StageKind = StageKind.IMPLEMENT,
    impact: ImpactLevel = ImpactLevel.LOW,
    exit_rules: list[str] | None = None,
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        kind=kind,
        description="test node",
        impact=impact,
        exit_rules=list(exit_rules or []),
    )


# A node opts into the evidence rule by declaring that it executes tests, not by
# being of kind TEST. A plan holds two TEST nodes: one authors the suite and one
# runs it, and only the second can produce a transcript.
def runs_tests(node_id: str = "t1") -> NodeSpec:
    return node(node_id, kind=StageKind.TEST, exit_rules=["tests_executed"])


def live_anthropic_key() -> str:
    """A correctly shaped Anthropic key, assembled at import time on purpose.

    The literal is split so that no contiguous key-shaped string ever sits in
    the repository, which keeps push-protection scanners quiet while still
    exercising the exact format the rule has to catch.
    """
    body = "R7kQ2vN8pLdT4wZs6YbG1hJmC3xF5nA9" * 3
    return "sk-" + "ant-" + "api03-" + body


LINES = "line one\nline two\n"


# --------------------------------------------------------------------------
# SecretScanRule
# --------------------------------------------------------------------------


def test_real_anthropic_key_format_is_caught() -> None:
    key = live_anthropic_key()
    violations = SecretScanRule().evaluate([art(f'ANTHROPIC_KEY = "{key}"')], node())
    assert len(violations) == 1
    assert violations[0].severity is Severity.CRITICAL
    assert violations[0].blocks
    assert "Anthropic API key" in violations[0].message


def test_anthropic_placeholder_is_not_flagged() -> None:
    content = "\n".join(
        [
            'ANTHROPIC_API_KEY = "sk-ant-your-key-here"',
            'FALLBACK = "sk-ant-api03-your-api-key-here-replace-before-running"',
            'FROM_ENV = "${ANTHROPIC_API_KEY}"',
            'DOCS = "<sk-ant-api03-abcdefghijklmnopqrstuvwxyz>"',
            'CHANGEME = "sk-ant-api03-CHANGEME-CHANGEME-CHANGEME-CHANGEME"',
        ]
    )
    assert SecretScanRule().evaluate([art(content, name=".env.example")], node()) == []


def test_secret_violation_names_artifact_and_line() -> None:
    key = live_anthropic_key()
    content = LINES + f'settings = dict(key="{key}")'
    violations = SecretScanRule().evaluate([art(content, name="config.py")], node())
    assert violations[0].location == "config.py:3"


def test_secret_location_includes_workspace_path_when_present() -> None:
    key = live_anthropic_key()
    artifact = art(f'K = "{key}"', name="config.py", path="src/config.py")
    violations = SecretScanRule().evaluate([artifact], node())
    assert violations[0].location == "config.py[src/config.py]:1"


def test_secret_message_redacts_the_credential() -> None:
    key = live_anthropic_key()
    violations = SecretScanRule().evaluate([art(f'K = "{key}"')], node())
    assert key not in violations[0].message
    assert "..." in violations[0].message


def test_aws_access_key_is_caught() -> None:
    content = 'AWS_ACCESS_KEY_ID = "' + "AKIA" + '2R7KQ4VN8PLDT3WZ"'
    violations = SecretScanRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "AWS access key id" in violations[0].message


def test_aws_documentation_key_is_not_flagged() -> None:
    content = 'AWS_ACCESS_KEY_ID = "' + "AKIA" + 'IOSFODNN7EXAMPLE"\nOTHER = "' + "AKIA" + 'XXXXXXXXXXXXXXXX"'
    assert SecretScanRule().evaluate([art(content)], node()) == []


def test_private_key_pem_header_is_caught() -> None:
    content = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----"
    violations = SecretScanRule().evaluate([art(content, name="id_rsa")], node())
    assert len(violations) == 1
    assert "private key" in violations[0].message.lower()
    assert violations[0].location == "id_rsa:1"


def test_jwt_is_caught() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dQw4w9WgXcQ7bLmVpNr2Zt"
    violations = SecretScanRule().evaluate([art(f'auth = "{jwt}"')], node())
    assert len(violations) == 1
    assert "JSON Web Token" in violations[0].message


def test_slack_token_is_caught() -> None:
    content = 'SLACK_BOT_TOKEN = "' + "xoxb" + '-2314872144-2314872144-abcDEF12ghiJKL34mnoPQR56"'
    violations = SecretScanRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "Slack" in violations[0].message


def test_github_token_is_caught() -> None:
    content = 'gh = "' + "ghp" + '_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"'
    violations = SecretScanRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "GitHub" in violations[0].message


def test_generic_api_key_assignment_is_caught() -> None:
    content = 'api_key = "' + "Zt7Qb2Wn" + '9Kp4Vr8Ls3Md6Xc1"'
    violations = SecretScanRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert violations[0].location == "app.py:1"


def test_env_var_name_as_value_is_not_flagged() -> None:
    content = '{"api_key": "ANTHROPIC_API_KEY", "secret": "STRIPE_WEBHOOK_SECRET"}'
    assert SecretScanRule().evaluate([art(content, name="settings.json")], node()) == []


def test_low_entropy_api_key_value_is_not_flagged() -> None:
    content = 'api_key = "aaaaaaaaaaaaaaaaaaaa"\napi_key = "----------------"'
    assert SecretScanRule().evaluate([art(content)], node()) == []


def test_env_lookup_is_not_flagged() -> None:
    content = 'api_key = os.environ["ANTHROPIC_API_KEY"]\ntoken = os.getenv("GH_TOKEN")'
    assert SecretScanRule().evaluate([art(content)], node()) == []


def test_clean_source_produces_no_secret_findings() -> None:
    content = "def shorten(url: str) -> str:\n    return hashlib.sha256(url.encode()).hexdigest()[:7]"
    assert SecretScanRule().evaluate([art(content)], node()) == []


def test_duplicate_secret_on_one_line_reported_once() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dQw4w9WgXcQ7bLmVpNr2Zt"
    violations = SecretScanRule().evaluate([art(f'token = "{jwt}"')], node())
    assert len(violations) == 1


# --------------------------------------------------------------------------
# OpenRedirectRule
# --------------------------------------------------------------------------

VULNERABLE_REDIRECT = """
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

app = FastAPI()
STORE = {}

@app.get("/{code}")
def follow(code):
    target = STORE[code]
    return RedirectResponse(target, status_code=302)
"""

SAFE_REDIRECT = """
import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}

def validate_url(candidate):
    parts = urlparse(candidate)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValueError("scheme not allowed")
    ip = ipaddress.ip_address(socket.gethostbyname(parts.hostname))
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        raise ValueError("internal host blocked")
    return candidate

def follow(code):
    return redirect(validate_url(STORE[code]))
"""


def test_unvalidated_redirect_is_flagged_for_scheme_and_host() -> None:
    violations = OpenRedirectRule().evaluate([art(VULNERABLE_REDIRECT)], node())
    messages = " ".join(v.message for v in violations)
    assert "scheme" in messages
    assert "internal hosts" in messages
    assert all(v.severity is Severity.HIGH and v.blocks for v in violations)
    assert all(v.location and v.location.startswith("app.py:") for v in violations)


def test_safe_redirect_is_not_flagged() -> None:
    assert OpenRedirectRule().evaluate([art(SAFE_REDIRECT)], node()) == []


def test_javascript_scheme_in_redirect_is_flagged() -> None:
    content = SAFE_REDIRECT + '\nDEFAULT_TARGET = "javascript:alert(document.cookie)"\n'
    violations = OpenRedirectRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "javascript:" in violations[0].message


def test_data_and_file_schemes_in_redirect_are_flagged() -> None:
    content = (
        SAFE_REDIRECT
        + '\nFALLBACKS = ["data:text/html;base64,PHNjcmlwdD4=", "file:///etc/passwd"]\n'
    )
    violations = OpenRedirectRule().evaluate([art(content)], node())
    schemes = {v.message.split("'")[1] for v in violations}
    assert schemes == {"data:", "file:"}


def test_scheme_allowlist_containing_javascript_is_flagged() -> None:
    content = SAFE_REDIRECT.replace(
        'ALLOWED_SCHEMES = {"http", "https"}',
        'ALLOWED_SCHEMES = {"http", "https", "javascript"}',
    )
    violations = OpenRedirectRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "javascript" in violations[0].message


def test_scheme_blocklist_is_not_mistaken_for_acceptance() -> None:
    content = SAFE_REDIRECT + '\nBLOCKED_SCHEMES = ("javascript:", "vbscript:", "file://")\n'
    assert OpenRedirectRule().evaluate([art(content)], node()) == []


def test_prose_describing_a_redirect_is_not_scanned() -> None:
    prose = (
        "The shortener will redirect the visitor to whatever target was stored "
        "at creation time, so the design must say who validates it."
    )
    assert OpenRedirectRule().evaluate([art(prose, name="design.md")], node()) == []


def test_requirement_prose_inside_a_python_file_is_not_redirect_handling() -> None:
    content = (
        "REQUIREMENT = (\n"
        '    "Build a URL shortener. It must redirect visitors from the short link "\n'
        '    "to the original. The service must not be usable as an open redirector."\n'
        ")\n"
    )
    assert OpenRedirectRule().evaluate([art(content, name="scenarios.py")], node()) == []


def test_dataclass_location_field_is_not_a_location_header() -> None:
    content = "@dataclass\nclass Violation:\n    location: str | None = None\n"
    assert OpenRedirectRule().evaluate([art(content, name="models.py")], node()) == []


def test_lowercase_location_payload_key_is_not_a_location_header() -> None:
    content = 'row = {"message": msg, "location": "config/app.yml:12"}\n'
    assert OpenRedirectRule().evaluate([art(content, name="audit.py")], node()) == []


def test_capitalized_location_header_assignment_is_scanned() -> None:
    content = 'def follow(code):\n    response.headers["Location"] = STORE[code]\n    return response\n'
    violations = OpenRedirectRule().evaluate([art(content)], node())
    assert len(violations) == 2
    assert violations[0].location == "app.py:2"


def test_code_without_redirect_handling_is_ignored() -> None:
    content = "def shorten(url):\n    return SLUGS[url]\n"
    assert OpenRedirectRule().evaluate([art(content)], node()) == []


# --------------------------------------------------------------------------
# DestructiveOperationRule
# --------------------------------------------------------------------------


def test_rm_rf_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art("subprocess.run('rm -rf /var/lib/data', shell=False)")], node()
    )
    assert len(violations) == 1
    assert violations[0].severity is Severity.CRITICAL
    assert "recursive force delete" in violations[0].message


def test_non_destructive_rm_is_not_flagged() -> None:
    content = "os.remove(path)\n# rm -i one_file.txt after confirming\n"
    assert DestructiveOperationRule().evaluate([art(content)], node()) == []


def test_rmtree_outside_workspace_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art("shutil.rmtree(Path.home() / 'Documents')")], node()
    )
    assert len(violations) == 1
    assert "outside the managed workspace" in violations[0].message


def test_rmtree_inside_workspace_is_not_flagged() -> None:
    content = "shutil.rmtree(self.workspace / 'build')\nshutil.rmtree(tmp_path)\n"
    assert DestructiveOperationRule().evaluate([art(content)], node()) == []


def test_drop_table_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art(LINES + "cur.execute('DROP TABLE links')", name="migrate.py")], node()
    )
    assert len(violations) == 1
    assert violations[0].location == "migrate.py:3"


def test_truncate_table_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art("TRUNCATE TABLE click_events;", name="reset.sql")], node()
    )
    assert len(violations) == 1
    assert "truncation" in violations[0].message


def test_truncate_in_english_prose_is_not_flagged() -> None:
    content = "We truncate the message before logging it so the line stays readable."
    assert DestructiveOperationRule().evaluate([art(content, name="notes.md")], node()) == []


def test_unbounded_delete_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art("DELETE FROM links;", name="cleanup.sql")], node()
    )
    assert len(violations) == 1
    assert "no WHERE clause" in violations[0].message


def test_delete_with_where_clause_is_not_flagged() -> None:
    content = "DELETE FROM links\nWHERE created_at < now() - interval '30 days';"
    assert DestructiveOperationRule().evaluate([art(content, name="cleanup.sql")], node()) == []


def test_delete_from_in_prose_is_not_flagged() -> None:
    content = "The worker will delete from the cache whatever expired overnight."
    assert DestructiveOperationRule().evaluate([art(content, name="notes.md")], node()) == []


def test_os_system_and_eval_and_exec_are_flagged() -> None:
    content = "os.system(cmd)\nvalue = eval(expr)\nexec(generated_source)\n"
    violations = DestructiveOperationRule().evaluate([art(content)], node())
    assert len(violations) == 3
    assert {v.location for v in violations} == {"app.py:1", "app.py:2", "app.py:3"}


def test_execute_and_dotted_eval_are_not_mistaken_for_exec() -> None:
    content = "cursor.execute(sql)\nresult = df.eval(expr)\nast.literal_eval(raw)\n"
    assert DestructiveOperationRule().evaluate([art(content)], node()) == []


def test_shell_true_is_flagged() -> None:
    violations = DestructiveOperationRule().evaluate(
        [art("subprocess.run(cmd, shell=True)")], node()
    )
    assert len(violations) == 1
    assert "shell=True" in violations[0].message


def test_docstring_warning_against_shell_true_is_not_flagged() -> None:
    content = (
        "def run(argv):\n"
        '    """Never `shell=True`: the argv comes from a generated plan."""\n'
        "    return subprocess.run(argv)\n"
    )
    assert DestructiveOperationRule().evaluate([art(content)], node()) == []


# --------------------------------------------------------------------------
# ChangeControlRule
# --------------------------------------------------------------------------


def test_high_impact_output_without_approval_is_flagged() -> None:
    high = node("deploy", impact=ImpactLevel.HIGH)
    violations = ChangeControlRule(set()).evaluate(
        [art("ok", name="release.txt", produced_by="deploy")], high
    )
    assert len(violations) == 1
    assert violations[0].severity is Severity.HIGH
    assert violations[0].blocks
    assert violations[0].location == "node:deploy"
    assert "release.txt" in violations[0].message


def test_high_impact_output_with_approval_passes() -> None:
    high = node("deploy", impact=ImpactLevel.HIGH)
    rule = ChangeControlRule({"deploy"})
    assert rule.evaluate([art("ok", produced_by="deploy")], high) == []


def test_approve_records_the_decision_for_later_gates() -> None:
    high = node("deploy", impact=ImpactLevel.HIGH)
    rule = ChangeControlRule()
    artifacts = [art("ok", produced_by="deploy")]
    assert len(rule.evaluate(artifacts, high)) == 1
    rule.approve("deploy")
    assert rule.evaluate(artifacts, high) == []


def test_low_impact_node_needs_no_approval() -> None:
    low = node("doc", impact=ImpactLevel.LOW)
    assert ChangeControlRule(set()).evaluate([art("ok", produced_by="doc")], low) == []


def test_high_impact_entry_gate_passes_before_anything_is_produced() -> None:
    high = node("deploy", impact=ImpactLevel.HIGH)
    upstream = [art("spec", name="spec.md", produced_by="design")]
    assert ChangeControlRule(set()).evaluate(upstream, high) == []


# --------------------------------------------------------------------------
# TestEvidenceRule
# --------------------------------------------------------------------------

PYTEST_OUTPUT = """
============================= test session starts ==============================
collected 12 items

tests/test_shorten.py ............                                       [100%]

============================== 12 passed in 0.41s ==============================
"""


def test_real_pytest_output_satisfies_the_evidence_rule() -> None:
    test_node = runs_tests("t1")
    artifact = art(PYTEST_OUTPUT, name="pytest.log", produced_by="t1")
    assert TestEvidenceRule().evaluate([artifact], test_node) == []


def test_claimed_pass_without_a_transcript_is_flagged() -> None:
    test_node = runs_tests("t1")
    artifact = art(
        "I wrote unit tests for the shortener and all tests pass.",
        name="test_report.md",
        produced_by="t1",
    )
    violations = TestEvidenceRule().evaluate([artifact], test_node)
    assert len(violations) == 1
    assert violations[0].severity is Severity.HIGH
    assert "claims a passing suite" in violations[0].message
    assert violations[0].location == "test_report.md:1"


def test_test_node_producing_no_results_artifact_is_flagged() -> None:
    test_node = runs_tests("t1")
    artifact = art("def test_shorten():\n    assert True\n", name="test_x.py", produced_by="t1")
    violations = TestEvidenceRule().evaluate([artifact], test_node)
    assert len(violations) == 1
    assert "no artifact containing real test results" in violations[0].message
    assert violations[0].location == "node:t1"


def test_junit_report_counts_as_evidence() -> None:
    test_node = runs_tests("t1")
    artifact = art(
        '<testsuite name="pytest" tests="12" failures="0"></testsuite>',
        name="junit.xml",
        produced_by="t1",
    )
    assert TestEvidenceRule().evaluate([artifact], test_node) == []


def test_non_test_node_is_not_asked_for_evidence() -> None:
    impl = node("i1", kind=StageKind.IMPLEMENT)
    artifact = art("all tests pass", name="notes.md", produced_by="i1")
    assert TestEvidenceRule().evaluate([artifact], impl) == []


def test_test_node_entry_gate_passes_before_anything_is_produced() -> None:
    test_node = runs_tests("t1")
    upstream = [art("code", name="app.py", produced_by="i1")]
    assert TestEvidenceRule().evaluate(upstream, test_node) == []


# --------------------------------------------------------------------------
# PIIInUrlRule
# --------------------------------------------------------------------------


def test_email_in_stored_url_is_flagged() -> None:
    content = LINES + 'STORE["a1b2c3"] = "https://crm.acme.io/leads?email=dana.reed@acme.io"'
    violations = PIIInUrlRule().evaluate([art(content, name="store.py")], node())
    assert len(violations) == 1
    assert violations[0].severity is Severity.MEDIUM
    assert not violations[0].blocks
    assert violations[0].location == "store.py:3"


def test_percent_encoded_email_in_url_is_flagged() -> None:
    content = 'target = "https://portal.acme.io/u/dana.reed%40acme.io/profile"'
    assert len(PIIInUrlRule().evaluate([art(content)], node())) == 1


def test_ssn_shaped_value_in_url_is_flagged() -> None:
    content = 'target = "https://records.example-agency.io/lookup?ssn=123-45-6789"'
    violations = PIIInUrlRule().evaluate([art(content)], node())
    assert len(violations) == 1
    assert "SSN-shaped" in violations[0].message


def test_ssn_in_relative_url_with_query_is_flagged() -> None:
    content = 'link = "/records/lookup?ssn=123-45-6789&source=crm"'
    assert len(PIIInUrlRule().evaluate([art(content)], node())) == 1


def test_documentation_email_domain_is_not_flagged() -> None:
    content = 'DOC = "https://short.ly/new?email=someone@example.com"'
    assert PIIInUrlRule().evaluate([art(content)], node()) == []


def test_email_outside_a_url_is_not_flagged() -> None:
    content = "# maintainer: dana.reed@acme.io\nOWNER = 'dana.reed@acme.io'\n"
    assert PIIInUrlRule().evaluate([art(content)], node()) == []


def test_clean_url_is_not_flagged() -> None:
    content = 'STORE["a1b2c3"] = "https://schwab.com/research/quotes?symbol=SCHW"'
    assert PIIInUrlRule().evaluate([art(content)], node()) == []


# --------------------------------------------------------------------------
# PolicyEngine
# --------------------------------------------------------------------------


class _ExplodingRule:
    rule_id = "test.exploding"
    severity = Severity.LOW

    def evaluate(self, artifacts: list[Artifact], node: NodeSpec) -> list[PolicyViolation]:
        raise RuntimeError("regex blew up")


def test_decide_denies_on_a_blocking_violation() -> None:
    engine = default_engine()
    leaked = art(f'KEY = "{live_anthropic_key()}"')
    decision = engine.decide("exit", node(), [leaked])
    assert decision.allowed is False
    assert decision.gate == "exit"
    assert decision.node_id == "n1"
    assert "security.secret_scan" in decision.reason
    assert "critical" in decision.reason
    assert decision.violations


def test_decide_allows_when_nothing_fires() -> None:
    engine = default_engine()
    clean = art("def shorten(url):\n    return SLUGS[url]\n")
    decision = engine.decide("entry", node(), [clean])
    assert decision.allowed is True
    assert decision.violations == []
    assert "no policy violations" in decision.reason


def test_decide_allows_but_records_advisory_findings() -> None:
    engine = PolicyEngine([PIIInUrlRule()])
    artifact = art('T = "https://crm.acme.io/leads?email=dana.reed@acme.io"')
    decision = engine.decide("exit", node(), [artifact])
    assert decision.allowed is True
    assert len(decision.violations) == 1
    assert "advisory" in decision.reason
    assert "privacy.pii_in_url (medium)" in decision.reason


def test_decide_reason_names_every_rule_that_fired() -> None:
    engine = default_engine()
    high = node("deploy", impact=ImpactLevel.HIGH)
    nasty = art(
        f'KEY = "{live_anthropic_key()}"\nos.system("rm -rf /srv/data")\n',
        produced_by="deploy",
    )
    decision = engine.decide("exit", high, [nasty])
    assert decision.allowed is False
    for rule_id in (
        "security.secret_scan",
        "safety.destructive_operation",
        "change_control.approval_required",
    ):
        assert rule_id in decision.reason


def test_decide_counts_repeated_findings_in_the_reason() -> None:
    engine = PolicyEngine([DestructiveOperationRule()])
    artifact = art("os.system(a)\nos.system(b)\n")
    decision = engine.decide("exit", node(), [artifact])
    assert "safety.destructive_operation (critical x2)" in decision.reason


def test_every_violation_carries_a_location() -> None:
    engine = default_engine()
    high = node("deploy", kind=StageKind.TEST, impact=ImpactLevel.HIGH)
    nasty = art(
        f'KEY = "{live_anthropic_key()}"\nDELETE FROM links;\n'
        'T = "https://crm.acme.io/u?email=dana.reed@acme.io"\n',
        produced_by="deploy",
    )
    violations = engine.evaluate([nasty], high)
    assert violations
    assert all(v.location for v in violations)


def test_evaluate_orders_findings_worst_first() -> None:
    engine = PolicyEngine([PIIInUrlRule(), SecretScanRule()])
    artifact = art(
        f'KEY = "{live_anthropic_key()}"\nT = "https://crm.acme.io/u?email=d.reed@acme.io"\n'
    )
    severities = [v.severity for v in engine.evaluate([artifact], node())]
    assert severities == [Severity.CRITICAL, Severity.MEDIUM]


def test_a_rule_that_raises_becomes_a_blocking_violation() -> None:
    engine = PolicyEngine([_ExplodingRule()])
    decision = engine.decide("exit", node(), [art("harmless")])
    assert decision.allowed is False
    assert decision.violations[0].rule_id == "test.exploding"
    assert "RuntimeError" in decision.violations[0].message


def test_register_rejects_duplicate_rule_ids() -> None:
    engine = PolicyEngine([SecretScanRule()])
    with pytest.raises(ValueError, match="duplicate rule_id"):
        engine.register(SecretScanRule())


def test_register_rejects_objects_that_are_not_rules() -> None:
    with pytest.raises(TypeError):
        PolicyEngine().register(object())  # type: ignore[arg-type]


def test_register_is_chainable() -> None:
    engine = PolicyEngine().register(SecretScanRule()).register(PIIInUrlRule())
    assert engine.rule_ids == ("security.secret_scan", "privacy.pii_in_url")


def test_default_engine_carries_the_standard_rule_set() -> None:
    engine = default_engine()
    assert set(engine.rule_ids) == {
        "security.secret_scan",
        "security.open_redirect",
        "safety.destructive_operation",
        "change_control.approval_required",
        "quality.test_evidence",
        "privacy.pii_in_url",
    }


def test_default_engine_rules_satisfy_the_frozen_protocol() -> None:
    assert all(isinstance(rule, PolicyRule) for rule in default_engine().rules)


def test_default_engine_plumbs_approved_nodes_through() -> None:
    engine = default_engine({"deploy"})
    high = node("deploy", impact=ImpactLevel.HIGH)
    decision = engine.decide("exit", high, [art("ok", name="release.txt", produced_by="deploy")])
    assert decision.allowed is True


# --------------------------------------------------------------------------
# Regression: the guard may live in a different module from the handler
# --------------------------------------------------------------------------

REDIRECT_HANDLER = '''
from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from app.urls import ensure_safe_target

router = APIRouter()


@router.get("/{code}")
def follow(code: str):
    link = lookup(code)
    ensure_safe_target(link.target)
    return RedirectResponse(link.target, status_code=302)
'''

URL_VALIDATOR = '''
import ipaddress
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_NETWORKS = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)
CLOUD_METADATA_IPV4 = ipaddress.IPv4Address("169.254.169.254")


def ensure_safe_target(raw: str) -> None:
    parts = urlparse(raw)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValueError("scheme not allowed")
    candidate = ipaddress.ip_address(parts.hostname)
    if candidate.is_private or candidate.is_link_local:
        raise ValueError("internal host not allowed")
'''


def test_guard_in_a_separate_module_is_recognized() -> None:
    """Regression from a live run: the rule punished good modularity.

    The generated service put its redirect in app/main.py and its SSRF
    validation in a dedicated app/urls.py. Evaluating each artifact in
    isolation could not see the guard, so the rule denied correct code, and
    because the denial feeds the retry it pressured the next attempt to inline
    the check purely to satisfy the gate.
    """
    artifacts = [
        art(REDIRECT_HANDLER, name="app/main.py", produced_by="implement"),
        art(URL_VALIDATOR, name="app/urls.py", produced_by="implement"),
    ]
    assert OpenRedirectRule().evaluate(artifacts, node()) == []


def test_a_redirect_with_no_guard_anywhere_is_still_flagged() -> None:
    """The other half: moving the check out must not mean removing it."""
    artifacts = [
        art(REDIRECT_HANDLER, name="app/main.py", produced_by="implement"),
        art("def lookup(code):\n    return None\n", name="app/db.py", produced_by="implement"),
    ]
    violations = OpenRedirectRule().evaluate(artifacts, node())
    assert violations, "an unguarded redirect passed the rule"
    assert any("scheme" in v.message for v in violations)
    assert any("internal hosts" in v.message for v in violations)


def test_schema_teardown_in_a_test_file_is_not_flagged() -> None:
    """Regression: a generated suite was blocked for dropping its own table.

    A live run produced a fixture that ran `DROP TABLE links` against its own
    temporary database, which is ordinary teardown, and the rule denied it at
    CRITICAL. That is the rule punishing correct work.
    """
    fixture = (
        "import sqlite3\n"
        "def reset(conn):\n"
        "    conn.execute('DROP TABLE links')\n"
    )
    for name in ("tests/test_expiry.py", "tests/conftest.py", "test_links.py"):
        artifact = art(fixture, name=name, produced_by="author-tests")
        artifact.path = name
        assert DestructiveOperationRule().evaluate([artifact], node("author-tests")) == [], (
            f"{name} was flagged for its own fixture teardown"
        )


def test_schema_destruction_in_application_code_is_still_critical() -> None:
    """The distinction is where the statement lives, not that it stopped mattering."""
    app = (
        "def migrate(conn):\n"
        "    conn.execute('DROP TABLE links')\n"
    )
    artifact = art(app, name="app/db.py", produced_by="implement")
    artifact.path = "app/db.py"
    violations = DestructiveOperationRule().evaluate([artifact], node("implement"))
    assert violations, "application code dropping a table was not flagged"
    assert violations[0].severity is Severity.CRITICAL
