"""Bundle writing, packing and (defensive) reading."""

from __future__ import annotations

import datetime as _dt
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from oxfscope import fake, layout
from oxfscope.bundle import Bundle, BundleBuilder
from oxfscope.errors import BundleError, ManifestError
from oxfscope.manifest import ProbeResult, Quality, TargetInfo, build_manifest

STARTED = _dt.datetime(2026, 8, 6, 14, 25, 30, tzinfo=_dt.timezone.utc)


def _minimal_manifest(builder: BundleBuilder):
    return build_manifest(
        label=builder.label,
        profile="light",
        duration_s=1.0,
        started_at=builder.started_at,
        target=TargetInfo(pid=1234, comm="t", thread_count_start=2, thread_count_end=2),
        probes=[ProbeResult(name="oncpu", status="ok", outputs=[layout.STACK_ONCPU])],
        quality=Quality(),
        tool_versions={"oxfscope": "0.1.0"},
    )


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _builder(self, label="unit"):
        return BundleBuilder(self.tmp, label=label, started_at=STARTED)

    def test_layout_directories_are_created(self):
        builder = self._builder()
        for name in layout.BUNDLE_DIRS:
            self.assertTrue((builder.root / name).is_dir(), name)

    def test_refuses_unsafe_label(self):
        with self.assertRaises(BundleError):
            BundleBuilder(self.tmp, label="../escape")

    def test_refuses_existing_run_directory(self):
        self._builder()
        with self.assertRaises(BundleError):
            self._builder()

    def test_refuses_unsafe_relpath(self):
        builder = self._builder()
        for bad in ("../x", "/abs", "stacks/../../x", "", "a\x00b"):
            with self.subTest(path=bad):
                with self.assertRaises(BundleError):
                    builder.add_text(bad, "x")

    def test_refuses_invalid_probe_name(self):
        builder = self._builder()
        with self.assertRaises(BundleError):
            builder.probe_log_path("../evil")

    def test_series_header_is_enforced(self):
        builder = self._builder()
        with self.assertRaises(BundleError):
            builder.add_series(layout.SERIES_THREADS, [[0, 1]])  # too few columns
        with self.assertRaises(BundleError):
            builder.add_series("series/unknown.csv", [])

    def test_invalid_manifest_is_never_written(self):
        builder = self._builder()
        document = _minimal_manifest(builder)
        document["quality"]["unknown_frame_ratio"] = 5.0
        with self.assertRaises(ManifestError):
            builder.write_manifest(document)
        self.assertFalse((builder.root / layout.MANIFEST).exists())

    def test_manifest_run_id_must_match_builder(self):
        builder = self._builder()
        document = _minimal_manifest(builder)
        document["run_id"] = "20200101T000000Z-other"
        with self.assertRaises(BundleError):
            builder.write_manifest(document)

    def test_cannot_pack_without_manifest(self):
        builder = self._builder()
        with self.assertRaises(BundleError):
            builder.pack()

    def test_inventory_covers_every_payload_file(self):
        builder = self._builder()
        builder.add_folded(layout.STACK_ONCPU, [("main;work", 10)])
        builder.add_series(layout.SERIES_THREADS, [[0, 3, 100, 5]])
        builder.write_manifest(_minimal_manifest(builder))
        document = json.loads((builder.root / layout.MANIFEST).read_text())
        paths = {entry["path"] for entry in document["files"]}
        self.assertEqual(paths, {layout.STACK_ONCPU, layout.SERIES_THREADS})
        self.assertNotIn(layout.MANIFEST, paths)
        rows = {e["path"]: e.get("rows") for e in document["files"]}
        self.assertEqual(rows[layout.STACK_ONCPU], 1)
        self.assertEqual(rows[layout.SERIES_THREADS], 1)  # header excluded


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir, self.archive = fake.generate(
            self.tmp, label="unit", duration_s=5, thread_count=12, started_at=STARTED
        )

    def test_directory_and_archive_agree(self):
        with Bundle.open(self.run_dir) as from_dir, Bundle.open(self.archive) as from_tar:
            self.assertEqual(from_dir.manifest, from_tar.manifest)
            self.assertEqual(from_dir.paths(), from_tar.paths())
            self.assertEqual(
                from_dir.read_bytes(layout.STACK_ONCPU),
                from_tar.read_bytes(layout.STACK_ONCPU),
            )

    def test_synthetic_bundle_validates(self):
        with Bundle.open(self.archive) as bundle:
            report = bundle.validate(verify_hashes=True)
            self.assertTrue(report.ok, report.flat())
            self.assertIn(layout.MANIFEST, report.checked)
            self.assertIn(layout.META_THREADS, report.checked)
            self.assertIn(layout.HIST_RUNQLAT, report.checked)

    def test_folded_parsing(self):
        with Bundle.open(self.archive) as bundle:
            entries = list(bundle.iter_folded(layout.STACK_OFFCPU))
        self.assertTrue(entries)
        for stack, value in entries:
            self.assertIsInstance(value, int)
            self.assertNotIn(" ", stack.split(";")[0])

    def test_series_parsing(self):
        with Bundle.open(self.archive) as bundle:
            rows = bundle.series_rows(layout.SERIES_THREADS)
        self.assertEqual(len(rows), 5)
        self.assertEqual(set(rows[0]), set(layout.SERIES_HEADERS[layout.SERIES_THREADS]))

    def test_missing_member_is_an_error(self):
        with Bundle.open(self.archive) as bundle:
            with self.assertRaises(BundleError):
                bundle.read_text("stacks/does_not_exist.folded")

    def test_open_rejects_directory_without_manifest(self):
        with self.assertRaises(BundleError):
            Bundle.open(self.tmp)

    def test_open_rejects_missing_path(self):
        with self.assertRaises(BundleError):
            Bundle.open(self.tmp / "nope.tgz")


