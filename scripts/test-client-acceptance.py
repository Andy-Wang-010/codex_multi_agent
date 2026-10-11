#!/usr/bin/env python3
"""Bounded, opt-in client acceptance; default preflight cannot reach providers.

All state belongs to one temporary directory. JSON contains allowlisted evidence,
never credentials, installation identifiers, raw protocol dumps, or reasoning.
Live flags acknowledge an independently approved route; they do not grant consent.
"""
import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import selectors
import shlex
import shutil
import signal
import socket
import socketserver
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

LEAD_MODEL = 'gpt-6-astra'
REVIEWER_MODEL = 'gpt-6.1-sol'
EFFORT = 'high'
CLIENT_VERSION = '0.162.0-alpha.17.2'
HOSTS = frozenset(('chatgpt.com', 'api.openai.com', 'auth.openai.com'))
PREFIX = 'kit-client-acceptance-'
ROOT_NETNS_ENV = 'KIT_ACCEPTANCE_LINUX_ROOT_NETNS'
# Only fixed system helpers run with privilege. All environment assignments and
# target arguments remain positional data until setpriv irreversibly drops uid.
ROOT_NETNS_BOOTSTRAP = '''set -eu
/usr/sbin/ip link set dev lo up
uid=$1; gid=$2; shift 2
exec /usr/bin/setpriv --reuid="$uid" --regid="$gid" --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs -- /usr/bin/env -i "$@"
'''
BASELINE = '''"""Small inventory rule for the client acceptance review."""

def can_reserve(stock: int, requested: int) -> bool:
    """Accept a positive request that does not exceed available stock."""
    return 0 < requested <= stock
'''
CONTRACT = ('can_reserve(stock, requested) accepts positive integer requests no greater '
            'than available stock. Equality must be accepted; zero and negative requests '
            'must be rejected. Review only regressions relative to baseline/inventory.py.\n')


def redact(text, replacements=()):
    """Defense in depth for explicitly retained strings; never dump raw auth data."""
    text = str(text)
    for old, new in sorted(replacements, key=lambda pair: len(str(pair[0])), reverse=True):
        text = text.replace(str(old), str(new))
    text = re.sub(r'(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@', r'\1<redacted>@', text)
    text = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+', r'\1<redacted>', text)
    return re.sub(r'\bsk-[A-Za-z0-9_-]+', '<redacted>', text)


def remove_owned_tree(path, parent, prefix):
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or path.parent.resolve() != parent or not path.name.startswith(prefix):
        raise ValueError('Refusing to remove a directory outside the owned temporary root')
    if path.resolve() == parent:
        raise ValueError('Refusing to remove the parent directory')
    if path.exists():
        shutil.rmtree(path)


def finding_matches(text):
    """Verify the reported witness against the contract, not prose keywords."""
    lines = [line.strip() for line in text.splitlines()
             if line.strip().startswith('ACCEPTANCE_FINDING:')]
    if len(lines) != 1:
        return False
    try:
        value = json.loads(lines[0].split(':', 1)[1])
        if not isinstance(value, dict):
            return False
        if value.get('path') != 'inventory.py' or type(value.get('line')) is not int or value['line'] != 5:
            return False
        if any(type(value.get(key)) is not int for key in ('stock', 'requested')):
            return False
        if any(type(value.get(key)) is not bool for key in ('expected', 'actual')):
            return False
        stock, requested = value['stock'], value['requested']
        expected = 0 < requested <= stock
        actual = 0 < requested < stock
        return expected is True and actual is False and value['expected'] == expected and value['actual'] == actual
    except (ValueError, TypeError, KeyError):
        return False


SCOPED_INPUTS = frozenset(('README.md', 'baseline/inventory.py', 'inventory.py'))


def scoped_path(value, fixture_root=None, cwd=None):
    if not isinstance(value, str):
        return None
    value = value.replace('\\', '/')
    path = Path(value)
    if fixture_root is not None:
        root = Path(os.path.normpath(str(fixture_root)))
        if not path.is_absolute():
            path = Path(cwd) / path if cwd else root / path
        try:
            relative = Path(os.path.normpath(str(path))).relative_to(root).as_posix()
        except ValueError:
            return None
    else:
        if path.is_absolute() or '..' in path.parts:
            return None
        relative = path.as_posix()
    return relative if relative in SCOPED_INPUTS else None


