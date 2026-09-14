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
    rt = RawProotRuntime(rootfs=tmp_path)
    cmd = rt._proot_command(tmp_path, ["cat", "/etc/alpine-release"])
    wrapper = cmd[cmd.index("-c") + 1]
    assert "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in wrapper
    assert cmd[-2:] == ["cat", "/etc/alpine-release"]
