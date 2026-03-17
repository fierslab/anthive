"""Setup script for Anthive Client."""

from setuptools import setup
from pathlib import Path

# Read README for long description (from doc folder)
# For PyPI packaging, copy ../doc/ANTCLIENT_README.md to ./README.md
readme_file = Path(__file__).parent / "README.md"
if not readme_file.exists():
    readme_file = Path(__file__).parent.parent / "doc" / "ANTCLIENT_README.md"
long_description = readme_file.read_text() if readme_file.exists() else ""

# Read version from antclient.py
version = "1.0.0"
init_file = Path(__file__).parent / "antclient.py"
if init_file.exists():
    for line in init_file.read_text().split('\n'):
        if line.startswith('__version__'):
            version = line.split('"')[1]
            break

setup(
    name="antclient",
    version=version,
    author="Anthive Team",
    author_email="anthive@example.com",
    description="Lightweight Python client for Anthive REST API - query single-cell expression databases",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/anthive/anthive4",
    py_modules=["antclient", "anthelper"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: OS Independent",
    ],
    keywords="bioinformatics single-cell rna-seq api-client",
    python_requires=">=3.9",
    install_requires=[
        "requests>=2.25.0",
    ],
    extras_require={
        "pandas": [
            "pandas>=1.3.0",
        ],
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=22.0.0",
            "flake8>=4.0.0",
            "build>=0.7.0",
            "twine>=4.0.0",
        ],
    },
    project_urls={
        "Bug Reports": "https://github.com/anthive/anthive4/issues",
        "Source": "https://github.com/anthive/anthive4",
        "Documentation": "https://github.com/anthive/anthive4/blob/main/README.md",
    },
)