def command_read_paths(command, actions, fixture_root=None, cwd=None):
    """Collect supported read evidence without treating echoed paths as reads.

    Host-parsed read actions are preferred. Fallbacks support straightforward
    read-only file operands and unconditional literal Python reads; shell
    compound commands, substitutions and redirects remain unverified.
    """
    found = set()
    for action in actions if isinstance(actions, list) else ():
        if isinstance(action, dict) and action.get('type') == 'read':
            path = scoped_path(action.get('path'), fixture_root, cwd)
            if path:
                found.add(path)

    def inspect(script, depth=0):
        if depth > 2 or '$' in script or '`' in script:
            return
        try:
            lexer = shlex.shlex(script.strip(), posix=True, punctuation_chars=';&|<>\n')
            lexer.whitespace_split = True
            lexer.whitespace = ' \t\r'
            words = list(lexer)
        except ValueError:
            return
        # A successful compound command does not prove each operand executed:
        # `true || cat file` and `exit 0; cat file` never read the file. Keep the
        # fallback bounded to one simple command instead of guessing reachability.
        if any(word and all(char in ';&|<>\n' for char in word) for word in words):
            return
        for words in [words]:
            if not words:
                continue
            executable = Path(words[0]).name
            if executable in ('sh', 'bash', 'zsh', 'dash'):
                for index, word in enumerate(words[1:-1], 1):
                    if word.startswith('-') and 'c' in word:
                        inspect(words[index + 1], depth + 1)
                        break
            elif executable in ('cat', 'head', 'tail', 'nl'):
                operands, index, supported = [], 1, True
                while index < len(words):
                    word = words[index]
                    if word == '--':
                        operands.extend(words[index + 1:])
                        break
                    if not word.startswith('-') or word == '-':
                        operands.append(word)
                    elif executable == 'cat' and re.fullmatch(r'-[AbEenstTv]+', word):
                        pass
                    elif executable in ('head', 'tail') and re.fullmatch(r'-(?:[nc])?[0-9]+', word):
                        pass
                    elif executable in ('head', 'tail') and word in ('-n', '-c', '--lines', '--bytes'):
                        index += 1
                        if index >= len(words) or not re.fullmatch(r'[0-9]+', words[index]):
                            supported = False
                            break
                    elif executable == 'nl' and word in ('-ba', '-bt', '-bn'):
                        pass
                    else:
                        supported = False
                        break
                    index += 1
                if supported:
                    found.update(path for word in operands if (path := scoped_path(word, fixture_root, cwd)))
            elif re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', executable) and '-c' in words:
                index = words.index('-c')
                if index + 1 >= len(words):
                    continue
                try:
                    tree = ast.parse(words[index + 1])
                except (SyntaxError, ValueError):
                    continue
                shadowed = {node.id for node in ast.walk(tree)
                            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
                path_imported = any(isinstance(node, ast.ImportFrom) and node.module == 'pathlib'
                                    and any(alias.name == 'Path' and alias.asname in (None, 'Path') for alias in node.names)
                                    for node in tree.body) and 'Path' not in shadowed

                def python_reads(node):
                    # Only eager expressions in top-level statements are used.
                    # Loops, branches, functions, lambdas and comprehensions can
                    # contain reads that never execute and remain unverified.
                    if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
                        if node.value is not None:
                            python_reads(node.value)
                    elif isinstance(node, ast.Call):
                        argument = None
                        if isinstance(node.func, ast.Name) and node.func.id == 'open' and 'open' not in shadowed:
                            mode = node.args[1] if len(node.args) > 1 else ast.Constant(value='r')
                            for keyword in node.keywords:
                                if keyword.arg == 'mode':
                                    mode = keyword.value
                            if isinstance(mode, ast.Constant) and mode.value in ('r', 'rb', 'rt'):
                                argument = node.args[0] if node.args else None
                        elif (isinstance(node.func, ast.Attribute) and node.func.attr in ('read_text', 'read_bytes')
                              and isinstance(node.func.value, ast.Call) and path_imported):
                            factory = node.func.value
                            if isinstance(factory.func, ast.Name) and factory.func.id == 'Path' and len(factory.args) == 1:
                                argument = factory.args[0]
                        if isinstance(argument, ast.Constant):
                            path = scoped_path(argument.value, fixture_root, cwd)
                            if path:
                                found.add(path)
                        for child in [node.func, *node.args, *(keyword.value for keyword in node.keywords)]:
                            python_reads(child)
                    elif isinstance(node, ast.Attribute):
                        python_reads(node.value)
                    elif isinstance(node, (ast.List, ast.Tuple)):
                        for child in node.elts:
                            python_reads(child)
                for statement in tree.body:
                    python_reads(statement)
    inspect(command)
    return sorted(found)


class EvidenceCollector:
    """Reconcile host events without relying on agent narratives for identity."""
    def __init__(self, lead_id=None, fixture_root=None):
        self.lead_id = lead_id
        self.fixture_root = fixture_root
        self.children = set()
        self.metadata_pending = set()
        self.metadata = {}
        self.metadata_observations = []
        self.spawns = {}
        self.spawn_results = {}
        self.messages = {}
        self.commands = {}
        self.turns = {}
        self.errors = []
        self.provider = []
        self.tool_calls = {}
        self.tool_failures = {}

    def fail(self, reason):
        if reason not in self.errors:
            self.errors.append(str(reason))

    def apply_metadata(self, thread_id, thread):
        keys = ('id', 'parentThreadId', 'agentRole', 'model', 'reasoningEffort', 'modelProvider')
        self.metadata[thread_id] = {key: thread.get(key) for key in keys}
        self.metadata_observations.append(dict(self.metadata[thread_id]))
        expected = {'id': thread_id, 'model': LEAD_MODEL if thread_id == self.lead_id else REVIEWER_MODEL,
                    'reasoningEffort': EFFORT}
        if thread_id != self.lead_id:
            expected.update(parentThreadId=self.lead_id, agentRole='team-reviewer')
        for key, value in expected.items():
            if thread.get(key) is not None and thread[key] != value:
                self.fail('Host metadata mismatch observed for ' + key + ' on ' + thread_id)
        self.metadata_pending.discard(thread_id)

    def handle_event(self, method, params):
        tid = params.get('threadId')
        if self.lead_id is not None and tid and tid != self.lead_id and method in ('item/started', 'item/completed', 'turn/completed'):
            first = tid not in self.children
            self.children.add(tid)
            if first or method == 'turn/completed':
                self.metadata_pending.add(tid)
        if method == 'thread/started':
            thread = params.get('thread', {})
            if self.lead_id is not None and thread.get('id') and thread.get('parentThreadId') == self.lead_id:
                self.children.add(thread['id'])
                self.apply_metadata(thread['id'], thread)
        if method == 'rawResponseItem/completed':
            raw = params.get('item', {})
            call = raw.get('call_id')
            tool = raw.get('name', '').split('.')[-1]
            if (raw.get('type') in ('function_call', 'custom_tool_call')
                    and tool in ('exec', 'wait', 'exec_command', 'write_stdin') and isinstance(call, str)):
                self.tool_calls[call] = tool
            if raw.get('type') in ('function_call_output', 'custom_tool_call_output') and call in self.tool_calls:
                output = raw.get('output', '')
                if isinstance(output, list):
                    output = '\n'.join(value.get('text', '') for value in output
                                       if isinstance(value, dict) and isinstance(value.get('text'), str))
                if isinstance(output, str):
                    lowered = output[:65536].lower()
                    category = None
                    if 'codex-code-mode-host' in lowered and any(word in lowered for word in ('unavailable', 'not found', 'failed', 'missing')):
                        category = 'code_mode_host_unavailable'
                    elif lowered.startswith(('error:', 'script error:')):
                        category = 'tool_execution_failed'
                    if category:
                        self.tool_failures[call] = {'threadId': tid, 'callId': call,
                                                    'tool': self.tool_calls[call], 'category': category}
            if raw.get('type') == 'function_call' and raw.get('name', '').split('.')[-1] == 'spawn_agent':
                if not isinstance(call, str) or not call:
                    self.fail('Spawn call has no usable call ID')
                try:
                    arguments = json.loads(raw.get('arguments', '{}'))
                    if not isinstance(arguments, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    arguments = {}
                keep = ('agent_type', 'task_name', 'fork_turns', 'model', 'reasoning_effort')
                self.spawns[call] = {'threadId': tid, 'arguments': {k: arguments[k] for k in keep if k in arguments}}
            if raw.get('type') == 'function_call_output' and call in self.spawns:
                output = raw.get('output', '')
                if isinstance(output, list):
                    output = '\n'.join(v.get('text', '') for v in output if isinstance(v, dict))
                try:
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    result = {}
                if result.get('error'):
                    self.fail('Client reported failed child creation')
                    result = {}
                self.spawn_results[call] = {k: result[k] for k in ('task_name', 'agent_id')
                                            if isinstance(result.get(k), str)}
        if method == 'item/completed':
            item = params.get('item', {})
            ident = (tid, item.get('id'))
            if item.get('type') == 'agentMessage':
                self.messages[ident] = {'threadId': tid, 'phase': item.get('phase'), 'text': item.get('text', '')}
            elif item.get('type') == 'commandExecution':
                self.commands[ident] = {'threadId': tid, 'command': item.get('command', ''),
                                        'exitCode': item.get('exitCode'),
                                        'readPaths': command_read_paths(item.get('command', ''), item.get('commandActions', []),
                                                                        self.fixture_root, item.get('cwd'))}
        if method == 'turn/completed':
            self.turns[tid] = params.get('turn', {}).get('status')
        if method == 'error' and not params.get('willRetry'):
            self.fail('Client reported an unrecoverable turn error')
        # Do not turn arbitrary usage metadata into provider identity. The current
        # protocol has no typed provider-resolved model field; retain unknown.

    def assess(self):
        errors = list(self.errors)
        if len(self.spawns) != 1:
            errors.append('Exactly one spawn call is required')
        for call, spawn in self.spawns.items():
            args = spawn['arguments']
            if (spawn['threadId'] != self.lead_id or args.get('agent_type') != 'team-reviewer'
                    or args.get('fork_turns') != 'none' or 'model' in args or 'reasoning_effort' in args):
                errors.append('Spawn must select the exact reviewer with fresh context and no model override')
            if not self.spawn_results.get(call):
                errors.append('A matching successful spawn response is required')
            returned_id = self.spawn_results.get(call, {}).get('agent_id')
            if returned_id and returned_id not in self.children:
                errors.append('Returned child ID does not match the executing child')
        if len(self.children) != 1:
            errors.append('Exactly one executing child thread is required')
        for tid in {self.lead_id} | self.children:
            meta = self.metadata.get(tid, {})
            expected = LEAD_MODEL if tid == self.lead_id else REVIEWER_MODEL
            if meta.get('id') != tid or meta.get('model') != expected or meta.get('reasoningEffort') != EFFORT:
                errors.append('Missing or mismatched host model/effort metadata for ' + str(tid))
            if tid != self.lead_id and (meta.get('parentThreadId') != self.lead_id or meta.get('agentRole') != 'team-reviewer'):
                errors.append('Missing or mismatched host child role/parent metadata for ' + str(tid))
        identity = not errors
        for tid in {self.lead_id} | self.children:
            if self.turns.get(tid) != 'completed':
                errors.append('Completed turn missing for ' + str(tid))
            finals = [m['text'] for m in self.messages.values()
                      if m['threadId'] == tid and m['phase'] == 'final_answer']
            if not any(finding_matches(m) for m in finals):
                errors.append('Expected final finding missing for ' + str(tid))
        for tid in self.children:
            reads = {path for command in self.commands.values()
                     if command['threadId'] == tid and command['exitCode'] == 0
                     for path in command.get('readPaths', [])}
            if not SCOPED_INPUTS.issubset(reads):
                errors.append('Successful child reads of all three scoped inputs were not observed')
        return {'identityPassed': identity, 'workflowPassed': not errors, 'errors': errors,
                'intended': {'lead': {'model': LEAD_MODEL, 'effort': EFFORT},
                             'reviewer': {'role': 'team-reviewer', 'model': REVIEWER_MODEL, 'effort': EFFORT}},
                'configured': self.metadata, 'configuredObservations': self.metadata_observations,
                'provider': {'resolvedModel': None, 'evidence': self.provider},
                'observedChildThreadIds': sorted(self.children), 'spawns': self.spawns,
                'spawnResults': self.spawn_results, 'turns': self.turns,
                'messages': list(self.messages.values()), 'commands': list(self.commands.values()),
                'toolFailures': list(self.tool_failures.values())}


def read_header(sock):
    data = bytearray()
    while not data.endswith(b'\r\n\r\n') and len(data) < 16384:
        part = sock.recv(1)
        if not part:
            raise OSError('Incomplete CONNECT header')
        data.extend(part)
    if not data.endswith(b'\r\n\r\n'):
        raise OSError('Oversized CONNECT header')
    return bytes(data)


def relay(left, right):
    idle = time.monotonic()
    while time.monotonic() - idle < 180:
        ready, _, _ = select.select([left, right], [], [], 1)
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)
            idle = time.monotonic()


class ThreadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ConnectGuard:
    """A fixed-destination TLS tunnel. Proxy credentials never leave memory/logs."""
    def __init__(self, proxy_url=None, allow_direct=False, enabled=False):
        self.enabled = enabled
        self.allow_direct = allow_direct
        try:
            self.proxy = urlsplit(proxy_url) if proxy_url else None
            if self.proxy:
                _ = self.proxy.port
        except ValueError:
            raise ValueError('Invalid approved proxy endpoint') from None
        if self.proxy and (self.proxy.scheme not in ('http', 'https') or not self.proxy.hostname
                           or self.proxy.path not in ('', '/') or self.proxy.query or self.proxy.fragment):
            raise ValueError('Approved proxy must be an HTTP(S) CONNECT endpoint')
        if enabled and not (self.proxy or allow_direct):
            raise ValueError('Live mode requires an explicitly approved route')
        if self.proxy and allow_direct:
            raise ValueError('Choose one route; direct fallback is not supported')
        self.requests = []
        self.sockets = set()
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.active_handlers = set()
        self.closing = False
        self.server = None
        self.unix_server = None

    def _upstream(self, host):
        with self.lock:
            if self.closing:
                raise OSError('CONNECT guard is shutting down')
        if self.proxy:
            p = self.proxy
            sock = socket.create_connection((p.hostname, p.port or (443 if p.scheme == 'https' else 80)), 15)
            try:
                if p.scheme == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
                auth = ''
                if p.username is not None:
                    value = unquote(p.username) + ':' + unquote(p.password or '')
                    auth = 'Proxy-Authorization: Basic ' + base64.b64encode(value.encode()).decode() + '\r\n'
                sock.sendall(('CONNECT ' + host + ':443 HTTP/1.1\r\nHost: ' + host + ':443\r\n' + auth + '\r\n').encode())
                header = read_header(sock)
                if header.split(b' ', 2)[1] != b'200':
                    raise OSError('Approved proxy refused the tunnel')
                return sock
            except BaseException:
                sock.close()
                raise
        if self.allow_direct:
            return socket.create_connection((host, 443), 15)
        raise OSError('No permitted external route')

    def _handler(self):
        guard = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                upstream = None
                worker = threading.current_thread()
                # Track the request before reading headers or dialing. Shutdown
                # can then interrupt slow headers and account for pending dials.
                with guard.condition:
                    if guard.closing:
                        return
                    guard.active_handlers.add(worker)
                    guard.sockets.add(self.request)
                try:
                    self.request.settimeout(15)
                    header = read_header(self.request)
                    method, target, _ = header.split(b'\r\n', 1)[0].decode('ascii').split(' ', 2)
                    host, port = target.rsplit(':', 1)
                    with guard.lock:
                        allowed = (not guard.closing and guard.enabled and method == 'CONNECT'
                                   and host in HOSTS and port == '443')
                        guard.requests.append({'host': host if host in HOSTS else '<other>', 'allowed': allowed})
                    if not allowed:
                        self.request.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                        return
                    upstream = guard._upstream(host)
                    with guard.lock:
                        # A dial admitted before close() may finish afterward.
                        # Never publish or relay that late connection.
                        if guard.closing or not guard.enabled:
                            return
                        guard.sockets.add(upstream)
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    self.request.settimeout(None)
                    upstream.settimeout(None)
                    relay(self.request, upstream)
                except (OSError, ValueError, UnicodeError, IndexError):
                    # Never log exception text from proxy/auth handling.
                    pass
                finally:
                    if upstream:
                        try:
                            upstream.close()
                        except OSError:
                            pass
                    with guard.condition:
                        guard.sockets.discard(self.request)
                        guard.sockets.discard(upstream)
                        guard.active_handlers.discard(worker)
                        guard.condition.notify_all()
        return Handler

    def start(self):
        self.server = ThreadServer(('127.0.0.1', 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = 'http://127.0.0.1:' + str(self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def unix_bridge(self, path):
        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.unix_server = UnixServer(str(path), self._handler())
        threading.Thread(target=self.unix_server.serve_forever, daemon=True).start()

    def summary(self):
        return {'route': 'approved proxy' if self.proxy else ('approved direct' if self.allow_direct else 'offline'),
                'connections': list(self.requests)}

    def close(self, timeout=20):
        deadline = time.monotonic() + timeout
        with self.lock:
            self.enabled = False
            self.closing = True
            sockets = tuple(self.sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for server in (self.server, self.unix_server):
            if server:
                server.shutdown()
                server.server_close()
        with self.condition:
            while self.active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('CONNECT guard request handlers did not finish during bounded shutdown')
                self.condition.wait(remaining)


def linux_net_child(bridge, config_path):
    """Run inside an empty network namespace; only the Unix bridge escapes it."""
    import fcntl
    import struct
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags = fcntl.ioctl(control, 0x8913, struct.pack('16sh', b'lo', 0))
        value = struct.unpack('16sh', flags[:18])[1]
        # The privileged opt-in backend raises loopback before dropping uid.
        # An already-up interface requires no capability in this child.
        if not value & 1:
            fcntl.ioctl(control, 0x8914, struct.pack('16sh', b'lo', value | 1))

    class Bridge(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as target:
                try:
                    target.connect(bridge)
                    relay(self.request, target)
                except OSError:
                    pass
    server = ThreadServer(('127.0.0.1', 0), Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = json.loads(Path(config_path).read_text())
    env = os.environ.copy()
    address = 'http://127.0.0.1:' + str(server.server_address[1])
    env.update(HTTP_PROXY=address, HTTPS_PROXY=address, ALL_PROXY=address)
    try:
        return subprocess.call(config['argv'], env=env)
    finally:
        server.shutdown()
        server.server_close()


def system_helper(path):
    """Reject missing or user-writable executables/ancestors before sudo."""
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
        for component in (candidate, *candidate.parents, resolved, *resolved.parents):
            info = component.lstat()
            if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                raise ValueError('Untrusted helper')
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError('Unusable helper')
    except (OSError, ValueError):
        raise RuntimeError('Linux namespace isolation requires a root-owned system helper: ' + path) from None
    return path


def sandbox_command(argv, root, *, loopback=False, deny_read=None, linux_root_netns=False, child_env=None):
    if linux_root_netns and not sys.platform.startswith('linux'):
        raise RuntimeError('--linux-root-netns is available only on Linux')
    if sys.platform == 'darwin':
        policy = '(version 1)(allow default)(deny network*)'
        policy += '(allow network* (local unix-socket) (remote unix-socket))'
        if loopback:
            policy += '(allow network-bind (local ip "localhost:*"))(allow network-inbound (local ip "localhost:*"))(allow network-outbound (remote ip "localhost:*"))'
        policy += '(deny file-write*)(allow file-write* (subpath ' + json.dumps(str(root)) + ')(literal "/dev/null"))'
        for personal in (Path.home() / '.agents', Path.home() / '.codex'):
            policy += '(deny file-read* (subpath ' + json.dumps(str(personal)) + '))'
        if deny_read:
            policy += '(deny file-read* (subpath ' + json.dumps(str(deny_read)) + '))'
        return ['/usr/bin/sandbox-exec', '-p', policy] + list(argv)
    if sys.platform.startswith('linux'):
        if linux_root_netns:
            uid, gid = os.getuid(), os.getgid()
            if uid == 0 or gid == 0:
                raise RuntimeError('Linux privileged namespace requires an original nonzero uid and gid')
            if child_env is None:
                raise RuntimeError('Linux privileged namespace requires an explicit cleaned child environment')
            for helper in ('/usr/bin/sudo', '/usr/bin/env', '/usr/bin/unshare', '/bin/sh',
                           '/usr/sbin/ip', '/usr/bin/setpriv'):
                system_helper(helper)
            assignments = []
            for key, value in sorted(child_env.items()):
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or '\0' in value:
                    raise ValueError('Invalid cleaned child environment')
                assignments.append(key + '=' + value)
            return ['/usr/bin/sudo', '-n', '--', '/usr/bin/env', '-i',
                    'PATH=/usr/sbin:/usr/bin:/sbin:/bin', '/usr/bin/unshare', '--net', '--',
                    '/bin/sh', '-c', ROOT_NETNS_BOOTSTRAP, 'kit-acceptance-netns', str(uid), str(gid),
                    *assignments, *list(argv)]
        unshare = system_helper('/usr/bin/unshare')
        return [unshare, '--user', '--map-root-user', '--net', '--'] + list(argv)
    raise RuntimeError('Acceptance currently supports macOS and Linux only')


def clean_environment(root, pwsh):
    # Do not inherit API keys, proxy credentials, personal Codex settings or hooks.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'TERM', 'HOME') if k in os.environ}
    env.update(CODEX_HOME=str(root / 'client-home'), TMPDIR=str(root / 'tmp'),
               XDG_CACHE_HOME=str(root / 'cache'), XDG_CONFIG_HOME=str(root / 'config'),
               XDG_DATA_HOME=str(root / 'data'), POWERSHELL_TELEMETRY_OPTOUT='1',
               POWERSHELL_UPDATECHECK='Off', DOTNET_CLI_TELEMETRY_OPTOUT='1',
               PYTHONDONTWRITEBYTECODE='1', NO_PROXY='localhost,127.0.0.1,::1')
    env['PATH'] = str(Path(pwsh).parent) + os.pathsep + env.get('PATH', '')
    for name in ('client-home', 'tmp', 'cache', 'config', 'data'):
        (root / name).mkdir()
    return env


def run_offline(argv, root, env, cwd=None, deny_read=None, timeout=120):
    process = subprocess.Popen(sandbox_command(argv, root, deny_read=deny_read,
                               linux_root_netns=env.get(ROOT_NETNS_ENV) == '1', child_env=env), cwd=cwd or root,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)

    def signal_group(value):
        try:
            os.killpg(process.pid, value)
        except ProcessLookupError:
            pass

    try:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Killing only the sandbox/PowerShell parent can leave its children
            # running with access to the owned temporary state.
            signal_group(signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGKILL)
                process.communicate(timeout=5)
            raise TimeoutError('Offline command timed out: ' + Path(argv[0]).name) from None
        if process.returncode:
            # Caller labels the operation; never retain raw stdout or stderr.
            raise RuntimeError('Offline command failed with exit ' + str(process.returncode) + ': ' + Path(argv[0]).name)
        return stdout
    finally:
        # Also remove background descendants after an ordinary exit or an
        # interruption; they must not outlive this bounded offline operation.
        signal_group(signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Unexpected symlink in disposable fixture')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def oracle(project):
    functions = []
    for relative in ('baseline/inventory.py', 'inventory.py'):
        scope = {}
        exec(compile((project / relative).read_text(), relative, 'exec'), scope)
        functions.append(scope['can_reserve'])
    mismatches = []
    for stock in range(-3, 8):
        for requested in range(-3, 10):
            expected = 0 < requested <= stock
            if functions[0](stock, requested) is not expected:
                raise RuntimeError('Baseline does not satisfy the fixture contract')
            if functions[1](stock, requested) is not expected:
                mismatches.append([stock, requested])
    if mismatches != [[v, v] for v in range(1, 8)]:
        raise RuntimeError('Candidate no longer matches the seeded regression')
    return {'caseCount': 143, 'baselineMatchesContract': True, 'candidateFailures': mismatches}


def prepare_fixture(source, root, pwsh, env):
    project = root / 'project'
    project.mkdir()
    (project / 'baseline').mkdir()
    (project / 'README.md').write_text(CONTRACT)
    (project / 'baseline/inventory.py').write_text(BASELINE)
    (project / 'inventory.py').write_text(BASELINE.replace('<= stock', '< stock'))
    (project / 'unrelated-sentinel').write_text('preserve\n')
    homes = ['-CodexHome', str(project / '.codex'), '-AgentsHome', str(project / '.agents')]
    ps = [str(pwsh), '-NoLogo', '-NoProfile', '-NonInteractive', '-File']
    run_offline(ps + [str(source / 'scripts/install-user.ps1')] + homes + ['-WhatIf'], root, env)
    run_offline(ps + [str(source / 'scripts/install-user.ps1')] + homes, root, env)
    config = (source / 'config/recommended-config.toml').read_text()
    (project / '.codex/config.toml').write_text(config)
    # This is the disposable client's own trust setting, never the user's config.
    (root / 'client-home/config.toml').write_text('[projects.' + json.dumps(str(project)) + ']\ntrust_level = "trusted"\n')
    before = hashes(project)
    if len(list((project / '.codex/agents').glob('*.toml'))) != 8:
        raise RuntimeError('Expected eight installed agent profiles')
    manager = project / '.agents/codex-multi-agent/rollback.ps1'
    steps = []

    def manage(action, extra=(), deny=False):
        run_offline(ps + [str(manager), '-Action', action] + homes + list(extra), root, env,
                    cwd=project, deny_read=source if deny else None)
        steps.append({'action': action, 'preview': '-WhatIf' in extra, 'exitCode': 0})

    manage('Verify', deny=True)
    manage('MarkStable', deny=True)
    alternate = root / 'alternate-source'
    shutil.copytree(source, alternate, ignore=shutil.ignore_patterns('.git', '.codex', '.agents', '.env*', '__pycache__', '.client-acceptance-*'))
    with (alternate / 'agents/team-reviewer.toml').open('a') as stream:
        stream.write('\n# Disposable restore acceptance fixture.\n')
    manage('Deploy', ('-SourceRoot', str(alternate), '-WhatIf'))
    manage('Deploy', ('-SourceRoot', str(alternate)))
    manage('Verify')
    changed = hashes(project).get('.codex/agents/team-reviewer.toml') != before['.codex/agents/team-reviewer.toml']
    shutil.rmtree(alternate)
    manage('Restore', ('-WhatIf',), deny=True)
    manage('Restore', deny=True)
    manage('Verify', deny=True)
    after = hashes(project)
    keys = [k for k in before if k.startswith(('.codex/agents/', '.agents/skills/')) or k in
            ('README.md', 'inventory.py', 'baseline/inventory.py', '.codex/config.toml', 'unrelated-sentinel')]
    restored = all(before[k] == after.get(k) for k in keys)
    if not changed or not restored:
        raise RuntimeError('Standalone restore did not preserve the expected bytes')
    return project, {'passed': True, 'steps': steps, 'alternateSourceRemoved': True,
                     'changedPayloadObserved': changed, 'exactPayloadRestored': restored,
                     'sourceReadEnforcement': 'OS denied on macOS; source-independent invocation on Linux'}


class RpcClient:
    """Nonblocking event pump; metadata reads never recursively wait in a callback."""
    def __init__(self, argv, env, cwd, collector):
        self.proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ, 'out')
        self.sel.register(self.proc.stderr, selectors.EVENT_READ, 'err')
        self.buffers = {'out': b'', 'err': b''}
        self.replies = {}
        self.pending_meta = {}
        self.collector = collector
        self.next_id = 0
        self.login = None
        self.stderr_lines = 0

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + '\n').encode())
        self.proc.stdin.flush()

    def begin(self, method, params):
        self.next_id += 1
        self.send({'id': self.next_id, 'method': method, 'params': params})
        return self.next_id

    def pump(self, timeout=.2):
        for key, _ in self.sel.select(timeout):
            data = os.read(key.fileobj.fileno(), 131072)
            if not data:
                self.sel.unregister(key.fileobj)
                continue
            self.buffers[key.data] += data
            if len(self.buffers[key.data]) > 8 * 1024 * 1024:
                raise RuntimeError('Protocol line exceeds acceptance limit')
            while b'\n' in self.buffers[key.data]:
                line, self.buffers[key.data] = self.buffers[key.data].split(b'\n', 1)
                if key.data == 'err':
                    self.stderr_lines += 1
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    raise RuntimeError('Malformed app-server JSON') from None
                if 'id' in obj and ('result' in obj or 'error' in obj):
                    if obj['id'] in self.pending_meta:
                        tid = self.pending_meta.pop(obj['id'])
                        if 'result' in obj:
                            self.collector.apply_metadata(tid, obj['result']['thread'])
                    else:
                        self.replies[obj['id']] = obj
                elif 'id' in obj:
                    self.send({'id': obj['id'], 'error': {'code': -32601, 'message': 'Interactive tool requests are outside acceptance scope'}})
                else:
                    method, params = obj.get('method', ''), obj.get('params', {})
                    if method == 'account/login/completed':
                        self.login = params
                    self.collector.handle_event(method, params)
        for tid in list(self.collector.metadata_pending):
            if tid not in self.pending_meta.values():
                self.collector.metadata_pending.discard(tid)
                ident = self.begin('thread/read', {'threadId': tid, 'includeTurns': False})
                self.pending_meta[ident] = tid

    def request(self, method, params, timeout=60):
        ident = self.begin(method, params)
        deadline = time.monotonic() + timeout
        while ident not in self.replies:
            self.pump()
            if self.proc.poll() is not None:
                raise RuntimeError('Client exited during ' + method)
            if time.monotonic() >= deadline:
                raise TimeoutError('Timed out during ' + method)
        reply = self.replies.pop(ident)
        if 'error' in reply:
            raise RuntimeError('JSON-RPC ' + method + ' failed with code ' + str(reply['error'].get('code')))
        return reply['result']

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.sel.close()


def check_linux_workspace_sandbox(client, root, env, project):
    # The pinned client accepts a command directly after "sandbox"; older
    # "sandbox linux --full-auto" syntax would try to execute a program named linux.
    run_offline([str(client), 'sandbox', '-c', 'sandbox_mode="workspace-write"',
                 '-c', 'sandbox_workspace_write.network_access=false', '--', '/usr/bin/true'],
                root, env, cwd=project)
    return {'passed': True, 'policy': 'workspace-write', 'networkAccess': False}


def check_schema(client, root, env):
    destination = root / 'schema'
    destination.mkdir()
    run_offline([str(client), 'app-server', 'generate-json-schema', '--experimental', '--out', str(destination)], root, env)
    definitions = {}
    for path in destination.glob('*.json'):
        value = json.loads(path.read_text())
        definitions.update(value.get('definitions', {}))
    required = {'Thread': ('id', 'parentThreadId', 'agentRole', 'model', 'reasoningEffort'),
                'ThreadReadParams': ('threadId', 'includeTurns'),
                'RawResponseItemCompletedNotification': ('threadId', 'item'),
                'AccountLoginCompletedNotification': ('success',),
                'ThreadStartParams': ('ephemeral', 'allowProviderModelFallback', 'experimentalRawEvents', 'model'),
                'TurnStartParams': ('effort', 'sandboxPolicy', 'input')}
    for kind, fields in required.items():
        if not all(f in definitions.get(kind, {}).get('properties', {}) for f in fields):
            raise RuntimeError('Client protocol lacks required ' + kind + ' fields')
    def variant(kind, value, fields):
        choices = definitions.get(kind, {}).get('oneOf', [])
        matches = [choice.get('properties', {}) for choice in choices
                   if value in choice.get('properties', {}).get('type', {}).get('enum', [])]
        if len(matches) != 1 or not all(field in matches[0] for field in fields):
            raise RuntimeError('Client protocol lacks required ' + kind + ' variant ' + value)
    variant('LoginAccountParams', 'chatgptDeviceCode', ('type',))
    variant('LoginAccountResponse', 'chatgptDeviceCode', ('verificationUrl', 'userCode'))
    sandbox = 'externalSandbox' if sys.platform == 'darwin' else 'workspaceWrite'
    variant('SandboxPolicy', sandbox, ('networkAccess',))
    return {'passed': True, 'requiredTypes': list(required) + ['LoginAccountParams', 'LoginAccountResponse', 'SandboxPolicy'],
            'deviceAuthentication': True, 'sandboxVariant': sandbox,
            'namedRoleAvailability': 'requires live invocation'}


def stage_client_runtime(client, helper, source, root, version):
    """Pin the helper, then expose the official sibling-bin package layout."""
    machine = platform.machine().lower()
    target = {'darwin': 'aarch64-apple-darwin', 'linux': 'aarch64-unknown-linux-musl'}.get(sys.platform)
    if machine not in ('arm64', 'aarch64') or target is None:
        raise RuntimeError('Pinned code-mode runtime requires macOS or Linux ARM64')
    lock = json.loads((source / 'config/client-acceptance-lock.json').read_text())
    pin = lock['platforms'][sys.platform + '-arm64']['codeModeHost']
    if lock.get('clientVersion') != version or pin.get('version') != version:
        raise RuntimeError('Code-mode host release does not match the pinned client')
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError('Code-mode host must be a provisioned executable file')
    helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}', pin.get('binarySha256', '')) or helper_hash != pin['binarySha256']:
        raise RuntimeError('Code-mode host binary does not match its same-release platform pin')
    package = root / 'client-runtime'
    (package / 'bin').mkdir(parents=True)
    for original, name in ((client, 'codex'), (helper, 'codex-code-mode-host')):
        staged = package / 'bin' / name
        shutil.copy2(original, staged)
        if hashlib.sha256(staged.read_bytes()).digest() != hashlib.sha256(original.read_bytes()).digest():
            raise RuntimeError('Client runtime changed while staging')
    if hashlib.sha256((package / 'bin/codex-code-mode-host').read_bytes()).hexdigest() != helper_hash:
        raise RuntimeError('Code-mode host changed while staging')
    for name in ('codex-resources', 'codex-path'):
        (package / name).mkdir()
    (package / 'codex-package.json').write_text(json.dumps({
        'layoutVersion': 1, 'version': version, 'target': target, 'variant': 'codex',
        'entrypoint': 'bin/codex', 'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}))
    return package / 'bin/codex', package / 'bin/codex-code-mode-host', {
        'version': version, 'binarySha256': helper_hash, 'archiveSha256': pin['sha256'],
        'packageLayout': 'disposable-sibling-bin', 'binaryPinPassed': True}


def check_runtime_preservation(runtime):
    """Compare staged bytes with hashes captured from sources before staging."""
    files = {}
    for label, (path, expected) in runtime.items():
        observed = None
        try:
            # Refuse replacement links, including linked ancestor directories,
            # instead of hashing files outside the owned staged runtime.
            if not path.is_symlink() and path.resolve(strict=True) == path and path.is_file():
                observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, RuntimeError):
            pass
        files[label] = {'expectedSha256': expected, 'observedSha256': observed,
                        'passed': observed is not None and observed == expected}
    return {'passed': bool(files) and all(value['passed'] for value in files.values()), 'files': files}


def check_code_mode_execution(helper, root, env):
    """Execute a real cell over the pinned helper's bounded v1 stdio protocol."""
    proc = subprocess.Popen(sandbox_command([str(helper), '--listen', 'stdio'], root,
        linux_root_netns=env.get(ROOT_NETNS_ENV) == '1', child_env=env), env=env, cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 30

    def send(value):
        encoded = json.dumps(value).encode()
        proc.stdin.write(struct.pack('<I', len(encoded)) + encoded)
        proc.stdin.flush()

    def read_exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                raise TimeoutError('Code-mode execution smoke exceeded thirty seconds')
            part = os.read(proc.stdout.fileno(), size - len(data))
            if not part:
                raise RuntimeError('Code-mode host exited before completing the smoke')
            data.extend(part)
        return bytes(data)

    def receive():
        size = struct.unpack('<I', read_exact(4))[0]
        if size > 1024 * 1024:
            raise RuntimeError('Code-mode smoke response exceeds its bounded frame size')
        try:
            value = json.loads(read_exact(size))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('Code-mode smoke received malformed JSON') from None
        if not isinstance(value, dict):
            raise RuntimeError('Code-mode smoke received an invalid frame')
        return value

    def operation(ident, request, expected):
        send({'type': 'operation/request', 'id': ident, 'request': request})
        value = receive()
        result = value.get('result', {})
        if (value.get('type') != 'operation/response' or value.get('id') != ident
                or not isinstance(result, dict) or result.get('status') != 'ok'
                or not isinstance(result.get('value'), dict) or result['value'].get('type') != expected):
            raise RuntimeError('Code-mode smoke operation was not confirmed')
        return result['value']

    try:
        send({'type': 'connection/hello', 'supportedVersions': [1],
              'requiredCapabilities': [], 'optionalCapabilities': []})
        hello = receive()
        if hello.get('type') != 'connection/ready' or hello.get('selectedVersion') != 1:
            raise RuntimeError('Code-mode host protocol v1 is unavailable')
        opened = operation(1, {'method': 'session/open', 'sessionId': 'acceptance-smoke'}, 'session/ready')
        if opened.get('sessionId') != 'acceptance-smoke':
            raise RuntimeError('Code-mode smoke session identity mismatch')
        started = operation(2, {'method': 'session/execute', 'sessionId': 'acceptance-smoke',
            'request': {'tool_call_id': 'acceptance-smoke', 'enabled_tools': [], 'source': 'text(6 * 7);',
                        'max_output_tokens': 128, 'yield_time_ms': 1000}}, 'execution/started')
        completed = receive()
        result = completed.get('result', {})
        value = result.get('value') if isinstance(result, dict) else None
        value = value.get('Result') if isinstance(value, dict) else None
        if (completed.get('type') != 'execute/initialResponse' or completed.get('id') != 2
                or not isinstance(result, dict) or result.get('status') != 'ok' or not isinstance(value, dict)
                or value.get('cell_id') != started.get('cellId')
                or value.get('error_text') is not None
                or value.get('content_items') != [{'type': 'input_text', 'text': '42'}]):
            raise RuntimeError('Code-mode smoke did not return the expected executed result')
        return {'passed': True, 'protocolVersion': 1, 'executedCell': 'text(6 * 7);', 'observedText': '42'}
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.stdout:
            proc.stdout.close()


def owned_helper_running(parent_pid, helper):
    """Observe only executable identities; never collect process arguments."""
    rows = subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid='], text=True, timeout=5)
    pairs = [tuple(map(int, row.split())) for row in rows.splitlines() if row.strip()]
    owned = {parent_pid}
    for _ in range(len(pairs)):
        found = owned | {pid for pid, parent in pairs if parent in owned}
        if found == owned:
            break
        owned = found
    owned.discard(parent_pid)
    if not owned:
        return False
    if sys.platform.startswith('linux'):
        for pid in owned:
            try:
                if Path('/proc/' + str(pid) + '/exe').resolve(strict=True) == helper:
                    return True
            except (OSError, RuntimeError):
                pass  # A process may exit between the snapshot and inspection.
        return False
    rows = subprocess.run(['/bin/ps', '-p', ','.join(map(str, sorted(owned))), '-o', 'pid=,comm='],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    return any(len(parts := line.strip().split(None, 1)) == 2 and Path(parts[1]) == helper
               for line in rows.stdout.splitlines())


def app_server_arguments(client, root, env, *, prewarm=False):
    overrides = ['analytics.enabled=false', 'feedback.enabled=false', 'check_for_update_on_startup=false',
                 'cli_auth_credentials_store="ephemeral"', 'web_search="disabled"',
                 'agents.enabled=true', 'agents.max_concurrent_threads_per_session=1',
                 'sqlite_home=' + json.dumps(str(root / 'sqlite')), 'log_dir=' + json.dumps(str(root / 'logs'))]
    feature_text = run_offline([str(client), 'features', 'list'], root, env)
    known = {line.split()[0] for line in feature_text.splitlines() if line.split()}
    for feature in ('apps', 'plugins', 'remote_plugin', 'recommended_plugins', 'hooks', 'memories',
                    'shell_snapshot', 'browser_use', 'computer_use', 'in_app_browser',
                    'unbounded_connection_retries', 'system_proxy_fallback'):
        if feature in known:
            overrides.append('features.' + feature + '=false')
    if prewarm:
        if not {'code_mode', 'code_mode_host', 'code_mode_prewarm'}.issubset(known):
            raise RuntimeError('Client lacks required offline code-mode prewarm features')
        overrides.extend(['features.code_mode=true', 'features.code_mode_host=true', 'features.code_mode_prewarm=true'])
    argv = [str(client), 'app-server', '--listen', 'stdio://']
    for value in overrides:
        argv += ['-c', value]
    return argv


def check_client_helper_resolution(client, helper, root, env):
    probe = root / 'helper-preflight'
    probe.mkdir()
    (probe / 'probe.txt').write_text('acceptance-local-read\n')
    argv = app_server_arguments(client, root, env, prewarm=True)
    rpc = RpcClient(sandbox_command(argv, root, linux_root_netns=env.get(ROOT_NETNS_ENV) == '1', child_env=env),
                    env, probe, EvidenceCollector())
    try:
        rpc.request('initialize', {'clientInfo': {'name': 'kit_helper_preflight', 'version': '0.10.2'},
                                  'capabilities': {'experimentalApi': True}}, timeout=20)
        rpc.send({'method': 'initialized'})
        rpc.request('thread/start', {'cwd': str(probe), 'model': LEAD_MODEL, 'ephemeral': True,
                    'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'allowProviderModelFallback': False}, timeout=20)
        deadline = time.monotonic() + 15
        while not owned_helper_running(rpc.proc.pid, helper):
            if rpc.proc.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('Client did not start the staged local code-mode host')
            rpc.pump(.1)
        sandbox = ({'type': 'externalSandbox', 'networkAccess': 'restricted'} if sys.platform == 'darwin'
                   else {'type': 'workspaceWrite', 'writableRoots': [str(probe)], 'networkAccess': False,
                         'excludeTmpdirEnvVar': False, 'excludeSlashTmp': True})
        result = rpc.request('command/exec', {'command': [sys.executable, '-c',
            'from pathlib import Path; print(Path("probe.txt").read_text(), end="")'],
            'cwd': str(probe), 'sandboxPolicy': sandbox, 'timeoutMs': 10000, 'outputBytesCap': 1024}, timeout=15)
        if result.get('exitCode') != 0 or result.get('stdout') != 'acceptance-local-read\n':
            raise RuntimeError('Client offline fixture-read command did not return the expected content')
        if not owned_helper_running(rpc.proc.pid, helper):
            raise RuntimeError('Client code-mode host exited during the offline fixture-read check')
        return {'passed': True, 'stagedExecutableObserved': True, 'ephemeralThread': True,
                'modelTurnSent': False, 'fixtureReadPassed': True}
    finally:
        rpc.close()


def app_server(client, root, env, project, guard, collector):
    argv = app_server_arguments(client, root, env)
    child_env = dict(env)
    child_env.update(HTTP_PROXY=guard.url, HTTPS_PROXY=guard.url, ALL_PROXY=guard.url)
    if sys.platform.startswith('linux'):
        bridge = root / 'bridge.sock'
        guard.unix_bridge(bridge)
        config = root / 'launch.json'
        config.write_text(json.dumps({'argv': argv}))
        argv = sandbox_command([sys.executable, str(Path(__file__).resolve()), '_net-child', str(bridge), str(config)], root,
                               linux_root_netns=env.get(ROOT_NETNS_ENV) == '1', child_env=child_env)
    else:
        argv = sandbox_command(argv, root, loopback=True)
    rpc = RpcClient(argv, child_env, project, collector)
    try:
        rpc.request('initialize', {'clientInfo': {'name': 'kit_client_acceptance', 'version': '0.10.2'},
                                   'capabilities': {'experimentalApi': True}})
        rpc.send({'method': 'initialized'})
        return rpc
    except BaseException:
        rpc.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True, type=Path)
    parser.add_argument('--code-mode-host', required=True, type=Path,
                        help='Same-release local helper executable; never a remote host URL')
    parser.add_argument('--pwsh', required=True, type=Path)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--result', required=True, type=Path)
    parser.add_argument('--mode', choices=('preflight', 'live'), default='preflight')
    parser.add_argument('--expected-client-version', default=CLIENT_VERSION)
    route = parser.add_mutually_exclusive_group()
    route.add_argument('--proxy-url-env', help='Name of the environment variable containing the approved proxy URL')
    route.add_argument('--allow-direct', action='store_true', help='Acknowledge a separately approved bounded direct run')
    parser.add_argument('--allow-installation-id', action='store_true')
    parser.add_argument('--linux-root-netns', action='store_true',
                        help='Explicit Linux opt-in: sudo -n creates an isolated network namespace, then drops all privilege')
    args = parser.parse_args(argv)
    if args.result.exists() or args.result.is_symlink() or not args.result.parent.is_dir():
        print('Result destination must be a new file in an existing directory.', file=sys.stderr)
        return 2
    for name in ('client', 'code_mode_host', 'pwsh', 'source_root', 'result'):
        setattr(args, name, getattr(args, name).resolve())
    report = {'schemaVersion': 1, 'mode': args.mode, 'passed': False, 'status': 'setup_failed',
              'runtime': {'os': platform.system(), 'release': platform.release(), 'architecture': platform.machine(),
                          'python': platform.python_version()},
              'harnessSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'intendedSettings': {'lead': {'model': LEAD_MODEL, 'effort': EFFORT},
                                   'reviewer': {'role': 'team-reviewer', 'model': REVIEWER_MODEL,
                                                'effort': EFFORT, 'forkTurns': 'none'}},
              'hostConfiguredSettings': {},
              'providerIdentity': {'resolvedModel': None, 'required': False},
              'authentication': {'attempted': False, 'success': None},
              'workflow': {'requested': False, 'completed': False},
              'cleanup': {'completed': False}, 'providerIdentityRequired': False}
    root = Path(tempfile.mkdtemp(prefix=PREFIX)).resolve()
    project = None
    before = None
    guard = None
    rpc = None
    staged_runtime = None
    collector = EvidenceCollector()
    try:
        if not args.result.parent.is_dir():
            raise ValueError('Result parent must exist')
        if not args.client.is_file() or not args.pwsh.is_file():
            raise ValueError('Client and PowerShell must be provisioned local files')
        if args.mode == 'live' and (not args.allow_installation_id or not (args.proxy_url_env or args.allow_direct)):
            raise ValueError('Live mode needs an approved route and explicit installation-ID authorization')
        proxy = os.environ.get(args.proxy_url_env, '') if args.proxy_url_env else None
        if args.proxy_url_env and not proxy:
            raise ValueError('Approved proxy variable is absent or empty')
        env = clean_environment(root, args.pwsh)
        if args.linux_root_netns:
            if not sys.platform.startswith('linux'):
                raise ValueError('--linux-root-netns is available only on Linux')
            env[ROOT_NETNS_ENV] = '1'
        report['namespaceBackend'] = ('root-created-drop-uid' if args.linux_root_netns else
                                      'unprivileged-user' if sys.platform.startswith('linux') else 'seatbelt')
        print('Offline preflight: pinned client and protocol capabilities', flush=True)
        version = run_offline([str(args.client), '--version'], root, env).strip().split()[-1]
        report['client'] = {'version': version, 'sha256': hashlib.sha256(args.client.read_bytes()).hexdigest()}
        if version != args.expected_client_version:
            raise RuntimeError('Client version does not match the pinned version')
        report['codeModeHost'] = {'passed': False}
        helper_stage = 'binary_pin_and_staging'
        try:
            client, helper, report['codeModeHost'] = stage_client_runtime(
                args.client, args.code_mode_host, args.source_root, root, version)
            staged_runtime = {'client': (client, report['client']['sha256']),
                              'codeModeHost': (helper, report['codeModeHost']['binarySha256'])}
            report['codeModeHost']['passed'] = False
            env['PATH'] = str(client.parent) + os.pathsep + env.get('PATH', '')
            print('Offline preflight: real code-mode helper execution and client resolution', flush=True)
            helper_stage = 'helper_cell_execution'
            report['codeModeHost']['execution'] = check_code_mode_execution(helper, root, env)
            helper_stage = 'client_resolution_and_file_read'
            report['codeModeHost']['clientResolution'] = check_client_helper_resolution(client, helper, root, env)
            report['codeModeHost']['passed'] = True
        except Exception:
            report['codeModeHost']['failedStage'] = helper_stage
            report['status'] = 'compatibility_blocked'
            raise RuntimeError('Pinned code-mode host execution or client resolution failed before authentication') from None
        report['protocol'] = check_schema(client, root, env)
        report['runtime']['powerShell'] = run_offline([str(args.pwsh), '-NoLogo', '-NoProfile',
            '-NonInteractive', '-Command', '$PSVersionTable.PSVersion.ToString()'], root, env).strip()
        try:
            report['sourceCommit'] = subprocess.check_output(['git', '-C', str(args.source_root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
        except subprocess.CalledProcessError:
            report['sourceCommit'] = None
        print('Offline preflight: disposable installation and standalone restore', flush=True)
        project, report['recovery'] = prepare_fixture(args.source_root, root, args.pwsh, env)
        collector.fixture_root = project
        if sys.platform.startswith('linux'):
            report['commandSandbox'] = {'passed': False, 'policy': 'workspace-write', 'networkAccess': False}
            print('Offline preflight: native Linux workspace command sandbox', flush=True)
            # This uses no account or model. The outer network namespace remains
            # in force while the client's actual workspace sandbox runs a no-op.
            try:
                report['commandSandbox'] = check_linux_workspace_sandbox(client, root, env, project)
            except Exception:
                report['status'] = 'compatibility_blocked'
                raise RuntimeError('Linux workspace command sandbox failed; verify bubblewrap and nested namespace support') from None
        report['oracle'] = oracle(project)
        before = hashes(project)
        report['installedPayloadSha256'] = hashlib.sha256(''.join(
            k + ':' + before[k] + '\n' for k in sorted(before)
            if k.startswith(('.codex/agents/', '.agents/skills/'))).encode()).hexdigest()
        guard = ConnectGuard(proxy, args.allow_direct, enabled=False).start()
        print('Offline preflight: fresh client configuration and Skill discovery', flush=True)
        rpc = app_server(client, root, env, project, guard, collector)
        config = rpc.request('config/read', {'cwd': str(project), 'includeLayers': True})
        layers = [layer for layer in config.get('layers', [])
                  if layer.get('name', {}).get('dotCodexFolder') == str(project / '.codex')]
        if len(layers) != 1 or layers[0].get('disabledReason'):
            raise RuntimeError('Disposable project config did not load as trusted')
        skills = rpc.request('skills/list', {'cwds': [str(project)], 'forceReload': True})
        expected = {p.name for p in (project / '.agents/skills').iterdir() if p.is_dir()}
        found = {}
        external_skills = []
        for entry in skills.get('data', []):
            for skill in entry.get('skills', []):
                path = Path(skill.get('path', ''))
                if path.is_relative_to(project / '.agents/skills'):
                    found[skill['name']] = {'enabled': skill.get('enabled'), 'path': path.relative_to(project).as_posix()}
                elif skill.get('enabled') and not path.is_relative_to(root / 'client-home'):
                    external_skills.append({'scope': skill.get('scope'), 'name': skill.get('name')})
            if entry.get('errors'):
                raise RuntimeError('Client reported Skill discovery errors')
        report['discovery'] = {'skills': found, 'projectConfigLoaded': True,
                               'externalEnabledSkills': external_skills}
        if external_skills:
            raise RuntimeError('Client enabled Skills outside the disposable fixture and client home')
        if len(expected) != 13 or set(found) != expected or not all(v['enabled'] for v in found.values()):
            raise RuntimeError('Client did not enable all thirteen installed Skills')
        report['preflightPassed'] = True
        if args.mode == 'live':
            print('Live destination: chatgpt.com, api.openai.com, auth.openai.com; route: ' +
                  ('approved proxy' if proxy else 'approved direct') + '; purpose: device login and one bounded review.', flush=True)
            guard.enabled = True
            report['authentication']['attempted'] = True
            report['status'] = 'authentication_failed'
            login = rpc.request('account/login/start', {'type': 'chatgptDeviceCode'})
            if login.get('type') != 'chatgptDeviceCode':
                raise RuntimeError('Client did not offer device-code authentication')
            url = login.get('verificationUrl', '')
            if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in HOSTS:
                raise RuntimeError('Unexpected device-login verification destination')
            # Interactive output only. Never include the one-time code in JSON/log artifacts.
            print('Complete device login: ' + url + '\nOne-time code: ' + login['userCode'], flush=True)
            deadline = time.monotonic() + 600
            while rpc.login is None and time.monotonic() < deadline:
                rpc.pump()
                if rpc.proc.poll() is not None:
                    raise RuntimeError('Client exited during device login')
            if not rpc.login or not rpc.login.get('success'):
                report['authentication'].update(success=False, timedOut=rpc.login is None)
                raise RuntimeError('Device login failed or exceeded ten minutes')
            report['authentication'].update(success=True, timedOut=False)
            report['status'] = 'acceptance_failed'
            settings = {'model_reasoning_effort': EFFORT, 'web_search': 'disabled'}
            for name in (config['config'].get('mcp_servers') or {}):
                settings['mcp_servers.' + name + '.enabled'] = False
            thread = rpc.request('thread/start', {'cwd': str(project), 'ephemeral': False,
                'model': LEAD_MODEL, 'modelProvider': 'openai', 'allowProviderModelFallback': False,
                'approvalPolicy': 'never', 'sandbox': 'read-only', 'experimentalRawEvents': True,
                'config': settings, 'developerInstructions': 'Bounded acceptance: inspect only this fixture and its installed Skills. Exactly one named team-reviewer is allowed. Preserve all input/installed files. No networking, connectors, personal files, additional agents, or model/role substitutions. Minimal feedback may be recorded only under .agents/codex-multi-agent/feedback.'})
            collector.lead_id = thread['thread']['id']
            collector.apply_metadata(collector.lead_id, dict(thread['thread'], model=thread['model'], reasoningEffort=thread.get('reasoningEffort')))
            if (thread.get('model') != LEAD_MODEL or thread.get('reasoningEffort') != EFFORT
                    or collector.errors):
                raise RuntimeError('Lead host configuration was not confirmed before inference')
            prompt = ('$team-review Review baseline/inventory.py versus inventory.py against README.md; assume integer inputs. '
                      'Read installed workflow references. Use exactly one real agent_type: team-reviewer with fork_turns: none, '
                      'self-contained fixture paths/instructions and no model or effort overrides. If unavailable stop without fallback. '
                      'Wait for its final finding and give the normal findings, coverage and actual-agent roster. '
                      'The reviewer must read files and check the documented integer boundaries. No edits to inputs or installed kit. '
                      'Use straightforward successful file-read commands for all three scoped files, such as cat with explicit '
                      'file operands, or literal Python open/Path reads. Complex read syntax may remain unverified by the harness. '
                      'In both the reviewer final and your final, retain normal findings/coverage prose and append exactly one plain '
                      'line starting ACCEPTANCE_FINDING: followed by a JSON object for a confirmed regression. Its fields are '
                      'path (repository-relative string), line (integer), stock (integer), requested (integer), expected '
                      '(boolean required by the documented contract), and actual (boolean returned by the candidate). '
                      'Compute a concrete input witness from the files; do not invent one. If no regression can be confirmed, '
                      'explain the gap and omit the marker. Include this neutral evidence format in the reviewer instructions. '
                      'Only minimal local feedback under .agents/codex-multi-agent/feedback is permitted. Do not browse or inspect surrounding directories.')
            sandbox = ({'type': 'externalSandbox', 'networkAccess': 'restricted'} if sys.platform == 'darwin'
                       else {'type': 'workspaceWrite', 'writableRoots': [str(project)], 'networkAccess': False,
                             'excludeTmpdirEnvVar': False, 'excludeSlashTmp': True})
            report['workflow']['requested'] = True
            turn = rpc.request('turn/start', {'threadId': collector.lead_id, 'input': [
                {'type': 'text', 'text': prompt}, {'type': 'skill', 'name': 'team-review', 'path': str(project / '.agents/skills/team-review/SKILL.md')}],
                'effort': EFFORT, 'approvalPolicy': 'never', 'sandboxPolicy': sandbox})
            deadline, tick = time.monotonic() + 600, time.monotonic()
            while collector.lead_id not in collector.turns and time.monotonic() < deadline:
                rpc.pump()
                if rpc.proc.poll() is not None:
                    raise RuntimeError('Client exited during workflow')
                if len(collector.spawns) > 1 or len(collector.children) > 1:
                    collector.fail('Unexpected additional agent; interrupted bounded run')
                if collector.errors:
                    rpc.request('turn/interrupt', {'threadId': collector.lead_id, 'turnId': turn['turn']['id']})
                    break
                if time.monotonic() - tick >= 30:
                    print('Review active; observed children: ' + str(len(collector.children)), flush=True)
                    tick = time.monotonic()
            if collector.lead_id not in collector.turns:
                if not collector.errors:
                    collector.fail('Timed out after 600 seconds')
                    rpc.request('turn/interrupt', {'threadId': collector.lead_id, 'turnId': turn['turn']['id']})
            for tid in {collector.lead_id} | collector.children:
                meta = rpc.request('thread/read', {'threadId': tid, 'includeTurns': False})['thread']
                collector.apply_metadata(tid, meta)
            report['assessment'] = collector.assess()
            report['workflow']['completed'] = report['assessment']['workflowPassed']
            report['passed'] = report['assessment']['workflowPassed']
        else:
            report['passed'] = True
        report['status'] = 'passed' if report['passed'] else 'acceptance_failed'
    except (Exception, KeyboardInterrupt) as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        if collector.lead_id:
            collector.fail('Acceptance runtime interrupted or failed')
            report['assessment'] = collector.assess()
    finally:
        report['hostConfiguredSettings'] = dict(collector.metadata)
        if rpc:
            try:
                rpc.close()
            except Exception:
                report['cleanup']['clientShutdownFailed'] = True
                report['passed'] = False
            report['stderrLineCount'] = rpc.stderr_lines
        if guard:
            try:
                guard.close()
            except Exception:
                report['cleanup']['guardShutdownFailed'] = True
                report['passed'] = False
            report['network'] = guard.summary()
        if staged_runtime:
            report['runtimePreservation'] = check_runtime_preservation(staged_runtime)
            if not report['runtimePreservation']['passed']:
                report['passed'] = False
                report['status'] = 'acceptance_failed'
        if project and before:
            try:
                after = hashes(project)
                changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                allowed = all(k.startswith('.agents/codex-multi-agent/feedback/') for k in changed)
                report['preservation'] = {'passed': allowed, 'changedPaths': changed}
                if not allowed:
                    report['passed'] = False
                    report['status'] = 'acceptance_failed'
            except Exception:
                report['passed'] = False
                report['preservation'] = {'passed': False, 'error': 'Fixture could not be safely inventoried'}
        replacements = ((str(project), '<fixture>'), (str(root), '<temporary>'),
                        (str(args.source_root), '<source>'), (str(args.client), '<client>'),
                        (str(args.pwsh), '<pwsh>'), (str(Path.home()), '<home>'))
        try:
            remove_owned_tree(root, root.parent, PREFIX)
            report['cleanup']['temporaryStateRemoved'] = True
        except Exception:
            report['cleanup']['error'] = 'Owned temporary state could not be removed'
            report['passed'] = False
            report['cleanup']['temporaryStateRemoved'] = False
        report['cleanup']['completed'] = (report['cleanup'].get('temporaryStateRemoved') is True
                                          and not report['cleanup'].get('clientShutdownFailed')
                                          and not report['cleanup'].get('guardShutdownFailed'))
        if not report['cleanup']['completed']:
            report['status'] = 'cleanup_failed'
        elif not report['passed'] and report['status'] == 'passed':
            report['status'] = 'acceptance_failed'
        report['finishedAt'] = datetime.now(timezone.utc).isoformat()
        serialized = redact(json.dumps(report, indent=2, ensure_ascii=False), replacements)
        if args.result.parent.is_dir():
            with args.result.open('x', encoding='utf-8') as stream:
                stream.write(serialized + '\n')
        print('Acceptance ' + ('passed' if report['passed'] else 'failed') + '; mode=' + args.mode, flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_net-child':
        sys.exit(linux_net_child(sys.argv[2], sys.argv[3]))
    sys.exit(main())
