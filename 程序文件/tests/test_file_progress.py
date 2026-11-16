"""File-slot scheduling and truthful progress with real spawned worker processes."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))
import tx
from test_parallel import TinyEngine


def file_worker(reader, writer, config, tasks):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        try:
            task_id = reader.recv()
        except EOFError:
            return
        if task_id is None:
            return
        task = tasks[task_id]
        delay = config.get('delays', {}).get(Path(task['source']).stem, .04)
        tx.transcribe_chunk(TinyEngine(delay), task)
        writer.send({'kind': 'chunk', 'index': task['index']})


class FileProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / '结果'
        self.args = SimpleNamespace(chunk_min=15, hotword_text='', asr_model='auto',
            model_dir=None, max_spk=10, no_norm=False, merge_thr=.62, enroll_thr=.5,
            part_min=30, keep_work=True, force=False, workers=4, threads=10,
            device='cpu', voiceprints=str(self.root/'声纹'), progress_events=True)
        self.events, self.completion_checks = [], []
        self.sources = []

    def fixture(self, count, chunks=2):
        for i in range(count):
            source = self.root / f'录音{i}.wav'
            sf.write(source, np.full(tx.SR * chunks, .1), tx.SR, subtype='PCM_16')
            self.sources.append(source)

    def run_batch(self, delays=None):
        real = tx.run_parallel_tasks
        def run(tasks, config, workers, **callbacks):
            return real(tasks, {**config, 'delays': delays or {}}, workers,
                        worker_target=file_worker, **callbacks)
        def decode(source, destination, norm=False):
            if not destination.exists():
                destination.write_bytes(Path(source).read_bytes())
        def plan(wav, seconds):
            count = round(sf.info(str(wav)).duration)
            return [(i, i+1) for i in range(count)], count
        def event(reporter, kind, **data):
            self.events.append({'type': kind, 'observed_at': time.time(), **data})
            if kind == 'file' and data.get('state') == 'completed':
                self.completion_checks.append((data['path'], tx.is_completed(
                    Path(data['path']), self.out, self.args)))
        with patch.object(tx, 'run_parallel_tasks', side_effect=run), \
             patch.object(tx, 'decode_to_wav', side_effect=decode), \
             patch.object(tx, 'plan_chunks', side_effect=plan), \
             patch.object(tx.BatchProgress, 'emit', event):
            result = tx.run_files(None, self.sources, self.out, self.args, {})
        return result, tx.read_json(self.out/'批次索引.json')

    def test_four_file_slots_refill_before_other_files_finish(self):
        self.fixture(6)
        result, batch = self.run_batch({'录音0': .6, '录音1': .04, '录音2': .5, '录音3': .55})
        self.assertEqual(0, result)
        file_events = [e for e in self.events if e['type'] == 'file']
        first_running = list(dict.fromkeys(e['path'] for e in file_events if e['state'] == 'running'))
        self.assertEqual([str(p) for p in self.sources[:4]], first_running[:4])
        done = [e for e in file_events if e['state'] == 'completed']
        self.assertEqual(str(self.sources[1]), done[0]['path'])
        fifth_start = next(e for e in file_events if e['path'] == str(self.sources[4]) and e['state'] == 'running')
        self.assertGreaterEqual(fifth_start['observed_at'], done[0]['observed_at'])
        self.assertLess(fifth_start['observed_at'], next(e['observed_at'] for e in done if e['path'] == str(self.sources[0])))
        self.assertEqual(6, len(self.completion_checks))
        self.assertTrue(all(verified for _, verified in self.completion_checks))
        self.assertEqual([str(p) for p in self.sources], [e['source'] for e in batch['files']])
        self.assertEqual(4, len(batch['worker_pids']))
        active = peak = 0
        for _, delta in sorted([(m['started_at'], 1) for m in batch['metrics']] +
                               [(m['finished_at'], -1) for m in batch['metrics']]):
            active += delta
            peak = max(peak, active)
        self.assertEqual(4, peak)
        for path in self.sources:
            updates = [e['progress'] for e in file_events if e['path'] == str(path)]
            self.assertEqual(sorted(updates), updates)
            self.assertIn(.49, updates)
            self.assertEqual(1, updates[-1])
        totals = [e['progress'] for e in self.events if e['type'] == 'status']
        self.assertEqual(sorted(totals), totals)
        self.assertLess(max(totals), 1)

    def test_two_files_use_four_slots_and_preserve_each_timeline(self):
        self.fixture(2)
        result, batch = self.run_batch({'录音0': .3, '录音1': .15})
        self.assertEqual(0, result)
        self.assertEqual(4, len(batch['worker_pids']))
        self.assertLess(max(m['started_at'] for m in batch['metrics']),
                        min(m['finished_at'] for m in batch['metrics']))
        for entry in batch['files']:
            data = tx.read_json(Path(entry['result'])/(Path(entry['source']).stem+'.json'))
            self.assertEqual([.05, 1.05], [sentence['start'] for sentence in data['句子']])

    def test_cache_hits_emit_completed_without_starting_workers(self):
        self.fixture(2)
        self.assertEqual(0, self.run_batch()[0])
        self.events.clear()
        with patch.object(tx, 'make_engine', side_effect=AssertionError('unneeded ASR')):
            result, batch = self.run_batch()
        self.assertEqual(0, result)
        self.assertEqual([], batch['worker_pids'])
        events = [e for e in self.events if e['type'] == 'file']
        self.assertEqual(['queued', 'queued', 'completed', 'completed'], [e['state'] for e in events])
        self.assertEqual(['skipped', 'skipped'], [e['status'] for e in batch['files']])

    def test_export_failure_never_emits_completed_for_that_file(self):
        self.fixture(2)
        finalize = tx.finalize_file
        def fail_one(job, *args):
            if job['src'] == self.sources[0]:
                raise OSError('output disk failed')
            return finalize(job, *args)
        with patch.object(tx, 'finalize_file', side_effect=fail_one):
            result, batch = self.run_batch()
        self.assertEqual(1, result)
        self.assertEqual(['failed', 'completed'], [e['status'] for e in batch['files']])
        states = [e['state'] for e in self.events if e['type'] == 'file' and e['path'] == str(self.sources[0])]
        self.assertNotIn('completed', states)
        self.assertEqual('failed', states[-1])


if __name__ == '__main__':
    unittest.main()
