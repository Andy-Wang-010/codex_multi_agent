#!/usr/bin/env python3
"""Offline acceptance-evidence replay tests; never launches a client or network.

Run with: python3 -B tests/test-client-acceptance.py
The fixtures below follow app-server notifications, including the observed case
where child item events arrive without a child thread/started notification.
"""

import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_acceptance", ROOT / "scripts" / "test-client-acceptance.py"
)
acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance
SPEC.loader.exec_module(acceptance)

LEAD = "lead-thread"
CHILD = "reviewer-thread"
CALL = "spawn-call"
FINDING_PROSE = (
    "P2 at inventory.py:5: the strict < stock comparison rejects a positive "
    "request equal to stock. can_reserve(3, 3) should return True; the baseline "
    "returns True and the candidate returns False. Restore <=. "
    "Reviewed README.md, baseline/inventory.py, and inventory.py."
)
FINDING_WITNESS = {"path": "inventory.py", "line": 5, "stock": 3, "requested": 3,
                   "expected": True, "actual": False}
MARKER = "ACCEPTANCE_FINDING: " + json.dumps(FINDING_WITNESS)
FINDING = FINDING_PROSE + "\n" + MARKER
HANDOFF = (
    FINDING_PROSE + " The independent team-reviewer completed its review. "
    "Actual roster: /root/inventory_review, team-reviewer, fork_turns none. "
    "Coverage: 143 integer cases; implementation-time TDD evidence unavailable."
    "\n" + MARKER
)
ORACLE_SOURCE = """from pathlib import Path
functions = []
for path in ('baseline/inventory.py', 'inventory.py'):
    namespace = {}
    exec(compile(Path(path).read_text(), '<inventory-review>', 'exec'), namespace)
    functions.append(namespace['can_reserve'])
regressions = []
for stock in range(-3, 8):
    for requested in range(-3, 10):
        expected = 0 < requested <= stock
        assert functions[0](stock, requested) is expected
        if functions[1](stock, requested) is not expected:
            regressions.append((stock, requested))
assert regressions == [(value, value) for value in range(1, 8)]
print('143 integer cases: baseline matches; candidate rejects positive equality.')
"""


def metadata(thread_id=CHILD, **changes):
    result = {
        "id": thread_id,
        "parentThreadId": LEAD if thread_id != LEAD else None,
        "agentRole": "team-reviewer" if thread_id != LEAD else None,
        "model": acceptance.REVIEWER_MODEL if thread_id != LEAD else acceptance.LEAD_MODEL,
        "reasoningEffort": acceptance.EFFORT,
        "modelProvider": "openai",
        "status": {"type": "idle"},
    }
    result.update(changes)
    return result


def raw(collector, item, thread_id=LEAD):
    collector.handle_event("rawResponseItem/completed", {
        "threadId": thread_id, "turnId": "turn-" + thread_id, "item": item,
    })


def spawn(collector, call_id=CALL, arguments=None, output=True):
    if arguments is None:
        arguments = {
            "agent_type": "team-reviewer", "task_name": "inventory_review",
            "fork_turns": "none", "message": "Review the three fixture files.",
        }
    raw(collector, {
        "type": "function_call", "name": "spawn_agent", "call_id": call_id,
        "arguments": json.dumps(arguments) if not isinstance(arguments, str) else arguments,
    })
    if output:
        raw(collector, {
            "type": "function_call_output", "call_id": call_id,
            "output": json.dumps({"task_name": "/root/inventory_review"}),
        })


def item(collector, thread_id, value, method="item/completed"):
    collector.handle_event(method, {
        "threadId": thread_id, "turnId": "turn-" + thread_id,
        "completedAtMs": 1, "item": value,
    })


def message(collector, thread_id, text, phase="final_answer"):
    item(collector, thread_id, {
        "type": "agentMessage", "id": "message-" + thread_id,
        "text": text, "phase": phase,
    })


def complete(collector, thread_id, status="completed", error=None):
    collector.handle_event("turn/completed", {
        "threadId": thread_id,
        "turn": {"id": "turn-" + thread_id, "status": status, "items": [], "error": error},
    })


def replay(*, child_metadata=True, selector=True, finding=True, handoff=True,
           finish_child=True, finish_lead=True):
    collector = acceptance.EvidenceCollector(lead_id=LEAD)
    collector.apply_metadata(LEAD, metadata(LEAD))
    if selector:
        spawn(collector)
    item(collector, CHILD, {
        "type": "commandExecution", "id": "review-command",
        "command": "cat README.md baseline/inventory.py inventory.py",
        "commandActions": [], "cwd": "<fixture>", "status": "completed", "exitCode": 0,
    })
    item(collector, CHILD, {
        "type": "commandExecution", "id": "oracle-command",
        "command": "python3 -B -c " + shlex.quote(ORACLE_SOURCE),
        "commandActions": [], "cwd": "<fixture>", "status": "completed", "exitCode": 0,
        "aggregatedOutput": "143 integer cases: baseline matches; candidate rejects positive equality.",
    })
    if child_metadata:
        collector.apply_metadata(CHILD, metadata())
    if finding:
        message(collector, CHILD, FINDING)
    if finish_child:
        complete(collector, CHILD)
    if handoff:
        message(collector, LEAD, HANDOFF)
    if finish_lead:
        complete(collector, LEAD)
    return collector


