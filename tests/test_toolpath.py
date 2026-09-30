"""Finding claude / codex (and node) outside the LaunchAgent's fixed PATH.

Every test uses a fake HOME with fake executables; the login shell is mocked
and the system directories are pointed into the temp dir, so what is
installed on the machine running the tests never matters."""
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from timetrace import cli, config, runner_state, toolpath
from timetrace.agent import Agent
from timetrace.db import Database

NATIVE = "#!/bin/sh\necho '2.1.3 (Claude Code)'\n"
NODE_SCRIPT = "#!/usr/bin/env node\nconsole.log('codex-cli 0.151.0')\n"


def make_exe(path: Path, body: str = NATIVE) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


class FakeHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.user = self.root / "Users" / "joey"
        self.user.mkdir(parents=True)
        self.sys = self.root / "sys"
        (self.sys / "bin").mkdir(parents=True)
        self.data = self.root / "data"
        self.shell_answers = {}
        for target, value in (
            ("timetrace.toolpath.SYSTEM_DIRS", (str(self.sys / "local"), str(self.sys / "brew"),
                                                str(self.sys / "usr"), str(self.sys / "bin"))),
            ("timetrace.toolpath.APP_DIRS", (str(self.root / "Codex.app"),)),
            ("timetrace.toolpath.login_shell_which", lambda name, *a, **k: self.shell_answers.get(name)),
            ("timetrace.toolpath.npm_global_bin", lambda *a, **k: None),
        ):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)

    def resolve(self, name, **kw):
        return toolpath.resolve_tool(name, self.user, shell_which=toolpath.login_shell_which, **kw)


class ScanTest(FakeHome):
    def test_each_common_install_location_is_found(self):
        cases = {
            ".volta/bin": "Volta", ".bun/bin": "bun", "Library/pnpm": "pnpm",
            ".local/share/mise/shims": "mise shims", ".asdf/shims": "asdf shims",
            ".nvm/versions/node/v20.11.0/bin": "nvm", ".local/share/mise/installs/node/latest/bin": "mise install",
            ".asdf/installs/nodejs/20.1.0/bin": "asdf install", ".npm-global/bin": "npm prefix",
            ".claude/local": "claude local install", ".local/bin": "native installer",
        }
        for rel, label in cases.items():
            with self.subTest(label):
                exe = make_exe(self.user / rel / "codex")
                res = self.resolve("codex")
                self.assertEqual(res.path, exe)
                self.assertEqual(res.source, "scan")
                os.remove(exe)

    def test_codex_app_bundle_is_found(self):
        exe = make_exe(self.root / "Codex.app" / "codex")
        self.assertEqual(self.resolve("codex").path, exe)

    def test_npm_global_prefix_is_scanned(self):
        exe = make_exe(self.root / "npm-prefix" / "bin" / "claude")
        self.assertIsNone(self.resolve("claude").path)
        self.assertEqual(self.resolve("claude", npm_bin=str(self.root / "npm-prefix" / "bin")).path, exe)

    def test_newest_nvm_version_and_mise_latest_win(self):
        make_exe(self.user / ".nvm/versions/node/v18.20.0/bin/claude")
        newest = make_exe(self.user / ".nvm/versions/node/v22.3.0/bin/claude")
        make_exe(self.user / ".nvm/versions/node/v9.0.0/bin/claude")
        self.assertEqual(self.resolve("claude").path, newest)
        make_exe(self.user / ".local/share/mise/installs/node/20.1.0/bin/codex")
        latest = make_exe(self.user / ".local/share/mise/installs/node/latest/bin/codex")
        self.assertEqual(self.resolve("codex").path, latest)

    def test_login_shell_answer_wins_over_the_scan(self):
        make_exe(self.user / ".local/bin/claude")
        shell = make_exe(self.user / ".nvm/versions/node/v20.0.0/bin/claude", NODE_SCRIPT)
        self.shell_answers["claude"] = shell
        res = self.resolve("claude")
        self.assertEqual((res.path, res.source), (shell, "login_shell"))

    def test_not_found_lists_the_places_searched(self):
        res = self.resolve("codex")
        self.assertIsNone(res.path)
        self.assertIn("command -v codex", res.searched[0])
        self.assertIn("~/.volta/bin", res.searched)
        self.assertIn("~/.local/share/mise/shims", res.searched)
        self.assertIn("~/Library/pnpm", res.searched)

    def test_node_script_gets_the_node_next_to_it(self):
        codex = make_exe(self.user / ".local/share/mise/installs/node/latest/bin/codex", NODE_SCRIPT)
        node = make_exe(self.user / ".local/share/mise/installs/node/latest/bin/node")
        make_exe(self.sys / "local" / "node")  # an older node earlier in the scan order
        res = self.resolve("codex")
        self.assertEqual(res.path, codex)
        self.assertTrue(res.needs_node)
        self.assertEqual(res.node, node)

    def test_npm_symlinked_bin_is_a_node_script(self):
        target = make_exe(self.user / ".volta/tools/image/packages/claude/lib/cli.js", NODE_SCRIPT)
        link = self.user / ".volta/bin/claude"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.shell_answers["node"] = make_exe(self.user / ".volta/bin/node")
        res = self.resolve("claude")
        self.assertEqual(res.path, str(link))
        self.assertTrue(res.needs_node)
        self.assertEqual(res.node, str(self.user / ".volta/bin/node"))

    def test_native_binary_needs_no_node(self):
        make_exe(self.user / ".local/bin/claude")
        self.assertFalse(self.resolve("claude").needs_node)

    def test_explicit_configured_path_is_used_as_is(self):
        exe = make_exe(self.root / "custom" / "claude")
        make_exe(self.user / ".local/bin/claude")
        res = self.resolve("claude", configured=exe)
        self.assertEqual((res.path, res.source), (exe, "config"))
        missing = self.resolve("claude", configured=str(self.root / "gone" / "claude"))
        self.assertIsNone(missing.path)


class LoginShellTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.exe = make_exe(Path(tmp.name) / "bin" / "claude")

    def fake(self, stdout="", raises=None):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            if raises:
                raise raises
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return run, calls

    def test_takes_the_path_line_and_ignores_rc_noise(self):
        run, calls = self.fake("Restored session\n%s\n" % self.exe)
        self.assertEqual(toolpath.login_shell_which("claude", shell="/bin/zsh", run=run), self.exe)
        cmd, kw = calls[0]
        self.assertEqual(cmd, ["/bin/zsh", "-lic", "command -v claude"])
        self.assertEqual(kw["timeout"], toolpath.SHELL_TIMEOUT_SECONDS)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_alias_is_followed(self):
        run, _ = self.fake("alias claude='%s'\n" % self.exe)
        self.assertEqual(toolpath.login_shell_which("claude", shell="/bin/zsh", run=run), self.exe)

    def test_timeout_missing_or_function_means_none(self):
        run, _ = self.fake(raises=subprocess.TimeoutExpired("zsh", 8))
        self.assertIsNone(toolpath.login_shell_which("claude", shell="/bin/zsh", run=run))
        run, _ = self.fake("claude\n")  # a shell function
        self.assertIsNone(toolpath.login_shell_which("claude", shell="/bin/zsh", run=run))
        run, _ = self.fake("/nonexistent/claude\n")
        self.assertIsNone(toolpath.login_shell_which("claude", shell="/bin/zsh", run=run))

    def test_refuses_names_that_would_need_quoting(self):
        run, calls = self.fake(self.exe)
        self.assertIsNone(toolpath.login_shell_which("claude; rm -rf ~", shell="/bin/zsh", run=run))
        self.assertEqual(calls, [])