class TamperTests(unittest.TestCase):
    """A bundle arrives from another machine. Treat it as untrusted input."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _tar_with(self, members):
        archive = self.tmp / "evil.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            for info, payload in members:
                tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
        return archive

    def test_symlink_member_is_rejected(self):
        info = tarfile.TarInfo("run_x/manifest.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        with self.assertRaises(BundleError) as ctx:
            Bundle.open(self._tar_with([(info, None)]))
        self.assertIn("link member", str(ctx.exception))

    def test_parent_traversal_member_is_rejected(self):
        payload = b"{}"
        info = tarfile.TarInfo("run_x/../../etc/passwd")
        info.size = len(payload)
        with self.assertRaises(BundleError) as ctx:
            Bundle.open(self._tar_with([(info, payload)]))
        self.assertIn("unsafe member path", str(ctx.exception))

    def test_multiple_top_level_directories_are_rejected(self):
        members = []
        for name in ("run_a/manifest.json", "run_b/manifest.json"):
            info = tarfile.TarInfo(name)
            info.size = 2
            members.append((info, b"{}"))
        with self.assertRaises(BundleError) as ctx:
            Bundle.open(self._tar_with(members))
        self.assertIn("one top level run directory", str(ctx.exception))

    def test_absent_manifest_is_rejected(self):
        info = tarfile.TarInfo("run_x/stacks/oncpu.folded")
        info.size = 5
        with self.assertRaises(BundleError):
            Bundle.open(self._tar_with([(info, b"a;b 1")]))

    def test_unparseable_manifest_is_rejected(self):
        info = tarfile.TarInfo("run_x/manifest.json")
        info.size = 3
        with self.assertRaises(ManifestError):
            Bundle.open(self._tar_with([(info, b"not")]))

    def test_corrupted_payload_is_caught_by_hash_check(self):
        run_dir, _archive = fake.generate(
            self.tmp, label="unit", duration_s=2, thread_count=4, started_at=STARTED
        )
        target = run_dir / layout.STACK_ONCPU
        target.write_text(target.read_text() + "injected;frame 1\n")
        with Bundle.open(run_dir) as bundle:
            report = bundle.validate(verify_hashes=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("sha256 mismatch" in p for p in report.flat()), report.flat())

    def test_undeclared_extra_file_is_reported(self):
        run_dir, _archive = fake.generate(
            self.tmp, label="unit", duration_s=2, thread_count=4, started_at=STARTED
        )
        (run_dir / "stacks" / "sneaky.folded").write_text("a;b 1\n")
        with Bundle.open(run_dir) as bundle:
            report = bundle.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("not listed in manifest.files" in p for p in report.flat()),
            report.flat(),
        )


class FakeGeneratorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_same_seed_gives_identical_output(self):
        first, _ = fake.generate(
            self.tmp / "a", label="unit", duration_s=3, thread_count=8, started_at=STARTED
        )
        second, _ = fake.generate(
            self.tmp / "b", label="unit", duration_s=3, thread_count=8, started_at=STARTED
        )
        for relpath in (layout.STACK_ONCPU, layout.MANIFEST, layout.META_THREADS):
            self.assertEqual(
                (first / relpath).read_bytes(),
                (second / relpath).read_bytes(),
                relpath,
            )

    def test_degraded_run_is_marked_partial_and_still_readable(self):
        _run, archive = fake.generate(
            self.tmp / "d",
            label="unit",
            duration_s=6,
            thread_count=8,
            degraded=True,
            started_at=STARTED,
        )
        with Bundle.open(archive) as bundle:
            self.assertEqual(bundle.status, "partial")
            self.assertIsNotNone(bundle.manifest.get("target_died_at"))
            self.assertTrue(bundle.validate().ok)
            # The failed probe left no output behind.
            self.assertFalse(bundle.exists(layout.HIST_SYSCALL_LATENCY))
            statuses = {p["name"]: p["status"] for p in bundle.manifest["probes"]}
            self.assertEqual(statuses["syscall_lat"], "failed")
            self.assertEqual(statuses["futex"], "partial")

    def test_bad_frame_pointers_trips_the_error_threshold(self):
        _run, archive = fake.generate(
            self.tmp / "f",
            label="unit",
            duration_s=3,
            thread_count=8,
            bad_frame_pointers=True,
            started_at=STARTED,
        )
        with Bundle.open(archive) as bundle:
            self.assertGreater(bundle.manifest["quality"]["unknown_frame_ratio"], 0.30)
            self.assertFalse(bundle.manifest["quality"]["frame_pointers_ok"])

    def test_thread_count_is_honoured(self):
        run_dir, _ = fake.generate(
            self.tmp / "t", label="unit", duration_s=2, thread_count=315, started_at=STARTED
        )
        threads = json.loads((run_dir / layout.META_THREADS).read_text())["threads"]
        self.assertEqual(len(threads), 315)


if __name__ == "__main__":
    unittest.main()
