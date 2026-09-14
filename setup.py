from setuptools import find_packages, setup

setup(
    name="pocketkube",
    version="0.2.4",
    description="Tiny Kubernetes-compatible API server for rootless Android/Termux container experiments",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "starlette>=0.38,<1",
        "uvicorn>=0.30,<1",
        "websockets==13.0",
    ],
    entry_points={"console_scripts": ["pocketkube=pocketkube.cli:main"]},
)
