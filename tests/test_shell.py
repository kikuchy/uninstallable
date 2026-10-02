"""Integration tests for the Bash executable; no alternate implementation is loaded."""
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "uninstallable.sh"


def installer(name="sampleapp", variable="SAMPLEAPP_INSTALL", flat=False, failure=False):
    target = '"$destination"' if flat else '"$destination/bin"'
    return f'''#!/bin/sh
set -eu
destination=${{{variable}:-$HOME/.{name}}}
mkdir -p {target}
bin={target}/{name}
printf '#!/bin/sh\nprintf "installed-ok\\n"\n' > "$bin"
chmod +x "$bin"
''' + ("exit 7\n" if failure else "")


@unittest.skipUnless(shutil.which("jq") and shutil.which("mise"), "jq and mise are required")
class ShellIntegrationTests(unittest.TestCase):
    command = ["/bin/bash", str(CLI)]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uninstallable-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("MISE_", "XDG_", "UNINSTALLABLE_"))}
        self.env.update({
            "HOME": str(self.home), "MISE_DATA_DIR": str(self.root / "mise data"),
            "MISE_CONFIG_DIR": str(self.root / "config"), "MISE_CACHE_DIR": str(self.root / "cache"),
            "MISE_STATE_DIR": str(self.root / "state"),
            "MISE_GLOBAL_CONFIG_FILE": str(self.root / "config/config.toml"),
            "UNINSTALLABLE_DATA_DIR": str(self.root / "uninstallable data"),
            "MISE_YES": "1", "MISE_NO_HOOKS": "1", "MISE_COLOR": "0",
        })
        self.mise = shutil.which("mise")

    def run_cli(self, script, *args, ok=True, env=None):
        result = subprocess.run([*self.command, *args], input=script, text=True,
                                env=env or self.env, cwd=self.root, capture_output=True, timeout=30,
                                start_new_session=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        return result

    def mise_run(self, *args):
        result = subprocess.run([self.mise, *args], env=self.env, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_install_execute_uninstall_reinstall(self):
        script = installer()
        self.run_cli(script)
        path = Path(self.mise_run("where", "sampleapp"))
        self.assertTrue((path / "bin/sampleapp").is_file())
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "installed-ok")
        self.assertFalse((self.home / ".sampleapp").exists())
        self.run_cli(script, ok=False)
        self.mise_run("uninstall", "sampleapp")
        self.assertFalse(path.exists())
        self.run_cli(script)
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "installed-ok")

    def test_flat_and_arguments(self):
        script = installer(variable="SAMPLEAPP_INSTALL_DIR", flat=True) + 'printf "%s" "$1" > "$destination/argument"\n'
        self.run_cli(script, "--", "a value; $(not-executed)")
        path = Path(self.mise_run("where", "sampleapp"))
        self.assertEqual((path / "bin/argument").read_text(), "a value; $(not-executed)")
        self.mise_run("uninstall", "sampleapp")
        self.assertFalse(path.exists())

    def test_known_registry_name(self):
        self.run_cli(installer("bun", "BUN_INSTALL"))
        self.assertEqual(self.mise_run("exec", "--", "bun"), "installed-ok")
        path = Path(self.mise_run("where", "bun"))
        self.mise_run("uninstall", "bun")
        self.assertFalse(path.exists())

    def test_dry_run_never_executes(self):
        marker = self.root / "side-effect"
        result = self.run_cli(installer() + f'touch "{marker}"\n', "--dry-run")
        self.assertEqual(json.loads(result.stdout)["name"], "sampleapp")
        self.assertFalse(marker.exists())
        self.assertFalse(Path(self.env["UNINSTALLABLE_DATA_DIR"]).exists())

    def test_unsupported_without_terminal_declines(self):
        marker = self.root / "side-effect"
        result = self.run_cli(f'#!/bin/sh\ntouch "{marker}"\n', ok=False)
        self.assertIn("cancelled", result.stderr)
        self.assertFalse(marker.exists())

    def test_removed_unmanaged_option_is_rejected(self):
        marker = self.root / "normal install"
        self.run_cli(f'#!/bin/sh\nprintf "%s" "$1" > "{marker}"\n', "--allow-unmanaged", "--", "ok", ok=False)
        self.assertFalse(marker.exists())
        self.assertFalse(Path(self.env["MISE_DATA_DIR"]).exists())

    def test_actual_terminal_confirmation_yes_and_no(self):
        for answer in (b"y\n", b"n\n"):
            with self.subTest(answer=answer):
                marker = self.root / ("accepted" if answer == b"y\n" else "declined")
                reader, writer = os.pipe()
                pid, master = pty.fork()
                if pid == 0:
                    os.close(writer)
                    os.dup2(reader, 0)
                    os.close(reader)
                    os.chdir(self.root)
                    os.execve(self.command[0], [*self.command, "--", "confirmed argument"], self.env)
                os.close(reader)
                exited = False
                try:
                    os.write(writer, f'#!/bin/sh\nprintf "%s" "$1" > "{marker}"\n'.encode())
                    os.close(writer)
                    output = b""
                    deadline = time.monotonic() + 10
                    while b"[y/N]" not in output and time.monotonic() < deadline:
                        ready, _, _ = select.select([master], [], [], 0.2)
                        if ready:
                            try:
                                output += os.read(master, 4096)
                            except OSError:
                                break
                    self.assertIn(b"[y/N]", output)
                    os.write(master, answer)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        child, status = os.waitpid(pid, os.WNOHANG)
                        if child:
                            exited = True
                            break
                        ready, _, _ = select.select([master], [], [], 0.05)
                        if ready:
                            try:
                                output += os.read(master, 4096)
                            except OSError:
                                pass
                    self.assertTrue(exited, "Installer did not finish after terminal confirmation")
                    self.assertEqual(marker.exists(), answer == b"y\n")
                    if marker.exists():
                        self.assertEqual(marker.read_text(), "confirmed argument")
                    self.assertEqual(os.waitstatus_to_exitcode(status) == 0, answer == b"y\n")
                finally:
                    if not exited:
                        os.kill(pid, signal.SIGKILL)
                    os.close(master)
                    if not exited:
                        os.waitpid(pid, 0)

    def test_failed_installer_no_activation(self):
        self.run_cli(installer(failure=True), ok=False)
        config = Path(self.env["MISE_GLOBAL_CONFIG_FILE"])
        self.assertFalse(config.exists())
        self.assertFalse((Path(self.env["MISE_DATA_DIR"]) / "plugins/sampleapp").exists())
        self.assertFalse([p for p in (Path(self.env["MISE_DATA_DIR"]) / "installs").rglob("sampleapp") if p.is_file()])

    def test_failed_reinstall_restores_previous_recipe(self):
        self.run_cli(installer())
        plugin = Path(self.env["UNINSTALLABLE_DATA_DIR"]) / "plugins/sampleapp"
        previous = (plugin / "installer.sh").read_text()
        self.mise_run("uninstall", "sampleapp")
        self.run_cli(installer(failure=True), ok=False)
        self.assertEqual((plugin / "installer.sh").read_text(), previous)
        self.mise_run("install", "sampleapp")
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "installed-ok")

    def test_unmanaged_variable_beats_inherited_install_dir(self):
        script = installer("uv", "UV_UNMANAGED_INSTALL", flat=True)
        script = script.replace('destination=${UV_UNMANAGED_INSTALL:-$HOME/.uv}',
                                'destination=${UV_INSTALL_DIR:-${UV_UNMANAGED_INSTALL:-$HOME/.uv}}')
        external = self.root / "inherited path"
        env = dict(self.env, UV_INSTALL_DIR=str(external))
        self.run_cli(script, env=env)
        self.assertFalse(external.exists())
        self.assertEqual(self.mise_run("exec", "--", "uv"), "installed-ok")

    def test_lock_prevents_simultaneous_install(self):
        state = Path(self.env["UNINSTALLABLE_DATA_DIR"])
        (state / ".lock").mkdir(parents=True)
        result = self.run_cli(installer(), ok=False)
        self.assertIn("Another install", result.stderr)
        self.assertTrue((state / ".lock").exists())
        self.assertFalse((state / "plugins").exists())

    def test_no_payload_is_rejected(self):
        result = self.run_cli('#!/bin/sh\nx=${SAMPLEAPP_INSTALL:-}\necho nothing installed\n', ok=False)
        self.assertIn("no executables", result.stderr)
        self.assertFalse(Path(self.env["MISE_GLOBAL_CONFIG_FILE"]).exists())

    def test_plugin_collision_preserved(self):
        plugin = Path(self.env["MISE_DATA_DIR"]) / "plugins/sampleapp"
        plugin.mkdir(parents=True)
        marker = plugin / "keep"
        marker.write_text("existing")
        self.run_cli(installer(), ok=False)
        self.assertEqual(marker.read_text(), "existing")

    def test_invalid_shell_and_empty_input(self):
        for script in ("", "#!/bin/sh\nif then", "\0binary"):
            with self.subTest(script=script):
                self.run_cli(script, ok=False)
        self.assertFalse(Path(self.env["UNINSTALLABLE_DATA_DIR"]).exists())

    def test_external_executable_symlink_is_rejected(self):
        script = '#!/bin/sh\nx=${SAMPLEAPP_INSTALL:-}\nmkdir -p "$x/bin"\nln -s /bin/sh "$x/bin/sampleapp"\n'
        result = self.run_cli(script, ok=False)
        self.assertIn("outside", result.stderr)
        self.assertTrue(Path("/bin/sh").exists())

    def test_detection(self):
        cases = [
            (installer(), "sampleapp", "SAMPLEAPP_INSTALL", "root", {}),
            (installer(variable="SAMPLEAPP_INSTALL_DIR", flat=True), "sampleapp", "SAMPLEAPP_INSTALL_DIR", "bin", {}),
            ('install_env=BUN_INSTALL\ninstall_dir=${!install_env:-$HOME/.bun}\n', "bun", "BUN_INSTALL", "root", {"SHELL": "/bin/sh"}),
            ('x=${UV_INSTALL_DIR:-}\ny=${UV_UNMANAGED_INSTALL:-}\nz=${CARGO_DIST_FORCE_INSTALL_DIR:-}\n',
             "uv", "UV_UNMANAGED_INSTALL", "bin", {"UV_INSTALL_DIR": "", "CARGO_DIST_FORCE_INSTALL_DIR": ""}),
            ('APP_NAME="hello"\nx=${INSTALL_DIR:-$HOME/.local/bin}\n', "hello", "INSTALL_DIR", "bin", {}),
            ('x=${DENO_INSTALL:-$HOME/.deno}\n', "deno", "DENO_INSTALL", "root", {"CI": "1"}),
            ('x=${FOO_HOME:-}\ny=${INSTALLER_NO_MODIFY_PATH:-}\n', "foo", "FOO_HOME", "root", {"INSTALLER_NO_MODIFY_PATH": "1"}),
        ]
        for script, name, variable, layout, extras in cases:
            with self.subTest(variable=variable):
                result = self.run_cli(script, "--shell", "bash", "--dry-run")
                self.assertEqual(json.loads(result.stdout), {
                    "name": name, "install_env": variable, "layout": layout,
                    "extra_env": extras, "shell": "bash", "args": [],
                })

    def test_unsupported_detection(self):
        for script in ['x=${FOO_HOME:-}\ny=${BAR_INSTALL_DIR:-}',
                       '# x=${FOO_INSTALL:-}\necho hello',
                       'x=${CARGO_HOME:-}\ny=${RUSTUP_HOME:-}',
                       'x=${INSTALL_DIR:-}']:
            with self.subTest(script=script):
                self.run_cli(script, "--dry-run", ok=False)

    def test_explicit_override(self):
        script = installer(variable="ODD_DEST", flat=True)
        self.run_cli(script, "--name", "sampleapp", "--install-env", "ODD_DEST", "--layout", "bin")
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "installed-ok")

    def test_shell_literals_and_empty_arguments(self):
        script = installer() + 'printf "%s\\n" "$@" > "$destination/arguments"\n'
        args = ["", "a 'quote'", '"double"', "line\nbreak", "$(touch must-not-exist)", "back\\slash", "*"]
        self.run_cli(script, "--", *args)
        destination = Path(self.mise_run("where", "sampleapp"))
        self.assertEqual((destination / "arguments").read_text(), "\n".join(args) + "\n")
        self.assertFalse((self.root / "must-not-exist").exists())

    def test_deno_profile_opt_out(self):
        script = installer("deno", "DENO_INSTALL") + 'test "$CI" = 1\n# --no-modify-path\nprintf "%s" "$1" > "$destination/flag"\n'
        self.run_cli(script)
        destination = Path(self.mise_run("where", "deno"))
        self.assertEqual((destination / "flag").read_text(), "--no-modify-path")

    def test_no_python_or_hash_tools_on_path(self):
        commands = self.root / "commands without python"
        commands.mkdir()
        required = ["bash", "jq", "mise", "cat", "head", "wc", "tr", "grep", "iconv", "dirname", "basename",
                    "readlink", "mktemp", "mkdir", "rmdir", "rm", "cp", "mv", "chmod", "sed",
                    "env", "sh", "uname", "touch", "git", "date", "sort", "find", "cut", "awk"]
        for name in required:
            target = shutil.which(name)
            if target:
                (commands / name).symlink_to(target)
        env = dict(self.env, PATH=str(commands))
        self.assertIsNone(shutil.which("python3", path=env["PATH"]))
        self.assertIsNone(shutil.which("sha256sum", path=env["PATH"]))
        self.assertIsNone(shutil.which("shasum", path=env["PATH"]))
        self.run_cli(installer(), env=env)
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "installed-ok")

    def test_fixed_version_installs_updated_script_after_uninstall(self):
        self.run_cli(installer())
        destination = Path(self.mise_run("where", "sampleapp"))
        self.assertEqual(destination.name, "0.0.0-uninstallable")
        self.mise_run("uninstall", "sampleapp")
        self.run_cli(installer().replace("installed-ok", "updated-ok"))
        self.assertEqual(Path(self.mise_run("where", "sampleapp")), destination)
        self.assertEqual(self.mise_run("exec", "--", "sampleapp"), "updated-ok")
        recipe = Path(self.env["UNINSTALLABLE_DATA_DIR"]) / "plugins/sampleapp/recipe.json"
        self.assertEqual(json.loads(recipe.read_text())["version"], "0.0.0-uninstallable")

    def test_invalid_options(self):
        for args in [("--name", "../oops"), ("--install-env", "X;bad"),
                     ("--shell", "python3"), ("--layout", "invalid"), ("--name",), ("--unknown",)]:
            with self.subTest(args=args):
                self.run_cli(installer(), *args, ok=False)


if __name__ == "__main__":
    unittest.main()
