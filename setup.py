from setuptools import setup, find_packages

setup(
    name="ib-trading-bridge",
    version="1.0.0",
    description="Bridge to connect Interactive Brokers TWS to IB Trading Dashboard",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "ibapi>=9.81.1",
        "python-socketio[client]>=5.13.0",
        "pandas",
        "numpy",
        "yfinance",
        "scipy",
    ],
    entry_points={
        "console_scripts": [
            "ib-bridge=cloud.bridge:main",
        ],
    },
)
