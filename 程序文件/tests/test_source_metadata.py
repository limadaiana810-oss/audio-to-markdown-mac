"""Real macOS metadata changes must not masquerade as modified audio bytes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_core_recovery as recovery
import batch_export
import tx


class SourceMetadataTests(unittest.TestCase):
    setUp = recovery.RecoveryTests.setUp
    decode = staticmethod(recovery.RecoveryTests.decode)
    process = recovery.RecoveryTests.process

    def change_metadata(self, source, value='opened'):
        if sys.platform == 'darwin':
            subprocess.run(['/usr/bin/xattr', '-w', 'com.example.tx-regression', value, str(source)], check=True)
        else:
            os.chmod(source, source.stat().st_mode ^ 0o100)

    @staticmethod
    def change_bytes_preserving_size_and_mtime(source):
        before = source.stat()
        with source.open('r+b') as stream:
            stream.seek(-1, 2)
            byte = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([byte[0] ^ 1]))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    def mutate_once_during_inference(self, mutate):
        run = self.engine.run_chunk
        changed = []
        def call(*args):
            if not changed:
                mutate(self.src)
                changed.append(True)
            return run(*args)
        self.engine.run_chunk = call

    def test_xattr_change_during_asr_succeeds_with_original_audio_sha(self):
        before = tx.source_identity(self.src)
        digest = hashlib.sha256(self.src.read_bytes()).hexdigest()
        self.mutate_once_during_inference(self.change_metadata)
        self.assertEqual(0, tx.run_files(self.engine, [self.src], self.out, self.args, {}))
        after = tx.source_identity(self.src)
        self.assertEqual(['ctime_ns'], [key for key in before if before[key] != after[key]])
        result = tx.output_dir_for(self.src, self.out)
        marker = tx.read_json(result/'.work/completed.json')
        self.assertEqual(after, marker['source'])
        self.assertEqual(digest, marker['source_sha256'])
        self.assertEqual(digest, hashlib.sha256(self.src.read_bytes()).hexdigest())
        self.assertEqual('completed', tx.read_json(self.out/'批次索引.json')['merged_markdown']['status'])

    def test_chmod_during_output_is_rechecked_and_accepted(self):
        write = tx.write_outputs
        def after_output(*args, **kwargs):
            result = write(*args, **kwargs)
            os.chmod(self.src, self.src.stat().st_mode ^ 0o100)
            return result
        with patch.object(tx, 'write_outputs', side_effect=after_output):
            result = self.process()
        self.assertTrue(tx.is_completed(self.src, self.out, self.args))
        self.assertEqual(tx.source_identity(self.src), tx.read_json(result/'.work/completed.json')['source'])

    def test_real_content_change_with_restored_size_and_mtime_still_fails(self):
        before = tx.source_identity(self.src)
        self.mutate_once_during_inference(self.change_bytes_preserving_size_and_mtime)
        with self.assertRaisesRegex(RuntimeError, '录音在处理过程中发生变化'):
            self.process()
        after = tx.source_identity(self.src)
        self.assertEqual(['ctime_ns'], [key for key in before if before[key] != after[key]])
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))

    def test_replaced_inode_is_rejected_even_when_audio_bytes_match(self):
        def replace(source):
            before = source.stat()
            other = source.with_suffix('.copy')
            other.write_bytes(source.read_bytes())
            os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(other, source)
        self.mutate_once_during_inference(replace)
        with self.assertRaisesRegex(RuntimeError, '录音在处理过程中发生变化'):
            self.process()

    def test_metadata_after_individual_completion_is_accepted_by_merge(self):
        load = batch_export._load_entry
        def change_then_load(entry):
            self.change_metadata(Path(entry['source']))
            return load(entry)
        with patch.object(batch_export, '_load_entry', side_effect=change_then_load):
            self.assertEqual(0, tx.run_files(self.engine, [self.src], self.out, self.args, {}))
        self.assertEqual('completed', tx.read_json(self.out/'批次索引.json')['merged_markdown']['status'])
        # Cross-batch cache reuse remains conservative, as in the prior version.
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))

    def merge_with_late_change(self, mutate):
        second = self.root/'第二份.wav'
        second.write_bytes(self.src.read_bytes())
        self.out.mkdir(exist_ok=True)
        latest = self.out/'合并全文.md'
        latest.write_text('previous complete document')
        load = batch_export._load_entry
        loaded = []
        def change_later(entry):
            data = load(entry)
            if loaded:
                mutate(loaded[0])
            loaded.append(data['source'])
            return data
        with patch.object(batch_export, '_load_entry', side_effect=change_later):
            result = tx.run_files(self.engine, [self.src, second], self.out, self.args, {})
        return result, tx.read_json(self.out/'批次索引.json'), latest

    def test_final_merge_recheck_accepts_metadata_only_change(self):
        result, batch, latest = self.merge_with_late_change(self.change_metadata)
        self.assertEqual(0, result)
        self.assertEqual('completed', batch['merged_markdown']['status'])
        self.assertIn('# 录音转写', latest.read_text())

    def test_final_merge_recheck_rejects_real_same_stat_content_change(self):
        result, batch, latest = self.merge_with_late_change(self.change_bytes_preserving_size_and_mtime)
        self.assertGreater(result, 0)
        self.assertEqual('partial', batch['merged_markdown']['status'])
        self.assertEqual('previous complete document', latest.read_text())

    def test_legacy_marker_without_audio_sha_does_not_relax_identity(self):
        result = self.process()
        marker = result/'.work/completed.json'
        content = tx.read_json(marker)
        content.pop('source_sha256')
        marker.write_text(json.dumps(content))
        self.change_metadata(self.src)
        with self.assertRaises(ValueError):
            batch_export._load_entry({'source': str(self.src), 'result': str(result), 'status': 'completed'})


if __name__ == '__main__':
    unittest.main()
