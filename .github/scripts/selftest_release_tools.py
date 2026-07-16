#!/usr/bin/env python3
"""Focused regression tests for the repository's release trust-root scripts."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import ModuleType
from unittest import mock


def load_release_checks() -> ModuleType:
    path = Path(__file__).with_name("release_checks.py")
    spec = importlib.util.spec_from_file_location("repository_release_checks", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load release_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECKS = load_release_checks()


def load_clean_install() -> ModuleType:
    path = Path(__file__).with_name("clean_install.py")
    spec = importlib.util.spec_from_file_location("repository_clean_install", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load clean_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLEAN_INSTALL = load_clean_install()


def load_real_tex_gate() -> ModuleType:
    path = Path(__file__).with_name("run_real_tex_gate.py")
    spec = importlib.util.spec_from_file_location("repository_real_tex_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load run_real_tex_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REAL_TEX = load_real_tex_gate()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


class ArchiveAndPrivacyTests(unittest.TestCase):
    def test_archive_member_traversal_is_rejected(self) -> None:
        with self.assertRaises(CHECKS.ReleaseCheckError):
            CHECKS.safe_member_name("docs/../../private.txt")

    def test_metadata_without_suffix_is_scanned(self) -> None:
        with self.assertRaises(CHECKS.ReleaseCheckError):
            CHECKS.scan_member("package.dist-info/METADATA", b"Path: C:/Users/person/paper.tex")

    def test_secret_is_rejected(self) -> None:
        with self.assertRaises(CHECKS.ReleaseCheckError):
            CHECKS.scan_member("README.md", b"-----BEGIN PRIVATE KEY-----")

    def test_modern_provider_secret_prefixes_are_rejected(self) -> None:
        tokens = (
            b"sk-" + b"proj-" + b"a" * 24,
            b"sk-" + b"svcacct-" + b"b" * 24,
            b"AKIA" + b"C" * 16,
            b"ASIA" + b"D" * 16,
            b"AIza" + b"E" * 35,
        )
        for token in tokens:
            with self.subTest(token=token[:8]), self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.scan_member("README.md", token)

    def test_oversize_text_fails_closed(self) -> None:
        with self.assertRaises(CHECKS.ReleaseCheckError):
            CHECKS.scan_member("README.md", b"x" * (CHECKS.MAX_SCAN_BYTES + 1))

    def test_exact_attack_vector_token_can_be_removed(self) -> None:
        token = b"/home/alice/paper.tex"
        CHECKS.scan_member("tests/test_bundle_coverage.py", token, allowed_tokens=(token,))

    def test_noncanonical_and_windows_unsafe_members_are_rejected(self) -> None:
        for name in (
            "package//module.py",
            "package/./module.py",
            "C:/outside.py",
            "package/name:stream.py",
            "package/NUL.txt",
            "package/trailing. ",
        ):
            with self.subTest(name=name), self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.safe_member_name(name)

    def test_portable_case_collision_is_rejected(self) -> None:
        entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
        CHECKS.register_archive_member(
            CHECKS.safe_member_name("package/README.md"),
            is_directory=False,
            entries=entries,
        )
        with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "colliding"):
            CHECKS.register_archive_member(
                CHECKS.safe_member_name("package/readme.md"),
                is_directory=False,
                entries=entries,
            )
        parent_entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
        CHECKS.register_archive_member(
            CHECKS.safe_member_name("package/Docs/one.py"),
            is_directory=False,
            entries=parent_entries,
        )
        with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "directory spelling"):
            CHECKS.register_archive_member(
                CHECKS.safe_member_name("package/docs/two.py"),
                is_directory=False,
                entries=parent_entries,
            )


class DistributionMetadataTests(unittest.TestCase):
    def write_wheel(self, root: Path, *, requires_python: str, extra_metadata: str = "") -> Path:
        path = root / "latex_word_review-0.1.0b1-py3-none-any.whl"
        dist_info = "latex_word_review-0.1.0b1.dist-info"
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: latex-word-review\n"
            "Version: 0.1.0b1\n"
            f"Requires-Python: {requires_python}\n"
            "License-Expression: Apache-2.0\n"
            + "".join(
                f"Provides-Extra: {name}\n" for name in sorted(CHECKS.EXPECTED_PROVIDES_EXTRA)
            )
            + "".join(
                f"Requires-Dist: {requirement}\n"
                for requirement in sorted(CHECKS.EXPECTED_REQUIRES_DIST)
            )
            + f"{extra_metadata}\n"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("latex_word_review/__about__.py", '__version__ = "0.1.0b1"\n')
            archive.writestr("latex_word_review/_image_worker.py", "# image worker\n")
            archive.writestr("latex_word_review/image_materializer.py", "# materializer\n")
            archive.writestr("latex_word_review/image_overlay.py", "# overlay\n")
            archive.writestr("latex_word_review/offset_mapping.py", "# offsets\n")
            archive.writestr("latex_word_review/py.typed", "")
            archive.writestr("latex_word_review/word_semantics.py", "# Word semantics\n")
            archive.writestr("latex_word_review/workflow.py", "# workflow\n")
            archive.writestr("latex_word_review/schemas/v1alpha/test.json", "{}\n")
            archive.writestr(f"{dist_info}/METADATA", metadata)
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                "[console_scripts]\nlatex-word-review = latex_word_review.cli:main\n",
            )
            archive.writestr(f"{dist_info}/licenses/LICENSE", "synthetic license\n")
            archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
            archive.writestr(f"{dist_info}/RECORD", "")
        return path

    def test_supported_python_interval_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = self.write_wheel(
                Path(temporary), requires_python=CHECKS.EXPECTED_REQUIRES_PYTHON
            )
            self.assertEqual(CHECKS.check_wheel(wheel), ("latex-word-review", "0.1.0b1"))

    def test_unbounded_python_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = self.write_wheel(Path(temporary), requires_python=">=3.12")
            with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "Requires-Python"):
                CHECKS.check_wheel(wheel)

    def test_wheel_normalization_collision_cannot_hide_private_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wheel = self.write_wheel(
                Path(temporary), requires_python=CHECKS.EXPECTED_REQUIRES_PYTHON
            )
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("latex_word_review/probe.py", "# benign\n")
                archive.writestr("latex_word_review//probe.py", "C:/Users/person/paper.tex\n")
            with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "non-canonical"):
                CHECKS.check_wheel(wheel)

    def test_wheel_rejects_symlink_nontext_and_duplicate_identity_metadata(self) -> None:
        mutations = ("symlink", "nontext", "duplicate-metadata", "duplicate-extra")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                wheel = self.write_wheel(
                    root,
                    requires_python=CHECKS.EXPECTED_REQUIRES_PYTHON,
                    extra_metadata=(
                        "Name: latex-word-review\n"
                        if mutation == "duplicate-metadata"
                        else "Provides-Extra: citations\n"
                        if mutation == "duplicate-extra"
                        else ""
                    ),
                )
                if mutation not in {"duplicate-metadata", "duplicate-extra"}:
                    with zipfile.ZipFile(wheel, "a") as archive:
                        if mutation == "symlink":
                            info = zipfile.ZipInfo("latex_word_review/link.py")
                            info.create_system = 3
                            info.external_attr = 0o120777 << 16
                            archive.writestr(info, "target.py")
                        else:
                            archive.writestr("latex_word_review/payload.bin", b"binary")
                with self.assertRaises(CHECKS.ReleaseCheckError):
                    CHECKS.check_wheel(wheel)


class PluginDistributionTests(unittest.TestCase):
    def valid_payloads(self) -> dict[str, bytes]:
        skill = b"---\nname: latex-word-review\ndescription: synthetic\n---\n"
        openai_yaml = b'interface:\n  display_name: "LaTeX Word Review"\n'
        manifest = {
            "name": "latex-word-review",
            "version": "0.1.0",
            "description": "Synthetic plugin manifest",
            "author": {"name": "LaTeX Word Review contributors"},
            "skills": "./skills/",
            "interface": {"displayName": "LaTeX Word Review"},
        }
        marketplace = {
            "name": "personal",
            "plugins": [
                {
                    "name": "latex-word-review",
                    "source": {"source": "local", "path": "./plugins/latex-word-review"},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Productivity",
                }
            ],
        }
        return {
            CHECKS.CANONICAL_SKILL_RELATIVE: skill,
            CHECKS.CANONICAL_OPENAI_YAML_RELATIVE: openai_yaml,
            CHECKS.EMBEDDED_SKILL_RELATIVE: skill,
            CHECKS.EMBEDDED_OPENAI_YAML_RELATIVE: openai_yaml,
            CHECKS.PLUGIN_MANIFEST_RELATIVE: json.dumps(manifest).encode("utf-8"),
            CHECKS.MARKETPLACE_RELATIVE: json.dumps(marketplace).encode("utf-8"),
        }

    def test_packaged_plugin_payloads_are_bound_and_synchronized(self) -> None:
        CHECKS.validate_plugin_distribution_payloads(self.valid_payloads())

    def test_packaged_plugin_skill_drift_is_rejected(self) -> None:
        payloads = self.valid_payloads()
        payloads[CHECKS.EMBEDDED_SKILL_RELATIVE] += b"stale\n"
        with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "differs from canonical"):
            CHECKS.validate_plugin_distribution_payloads(payloads)

    def test_packaged_marketplace_must_bind_the_plugin(self) -> None:
        payloads = self.valid_payloads()
        marketplace = json.loads(payloads[CHECKS.MARKETPLACE_RELATIVE])
        marketplace["plugins"][0]["source"]["path"] = "./plugins/another-plugin"
        payloads[CHECKS.MARKETPLACE_RELATIVE] = json.dumps(marketplace).encode("utf-8")
        with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "marketplace entry"):
            CHECKS.validate_plugin_distribution_payloads(payloads)

    def test_packaged_plugin_payload_set_must_be_complete(self) -> None:
        payloads = self.valid_payloads()
        del payloads[CHECKS.EMBEDDED_OPENAI_YAML_RELATIVE]
        with self.assertRaisesRegex(CHECKS.ReleaseCheckError, "payload set"):
            CHECKS.validate_plugin_distribution_payloads(payloads)


class FixtureGateTests(unittest.TestCase):
    def make_reports(self, root: Path) -> Path:
        reports = {
            "structure": {"status": "pass"},
            "changeset_oracle": {"status": "pass"},
            "privacy": {"status": "pass"},
            "provenance": {
                "status": "fail",
                "integrity_status": "pass",
                "release_readiness": "blocked",
                "release_checks": [
                    {"name": "privacy_review_complete", "pass": True},
                    {"name": "visual_review_complete", "pass": False},
                ],
            },
        }
        paths: dict[str, str] = {}
        for name, report in reports.items():
            relative = f"{name}.json"
            write_json(root / relative, report)
            paths[name] = relative
        summary = {
            "status": "fail",
            "fixture_integrity_status": "pass",
            "release_readiness": "blocked",
            "reports": paths,
        }
        summary_path = root / "summary.json"
        write_json(summary_path, summary)
        return summary_path

    def test_ci_allows_only_deferred_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = self.make_reports(Path(temporary))
            CHECKS.command_fixture(argparse.Namespace(summary=summary, mode="ci"))

    def test_release_rejects_deferred_visual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = self.make_reports(Path(temporary))
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_fixture(argparse.Namespace(summary=summary, mode="release"))

    def test_ci_rejects_non_visual_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = self.make_reports(root)
            provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
            provenance["release_checks"].append({"name": "privacy_review_complete", "pass": False})
            write_json(root / "provenance.json", provenance)
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_fixture(argparse.Namespace(summary=summary, mode="ci"))


class RepositoryScanTests(unittest.TestCase):
    def initialized_repository(self, root: Path) -> None:
        git(root, "init", "-q")

    def test_tracked_personal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized_repository(root)
            (root / "README.md").write_text(
                "local source C:/Users/person/private/paper.tex", encoding="utf-8"
            )
            git(root, "add", "README.md")
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_repo(argparse.Namespace(root=root))

    def test_tracked_lockfile_personal_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized_repository(root)
            (root / "uv.lock").write_text(
                "source = 'C:/Users/person/private/paper.tex'",
                encoding="utf-8",
            )
            git(root, "add", "uv.lock")
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_repo(argparse.Namespace(root=root))

    def test_exact_synthetic_vector_and_public_fixture_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized_repository(root)
            test_path = root / "tests" / "test_bundle_coverage.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_bytes(b"value = b'/home/alice/paper.tex'\n")
            fixture = root / "tests" / "fixtures" / "e0-minimal-paper" / "base" / "review-base.docx"
            fixture.parent.mkdir(parents=True)
            fixture.write_bytes(b"synthetic fixture placeholder")
            git(root, "add", "tests")
            CHECKS.command_repo(argparse.Namespace(root=root))

    def test_untracked_latex_private_path_is_rejected_in_candidate_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized_repository(root)
            (root / "main.tex").write_text(
                r"\input{C:/Users/person/private/paper.tex}", encoding="utf-8"
            )
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_repo(argparse.Namespace(root=root, include_untracked=True))

    def test_untracked_tex_runtime_cache_is_rejected_in_candidate_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized_repository(root)
            cache = root / "par-runtime-cache" / "cache.txt"
            cache.parent.mkdir()
            cache.write_text("generated", encoding="utf-8")
            with self.assertRaises(CHECKS.ReleaseCheckError):
                CHECKS.command_repo(argparse.Namespace(root=root, include_untracked=True))

    def test_repository_portable_case_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.txt").write_text("public", encoding="utf-8")
            with (
                mock.patch.object(CHECKS, "tracked_files", return_value=["A.txt", "a.txt"]),
                self.assertRaisesRegex(CHECKS.ReleaseCheckError, "colliding"),
            ):
                CHECKS.command_repo(argparse.Namespace(root=root, include_untracked=False))


class RealTexJUnitTests(unittest.TestCase):
    def write_junit(self, root: Path, *, skipped: int) -> Path:
        skipped_node = "<skipped/>" if skipped else ""
        path = root / "junit.xml"
        path.write_text(
            "<testsuites>"
            f'<testsuite tests="1" failures="0" errors="0" skipped="{skipped}">'
            '<testcase name="test_roundtrip[installed-latexmk-latexdiff]">'
            f"{skipped_node}</testcase></testsuite></testsuites>",
            encoding="utf-8",
        )
        return path

    def test_exact_real_case_pass_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = REAL_TEX.junit_summary(self.write_junit(Path(temporary), skipped=0))
            REAL_TEX.require_one_real_test(summary, 0)

    def test_pytest_skip_is_rejected_even_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = REAL_TEX.junit_summary(self.write_junit(Path(temporary), skipped=1))
            with self.assertRaises(REAL_TEX.RealTexGateError):
                REAL_TEX.require_one_real_test(summary, 0)

    def test_github_bootstrap_evidence_is_bound(self) -> None:
        environment = {
            "GITHUB_ACTIONS": "true",
            "MIKTEX_SETUP_FILENAME": REAL_TEX.EXPECTED_SETUP_FILENAME,
            "MIKTEX_SETUP_SHA256": REAL_TEX.EXPECTED_SETUP_SHA256,
        }
        with mock.patch.dict(REAL_TEX.os.environ, environment, clear=True):
            evidence = REAL_TEX.bootstrap_evidence()
        self.assertEqual(evidence["source"], "verified_official_setup_utility")
        self.assertEqual(evidence["sha256"], REAL_TEX.EXPECTED_SETUP_SHA256)

    def test_github_bootstrap_evidence_cannot_be_omitted(self) -> None:
        with (
            mock.patch.dict(REAL_TEX.os.environ, {"GITHUB_ACTIONS": "true"}, clear=True),
            self.assertRaises(REAL_TEX.RealTexGateError),
        ):
            REAL_TEX.bootstrap_evidence()

    def test_installed_miktex_inventory_and_required_package_evidence_are_bound(self) -> None:
        rows = [f"unused-package\t1.0\t{'f' * 32}\tfalse"]
        for package_id in reversed(REAL_TEX.REQUIRED_MIKTEX_PACKAGES):
            version = "2.6.0" if package_id == "ctex" else ""
            rows.append(f"{package_id}\t{version}\t{'a' * 32}\ttrue")
        rows.append(f"basepackage\t1.0\t{'b' * 32}\ttrue")

        def package_list(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, 60)
            self.assertEqual(command[1:4], ["--disable-installer", "packages", "list"])
            self.assertEqual(command[4:], ["--template", REAL_TEX.PACKAGE_LIST_TEMPLATE])
            return subprocess.CompletedProcess(command, 0, "\n".join(rows) + "\n", "")

        with (
            mock.patch.object(REAL_TEX.shutil, "which", return_value="miktex.exe"),
            mock.patch.object(REAL_TEX, "run_captured", side_effect=package_list),
        ):
            inventory = REAL_TEX.installed_miktex_package_inventory()
        packages = REAL_TEX.required_miktex_package_evidence(inventory)
        inventory_evidence = REAL_TEX.miktex_package_inventory_evidence(inventory)

        self.assertEqual(len(inventory), 30)
        self.assertEqual(
            [package["id"] for package in inventory],
            sorted(package["id"] for package in inventory),
        )
        self.assertEqual(
            [package["id"] for package in packages],
            [
                "amsmath",
                "bigintcalc",
                "bitset",
                "booktabs",
                "cjk",
                "ctex",
                "fandol",
                "gettitlestring",
                "graphics",
                "hycolor",
                "hyperref",
                "infwarerr",
                "intcalc",
                "kvdefinekeys",
                "kvoptions",
                "kvsetkeys",
                "latexdiff",
                "latexmk",
                "letltxmacro",
                "ltxcmds",
                "pdfescape",
                "refcount",
                "rerunfilecheck",
                "stringenc",
                "ulem",
                "uniquecounter",
                "xecjk",
                "xetex",
                "zhnumber",
            ],
        )
        self.assertEqual(packages[5]["version"], "2.6.0")
        self.assertEqual(packages[16]["version"], "not-reported")
        self.assertEqual(inventory_evidence["count"], 30)
        self.assertEqual(inventory_evidence["format"], REAL_TEX.PACKAGE_INVENTORY_FORMAT)
        self.assertRegex(str(inventory_evidence["sha256"]), r"^sha256:[0-9a-f]{64}$")

    def test_miktex_package_manifest_is_bound(self) -> None:
        evidence = REAL_TEX.package_manifest_evidence()
        self.assertEqual(evidence["count"], 29)
        self.assertEqual(evidence["path"], ".github/actions/real-tex-gate/miktex-packages.txt")
        self.assertRegex(str(evidence["sha256"]), r"^sha256:[0-9a-f]{64}$")
        self.assertIn("xecjk", REAL_TEX.REQUIRED_MIKTEX_PACKAGES)
        self.assertIn("rerunfilecheck", REAL_TEX.REQUIRED_MIKTEX_PACKAGES)

    def test_required_tex_resources_are_resolved(self) -> None:
        def kpsewhich(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, 30)
            return subprocess.CompletedProcess(command, 0, f"C:/tex/{command[-1]}\n", "")

        with (
            mock.patch.object(REAL_TEX.shutil, "which", return_value="kpsewhich.exe"),
            mock.patch.object(REAL_TEX, "run_captured", side_effect=kpsewhich),
        ):
            resources = REAL_TEX.verify_tex_resources()
        self.assertEqual(
            resources,
            {
                "ctex_sty": "resolved_by_kpsewhich",
                "fandol_song_regular": "resolved_by_kpsewhich",
                "xelatex_format": "resolved_by_kpsewhich",
            },
        )

    def test_missing_miktex_package_fails_closed(self) -> None:
        rows = []
        for package_id in REAL_TEX.REQUIRED_MIKTEX_PACKAGES:
            installed = "false" if package_id == "ctex" else "true"
            rows.append(f"{package_id}\t1.0\t{'b' * 32}\t{installed}")
        result = subprocess.CompletedProcess(
            ["miktex.exe"],
            0,
            "\n".join(rows) + "\n",
            "",
        )
        with (
            mock.patch.object(REAL_TEX.shutil, "which", return_value="miktex.exe"),
            mock.patch.object(REAL_TEX, "run_captured", return_value=result),
        ):
            inventory = REAL_TEX.installed_miktex_package_inventory()
        with self.assertRaisesRegex(REAL_TEX.RealTexGateError, "ctex"):
            REAL_TEX.required_miktex_package_evidence(inventory)

    def test_malformed_or_duplicate_miktex_inventory_fails_closed(self) -> None:
        malformed_outputs = {
            "missing fields": "amsmath\t2.17z\tmissing-fields\n",
            "invalid id": f"INVALID\t1.0\t{'a' * 32}\ttrue\n",
            "invalid digest": "amsmath\t2.17z\tnot-a-digest\ttrue\n",
            "invalid state": f"amsmath\t2.17z\t{'a' * 32}\tunknown\n",
            "duplicate id": (
                f"amsmath\t2.17z\t{'a' * 32}\ttrue\namsmath\t2.17z\t{'a' * 32}\tfalse\n"
            ),
        }
        for label, stdout in malformed_outputs.items():
            with self.subTest(label=label):
                result = subprocess.CompletedProcess(["miktex.exe"], 0, stdout, "")
                with (
                    mock.patch.object(REAL_TEX.shutil, "which", return_value="miktex.exe"),
                    mock.patch.object(REAL_TEX, "run_captured", return_value=result),
                    self.assertRaises(REAL_TEX.RealTexGateError),
                ):
                    REAL_TEX.installed_miktex_package_inventory()

    def test_miktex_inventory_change_is_rejected(self) -> None:
        before = ({"digest": "a" * 32, "id": "amsmath", "version": "2.17z"},)
        REAL_TEX.require_unchanged_miktex_package_inventory(before, tuple(dict(x) for x in before))
        after = (
            *before,
            {"digest": "b" * 32, "id": "new-package", "version": "1.0"},
        )
        with self.assertRaisesRegex(REAL_TEX.RealTexGateError, "inventory changed"):
            REAL_TEX.require_unchanged_miktex_package_inventory(before, after)


class CleanInstallArchiveTests(unittest.TestCase):
    def test_repository_sdist_include_contract_matches_clean_install_guard(self) -> None:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
        includes = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        self.assertEqual(tuple(includes), CLEAN_INSTALL.EXPECTED_SDIST_INCLUDES)

    def write_sdist(
        self,
        path: Path,
        members: list[tuple[str, bytes | None, bytes | None]],
    ) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name, data, link_target in members:
                info = tarfile.TarInfo(name)
                if link_target is not None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = link_target.decode("utf-8")
                    archive.addfile(info)
                elif data is None:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

    def write_valid_sdist_source(
        self, root: Path, *, version: str = "0.1.0b1"
    ) -> tuple[Path, Path]:
        source = root / f"latex_word_review-{version}"
        version_source = source / "src/latex_word_review/__about__.py"
        version_source.parent.mkdir(parents=True)
        version_source.write_text(
            f'"""Static package metadata."""\n\n__version__ = "{version}"\n',
            encoding="utf-8",
        )
        includes = ",\n".join(
            f"  {json.dumps(item)}" for item in CLEAN_INSTALL.EXPECTED_SDIST_INCLUDES
        )
        pyproject_content = (
            "[build-system]\n"
            'requires = ["hatchling==1.31.0"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "latex-word-review"\n'
            'dynamic = ["version"]\n'
            'readme = "README.md"\n'
            'requires-python = ">=3.12,<3.14"\n'
            'license = "Apache-2.0"\n'
            'license-files = ["LICENSE"]\n\n'
            "[tool.hatch.version]\n"
            'path = "src/latex_word_review/__about__.py"\n\n'
            "[tool.hatch.build.targets.sdist]\n"
            f"include = [\n{includes}\n]\n\n"
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/latex_word_review"]\n'
        )
        (source / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
        (root / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
        (source / "PKG-INFO").write_text(
            "Metadata-Version: 2.4\n"
            "Name: latex-word-review\n"
            f"Version: {version}\n"
            "Requires-Python: <3.14,>=3.12\n"
            "License-Expression: Apache-2.0\n\n",
            encoding="utf-8",
        )
        artifact = root / f"latex_word_review-{version}.tar.gz"
        artifact.write_bytes(b"synthetic")
        return source, artifact

    def test_safe_unpack_accepts_one_regular_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.tar.gz"
            self.write_sdist(
                archive,
                [
                    ("project-1.0", None, None),
                    ("project-1.0/pyproject.toml", b"[build-system]\n", None),
                    ("project-1.0/src/value.py", b"value = 1\n", None),
                ],
            )
            extracted = CLEAN_INSTALL.safe_unpack_sdist(archive, root / "unpacked")
            self.assertEqual(extracted.name, "project-1.0")
            self.assertEqual(
                (extracted / "src/value.py").read_text(encoding="utf-8"), "value = 1\n"
            )

    def test_sdist_traversal_is_rejected_without_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.tar.gz"
            self.write_sdist(
                archive,
                [
                    ("project-1.0/pyproject.toml", b"[build-system]\n", None),
                    ("project-1.0/../escape.txt", b"escape", None),
                ],
            )
            with self.assertRaises(CLEAN_INSTALL.CleanInstallError):
                CLEAN_INSTALL.safe_unpack_sdist(archive, root / "unpacked")
            self.assertFalse((root / "escape.txt").exists())

    def test_sdist_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.tar.gz"
            self.write_sdist(
                archive,
                [
                    ("project-1.0/pyproject.toml", b"[build-system]\n", None),
                    ("project-1.0/link", None, b"../../outside"),
                ],
            )
            with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "link"):
                CLEAN_INSTALL.safe_unpack_sdist(archive, root / "unpacked")

    def test_sdist_case_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "project.tar.gz"
            self.write_sdist(
                archive,
                [
                    ("project-1.0/pyproject.toml", b"[build-system]\n", None),
                    ("project-1.0/README.md", b"one", None),
                    ("project-1.0/readme.md", b"two", None),
                ],
            )
            with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "colliding"):
                CLEAN_INSTALL.safe_unpack_sdist(archive, root / "unpacked")

    def test_sdist_windows_aliases_and_prefix_collisions_are_rejected(self) -> None:
        for name in (
            "project-1.0/NUL.txt",
            "project-1.0/name:stream.py",
            "project-1.0/trailing.",
            "project-1.0/double//slash.py",
        ):
            with self.subTest(name=name), self.assertRaises(CLEAN_INSTALL.CleanInstallError):
                CLEAN_INSTALL._safe_sdist_member(name)
        entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
        CLEAN_INSTALL._register_sdist_member(
            CLEAN_INSTALL._safe_sdist_member("project-1.0/package"),
            is_directory=False,
            entries=entries,
        )
        with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "nested below a file"):
            CLEAN_INSTALL._register_sdist_member(
                CLEAN_INSTALL._safe_sdist_member("project-1.0/package/module.py"),
                is_directory=False,
                entries=entries,
            )
        parent_entries: dict[tuple[str, ...], tuple[bool | None, tuple[str, ...]]] = {}
        CLEAN_INSTALL._register_sdist_member(
            CLEAN_INSTALL._safe_sdist_member("project-1.0/Docs/one.py"),
            is_directory=False,
            entries=parent_entries,
        )
        with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "directory spelling"):
            CLEAN_INSTALL._register_sdist_member(
                CLEAN_INSTALL._safe_sdist_member("project-1.0/docs/two.py"),
                is_directory=False,
                entries=parent_entries,
            )

    def test_sdist_pax_metadata_is_covered_by_decompression_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "project.tar.gz"
            with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo("project-1.0/pyproject.toml")
                data = b"[build-system]\n"
                info.size = len(data)
                info.pax_headers = {"comment": "x" * 4096}
                archive.addfile(info, io.BytesIO(data))
            with (
                mock.patch.object(CLEAN_INSTALL, "MAX_SDIST_STREAM_BYTES", 1024),
                self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "hard limit"),
            ):
                CLEAN_INSTALL.safe_unpack_sdist(archive_path, root / "unpacked")
            with (
                mock.patch.object(CHECKS, "MAX_ARCHIVE_STREAM_BYTES", 1024),
                self.assertRaisesRegex(CHECKS.ReleaseCheckError, "hard limit"),
            ):
                CHECKS.check_sdist(archive_path, "1.0")

    def test_sdist_build_contract_accepts_only_reviewed_static_hatchling_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = self.write_valid_sdist_source(root)
            with mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root):
                CLEAN_INSTALL.validate_sdist_build_contract(
                    source,
                    artifact,
                    expected_name="latex-word-review",
                    expected_version="0.1.0b1",
                )

    def test_sdist_build_contract_rejects_backend_path_and_custom_hook(self) -> None:
        for mutation in (
            'backend-path = ["backend"]\n',
            '[tool.hatch.build.hooks.custom]\npath = "evil.py"\n',
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, artifact = self.write_valid_sdist_source(Path(temporary))
                pyproject = source / "pyproject.toml"
                content = pyproject.read_text(encoding="utf-8")
                if mutation.startswith("backend-path"):
                    content = content.replace(
                        'build-backend = "hatchling.build"\n',
                        'build-backend = "hatchling.build"\n' + mutation,
                    )
                else:
                    content += "\n" + mutation
                pyproject.write_text(content, encoding="utf-8")
                with (
                    mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", Path(temporary)),
                    self.assertRaises(CLEAN_INSTALL.CleanInstallError),
                ):
                    CLEAN_INSTALL.validate_sdist_build_contract(
                        source,
                        artifact,
                        expected_name="latex-word-review",
                        expected_version="0.1.0b1",
                    )

    def test_sdist_build_contract_rejects_executable_version_source_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, artifact = self.write_valid_sdist_source(root)
            (source / CLEAN_INSTALL.EXPECTED_PROJECT_VERSION_PATH).write_text(
                'import os\nos.system("never")\n__version__ = "0.1.0b1"\n',
                encoding="utf-8",
            )
            work = root / "work"
            work.mkdir()
            with (
                mock.patch.object(CLEAN_INSTALL, "safe_unpack_sdist", return_value=source),
                mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root),
                mock.patch.object(CLEAN_INSTALL, "run") as run_mock,
                self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "static assignment"),
            ):
                CLEAN_INSTALL.build_sdist_wheel(
                    artifact,
                    work,
                    {"PYTHONUTF8": "1"},
                    expected_name="latex-word-review",
                    expected_version="0.1.0b1",
                )
            run_mock.assert_not_called()

    def test_runtime_export_requires_exact_pins_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.txt"
            path.write_text("example==1.2.3 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
            self.assertEqual(CLEAN_INSTALL.validate_runtime_requirements(path), 1)
            path.write_text("example>=1.2\n", encoding="utf-8")
            with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "exactly pinned"):
                CLEAN_INSTALL.validate_runtime_requirements(path)
            path.write_text(
                "example==1.2.3 @ https://example.invalid/example.whl --hash=sha256:"
                + "a" * 64
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "non-registry"):
                CLEAN_INSTALL.validate_runtime_requirements(path)

    def test_sdist_build_explicitly_disables_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            commands: list[list[str]] = []

            def fake_run(
                command: list[str],
                *,
                timeout: int,
                cwd: Path | None = None,
                env: dict[str, str] | None = None,
            ) -> None:
                del timeout, cwd, env
                commands.append(command)
                (work / "project-wheel/project.whl").write_bytes(b"synthetic")

            with (
                mock.patch.object(CLEAN_INSTALL, "safe_unpack_sdist", return_value=source),
                mock.patch.object(CLEAN_INSTALL, "validate_sdist_build_contract"),
                mock.patch.object(CLEAN_INSTALL, "run", side_effect=fake_run),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "_wheel_identity",
                    return_value=("latex-word-review", "0.1.0b1"),
                ),
            ):
                result = CLEAN_INSTALL.build_sdist_wheel(
                    root / "project.tar.gz",
                    work,
                    {"PYTHONUTF8": "1"},
                    expected_name="latex-word-review",
                    expected_version="0.1.0b1",
                )
            self.assertEqual(result.name, "project.whl")
            self.assertEqual(len(commands), 1)
            self.assertIn("--no-isolation", commands[0])

    def test_runtime_wheelhouse_uses_hashes_locked_builder_and_no_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requirements = root / "requirements.txt"
            requirements.write_text(
                "example==1.0 --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            work = root / "work"
            work.mkdir()
            commands: list[list[str]] = []

            def fake_run(
                command: list[str],
                *,
                timeout: int,
                cwd: Path | None = None,
                env: dict[str, str] | None = None,
            ) -> None:
                del timeout, cwd, env
                commands.append(command)
                wheelhouse = work / "runtime-wheelhouse"
                (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(b"synthetic")

            with (
                mock.patch.object(CLEAN_INSTALL, "run", side_effect=fake_run),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "_wheel_identity",
                    return_value=("example", "1.0"),
                ),
            ):
                _, locked, identities = CLEAN_INSTALL.build_runtime_wheelhouse(
                    requirements, work, {"PYTHONUTF8": "1"}
                )
            self.assertEqual(identities, {"example": "1.0"})
            self.assertEqual(len(commands), 1)
            for flag in ("--require-hashes", "--no-deps", "--no-build-isolation", "--no-cache-dir"):
                self.assertIn(flag, commands[0])
            self.assertIn("file:", locked.read_text(encoding="utf-8"))

    def test_sdist_derived_wheel_must_match_release_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.whl"
            matching = root / "matching.whl"
            changed = root / "changed.whl"
            for path, payload in (
                (reference, b"same"),
                (matching, b"same"),
                (changed, b"different"),
            ):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("package/module.py", payload)
            CLEAN_INSTALL.require_matching_wheel_payload(reference, matching)
            with self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "payload differs"):
                CLEAN_INSTALL.require_matching_wheel_payload(reference, changed)


class CleanInstallBoundaryTests(unittest.TestCase):
    def temporary_repository(self) -> tempfile.TemporaryDirectory[str]:
        build = Path(__file__).resolve().parents[2] / "build"
        build.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="clean-install-selftest-", dir=build)

    def test_preexisting_demo_output_is_rejected_without_clobber(self) -> None:
        with self.temporary_repository() as temporary:
            root = Path(temporary)
            (root / "build/demo").mkdir(parents=True)
            marker = root / "build/demo/marker.txt"
            marker.write_text("keep", encoding="utf-8")
            (root / "dist").mkdir()
            (root / "dist/package.whl").write_bytes(b"wheel")
            with (
                mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root),
                mock.patch.object(CLEAN_INSTALL, "BUILD_ROOT", root / "build"),
                self.assertRaises(CLEAN_INSTALL.CleanInstallError),
            ):
                CLEAN_INSTALL.execute(
                    argparse.Namespace(
                        artifact="wheel",
                        dist_dir=root / "dist",
                        venv=root / "build/venv",
                        public_e0_output=root / "build/demo",
                    )
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "build/venv").exists())

    def test_preexisting_work_directory_is_rejected_without_clobber(self) -> None:
        with self.temporary_repository() as temporary:
            root = Path(temporary)
            work = root / "build/.venv-clean-install-work"
            work.mkdir(parents=True)
            marker = work / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            (root / "dist").mkdir()
            (root / "dist/package.whl").write_bytes(b"wheel")
            with (
                mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root),
                mock.patch.object(CLEAN_INSTALL, "BUILD_ROOT", root / "build"),
                self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "work directory already"),
            ):
                CLEAN_INSTALL.execute(
                    argparse.Namespace(
                        artifact="wheel",
                        dist_dir=root / "dist",
                        venv=root / "build/venv",
                        public_e0_output=None,
                    )
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "build/venv").exists())

    def test_overlapping_venv_and_demo_roots_are_rejected(self) -> None:
        with self.temporary_repository() as temporary:
            root = Path(temporary)
            (root / "dist").mkdir()
            (root / "dist/package.whl").write_bytes(b"wheel")
            with (
                mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root),
                mock.patch.object(CLEAN_INSTALL, "BUILD_ROOT", root / "build"),
                self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "must not overlap"),
            ):
                CLEAN_INSTALL.execute(
                    argparse.Namespace(
                        artifact="wheel",
                        dist_dir=root / "dist",
                        venv=root / "build/run",
                        public_e0_output=root / "build/run/demo",
                    )
                )
            self.assertFalse((root / "build/run").exists())

    def test_demo_failure_cleans_only_owned_output_and_venv(self) -> None:
        with self.temporary_repository() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            artifact = dist / "package.whl"
            artifact.write_bytes(b"wheel")
            demo_script = root / "scripts/run_public_e0_cli_demo.py"
            demo_script.parent.mkdir()
            demo_script.write_text("# synthetic self-test placeholder\n", encoding="utf-8")
            venv_root = root / "build/venv"
            demo_output = root / "build/demo"
            demo_commands: list[tuple[list[str], dict[str, str]]] = []
            all_commands: list[list[str]] = []

            def create_fake_venv(path: Path) -> None:
                python = CLEAN_INSTALL.environment_python(path)
                script = CLEAN_INSTALL.console_script(path)
                python.parent.mkdir(parents=True)
                python.write_bytes(b"python")
                script.write_bytes(b"entry point")

            builder = mock.Mock()
            builder.create.side_effect = create_fake_venv

            def fake_runtime_export(work: Path, environment: dict[str, str]) -> tuple[Path, int]:
                del environment
                requirements = work / "runtime-requirements.txt"
                requirements.write_text(
                    "example==1.0 --hash=sha256:" + "a" * 64 + "\n",
                    encoding="utf-8",
                )
                return requirements, 1

            def fake_runtime_wheelhouse(
                requirements: Path,
                work: Path,
                environment: dict[str, str],
            ) -> tuple[Path, Path, dict[str, str]]:
                del requirements, environment
                wheelhouse = work / "runtime-wheelhouse"
                wheelhouse.mkdir()
                locked = work / "runtime-wheels.txt"
                locked.write_text("synthetic\n", encoding="utf-8")
                return wheelhouse, locked, {"example": "1.0"}

            def fail_at_demo(
                command: list[str],
                *,
                timeout: int,
                cwd: Path | None = None,
                env: dict[str, str] | None = None,
            ) -> None:
                del timeout, cwd
                all_commands.append(command)
                if str(demo_script) not in command:
                    return
                self.assertIsNotNone(env)
                assert env is not None
                demo_commands.append((command, env))
                demo_output.mkdir(parents=True)
                (demo_output / "partial.txt").write_text("partial", encoding="utf-8")
                raise CLEAN_INSTALL.CleanInstallError("synthetic demo failure")

            with (
                mock.patch.object(CLEAN_INSTALL, "PROJECT_ROOT", root),
                mock.patch.object(CLEAN_INSTALL, "BUILD_ROOT", root / "build"),
                mock.patch.object(CLEAN_INSTALL, "PUBLIC_E0_DEMO", demo_script),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "require_locked_builder",
                    return_value={"hatchling": "1.31.0"},
                ),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "export_runtime_requirements",
                    side_effect=fake_runtime_export,
                ),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "build_runtime_wheelhouse",
                    side_effect=fake_runtime_wheelhouse,
                ),
                mock.patch.object(
                    CLEAN_INSTALL,
                    "_wheel_identity",
                    return_value=("latex-word-review", "0.1.0b1"),
                ),
                mock.patch.object(CLEAN_INSTALL.venv, "EnvBuilder", return_value=builder),
                mock.patch.object(CLEAN_INSTALL, "run", side_effect=fail_at_demo),
                mock.patch.dict(
                    CLEAN_INSTALL.os.environ,
                    {"PYTHONHOME": "unsafe-home", "PYTHONPATH": "unsafe-path"},
                ),
                self.assertRaisesRegex(CLEAN_INSTALL.CleanInstallError, "synthetic demo failure"),
            ):
                CLEAN_INSTALL.execute(
                    argparse.Namespace(
                        artifact="wheel",
                        dist_dir=dist,
                        venv=venv_root,
                        public_e0_output=demo_output,
                    )
                )

            self.assertEqual(len(demo_commands), 1)
            command, environment = demo_commands[0]
            self.assertEqual(command[0], str(CLEAN_INSTALL.environment_python(venv_root)))
            self.assertEqual(command[-1], "--skip-verification")
            self.assertNotIn("PYTHONHOME", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
            self.assertEqual(environment["PYTHONSAFEPATH"], "1")
            install_commands = [command for command in all_commands if "install" in command]
            self.assertEqual(len(install_commands), 2)
            self.assertTrue(all("--no-index" in command for command in install_commands))
            self.assertFalse(venv_root.exists())
            self.assertFalse(demo_output.exists())
            self.assertEqual(artifact.read_bytes(), b"wheel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