class LaunchPathTest(FakeHome):
    def test_launch_path_keeps_defaults_adds_tool_and_node_dirs_deduped(self):
        mise = self.user / ".local/share/mise/installs/node/latest/bin"
        codex = toolpath.Resolution("codex", path=str(mise / "codex"), needs_node=True, node=str(mise / "node"))
        claude = toolpath.Resolution("claude", path=str(self.user / ".local/bin/claude"))
        missing = toolpath.Resolution("gemini")
        value = toolpath.launch_path(self.user, [claude, codex, missing])
        parts = value.split(":")
        self.assertEqual(parts[0], str(mise))  # node's directory first
        for d in toolpath.default_path_dirs(self.user):
            self.assertIn(d, parts)
        self.assertEqual(len(parts), len(set(parts)))
        self.assertEqual(parts.count(str(mise)), 1)

    def test_runner_visibility(self):
        mise = self.user / ".local/share/mise/installs/node/latest/bin"
        codex = make_exe(mise / "codex", NODE_SCRIPT)
        make_exe(mise / "node")
        default = toolpath.join_path(toolpath.default_path_dirs(self.user))
        self.assertEqual(toolpath.runner_visibility(codex, True, default), (False, "not_on_path"))
        self.assertEqual(toolpath.runner_visibility(codex, True, default + ":" + str(mise)), (True, ""))
        other = self.root / "elsewhere"
        make_exe(other / "codex", NODE_SCRIPT)
        self.assertEqual(toolpath.runner_visibility(str(other / "codex"), True, str(other)),
                         (False, "node_not_on_path"))
        self.assertEqual(toolpath.runner_visibility(str(self.root / "gone"), False, default),
                         (False, "binary_missing"))

    def test_runtime_path_gains_the_configured_dirs(self):
        mise = self.user / ".local/share/mise/installs/node/latest/bin"
        codex = make_exe(mise / "codex", NODE_SCRIPT)
        make_exe(mise / "node")
        env = {"PATH": "/usr/bin:/bin"}
        toolpath.extend_runtime_path([codex, "claude", ""], environ=env)
        self.assertEqual(env["PATH"], "%s:/usr/bin:/bin" % mise)
        toolpath.extend_runtime_path([codex], environ=env)
        self.assertEqual(env["PATH"], "%s:/usr/bin:/bin" % mise)

    def test_launch_agent_state_parses_launchctl_print(self):
        def run(cmd, **kw):
            self.assertEqual(cmd[:2], ["launchctl", "print"])
            return subprocess.CompletedProcess(cmd, 0, stdout="gui/501/x = {\n\tstate = running\n\tpid = 4242\n}", stderr="")
        self.assertEqual(toolpath.launch_agent_state(501, run=run), {"loaded": True, "running": True, "pid": 4242})

        def missing(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 113, stdout="", stderr="Could not find service")
        self.assertEqual(toolpath.launch_agent_state(501, run=missing)["loaded"], False)


class Verified:
    def __init__(self, ok=True, reason="not_logged_in"):
        self.ok, self.reason = ok, reason

    def capabilities(self):
        return {"can_enforce_zero_spend": self.ok}

    def capability_details(self):
        if self.ok:
            return {"can_enforce_zero_spend": True, "auth_method": "claude.ai/max", "verified_at": 1000}
        return {"can_enforce_zero_spend": False, "unsupported_reason": self.reason}

    def plan_tier(self):
        return None


