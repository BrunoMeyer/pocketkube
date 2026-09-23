from pathlib import Path

import pytest

from pocketkube.runtime.raw_proot import RawProotRuntime


def make_rootfs(tmp_path: Path) -> Path:
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin" / "sh").write_text("placeholder")
    return rootfs


def test_raw_proot_accepts_alpine_and_builds_command(tmp_path):
    rootfs = make_rootfs(tmp_path)
    runtime = RawProotRuntime(rootfs=rootfs)

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
