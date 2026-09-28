from pathlib import Path

import pytest

from pocketkube.runtime.raw_proot import RawProotRuntime


def make_rootfs(tmp_path: Path) -> Path:
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "sh").write_text("placeholder")
    return rootfs


def test_raw_proot_accepts_alpine_and_builds_command(tmp_path, monkeypatch):
    rootfs = make_rootfs(tmp_path)
    runtime = RawProotRuntime(rootfs=rootfs)

    monkeypatch.setattr(runtime, "_supports_link2symlink", lambda: True)
    assert runtime._rootfs_for_image("alpine:3.24") == rootfs

    cmd = runtime._proot_command(
        rootfs,
        ["/bin/sh", "-c", "echo hello"],
        ["FOO=bar"],
    )

    assert cmd[0] == "proot"
    assert "--link2symlink" in cmd
    assert str(rootfs) in cmd
    assert any('unset LD_LIBRARY_PATH LD_PRELOAD' in part and 'exec \"$@\"' in part for part in cmd)
    assert cmd[-5:] == ["/usr/bin/env", "FOO=bar", "/bin/sh", "-c", "echo hello"]


def test_raw_proot_rejects_non_alpine(tmp_path):
    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))
    with pytest.raises(RuntimeError, match="not supported"):
        runtime._rootfs_for_image("nginx:alpine")


def test_old_termux_library_path_is_preserved_for_host(monkeypatch, tmp_path):
    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setenv("POCKETKUBE_PROOT_LD_LIBRARY_PATH", "/data/data/com.termux/files/usr/lib")
    monkeypatch.setenv("LD_PRELOAD", "bad-preload.so")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)

    env = runtime._host_environment()

    assert env["LD_LIBRARY_PATH"] == "/data/data/com.termux/files/usr/lib"
    assert "LD_PRELOAD" not in env


def test_raw_proot_accepts_absolute_shell_symlink(tmp_path):
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "busybox").write_text("placeholder")
    (rootfs / "bin" / "sh").symlink_to("/bin/busybox")

    runtime = RawProotRuntime(rootfs=rootfs)
    assert runtime._rootfs_for_image("alpine:3.24") == rootfs

def test_proot_command_sets_guest_path(tmp_path):
    from pocketkube.runtime.raw_proot import RawProotRuntime
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "sh").write_text("placeholder")
    rt = RawProotRuntime(rootfs=tmp_path)
    cmd = rt._proot_command(tmp_path, ["cat", "/etc/alpine-release"])
    wrapper = cmd[cmd.index("-c") + 1]
    assert "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in wrapper
    assert cmd[-2:] == ["cat", "/etc/alpine-release"]


def test_proot_command_runs_shellless_images_directly(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    runtime = RawProotRuntime(rootfs=rootfs)

    cmd = runtime._proot_command(rootfs, ["/app/chisel", "client"])

    assert cmd[-2:] == ["/app/chisel", "client"]
    assert "/bin/sh" not in cmd


def test_proot_command_does_not_require_env_for_shellless_images(tmp_path):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    runtime = RawProotRuntime(rootfs=rootfs)

    cmd = runtime._proot_command(rootfs, ["/app/chisel"], ["CHISEL_VERSION=1"])

    assert cmd[-1] == "/app/chisel"
    assert "CHISEL_VERSION=1" not in cmd
    assert "/usr/bin/env" not in cmd


def test_startup_failure_captures_stdout_and_stderr(tmp_path, monkeypatch):
    import asyncio
    import sys

    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))
    runtime.images.directory = tmp_path / 'images'
    monkeypatch.setattr(runtime, '_pod_command', lambda *args: [
        sys.executable, '-c',
        "import sys; print('entrypoint diagnostic', flush=True); "
        "print('bind permission denied', file=sys.stderr); sys.exit(1)",
    ])
    pod = {'spec': {'containers': [{'image': 'alpine', 'command': ['unused']}]}}

    async def run():
        with pytest.raises(RuntimeError) as error:
            await runtime.start_pod('default', 'broken', pod)
        assert 'status 1' in str(error.value)
        assert 'entrypoint diagnostic' in str(error.value)
        assert 'bind permission denied' in str(error.value)
        assert not runtime._output_tasks
        assert not runtime._pod_images
        assert not list((tmp_path / 'pods').iterdir())
    asyncio.run(run())


def test_output_capture_drains_and_bounds_memory():
    import asyncio

    async def run():
        stream = asyncio.StreamReader()
        stream.feed_data(b'x' * 200000 + b'final diagnostic')
        stream.feed_eof()
        output = bytearray()
        await RawProotRuntime._capture_output(stream, output)
        assert len(output) == 65536
        assert output.endswith(b'final diagnostic')
    asyncio.run(run())


def test_guest_devices_and_stdio(tmp_path):
    rootfs = make_rootfs(tmp_path)
    runtime = RawProotRuntime(rootfs=rootfs)
    runtime._prepare_devices(rootfs)
    assert (rootfs / 'dev/stdout').readlink() == Path('/proc/self/fd/1')
    assert (rootfs / 'dev/stderr').readlink() == Path('/proc/self/fd/2')
    cmd = runtime._proot_command(rootfs, ['nginx'])
    assert '/proc:/proc!' in cmd
    assert '/dev/null:/dev/null!' in cmd
    assert '/dev/urandom:/dev/urandom!' in cmd


