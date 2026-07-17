from setuptools import setup, find_packages

setup(
    name="ib-trading-bridge",
    version="1.0.0",
    description="Bridge to connect Interactive Brokers TWS to IB Trading Dashboard",
    packages=["bridge"],
    python_requires=">=3.10",
    install_requires=[
        "ibapi>=9.81.1",
        "python-socketio[client]>=5.12.0",
        "pandas>=2.0",
        "numpy>=1.24",
        "requests>=2.28",
        "certifi",
    ],
    entry_points={
        "console_scripts": [
            "ib-bridge=bridge.main:main",
        ],
    },
)