class CliDiscoveryTest(FakeHome):
    """The new Mac from the bug report: claude from the native installer in
    ~/.local/bin, codex an npm global of a mise-installed node."""

    def setUp(self):
        super().setUp()
        self.claude = make_exe(self.user / ".local/bin/claude")
        mise = self.user / ".local/share/mise/installs/node/latest/bin"
        self.mise = str(mise)
        self.codex = make_exe(mise / "codex", NODE_SCRIPT)
        self.node = make_exe(mise / "node", "#!/bin/sh\necho v22.9.0\n")
        # the shells's codex prints its version through the node script shebang;
        # a /bin/sh stand-in keeps the test hermetic
        Path(self.codex).write_text("#!/usr/bin/env node\n")
        self.shell_answers.update({"claude": self.claude, "codex": self.codex})
        self.adapters = {"claude": Verified(True), "codex": Verified(False, "not_logged_in")}
        self.installed = []
        for target, value in (
            ("timetrace.cli.Path.home", lambda: self.user),
            ("timetrace.cli._adapters", lambda cfg: self.adapters),
            ("timetrace.cli.CredentialStore.load", lambda store, account="default": None),
            ("timetrace.cli.install_launch_agent", lambda *a, **k: self.installed.append(k) or Path("/x.plist")),
            ("timetrace.cli._tool_version", lambda binary, node=None: {
                self.claude: "2.1.3 (Claude Code)", self.codex: "codex-cli 0.151.0"}.get(binary, "?")),
            ("timetrace.toolpath.launch_agent_state", lambda *a, **k: {"loaded": True, "running": True, "pid": 77}),
        ):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"TIMETRACE_HOME": str(self.data)})
        env.start()
        self.addCleanup(env.stop)

    def write_plist(self, path_value):
        plist = toolpath.plist_path(self.user)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"Label": toolpath.LAUNCH_AGENT_LABEL,
                                          "EnvironmentVariables": {"PATH": path_value}}))

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def user_config(self):
        return json.loads((self.data / "config.json").read_text())

    def test_agent_install_records_bins_and_builds_the_path(self):
        code, out = self.run_cli("agent", "install")
        self.assertEqual(code, 0, out)
        doc = self.user_config()
        self.assertEqual(doc["claude"], {"bin": self.claude, "bin_source": "auto"})
        self.assertEqual(doc["codex"], {"bin": self.codex, "bin_source": "auto"})
        path = self.installed[0]["path"].split(":")
        self.assertEqual(path[0], self.mise)                  # the node codex needs, first
        self.assertIn(str(self.user / ".local/bin"), path)
        self.assertIn("/usr/bin", [p.replace(str(self.sys), "") for p in path] + ["/usr/bin"])
        self.assertIn(self.codex, out)

    def test_explicit_bin_is_never_overwritten_but_auto_one_is_refreshed(self):
        custom = make_exe(self.root / "custom" / "claude")
        self.assertEqual(self.run_cli("config", "set", "claude.bin", custom)[0], 0)
        self.run_cli("agent", "install")
        doc = self.user_config()
        self.assertEqual(doc["claude"], {"bin": custom})
        # an auto-recorded path that moved (node upgrade) is re-resolved
        doc["codex"] = {"bin": "/old/node/v18/bin/codex", "bin_source": "auto"}
        (self.data / "config.json").write_text(json.dumps(doc))
        self.run_cli("agent", "install")
        self.assertEqual(self.user_config()["codex"]["bin"], self.codex)
        cfg = config.load(self.data)
        self.assertEqual(cfg["claude"]["bin"], custom)
        self.assertEqual(cli._runner_tools(cfg, {"claude": Verified(True)})[0]["status"], "available")

    def test_config_set_bin_validates_the_path(self):
        code, _ = self.run_cli("config", "set", "codex.bin", str(self.root / "nope"))
        self.assertEqual(code, 2)
        code, out = self.run_cli("config", "set", "codex.bin", self.codex)
        self.assertEqual(code, 0)
        self.assertIn("timetrace agent install", out)
        self.assertEqual(self.run_cli("config", "get", "codex.bin")[1].strip(), self.codex)

    def test_doctor_separates_shell_from_runner_and_explains_the_fix(self):
        self.write_plist(toolpath.join_path(toolpath.default_path_dirs(self.user)))
        self.data.mkdir()
        runner_state.record(self.data, inventory_pushed_at=1790000000, inventory_workspaces=0, inventory_tools=2,
                            quota_reported_at=1790000100)
        code, out = self.run_cli("agent", "doctor")
        self.assertEqual(code, 1)  # not paired, no workspaces
        self.assertIn("后台 Runner: 已安装，运行中（pid 77）", out)
        self.assertIn("上次上报工具清单: %s（0 个工作区，2 个工具）" % cli._iso_local(1790000000), out)
        self.assertIn("上次上报额度: %s" % cli._iso_local(1790000100), out)
        self.assertIn("工作区: 0 —— 没有登记仓库，手机无法派发远程任务；运行 timetrace workspace add <path>", out)
        self.assertIn("工具清单和额度仍会照常上报", out)
        claude, codex = out.split("\nclaude: ")[1].split("\ncodex: ")
        self.assertTrue(claude.startswith(self.claude + "（登录 shell 找到）"))
        self.assertIn("后台 Runner: 能找到", claude)
        self.assertIn("版本: 2.1.3 (Claude Code)", claude)
        self.assertIn("零付费核验: 通过", claude)
        self.assertTrue(codex.startswith(self.codex + "（登录 shell 找到）"))
        self.assertIn("后台 Runner: 找不到 —— LaunchAgent 的 PATH 里没有它所在的目录", codex)
        self.assertIn("修复: 运行 timetrace agent install（LaunchAgent 的 PATH 会加入 %s）" % self.mise, codex)
        self.assertIn("版本: codex-cli 0.151.0", codex)
        self.assertIn("零付费核验: 未通过（not_logged_in）", codex)

    def test_doctor_reports_node_missing_from_the_runner_path(self):
        self.write_plist(toolpath.join_path(toolpath.default_path_dirs(self.user) + [self.mise]))
        os.remove(self.node)
        code, out = self.run_cli("agent", "doctor")
        codex = out.split("\ncodex: ")[1]
        self.assertIn("注意: codex 是 node 脚本，但没有找到 node", codex)
        self.assertIn("它是 node 脚本，但 LaunchAgent 的 PATH 里没有 node", codex)

    def test_doctor_not_found_lists_places_and_the_config_hint(self):
        os.remove(self.codex)
        self.shell_answers.pop("codex")
        code, out = self.run_cli("agent", "doctor")
        self.assertIn("后台 Runner: 未安装 → 运行 timetrace agent install", out)
        self.assertIn("上次上报工具清单: 从未上报", out)
        codex = out.split("\ncodex: ")[1]
        self.assertTrue(codex.startswith("未找到"))
        self.assertIn("已查找: 登录 shell（", codex)
        self.assertIn("~/.nvm/versions/node/*/bin", codex)
        self.assertIn("~/.local/share/mise/installs/*/*/bin", codex)
        self.assertIn("~/.volta/bin", codex)
        self.assertIn("修复: 运行 timetrace config set codex.bin /path/to/codex 然后 timetrace agent install", codex)
        self.assertNotIn("codex", self.user_config() if (self.data / "config.json").exists() else {})

    def test_setup_warns_when_the_shell_sees_a_tool_the_runner_would_not(self):
        out = []
        args = cli.build_parser().parse_args(["setup", "--no-pair", "--yes"])
        cli.run_setup(args, input_fn=lambda q="": "", print_fn=lambda *a: out.append(" ".join(map(str, a))))
        text = "\n".join(out)
        self.assertIn("注意：codex 在终端里能找到（%s），但后台 Runner 的 PATH 里没有" % self.codex, text)
        self.assertIn("第 4 步安装后台 Runner 时会把 %s 加入它的 PATH" % self.mise, text)
        self.assertNotIn("注意：claude 在终端里能找到", text)
        self.assertEqual(self.installed[0]["path"].split(":")[0], self.mise)

    def test_setup_skipping_the_install_says_the_installed_runner_is_still_blind(self):
        self.write_plist(toolpath.join_path(toolpath.default_path_dirs(self.user)))
        out = []
        args = cli.build_parser().parse_args(["setup", "--no-pair", "--no-agent", "--yes"])
        cli.run_setup(args, input_fn=lambda q="": "", print_fn=lambda *a: out.append(" ".join(map(str, a))))
        self.assertIn("已安装的后台 Runner 仍找不到 codex，运行 `timetrace agent install` 修复", "\n".join(out))


class ZeroWorkspaceInventoryTest(unittest.TestCase):
    def test_inventory_and_quota_are_reported_without_workspaces(self):
        class Cloud:
            def __init__(self):
                self.inventory = []

            def update_inventory(self, token, workspaces, tools, *rest):
                self.inventory.append((workspaces, tools))

            def post_quota_samples(self, token, samples):
                return {}

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "timetrace.db")
            cloud = Cloud()
            tools = [{"id": "claude-default", "provider": "claude"}]
            agent = Agent(db, cloud, {}, Path(d), lambda: "t", inventory=lambda: ([], tools, 2, {"claude": 2}))
            agent.maintain(now=1790000000.0, force=True)
            self.assertEqual(cloud.inventory, [([], tools)])
            state = runner_state.load(Path(d))
            self.assertEqual((state["inventory_pushed_at"], state["inventory_workspaces"], state["inventory_tools"]),
                             (1790000000, 0, 1))
            self.assertEqual(state["quota_reported_at"], 1790000000)


if __name__ == "__main__":
    unittest.main()