@pytest.mark.parametrize("inherited", [None, "/data/data/com.termux/files/usr/lib"])
def test_modern_termux_does_not_shadow_android_libraries(monkeypatch, tmp_path, inherited):
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.delenv("POCKETKUBE_PROOT_LD_LIBRARY_PATH", raising=False)
    if inherited is None:
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH", inherited)
    monkeypatch.setenv("LD_PRELOAD", "termux-exec.so")
    env = RawProotRuntime(rootfs=tmp_path)._host_environment()
    assert "LD_LIBRARY_PATH" not in env
    assert "LD_PRELOAD" not in env


def test_empty_library_override_and_non_termux_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.delenv("POCKETKUBE_PROOT_LD_LIBRARY_PATH", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/custom/lib")
    runtime = RawProotRuntime(rootfs=tmp_path)
    assert runtime._host_environment()["LD_LIBRARY_PATH"] == "/custom/lib"
    monkeypatch.setenv("POCKETKUBE_PROOT_LD_LIBRARY_PATH", "")
    assert "LD_LIBRARY_PATH" not in runtime._host_environment()


@pytest.mark.parametrize('help_output,exit_code,supported', [
    (b'Usage: proot -r rootfs', 0, False),
    (b'Usage: proot --link2symlink', 0, True),
    (b'unknown option --link2symlink', 1, False),
])
def test_optional_proot_extension_is_detected_and_cached(tmp_path, monkeypatch, help_output, exit_code, supported):
    import subprocess
    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        assert kwargs['env'] == runtime._host_environment()
        assert kwargs['timeout'] == 2
        return subprocess.CompletedProcess(command, exit_code, help_output)

    monkeypatch.setattr('pocketkube.runtime.raw_proot.subprocess.run', probe)
    monkeypatch.setattr('pocketkube.runtime.raw_proot.terminal_host_command', lambda cmd, env: cmd)
    for command in (['/bin/sh'], ['echo', 'exec']):
        argv = runtime._proot_command(runtime.rootfs, command)
        assert ('--link2symlink' in argv) == supported
        assert '-0' in argv
    assert calls == [['proot', '--help']]


def test_proot_probe_failure_omits_optional_flag(tmp_path, monkeypatch):
    import subprocess
    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('proot', 2)

    monkeypatch.setattr('pocketkube.runtime.raw_proot.subprocess.run', timeout)
    monkeypatch.setattr('pocketkube.runtime.raw_proot.terminal_host_command', lambda cmd, env: cmd)
    assert '--link2symlink' not in runtime._proot_command(runtime.rootfs, ['/bin/sh'])


@pytest.mark.parametrize('prefix_key', ['PREFIX', 'TERMUX__PREFIX'])
@pytest.mark.parametrize('retry_ok', [True, False])
def test_missing_host_library_fallback(tmp_path, monkeypatch, prefix_key, retry_ok):
    import subprocess
    for key in ('PREFIX', 'TERMUX__PREFIX', 'POCKETKUBE_PROOT_LD_LIBRARY_PATH'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(prefix_key, str(tmp_path))
    runtime = RawProotRuntime(rootfs=make_rootfs(tmp_path))
    environments = []

    def probe(command, **kwargs):
        environments.append(kwargs['env'])
        if len(environments) == 1:
            return subprocess.CompletedProcess(command, 1, b'library "libtalloc.so.2" not found')
        return subprocess.CompletedProcess(command, 0 if retry_ok else 1, b'--link2symlink')

    monkeypatch.setattr('pocketkube.runtime.raw_proot.subprocess.run', probe)
    monkeypatch.setattr('pocketkube.runtime.raw_proot.terminal_host_command', lambda cmd, env: cmd)
    runtime._proot_command(runtime.rootfs, ['/bin/sh'])
    runtime._proot_command(runtime.rootfs, ['/bin/sh'])
    assert len(environments) == 2
    assert 'LD_LIBRARY_PATH' not in environments[0]
    assert environments[1]['LD_LIBRARY_PATH'] == str(tmp_path / 'lib')
    assert runtime._host_environment().get('LD_LIBRARY_PATH') == (str(tmp_path / 'lib') if retry_ok else None)
    assert runtime._supports_link2symlink() == retry_ok


@pytest.mark.parametrize('override,diagnostic', [
    ('', b'library "libtalloc.so.2" not found'),
    ('/custom/lib', b'library "libtalloc.so.2" not found'),
    (None, b'cannot locate symbol "Xzs_Construct"'),
])
def test_library_fallback_respects_override_and_other_errors(tmp_path, monkeypatch, override, diagnostic):
    import subprocess
    monkeypatch.setenv('PREFIX', str(tmp_path))
    monkeypatch.delenv('POCKETKUBE_PROOT_LD_LIBRARY_PATH', raising=False)
    if override is not None:
        monkeypatch.setenv('POCKETKUBE_PROOT_LD_LIBRARY_PATH', override)
    runtime = RawProotRuntime(rootfs=tmp_path)
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, diagnostic)

    monkeypatch.setattr('pocketkube.runtime.raw_proot.subprocess.run', probe)
    monkeypatch.setattr('pocketkube.runtime.raw_proot.terminal_host_command', lambda cmd, env: cmd)
    assert not runtime._supports_link2symlink()
    assert len(calls) == 1
    assert runtime._legacy_library_path is None
