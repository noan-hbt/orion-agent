"""Setuptools command hooks for reproducible builds from the checkout sources.

The repository may contain tracked/generated ``build/`` and ``*.egg-info``
artifacts from older releases.  Those artifacts are caches, never packaging
inputs.  These hooks make that invariant explicit without requiring callers to
clean the working tree before invoking the PEP 517 backend.
"""

from __future__ import annotations

import os

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.egg_info import egg_info, manifest_maker


class CanonicalBuildPy(build_py):
    """Always refresh build/lib from canonical modules and package data."""

    def run(self) -> None:
        # Distutils normally skips a copy when build/lib has a newer mtime.
        # That optimization is unsafe in a checkout where build/ can be stale
        # or tracked, so force every source/package-data copy for every wheel.
        self.force = True
        super().run()


class FreshManifestMaker(manifest_maker):
    """Never seed an sdist file list from an existing SOURCES.txt cache."""

    def read_manifest(self) -> None:
        # ``manifest_maker.add_defaults`` falls back to reading an existing
        # SOURCES.txt when no VCS file list is available (notably source
        # snapshots and CI copies).  That lets stale entries become source of
        # truth.  All real inputs are rediscovered by add_defaults/templates,
        # so deliberately ignore the cached manifest.
        return None


class CanonicalEggInfo(egg_info):
    """Generate SOURCES.txt from current sources rather than cached metadata."""

    def find_sources(self) -> None:
        manifest_filename = os.path.join(self.egg_info, "SOURCES.txt")
        maker = FreshManifestMaker(self.distribution)
        maker.ignore_egg_info_dir = self.ignore_egg_info_in_manifest
        maker.manifest = manifest_filename
        maker.run()
        self.filelist = maker.filelist


setup(
    cmdclass={
        "build_py": CanonicalBuildPy,
        "egg_info": CanonicalEggInfo,
    }
)
