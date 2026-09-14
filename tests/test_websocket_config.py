from pathlib import Path


def test_uvicorn_uses_websockets_backend():
    cli = Path("pocketkube/cli.py").read_text()
    assert 'ws="websockets"' in cli


def test_websockets_dependency_is_pinned():
    setup_py = Path("setup.py").read_text()
    assert 'websockets==13.0' in setup_py
    assert 'wsproto==1.2.0' not in setup_py