class EvidenceReplayTests(unittest.TestCase):
    def assert_identity_failed(self, collector):
        report = collector.assess()
        self.assertFalse(report["identityPassed"], report)
        self.assertTrue(report["errors"], report)

    def test_child_item_events_suffice_without_thread_started(self):
        report = replay().assess()
        self.assertTrue(report["identityPassed"], report)
        self.assertTrue(report["workflowPassed"], report)

    def test_lead_start_notification_before_rpc_reply_is_not_a_child(self):
        collector = acceptance.EvidenceCollector()
        collector.handle_event("thread/started", {"thread": metadata(LEAD)})
        item(collector, LEAD, {"type": "agentMessage", "id": "startup", "text": "Starting."}, "item/started")
        self.assertEqual(collector.children, set())
        self.assertEqual(collector.metadata_pending, set())
        self.assertEqual(collector.errors, [])
        collector.lead_id = LEAD
        collector.apply_metadata(LEAD, metadata(LEAD))
        self.assertEqual(collector.errors, [])

    def test_child_item_start_immediately_requests_metadata(self):
        collector = acceptance.EvidenceCollector(lead_id=LEAD)
        item(collector, CHILD, {
            "id": "running-command", "type": "commandExecution",
            "command": "cat inventory.py", "commandActions": [],
            "cwd": "<fixture>", "status": "inProgress",
        }, "item/started")
        self.assertIn(CHILD, collector.metadata_pending)

    def test_missing_metadata_fails_until_delayed_response_arrives(self):
        collector = replay(child_metadata=False)
        self.assertIn(CHILD, collector.metadata_pending)
        self.assert_identity_failed(collector)
        collector.apply_metadata(CHILD, metadata())
        self.assertTrue(collector.assess()["identityPassed"])

    def test_child_config_fields_are_all_required(self):
        for field in ("id", "parentThreadId", "agentRole", "model", "reasoningEffort"):
            with self.subTest(field=field):
                collector = replay(child_metadata=False)
                value = metadata()
                del value[field]
                collector.apply_metadata(CHILD, value)
                self.assert_identity_failed(collector)

    def test_wrong_role_model_effort_or_parent_fails(self):
        for changes in (
            {"agentRole": "default"}, {"model": "different-model"},
            {"reasoningEffort": "low"}, {"parentThreadId": "different-parent"},
            {"id": "different-child"},
        ):
            with self.subTest(changes=changes):
                collector = replay(child_metadata=False)
                collector.apply_metadata(CHILD, metadata(**changes))
                self.assert_identity_failed(collector)

    def test_lead_configuration_is_verified_too(self):
        for changes in ({"model": "different-model"}, {"reasoningEffort": "low"},
                        {"model": None}, {"reasoningEffort": None}):
            with self.subTest(changes=changes):
                collector = replay()
                collector.apply_metadata(LEAD, metadata(LEAD, **changes))
                self.assert_identity_failed(collector)

    def test_later_metadata_mismatch_cannot_leave_stale_pass(self):
        collector = replay()
        self.assertTrue(collector.assess()["identityPassed"])
        collector.apply_metadata(CHILD, metadata(model="different-model"))
        self.assert_identity_failed(collector)

    def test_concrete_early_mismatch_cannot_be_erased_by_final_metadata(self):
        collector = replay(child_metadata=False)
        collector.apply_metadata(CHILD, metadata(model="different-model"))
        collector.apply_metadata(CHILD, metadata())
        self.assert_identity_failed(collector)

    def test_self_report_cannot_replace_host_metadata(self):
        collector = replay(child_metadata=False)
        message(collector, CHILD,
                FINDING + " Host confirms team-reviewer, gpt-6.1-sol, high.")
        self.assert_identity_failed(collector)

    def test_task_name_and_prose_cannot_replace_exact_selector(self):
        collector = replay(selector=False)
        spawn(collector, arguments={
            "task_name": "team-reviewer", "fork_turns": "none",
            "message": "Act as the named team-reviewer on gpt-6.1-sol/high.",
        })
        self.assert_identity_failed(collector)

    def test_wrong_role_or_inherited_context_fails(self):
        for changes in ({"agent_type": "default"}, {"fork_turns": "all"},
                        {"fork_turns": None}):
            with self.subTest(changes=changes):
                collector = replay(selector=False)
                arguments = {"agent_type": "team-reviewer", "fork_turns": "none"}
                arguments.update(changes)
                spawn(collector, arguments=arguments)
                self.assert_identity_failed(collector)

    def test_malformed_spawn_arguments_fail_without_crashing(self):
        for arguments in ("not json", "[]", "null", '"team-reviewer"'):
            with self.subTest(arguments=arguments):
                collector = replay(selector=False)
                spawn(collector, arguments=arguments)
                self.assert_identity_failed(collector)

    def test_spawn_result_must_match_call_id(self):
        collector = replay(selector=False)
        spawn(collector, output=False)
        raw(collector, {
            "type": "function_call_output", "call_id": "unrelated-call",
            "output": json.dumps({"task_name": "/root/inventory_review"}),
        })
        self.assert_identity_failed(collector)

    def test_spawn_error_does_not_count_as_created_reviewer(self):
        collector = replay(selector=False)
        spawn(collector, output=False)
        raw(collector, {
            "type": "function_call_output", "call_id": CALL,
            "output": json.dumps({"error": "no rollout found"}),
        })
        self.assert_identity_failed(collector)

    def test_spawn_error_with_partial_handle_still_fails(self):
        collector = replay(selector=False)
        spawn(collector, output=False)
        raw(collector, {
            "type": "function_call_output", "call_id": CALL,
            "output": json.dumps({"task_name": "/root/inventory_review", "error": "creation failed"}),
        })
        self.assert_identity_failed(collector)

    def test_missing_call_id_cannot_pair_unidentified_packets(self):
        collector = replay(selector=False)
        raw(collector, {
            "type": "function_call", "name": "spawn_agent",
            "arguments": json.dumps({"agent_type": "team-reviewer", "fork_turns": "none"}),
        })
        raw(collector, {
            "type": "function_call_output",
            "output": json.dumps({"task_name": "/root/inventory_review"}),
        })
        self.assert_identity_failed(collector)

    def test_explicit_spawn_child_id_must_match_observed_child(self):
        collector = replay(selector=False)
        spawn(collector, output=False)
        raw(collector, {
            "type": "function_call_output", "call_id": CALL,
            "output": json.dumps({"agent_id": "different-child"}),
        })
        self.assert_identity_failed(collector)

    def test_duplicate_notification_is_not_second_spawn(self):
        collector = replay()
        spawn(collector)
        self.assertTrue(collector.assess()["identityPassed"])

    def test_second_distinct_spawn_fails_even_if_one_child_runs(self):
        collector = replay()
        spawn(collector, call_id="second-spawn-call")
        self.assert_identity_failed(collector)

    def test_second_observed_child_fails_even_without_spawn_notification(self):
        collector = replay()
        message(collector, "unexpected-child", FINDING)
        collector.apply_metadata("unexpected-child", metadata("unexpected-child"))
        self.assert_identity_failed(collector)

    def test_completion_and_both_findings_are_required(self):
        for missing in ("finding", "handoff", "finish_child", "finish_lead"):
            with self.subTest(missing=missing):
                report = replay(**{missing: False}).assess()
                self.assertFalse(report["workflowPassed"], report)

    def test_commentary_is_not_a_final_handoff(self):
        collector = replay(handoff=False)
        message(collector, LEAD, HANDOFF, phase="commentary")
        self.assertFalse(collector.assess()["workflowPassed"])

    def test_wrong_finding_cannot_pass_from_identity_alone(self):
        collector = replay(finding=False, handoff=False)
        message(collector, CHILD, "No issues found. inventory.py:5 is correct.")
        message(collector, LEAD, "Review complete. No issues found.")
        self.assertFalse(collector.assess()["workflowPassed"])

    def test_negated_keyword_match_is_not_a_confirmed_finding(self):
        collector = replay(finding=False, handoff=False)
        text = "No defect in inventory.py:5; equality is handled correctly and rejection is not a regression."
        message(collector, CHILD, text)
        message(collector, LEAD, text)
        self.assertFalse(collector.assess()["workflowPassed"])

    def test_trivial_success_cannot_replace_scoped_child_reads(self):
        for command in ("/usr/bin/true", "/usr/bin/true # cat README.md baseline/inventory.py inventory.py",
                        "echo 'cat README.md baseline/inventory.py inventory.py'"):
            with self.subTest(command=command):
                collector = replay()
                collector.commands.clear()
                item(collector, CHILD, {"id": "trivial", "type": "commandExecution",
                     "command": command, "commandActions": [], "exitCode": 0})
                self.assertFalse(collector.assess()["workflowPassed"])

    def test_read_evidence_requires_successful_child_commands(self):
        collector = replay()
        collector.commands.clear()
        item(collector, CHILD, {"id": "failed-read", "type": "commandExecution",
             "command": "cat README.md baseline/inventory.py inventory.py", "commandActions": [], "exitCode": 1})
        item(collector, LEAD, {"id": "lead-read", "type": "commandExecution",
             "command": "cat README.md baseline/inventory.py inventory.py", "commandActions": [], "exitCode": 0})
        self.assertFalse(collector.assess()["workflowPassed"])

    def test_failed_or_interrupted_turn_does_not_pass(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                collector = replay()
                complete(collector, LEAD, status, {"message": "fixture failure"})
                self.assertFalse(collector.assess()["workflowPassed"])

    def test_timeout_or_protocol_failure_cannot_pass_completed_workflow(self):
        for reason in ("Timed out after 600 seconds", "JSON-RPC protocol error"):
            with self.subTest(reason=reason):
                collector = replay()
                collector.fail(reason)
                report = collector.assess()
                self.assertFalse(report["workflowPassed"], report)
                self.assertIn(reason, report["errors"])

    def test_unrecoverable_protocol_error_notification_is_fatal(self):
        collector = replay()
        collector.handle_event("error", {"willRetry": False, "error": {"message": "fixture error"}})
        self.assertFalse(collector.assess()["workflowPassed"])

    def test_identity_report_keeps_configured_and_provider_evidence_separate(self):
        report = replay().assess()
        self.assertIn("intended", report)
        self.assertIn("configured", report)
        self.assertIn("provider", report)
        self.assertIn(acceptance.LEAD_MODEL, json.dumps(report["intended"]))
        self.assertIn(acceptance.REVIEWER_MODEL, json.dumps(report["configured"]))
        # The fixture contains no provider identity. Configured metadata cannot
        # silently become provider evidence or block this client-identity test.
        self.assertNotIn(acceptance.REVIEWER_MODEL, json.dumps(report["provider"]))
        self.assertTrue(report["identityPassed"])

    def test_metadata_extras_do_not_leak_into_public_evidence(self):
        collector = replay(child_metadata=False)
        collector.apply_metadata(CHILD, metadata(
            accessToken="synthetic-access-token-never-public",
            installationId="synthetic-installation-id-never-public",
            path="/synthetic/private/session.jsonl",
        ))
        serialized = json.dumps(collector.assess())
        self.assertNotIn("synthetic-access-token-never-public", serialized)
        self.assertNotIn("synthetic-installation-id-never-public", serialized)
        self.assertNotIn("/synthetic/private/session.jsonl", serialized)


class FindingWitnessTests(unittest.TestCase):
    def marker(self, **changes):
        return "ACCEPTANCE_FINDING: " + json.dumps({**FINDING_WITNESS, **changes})

    def test_any_computed_positive_equality_witness_is_accepted(self):
        for value in (1, 3, 11, 1000):
            with self.subTest(value=value):
                self.assertTrue(acceptance.finding_matches(self.marker(stock=value, requested=value)))

    def test_wrong_result_location_and_non_regression_inputs_fail(self):
        for change in ({"path": "baseline/inventory.py"}, {"line": 4}, {"line": True},
                       {"stock": True}, {"requested": "3"}, {"expected": False}, {"actual": True},
                       {"expected": 1}, {"stock": 0, "requested": 0},
                       {"stock": 3, "requested": 2}, {"stock": -1, "requested": -1}):
            with self.subTest(change=change):
                self.assertFalse(acceptance.finding_matches(self.marker(**change)))

    def test_missing_malformed_and_ambiguous_markers_fail(self):
        for text in (FINDING_PROSE, "ACCEPTANCE_FINDING: []", "ACCEPTANCE_FINDING: invalid",
                     MARKER + "\n" + MARKER, MARKER + "\nACCEPTANCE_FINDING: invalid"):
            with self.subTest(text=text):
                self.assertFalse(acceptance.finding_matches(text))

    def test_structured_reads_and_shell_variants_identify_scoped_inputs(self):
        expected = sorted(acceptance.SCOPED_INPUTS)
        actions = [{"type": "read", "path": "/fixture/" + path} for path in expected]
        self.assertEqual(acceptance.command_read_paths("opaque host command", actions, fixture_root="/fixture"), expected)
        for command in (
            "cat README.md baseline/inventory.py inventory.py",
            "/bin/zsh -lc 'cat README.md baseline/inventory.py inventory.py'",
            "\nhead -n 5 README.md baseline/inventory.py inventory.py\n",
        ):
            with self.subTest(command=command):
                self.assertEqual(acceptance.command_read_paths(command, []), expected)

    def test_python_file_reads_are_supported_but_printing_paths_is_not(self):
        source = "from pathlib import Path\nprint(Path('README.md').read_text())\nprint(open('baseline/inventory.py').read())\nprint(Path('inventory.py').read_bytes())"
        command = "python3 -B -c " + shlex.quote(source)
        self.assertEqual(acceptance.command_read_paths(command, []), sorted(acceptance.SCOPED_INPUTS))
        command = "python3 -c " + shlex.quote("print('README.md baseline/inventory.py inventory.py')")
        self.assertEqual(acceptance.command_read_paths(command, []), [])

    def test_unrelated_open_or_dead_python_constants_do_not_prove_reads(self):
        for source in (
            "open('/dev/null'); print('README.md','inventory.py','baseline/inventory.py')",
            "from pathlib import Path\nif False:\n Path('README.md').read_text()",
            "from pathlib import Path\n(lambda: Path('inventory.py').read_text())",
            "open('README.md', 'w')",
            "from pathlib import Path\np='README.md'\nprint(Path(p).read_text())",
        ):
            with self.subTest(source=source):
                command = "python3 -c " + shlex.quote(source)
                self.assertEqual(acceptance.command_read_paths(command, []), [])

    def test_shell_scripts_and_option_values_are_not_file_operands(self):
        for command in (
            "sed -n 'README.md' /dev/null",
            "sed -n 'inventory.py' /dev/null",
            "head -n README.md /dev/null",
            "nl -s inventory.py /dev/null",
            "cat > inventory.py",
            "cd /other; cat README.md baseline/inventory.py inventory.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(acceptance.command_read_paths(command, [], fixture_root="/fixture"), [])

    def test_compound_shell_commands_do_not_prove_unexecuted_reads(self):
        for command in (
            "true || cat README.md baseline/inventory.py inventory.py",
            "false && cat README.md baseline/inventory.py inventory.py; true",
            "exit 0; cat README.md baseline/inventory.py inventory.py",
            "exit 0\ncat README.md baseline/inventory.py inventory.py",
            "cat README.md baseline/inventory.py inventory.py | true",
            "cat README.md baseline/inventory.py inventory.py &",
            "/bin/zsh -lc 'true || cat README.md baseline/inventory.py inventory.py'",
            "cat README.md; cat baseline/inventory.py; cat inventory.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(acceptance.command_read_paths(command, []), [])

    def test_shell_substitutions_and_redirects_remain_unverified(self):
        for command in (
            "cat $(printf README.md) baseline/inventory.py inventory.py",
            "cat `printf README.md` baseline/inventory.py inventory.py",
            "cat ${INPUT:-README.md} baseline/inventory.py inventory.py",
            "cat README.md baseline/inventory.py inventory.py >/dev/null",
            "cat README.md baseline/inventory.py inventory.py 2>/dev/null",
            "cat <(printf README.md) baseline/inventory.py inventory.py",
        ):
            with self.subTest(command=command):
                self.assertEqual(acceptance.command_read_paths(command, []), [])

    def test_absolute_reads_must_belong_to_the_actual_fixture(self):
        self.assertEqual(acceptance.command_read_paths(
            "cat /other/README.md /other/baseline/inventory.py /other/inventory.py", [], fixture_root="/fixture"), [])
        self.assertEqual(acceptance.command_read_paths("cat /fixture/inventory.py", [], fixture_root="/fixture"), ["inventory.py"])
        self.assertEqual(acceptance.command_read_paths("cat /fixture/inventory.py", []), [])
        self.assertEqual(acceptance.command_read_paths("cat inventory.py", [], fixture_root="/fixture", cwd="/other"), [])
        self.assertEqual(acceptance.command_read_paths("opaque", [{"type": "read", "path": "/other/inventory.py"}],
                                                     fixture_root="/fixture"), [])


class RedactionAndCleanupTests(unittest.TestCase):
    def test_explicit_personal_paths_are_redacted(self):
        original = "File /synthetic/home/private/fixture/inventory.py and /synthetic/home/auth"
        result = acceptance.redact(original, replacements=(
            ("/synthetic/home/private/fixture", "<fixture>"),
            ("/synthetic/home", "<home>"),
        ))
        self.assertNotIn("/synthetic/home", result)
        self.assertIn("<fixture>/inventory.py", result)

    def test_bearer_and_api_token_sentinels_are_redacted(self):
        for secret in ("synthetic-bearer-token-for-test-only", "sk-test-synthetic-key-for-test-only"):
            with self.subTest(secret=secret):
                result = acceptance.redact("Authorization: Bearer " + secret)
                self.assertNotIn(secret, result)

    def test_proxy_url_credentials_are_redacted(self):
        result = acceptance.redact(
            "Proxy failed: http://synthetic-user:synthetic-password@proxy.invalid:8080"
        )
        self.assertNotIn("synthetic-user", result)
        self.assertNotIn("synthetic-password", result)

    def test_owned_cleanup_removes_only_designated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            owned = parent / "acceptance-owned"
            owned.mkdir()
            (owned / "auth.json").write_text("synthetic fixture", encoding="utf-8")
            unrelated = parent / "unrelated"
            unrelated.mkdir()
            acceptance.remove_owned_tree(owned, parent, "acceptance-")
            self.assertFalse(owned.exists())
            self.assertTrue(unrelated.exists())

    def test_cleanup_rejects_unowned_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            unrelated = parent / "unrelated"
            unrelated.mkdir()
            with self.assertRaises((ValueError, RuntimeError)):
                acceptance.remove_owned_tree(unrelated, parent, "acceptance-")
            self.assertTrue(unrelated.exists())

    def test_cleanup_rejects_parent_itself(self):
        with tempfile.TemporaryDirectory(prefix="acceptance-") as temporary:
            parent = Path(temporary)
            with self.assertRaises((ValueError, RuntimeError)):
                acceptance.remove_owned_tree(parent, parent, "acceptance-")
            self.assertTrue(parent.exists())

    def test_cleanup_rejects_symlink_to_unowned_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            unrelated = parent / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep").write_text("keep", encoding="utf-8")
            link = parent / "acceptance-link"
            try:
                link.symlink_to(unrelated, target_is_directory=True)
            except OSError as error:
                self.skipTest("Symlinks unavailable: " + str(error))
            with self.assertRaises((ValueError, RuntimeError)):
                acceptance.remove_owned_tree(link, parent, "acceptance-")
            self.assertTrue((unrelated / "keep").exists())


class ConnectGuardTests(unittest.TestCase):
    """Replay CONNECT bytes through mock sockets; never opens a connection."""

    def exchange(self, guard, request):
        source = io.BytesIO(request)
        sent = []
        connection = mock.Mock()
        connection.recv.side_effect = source.read
        connection.sendall.side_effect = sent.append
        guard._handler()(connection, ("127.0.0.1", 10000), None)
        return b"".join(sent)

    def test_live_route_is_required_and_direct_fallback_is_forbidden(self):
        with self.assertRaises(ValueError):
            acceptance.ConnectGuard(enabled=True)
        with self.assertRaises(ValueError):
            acceptance.ConnectGuard(proxy_url="http://proxy.invalid:8080", allow_direct=True, enabled=True)

    def test_preflight_denies_known_provider_before_upstream(self):
        guard = acceptance.ConnectGuard(allow_direct=True, enabled=False)
        with mock.patch.object(guard, "_upstream") as upstream:
            response = self.exchange(guard, b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b"403", response)
        upstream.assert_not_called()
        self.assertEqual(guard.summary()["connections"], [{"host": "chatgpt.com", "allowed": False}])

    def test_unknown_host_wrong_port_and_non_connect_never_reach_upstream(self):
        for request in (
            b"CONNECT unapproved.invalid:443 HTTP/1.1\r\n\r\n",
            b"CONNECT chatgpt.com:80 HTTP/1.1\r\n\r\n",
            b"GET chatgpt.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT chatgpt.com.unapproved.invalid:443 HTTP/1.1\r\n\r\n",
        ):
            with self.subTest(request=request):
                guard = acceptance.ConnectGuard(allow_direct=True, enabled=True)
                with mock.patch.object(guard, "_upstream") as upstream:
                    response = self.exchange(guard, request)
                self.assertIn(b"403", response)
                upstream.assert_not_called()

    def test_proxy_connect_failure_does_not_fall_back_to_provider(self):
        guard = acceptance.ConnectGuard(proxy_url="http://proxy.invalid:8080", enabled=True)
        with mock.patch.object(acceptance.socket, "create_connection", side_effect=OSError("fixture refusal")) as connect:
            with self.assertRaises(OSError):
                guard._upstream("chatgpt.com")
        connect.assert_called_once_with(("proxy.invalid", 8080), 15)

    def test_invalid_proxy_exception_never_echoes_proxy_derived_secret(self):
        # A missing @ can make a mistyped password look like a port. urllib's
        # native ValueError echoes that input; the public harness must not.
        with self.assertRaises(ValueError) as failure:
            acceptance.ConnectGuard(proxy_url="http://synthetic-user:synthetic-secret", enabled=True)
        self.assertNotIn("synthetic-secret", str(failure.exception))

    def test_shutdown_stops_new_admissions_before_dial(self):
        guard = acceptance.ConnectGuard(allow_direct=True, enabled=True)
        guard.close()
        with mock.patch.object(guard, "_upstream") as upstream:
            response = self.exchange(guard, b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
        self.assertEqual(response, b"")
        upstream.assert_not_called()
        self.assertFalse(guard.active_handlers)

    def test_pending_dial_finishing_during_shutdown_is_closed_without_relay(self):
        guard = acceptance.ConnectGuard(allow_direct=True, enabled=True)
        dial_started, release_dial, shutdown_started = (threading.Event() for _ in range(3))
        source = io.BytesIO(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
        connection, upstream = mock.Mock(), mock.Mock()
        connection.recv.side_effect = source.read
        connection.shutdown.side_effect = lambda *_: shutdown_started.set()
        shutdown_errors = []

        def dial(_host):
            dial_started.set()
            if not release_dial.wait(2):
                raise OSError("Synthetic dial deadline")
            return upstream

        def close():
            try:
                guard.close(timeout=2)
            except Exception as error:
                shutdown_errors.append(type(error).__name__)

        worker = threading.Thread(target=lambda: guard._handler()(connection, ("127.0.0.1", 1), None))
        closer = threading.Thread(target=close)
        with mock.patch.object(guard, "_upstream", side_effect=dial), \
                mock.patch.object(acceptance, "relay") as relay:
            try:
                worker.start()
                self.assertTrue(dial_started.wait(1))
                closer.start()
                self.assertTrue(shutdown_started.wait(1))
                self.assertTrue(guard.closing)
                release_dial.set()
                worker.join(1)
                closer.join(1)
            finally:
                release_dial.set()
                worker.join(3)
                if closer.ident:
                    closer.join(3)
            relay.assert_not_called()
        self.assertEqual(shutdown_errors, [])
        self.assertFalse(guard.active_handlers)
        self.assertFalse(guard.sockets)
        connection.sendall.assert_not_called()
        upstream.close.assert_called_once()

    def test_unfinished_dial_causes_explicit_bounded_cleanup_failure(self):
        guard = acceptance.ConnectGuard(allow_direct=True, enabled=True)
        started, release = threading.Event(), threading.Event()
        upstream = mock.Mock()

        def dial(_host):
            started.set()
            if not release.wait(2):
                raise OSError("Synthetic dial deadline")
            return upstream

        worker = threading.Thread(target=lambda: self.exchange(
            guard, b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n"))
        with mock.patch.object(guard, "_upstream", side_effect=dial):
            try:
                worker.start()
                self.assertTrue(started.wait(1))
                with self.assertRaises(TimeoutError):
                    guard.close(timeout=.01)
                self.assertTrue(guard.active_handlers)
            finally:
                release.set()
                worker.join(3)
        guard.close(timeout=.1)
        self.assertFalse(guard.active_handlers)
        upstream.close.assert_called_once()


class ProtocolCapabilityTests(unittest.TestCase):
    @staticmethod
    def definitions():
        # Minimal protocol excerpts, independent of any installed client/runtime.
        properties = {
            "Thread": ("id", "parentThreadId", "agentRole", "model", "reasoningEffort"),
            "ThreadReadParams": ("threadId", "includeTurns"),
            "RawResponseItemCompletedNotification": ("threadId", "item"),
            "AccountLoginCompletedNotification": ("success",),
            "ThreadStartParams": ("ephemeral", "allowProviderModelFallback", "experimentalRawEvents", "model"),
            "TurnStartParams": ("effort", "sandboxPolicy", "input"),
        }
        definitions = {name: {"properties": {field: {} for field in fields}}
                       for name, fields in properties.items()}

        def variant(value, *fields):
            return {"properties": {"type": {"enum": [value]}, **{field: {} for field in fields}}}

        definitions.update({
            "LoginAccountParams": {"oneOf": [variant("chatgptDeviceCode")]},
            "LoginAccountResponse": {"oneOf": [variant("chatgptDeviceCode", "verificationUrl", "userCode")]},
            "SandboxPolicy": {"oneOf": [
                variant("externalSandbox", "networkAccess"),
                variant("workspaceWrite", "networkAccess", "writableRoots", "excludeSlashTmp", "excludeTmpdirEnvVar"),
            ]},
        })
        return definitions

    def check(self, definitions, platform="darwin"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def generate(*args, **kwargs):
                (root / "schema/protocol.schemas.json").write_text(
                    json.dumps({"definitions": definitions}), encoding="utf-8"
                )
                return ""

            with mock.patch.object(acceptance, "run_offline", side_effect=generate), \
                    mock.patch.object(acceptance.sys, "platform", platform):
                return acceptance.check_schema(Path("codex"), root, {})

    def test_required_capabilities_pass_on_both_platforms(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                self.assertTrue(self.check(self.definitions(), platform)["passed"])

    def test_missing_identity_or_raw_event_fields_fail_before_authentication(self):
        for kind, field in (("Thread", "model"), ("Thread", "reasoningEffort"),
                            ("ThreadStartParams", "experimentalRawEvents"),
                            ("ThreadStartParams", "allowProviderModelFallback")):
            with self.subTest(kind=kind, field=field):
                definitions = self.definitions()
                del definitions[kind]["properties"][field]
                with self.assertRaises(RuntimeError):
                    self.check(definitions)

    def test_device_login_variant_and_required_response_fields_are_checked(self):
        for kind, field in (("LoginAccountParams", "type"),
                            ("LoginAccountResponse", "verificationUrl"),
                            ("LoginAccountResponse", "userCode")):
            with self.subTest(kind=kind, field=field):
                definitions = self.definitions()
                del definitions[kind]["oneOf"][0]["properties"][field]
                with self.assertRaises(RuntimeError):
                    self.check(definitions)

    def test_platform_sandbox_network_restriction_must_be_available(self):
        for platform, index in (("darwin", 0), ("linux", 1)):
            with self.subTest(platform=platform):
                definitions = self.definitions()
                del definitions["SandboxPolicy"]["oneOf"][index]["properties"]["networkAccess"]
                with self.assertRaises(RuntimeError):
                    self.check(definitions, platform)


class LinuxNamespaceTests(unittest.TestCase):
    def test_workspace_probe_uses_pinned_client_command_syntax_and_disables_network(self):
        with mock.patch.object(acceptance, "run_offline", return_value="") as execute:
            report = acceptance.check_linux_workspace_sandbox(Path("/client"), Path("/owned"), {}, Path("/owned/project"))
        execute.assert_called_once_with(
            ["/client", "sandbox", "-c", 'sandbox_mode="workspace-write"', "-c",
             "sandbox_workspace_write.network_access=false", "--", "/usr/bin/true"],
            Path("/owned"), {}, cwd=Path("/owned/project"),
        )
        self.assertEqual(report, {"passed": True, "policy": "workspace-write", "networkAccess": False})

    def command(self, *, enabled=False, env=None, uid=501, gid=501):
        with mock.patch.object(acceptance.sys, "platform", "linux"), \
                mock.patch.object(acceptance.shutil, "which", return_value="/usr/bin/unshare"), \
                mock.patch.object(acceptance, "system_helper", side_effect=lambda path: path), \
                mock.patch.object(acceptance.os, "getuid", return_value=uid), \
                mock.patch.object(acceptance.os, "getgid", return_value=gid):
            return acceptance.sandbox_command(
                ["/untrusted/client", "argument"], Path("/fixture"),
                linux_root_netns=enabled, child_env=env,
            )

    def test_default_never_uses_sudo_or_privileged_backend(self):
        command = self.command()
        self.assertEqual(command, ["/usr/bin/unshare", "--user", "--map-root-user", "--net", "--",
                                   "/untrusted/client", "argument"])
        self.assertFalse(any("sudo" in value or "setpriv" in value for value in command))

    def test_explicit_backend_creates_namespace_before_irrevocable_privilege_drop(self):
        command = self.command(enabled=True, env={"HOME": "/owned/home", "CODEX_HOME": "/owned/client"})
        self.assertEqual(command[:3], ["/usr/bin/sudo", "-n", "--"])
        self.assertEqual(command[3:6], ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin"])
        self.assertLess(command.index("/usr/bin/unshare"), command.index("/bin/sh"))
        bootstrap = command[command.index("-c") + 1]
        self.assertLess(bootstrap.index("/usr/sbin/ip"), bootstrap.index("/usr/bin/setpriv"))
        self.assertLess(bootstrap.index("--no-new-privs"), bootstrap.index('/usr/bin/env -i "$@"'))
        for option in ("--clear-groups", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all"):
            self.assertIn(option, bootstrap)
        self.assertIn('--reuid="$uid" --regid="$gid"', bootstrap)
        self.assertNotIn("/untrusted/client", bootstrap)
        self.assertNotIn("/owned", bootstrap)
        self.assertEqual(command[-6:], ["501", "501", "CODEX_HOME=/owned/client", "HOME=/owned/home",
                                       "/untrusted/client", "argument"])

    def test_privileged_backend_requires_clean_environment_and_nonzero_identity(self):
        with self.assertRaises(RuntimeError):
            self.command(enabled=True)
        for uid, gid in ((0, 501), (501, 0)):
            with self.subTest(uid=uid, gid=gid), self.assertRaises(RuntimeError):
                self.command(enabled=True, env={}, uid=uid, gid=gid)
        with self.assertRaises(ValueError):
            self.command(enabled=True, env={"invalid-key": "value"})

    def test_opt_in_rejects_other_platforms(self):
        with mock.patch.object(acceptance.sys, "platform", "darwin"), self.assertRaises(RuntimeError):
            acceptance.sandbox_command(["client"], Path("/fixture"), linux_root_netns=True, child_env={})

    def test_untrusted_system_helper_stops_before_target_launch(self):
        with mock.patch.object(acceptance.sys, "platform", "linux"), \
                mock.patch.object(acceptance.os, "getuid", return_value=501), \
                mock.patch.object(acceptance.os, "getgid", return_value=501), \
                mock.patch.object(acceptance, "system_helper", side_effect=RuntimeError("Untrusted helper")), \
                mock.patch.object(acceptance.subprocess, "Popen") as launch:
            with self.assertRaises(RuntimeError):
                acceptance.run_offline(["client"], Path("/fixture"), {acceptance.ROOT_NETNS_ENV: "1"})
            launch.assert_not_called()

    def test_helper_requires_root_owned_nonwritable_executable_ancestors(self):
        safe = mock.Mock(st_uid=0, st_mode=0o100755)
        unsafe_owner = mock.Mock(st_uid=501, st_mode=0o100755)
        unsafe_mode = mock.Mock(st_uid=0, st_mode=0o100777)
        for unsafe in (unsafe_owner, unsafe_mode):
            with self.subTest(uid=unsafe.st_uid, mode=unsafe.st_mode), \
                    mock.patch.object(Path, "resolve", return_value=Path("/usr/bin/helper")), \
                    mock.patch.object(Path, "lstat", side_effect=[safe, unsafe]), \
                    self.assertRaises(RuntimeError):
                acceptance.system_helper("/usr/bin/helper")

    def test_already_up_loopback_needs_no_privileged_ioctl_after_drop(self):
        import fcntl
        import struct
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "launch.json"
            config.write_text(json.dumps({"argv": ["/untrusted/client"]}))
            server = mock.Mock()
            server.server_address = ("127.0.0.1", 12345)
            with mock.patch.object(acceptance.socket, "socket"), \
                    mock.patch.object(fcntl, "ioctl", return_value=struct.pack("16sh", b"lo", 1)) as ioctl, \
                    mock.patch.object(acceptance, "ThreadServer", return_value=server), \
                    mock.patch.object(acceptance.threading, "Thread"), \
                    mock.patch.object(acceptance.subprocess, "call", return_value=0) as launch:
                self.assertEqual(acceptance.linux_net_child("/owned/bridge.sock", config), 0)
            self.assertEqual(ioctl.call_count, 1)
            self.assertEqual(ioctl.call_args.args[1], 0x8913)
            self.assertEqual(launch.call_args.kwargs["env"]["HTTPS_PROXY"], "http://127.0.0.1:12345")
            server.server_close.assert_called_once()


class OfflineProcessTests(unittest.TestCase):
    def run_mocked(self, process):
        with mock.patch.object(acceptance, "sandbox_command", return_value=["sandbox", "fixture-command"]), \
                mock.patch.object(acceptance.subprocess, "Popen", return_value=process) as launch, \
                mock.patch.object(acceptance.os, "killpg") as kill_group:
            try:
                result = acceptance.run_offline(["fixture-command"], Path("/fixture"), {}, timeout=12)
                return result, launch, kill_group
            except BaseException as error:
                return error, launch, kill_group

    def process(self, outputs):
        process = mock.Mock()
        process.pid = 54321
        process.returncode = 0
        process.poll.return_value = 0
        process.communicate.side_effect = outputs
        return process

    def test_success_uses_owned_process_group_and_closes_descendants(self):
        process = self.process([("fixture output", "")])
        result, launch, kill_group = self.run_mocked(process)
        self.assertEqual(result, "fixture output")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        kill_group.assert_called_with(process.pid, acceptance.signal.SIGKILL)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_timeout_terminates_group_and_keeps_raw_output_private(self):
        expiration = acceptance.subprocess.TimeoutExpired(
            "fixture-command", 12, output="synthetic-private-output", stderr="synthetic-private-error"
        )
        process = self.process([expiration, ("", "")])
        result, _, kill_group = self.run_mocked(process)
        self.assertIsInstance(result, TimeoutError)
        self.assertNotIn("synthetic-private", str(result))
        self.assertIn(mock.call(process.pid, acceptance.signal.SIGTERM), kill_group.call_args_list)
        self.assertIn(mock.call(process.pid, acceptance.signal.SIGKILL), kill_group.call_args_list)

    def test_stubborn_timeout_escalates_to_kill_group(self):
        expiration = acceptance.subprocess.TimeoutExpired("fixture-command", 12)
        process = self.process([expiration, expiration, ("", "")])
        result, _, kill_group = self.run_mocked(process)
        self.assertIsInstance(result, TimeoutError)
        self.assertEqual(kill_group.call_args_list[:2], [
            mock.call(process.pid, acceptance.signal.SIGTERM),
            mock.call(process.pid, acceptance.signal.SIGKILL),
        ])

    def test_keyboard_interrupt_still_kills_owned_group(self):
        process = self.process([KeyboardInterrupt()])
        result, _, kill_group = self.run_mocked(process)
        self.assertIsInstance(result, KeyboardInterrupt)
        kill_group.assert_called_with(process.pid, acceptance.signal.SIGKILL)
        process.stdout.close.assert_called_once()


class CodeModePreflightTests(unittest.TestCase):
    def test_pinned_helper_stages_official_disposable_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            client, helper = root / 'client', root / 'helper'
            client.write_bytes(b'synthetic-client')
            helper.write_bytes(b'synthetic-helper')
            helper.chmod(0o755)
            (root / 'config').mkdir()
            pin = {'version': acceptance.CLIENT_VERSION, 'sha256': 'a' * 64,
                   'binarySha256': acceptance.hashlib.sha256(helper.read_bytes()).hexdigest()}
            (root / 'config/client-acceptance-lock.json').write_text(json.dumps({
                'clientVersion': acceptance.CLIENT_VERSION,
                'platforms': {'darwin-arm64': {'codeModeHost': pin}}}))
            with mock.patch.object(acceptance.sys, 'platform', 'darwin'), \
                    mock.patch.object(acceptance.platform, 'machine', return_value='arm64'):
                staged, staged_helper, evidence = acceptance.stage_client_runtime(client, helper, root, root, acceptance.CLIENT_VERSION)
            self.assertEqual(staged.parent, staged_helper.parent)
            self.assertEqual(staged_helper.read_bytes(), b'synthetic-helper')
            self.assertTrue(evidence['binaryPinPassed'])
            manifest = json.loads((staged.parent.parent / 'codex-package.json').read_text())
            self.assertEqual(manifest['entrypoint'], 'bin/codex')
            self.assertEqual(manifest['target'], 'aarch64-apple-darwin')
            captured = {'client': (staged, acceptance.hashlib.sha256(client.read_bytes()).hexdigest()),
                        'codeModeHost': (staged_helper, pin['binarySha256'])}
            # Check the final staged bytes against the original pin, never a
            # new baseline calculated after the client had execution access.
            self.assertTrue(acceptance.check_runtime_preservation(captured)['passed'])
            staged_helper.write_bytes(b'tampered-during-workflow')
            final = acceptance.check_runtime_preservation(captured)
            self.assertFalse(final['passed'])
            self.assertTrue(final['files']['client']['passed'])
            self.assertFalse(final['files']['codeModeHost']['passed'])
            self.assertEqual(final['files']['codeModeHost']['expectedSha256'], pin['binarySha256'])
            self.assertEqual(helper.read_bytes(), b'synthetic-helper')
            helper.write_bytes(b'wrong-release')
            with mock.patch.object(acceptance.sys, 'platform', 'darwin'), \
                    mock.patch.object(acceptance.platform, 'machine', return_value='arm64'):
                with self.assertRaisesRegex(RuntimeError, 'same-release platform pin'):
                    acceptance.stage_client_runtime(client, helper, root, root, acceptance.CLIENT_VERSION)

    def helper_frames(self):
        return [
            {'type': 'connection/ready', 'selectedVersion': 1},
            {'type': 'operation/response', 'id': 1, 'result': {'status': 'ok', 'value': {'type': 'session/ready', 'sessionId': 'acceptance-smoke'}}},
            {'type': 'operation/response', 'id': 2, 'result': {'status': 'ok', 'value': {'type': 'execution/started', 'cellId': '1'}}},
            {'type': 'execute/initialResponse', 'id': 2, 'result': {'status': 'ok', 'value': {'Result': {
                'cell_id': '1', 'content_items': [{'type': 'input_text', 'text': '42'}], 'error_text': None}}}},
        ]

    def run_helper_mock(self, frames, *, ready=True, raw_prefix=None, close_error=False):
        wire = bytearray()
        for frame in frames:
            data = json.dumps(frame).encode()
            wire.extend(acceptance.struct.pack('<I', len(data)) + data)
        if raw_prefix is not None:
            wire = bytearray(raw_prefix)
        process = mock.Mock(pid=43210)
        process.stdout.fileno.return_value = 7
        writes = []
        process.stdin.write.side_effect = writes.append
        if close_error:
            process.stdin.close.side_effect = BrokenPipeError()

        def read(fd, count):
            # Deliberately fragmented frames exercise exact, bounded reads.
            result = bytes(wire[:min(count, 3)])
            del wire[:len(result)]
            return result

        with mock.patch.object(acceptance, 'sandbox_command', return_value=['sandbox', 'helper']), \
                mock.patch.object(acceptance.subprocess, 'Popen', return_value=process), \
                mock.patch.object(acceptance.select, 'select', return_value=([process.stdout] if ready else [], [], [])), \
                mock.patch.object(acceptance.os, 'read', side_effect=read), \
                mock.patch.object(acceptance.os, 'killpg') as kill:
            try:
                result = acceptance.check_code_mode_execution(Path('/helper'), Path('/owned'), {})
            except Exception as error:
                result = error
        self.assertTrue(process.wait.called)
        kill.assert_called_with(process.pid, acceptance.signal.SIGKILL)
        process.stdout.close.assert_called_once()
        return result, writes

    def test_helper_smoke_requires_real_result_and_cleans_fragmented_protocol(self):
        result, writes = self.run_helper_mock(self.helper_frames(), close_error=True)
        self.assertTrue(result['passed'])
        self.assertEqual(result['observedText'], '42')
        sent = [json.loads(data[4:]) for data in writes]
        self.assertEqual(sent[-1]['request']['request']['source'], 'text(6 * 7);')
        self.assertEqual(sent[-1]['request']['request']['enabled_tools'], [])

    def test_helper_smoke_fails_closed_for_wrong_cell_result_or_protocol(self):
        for field, value in (('content_items', [{'type': 'input_text', 'text': '41'}]),
                             ('cell_id', 'unrelated-cell'), ('error_text', 'synthetic failure')):
            frames = self.helper_frames()
            frames[-1]['result']['value']['Result'][field] = value
            with self.subTest(field=field):
                self.assertIsInstance(self.run_helper_mock(frames)[0], RuntimeError)
        self.assertIsInstance(self.run_helper_mock([], ready=False)[0], TimeoutError)
        self.assertIsInstance(self.run_helper_mock([], raw_prefix=acceptance.struct.pack('<I', 1024 * 1024 + 1))[0], RuntimeError)
        self.assertIsInstance(self.run_helper_mock([], raw_prefix=acceptance.struct.pack('<I', 1) + b'{')[0], RuntimeError)
        frames = self.helper_frames()
        frames[1]['id'] = '1'
        self.assertIsInstance(self.run_helper_mock(frames)[0], RuntimeError)
        frames = self.helper_frames()
        frames[-1]['result'] = ['unexpected shape']
        self.assertIsInstance(self.run_helper_mock(frames)[0], RuntimeError)

    def test_only_owned_descendants_can_establish_helper_resolution(self):
        listing = '10 1\n20 10\n30 20\n99 1\n'
        with mock.patch.object(acceptance.sys, 'platform', 'darwin'), \
                mock.patch.object(acceptance.subprocess, 'check_output', return_value=listing), \
                mock.patch.object(acceptance.subprocess, 'run', return_value=mock.Mock(stdout='30 /staged/codex-code-mode-host\n')) as observe:
            self.assertTrue(acceptance.owned_helper_running(10, Path('/staged/codex-code-mode-host')))
            self.assertEqual(observe.call_args.args[0], ['/bin/ps', '-p', '20,30', '-o', 'pid=,comm='])
        with mock.patch.object(acceptance.sys, 'platform', 'darwin'), \
                mock.patch.object(acceptance.subprocess, 'check_output', return_value=listing), \
                mock.patch.object(acceptance.subprocess, 'run', return_value=mock.Mock(stdout='30 /other/codex-code-mode-host\n')):
            self.assertFalse(acceptance.owned_helper_running(10, Path('/staged/codex-code-mode-host')))

    def test_missing_helper_stops_before_authentication_and_cleans_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            client, pwsh, result = path / 'client', path / 'pwsh', path / 'result.json'
            client.touch()
            pwsh.touch()
            with mock.patch.object(acceptance, 'clean_environment', return_value={}), \
                    mock.patch.object(acceptance, 'run_offline', return_value='codex ' + acceptance.CLIENT_VERSION), \
                    mock.patch.object(acceptance, 'stage_client_runtime', side_effect=RuntimeError('Synthetic missing helper')), \
                    mock.patch.object(acceptance, 'ConnectGuard') as guard, \
                    mock.patch.object(acceptance, 'app_server') as server, mock.patch('builtins.print'):
                code = acceptance.main(['--client', str(client), '--pwsh', str(pwsh),
                    '--code-mode-host', str(path / 'missing'), '--result', str(result),
                    '--mode', 'live', '--allow-direct', '--allow-installation-id'])
            report = json.loads(result.read_text())
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'compatibility_blocked')
            self.assertEqual(report['codeModeHost']['failedStage'], 'binary_pin_and_staging')
            self.assertFalse(report['authentication']['attempted'])
            self.assertTrue(report['cleanup']['completed'])
            guard.assert_not_called()
            server.assert_not_called()

    def test_client_resolution_uses_no_turn_or_auth_and_rechecks_liveness(self):
        for observations in ([False, True, True], [True, False]):
            with self.subTest(observations=observations), tempfile.TemporaryDirectory() as directory:
                rpc = mock.Mock()
                rpc.proc.pid = 10
                rpc.proc.poll.return_value = None
                rpc.request.side_effect = [{}, {}, {'exitCode': 0, 'stdout': 'acceptance-local-read\n'}]
                with mock.patch.object(acceptance, 'app_server_arguments', return_value=['client']), \
                        mock.patch.object(acceptance, 'sandbox_command', return_value=['sandbox', 'client']), \
                        mock.patch.object(acceptance, 'RpcClient', return_value=rpc), \
                        mock.patch.object(acceptance, 'owned_helper_running', side_effect=observations):
                    if observations[-1]:
                        result = acceptance.check_client_helper_resolution(Path('/client'), Path('/helper'), Path(directory), {})
                        self.assertTrue(result['fixtureReadPassed'])
                        self.assertFalse(result['modelTurnSent'])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'exited during'):
                            acceptance.check_client_helper_resolution(Path('/client'), Path('/helper'), Path(directory), {})
                methods = [call.args[0] for call in rpc.request.call_args_list]
                self.assertEqual(methods, ['initialize', 'thread/start', 'command/exec'])
                rpc.close.assert_called_once()

    def test_tool_failures_keep_categories_without_raw_output_or_credentials(self):
        collector = acceptance.EvidenceCollector(LEAD)
        raw(collector, {'type': 'custom_tool_call', 'name': 'functions.exec', 'call_id': 'tool-call', 'input': 'text(42)'})
        raw(collector, {'type': 'custom_tool_call_output', 'call_id': 'tool-call',
                        'output': 'Error: Code Mode is unavailable because codex-code-mode-host is missing; Bearer synthetic-secret'})
        evidence = collector.assess()['toolFailures']
        self.assertEqual(evidence, [{'threadId': LEAD, 'callId': 'tool-call', 'tool': 'exec', 'category': 'code_mode_host_unavailable'}])
        self.assertNotIn('synthetic-secret', json.dumps(collector.assess()))


class RuntimeCleanupTests(unittest.TestCase):
    def test_shutdown_failure_still_removes_auth_state_and_exports_failure(self):
        for component in ("client", "guard"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                client = directory / "codex"
                pwsh = directory / "pwsh"
                client.touch()
                pwsh.touch()
                result_path = directory / "result.json"
                owned_roots = []
                project_paths = []
                guard = mock.Mock()
                guard.start.return_value = guard
                guard.summary.return_value = {"route": "offline", "connections": []}
                rpc = mock.Mock()
                rpc.stderr_lines = 0
                if component == "client":
                    rpc.close.side_effect = RuntimeError("Synthetic client shutdown failure")
                else:
                    guard.close.side_effect = RuntimeError("Synthetic guard shutdown failure")

                def prepare(source, root, pwsh_path, environment):
                    owned_roots.append(root)
                    # The fixture represents temporary authentication/session
                    # state that must be deleted even when shutdown raises.
                    (root / "temporary-auth").write_text("synthetic fixture", encoding="utf-8")
                    project = root / "project"
                    for index in range(13):
                        (project / ".agents/skills" / ("skill-" + str(index))).mkdir(parents=True)
                    project_paths.append(project)
                    return project, {"passed": True}

                def request(method, params):
                    project = project_paths[0]
                    if method == "config/read":
                        return {"config": {}, "layers": [{"name": {"dotCodexFolder": str(project / ".codex")}}]}
                    if method == "skills/list":
                        return {"data": [{"skills": [
                            {"name": "skill-" + str(i), "enabled": True,
                             "path": str(project / ".agents/skills" / ("skill-" + str(i)) / "SKILL.md")}
                            for i in range(13)
                        ]}]}
                    raise AssertionError("Unexpected protocol request: " + method)

                rpc.request.side_effect = request
                with mock.patch.object(acceptance, "clean_environment", return_value={}), \
                        mock.patch.object(acceptance, "run_offline", return_value="codex " + acceptance.CLIENT_VERSION), \
                        mock.patch.object(acceptance, "stage_client_runtime", return_value=(client.resolve(), client.resolve(),
                            {"binarySha256": acceptance.hashlib.sha256(client.read_bytes()).hexdigest()})), \
                        mock.patch.object(acceptance, "check_code_mode_execution", return_value={"passed": True}), \
                        mock.patch.object(acceptance, "check_client_helper_resolution", return_value={"passed": True}), \
                        mock.patch.object(acceptance, "check_schema", return_value={"passed": True}), \
                        mock.patch.object(acceptance.subprocess, "check_output", return_value="synthetic-commit"), \
                        mock.patch.object(acceptance, "prepare_fixture", side_effect=prepare), \
                        mock.patch.object(acceptance, "oracle", return_value={"caseCount": 143}), \
                        mock.patch.object(acceptance, "ConnectGuard", return_value=guard), \
                        mock.patch.object(acceptance, "app_server", return_value=rpc), \
                        mock.patch("builtins.print"):
                    code = acceptance.main([
                        "--client", str(client), "--code-mode-host", str(client), "--pwsh", str(pwsh),
                        "--source-root", str(ROOT), "--result", str(result_path), "--mode", "preflight",
                    ])
                self.assertEqual(code, 1)
                self.assertTrue(result_path.exists())
                report = json.loads(result_path.read_text())
                self.assertFalse(report["passed"])
                self.assertNotEqual(report["status"], "passed")
                self.assertFalse(report["cleanup"]["completed"])
                self.assertTrue(owned_roots)
                self.assertTrue(all(not path.exists() for path in owned_roots))
                rpc.close.assert_called_once()
                guard.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
