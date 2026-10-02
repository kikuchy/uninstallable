# Contributing

Keep `uninstallable.sh` a single executable using Bash 3.2+, jq 1.6+, and standard
Unix utilities. Generated mise hooks should remain small POSIX shell scripts.

Run these checks with mise and jq on PATH:

```sh
bash -n uninstallable.sh
python3 -m unittest discover -s tests -v
```

Python 3.9+ is used only to run tests, including pseudo-terminal checks. It is not
a runtime dependency of uninstallable. The test suite invokes the Bash executable
directly, isolates HOME and all mise directories, and generates small local
installers. Do not make the default tests download or run third-party installers.

When adding a detection rule, include a reduced test case, expected detection
results, and ambiguous or unsupported cases. Never source or evaluate installer
code during detection. Keep the terminal confirmation for unmanaged installs.

For vendor-specific behavior, cite the upstream installer in the pull request.
Explain how profile edits and files outside the managed installation are handled.
Do not claim arbitrary scripts are sandboxed.

Use `--dry-run` when diagnosing detection. Remove secrets before sharing installer
scripts, arguments, or generated recipe files.
