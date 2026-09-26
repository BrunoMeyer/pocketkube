"""Exercise the curl | bash interface without downloading or installing packages."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, code):
    path.write_text('#!' + sys.executable + '\n' + code)
    path.chmod(0o700)


@pytest.fixture
def installer(tmp_path):
    commands = tmp_path / 'bin'
    commands.mkdir()
    log = tmp_path / 'calls.jsonl'
    # Emulate only package installation; use the actual CLI to generate kubeconfig.
    stub = '''import json, os, pathlib, subprocess, sys
with open(os.environ['INSTALL_TEST_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['-m', 'venv']:
    target = pathlib.Path(sys.argv[3]) / 'bin' / 'python'
    target.parent.mkdir(parents=True)
    target.write_bytes(pathlib.Path(__file__).read_bytes())
    target.chmod(0o700)
elif sys.argv[1:3] == ['-m', 'pip']:
    sys.exit(int(os.environ.get('INSTALL_TEST_PIP_EXIT', '0')))
elif sys.argv[1:4] == ['-m', 'pocketkube.cli', 'serve']:
    pass
else:
    sys.exit(subprocess.call([REAL_PYTHON, *sys.argv[1:]]))
'''.replace('REAL_PYTHON', repr(sys.executable))
    executable(commands / 'python3', stub)
    executable(commands / 'git', "import pathlib, sys\npathlib.Path(sys.argv[-1]).mkdir()\n")
    executable(commands / 'proot', 'pass\n')
    env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
               INSTALL_TEST_LOG=str(log), PYTHONPATH=str(ROOT))
    # Metacharacters must remain literal when written to config and launcher files.
    directory = tmp_path / 'installation $(touch INJECTED)'
    config = tmp_path / 'kube config'

    def run(start='no', extra_env=None, port='8443', overwrite=None):
        answers = [str(directory), '', '', '', port, 'host', str(config)]
        if overwrite is not None:
            answers.append(overwrite)
        answers.append(start)
        master, slave = pty.openpty()

        def terminal():
            os.setsid()
            fcntl.ioctl(1, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(['bash'], stdin=subprocess.PIPE, stdout=slave,
                                   stderr=slave, preexec_fn=terminal, cwd=tmp_path,
                                   env=dict(env, **(extra_env or {})))
        os.close(slave)
        output = bytearray()
        try:
            # Script arrives through stdin, answers through the controlling terminal.
            process.stdin.write((ROOT / 'install.sh').read_bytes())
            process.stdin.close()
            os.write(master, ('\n'.join(answers) + '\n').encode())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    output.extend(data)
                elif process.poll() is not None:
                    break
            code = process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return code, output.decode(), calls

    return run, directory, config


@pytest.mark.parametrize('start', ['yes', 'no'])
def test_piped_interactive_install(installer, start):
    run, directory, config = installer
    code, output, calls = run(start=start)
    assert code == 0, output
    assert 'server: http://127.0.0.1:8443' in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o600
    assert (directory / 'start.sh').stat().st_mode & 0o777 == 0o700
    assert not (directory.parent / 'INJECTED').exists()
    assert any(args[:2] == ['-m', 'pip'] for args in calls)
    launches = [args for args in calls if args[:3] == ['-m', 'pocketkube.cli', 'serve']]
    assert len(launches) == (start == 'yes')
    if launches:
        assert launches[0][3:] == ['--host', '127.0.0.1', '--port', '8443', '--runtime', 'proot']
    # A repeated installation must not overwrite the existing installation.
    code, output, _ = run()
    assert code != 0
    assert 'Installation directory is not empty' in output


def test_failed_package_install_does_not_launch_or_write_config(installer):
    run, directory, config = installer
    code, _, calls = run(start='yes', extra_env={'INSTALL_TEST_PIP_EXIT': '1'})
    assert code != 0
    assert not config.exists()
    assert not (directory / 'start.sh').exists()
    assert not any(args[:3] == ['-m', 'pocketkube.cli', 'serve'] for args in calls)


def test_preserve_existing_kubeconfig(installer):
    run, directory, config = installer
    config.write_text('existing config')
    code, output, _ = run(overwrite='no')
    assert code != 0
    assert 'Existing kubeconfig preserved' in output
    assert config.read_text() == 'existing config'
    assert not directory.exists()


def test_invalid_port_stops_before_install(installer):
    run, directory, _ = installer
    code, output, calls = run(port='65536')
    assert code != 0
    assert 'Port must be from 1 to 65535' in output
    assert not calls
    assert not directory.exists()
