"""GUI bridge regressions use real tiny subprocesses, no models or user recordings."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import gui_backend as gui


FAKE_TX = r'''
import argparse,hashlib,json,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('cmd')
for key in ('out','voiceprints','hotwords','engine','model-dir','workers','batch-id-file'):
    p.add_argument(('-o' if key=='out' else '--'+key),dest=key.replace('-','_'))
p.add_argument('--progress-events', action='store_true')
p.add_argument('inputs',nargs='+')
a=p.parse_args()
root=Path(a.out);history=root/'.批次';history.mkdir(exist_ok=True)
index=history/(str(time.time_ns())+'.json')
archive=root/'合并全文';archive.mkdir(exist_ok=True)
doc=archive/'current_合并全文.md';doc.write_text('# 当前批次\n\n原文。\n')
batch={'status':'completed','started_at':time.time(),'index_path':str(index),
'files':[{'source':s,'status':'skipped'} for s in a.inputs],
'merged_markdown':{'status':'completed','path':str(doc),'sha256':hashlib.sha256(doc.read_bytes()).hexdigest()}}
index.write_text(json.dumps(batch));Path(a.batch_id_file).write_text(str(index)+'\n')
for done in (1,2):
    print('TX_PROGRESS '+json.dumps({'type':'status','progress':done*.45,'completed_chunks':done}),flush=True)
print('private transcript must stay in the log',flush=True)
'''


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.program = self.root / 'program'
        self.program.mkdir()
        self.app, self.user = self.root/'app', self.root/'user'
        self.source = self.root/'录音 $(安全) #1.wav'
        self.source.write_bytes(b'fixture')
        (self.program/'selftest.py').write_text("print('verification ok')\n")
        (self.program/'tx.py').write_text(FAKE_TX)
        self.events = []

    def emit(self, kind, **data):
        self.events.append({'type': kind, **data})

    def run_gui(self, inputs=None, **kw):
        with patch.object(gui.shutil, 'which', return_value=None):
            return gui.run_gui(inputs or [self.source], app_dir=self.app, user_dir=self.user,
                               program_dir=self.program, emit=self.emit, **kw)

    def test_folder_and_multiple_files_deduplicate_and_ignore_subfolders(self):
        folder = self.root/'录音目录';folder.mkdir()
        audio=folder/'a.WAV';audio.write_bytes(b'a')
        (folder/'readme.txt').write_text('ignored')
        (folder/'.hidden.wav').write_bytes(b'a')
        (folder/'sub').mkdir();(folder/'sub/b.wav').write_bytes(b'b')
        self.assertEqual([audio], gui.collect_inputs([folder,audio]))

    def test_empty_missing_and_unsupported_inputs_are_actionable(self):
        empty=self.root/'empty';empty.mkdir()
        for paths in ([empty],[self.root/'missing.wav'],[self.program/'tx.py']):
            with self.subTest(paths=paths):
                with self.assertRaises(ValueError):gui.collect_inputs(paths)

    def test_real_process_flow_returns_only_current_verified_markdown(self):
        out=self.user/'转写结果';out.mkdir(parents=True)
        (out/'批次索引.json').write_text('{"status":"completed","old":true}')
        (out/'合并全文.md').write_text('OLD')
        self.assertEqual(0,self.run_gui())
        event=self.events[-1]
        self.assertEqual('complete',event['type'])
        self.assertEqual(1,event['file_count'])
        self.assertIn('当前批次',Path(event['path']).read_text())
        self.assertEqual('OLD',(out/'合并全文.md').read_text())
        done=[e['completed_chunks'] for e in self.events if 'completed_chunks' in e]
        self.assertEqual(2,max(done))
        self.assertNotIn('private transcript',json.dumps(self.events))
        self.assertIn('private transcript',next((self.user/'日志').glob('*.log')).read_text())

    def test_portable_model_root_is_used_for_verification_and_transcription(self):
        bundled = self.root / "portable model directory"
        bundled.mkdir()
        (self.program / 'selftest.py').write_text(
            "import os,sys\nassert sys.argv[sys.argv.index('--model-dir')+1] == os.environ['TX_MODEL_DIR']\n")
        (self.program / 'tx.py').write_text(FAKE_TX +
            "\nimport os\nassert a.model_dir == os.environ['TX_MODEL_DIR']\nassert a.progress_events\n")
        with patch.dict(os.environ, {"TX_MODEL_DIR": str(bundled)}):
            self.assertEqual(0, self.run_gui())

    def test_missing_private_pointer_cannot_use_previous_latest_batch(self):
        (self.program/'tx.py').write_text("print('exit successfully without publishing a batch')\n")
        self.assertEqual(2,self.run_gui())
        self.assertEqual('error',self.events[-1]['type'])
        self.assertFalse(any(e['type']=='complete' for e in self.events))

    def test_concurrent_gui_task_is_rejected_without_starting_a_process(self):
        with gui.TaskLock(self.app/'.gui-task.lock'):
            with patch.object(gui,'run_process') as runner:
                self.assertEqual(2,self.run_gui())
                runner.assert_not_called()
        self.assertIn('已有转写任务',self.events[-1]['message'])

    def test_verification_failure_does_not_start_transcription(self):
        (self.program/'selftest.py').write_text('raise SystemExit(7)\n')
        (self.program/'tx.py').write_text("raise AssertionError('MUST NOT RUN')\n")
        self.assertEqual(2,self.run_gui())
        self.assertEqual('error',self.events[-1]['type'])
        self.assertNotIn('MUST NOT RUN',next((self.user/'日志').glob('*.log')).read_text())

    def test_failed_transcription_cannot_publish_complete(self):
        (self.program/'tx.py').write_text(FAKE_TX+'\nraise SystemExit(2)\n')
        self.assertEqual(2,self.run_gui())
        self.assertEqual('error',self.events[-1]['type'])
        self.assertFalse(any(e['type']=='complete' for e in self.events))

    def test_only_explicit_progress_protocol_reaches_the_ui(self):
        progress = gui.Progress(self.emit, ["/a.wav", "/b.wav"])
        events = [
            {"type": "file", "path": "/a.wav", "state": "running", "progress": .49,
             "completed_chunks": 1, "total_chunks": 2},
            {"type": "status", "progress": .245, "completed_chunks": 1},
            {"type": "file", "path": "/b.wav", "state": "completed", "progress": 1},
            {"type": "file", "path": "/b.wav", "state": "running", "progress": .2},
            {"type": "status", "progress": .1},
        ]
        for event in events:
            progress.feed(gui.Progress.PREFIX + json.dumps(event))
        self.assertEqual(["running", "completed"], [e["state"] for e in self.events if e["type"] == "file"])
        self.assertEqual([.245, .245], [e["progress"] for e in self.events if e["type"] == "status"])
        count = len(self.events)
        for line in ["12:00:01 完成 → private text", "TX_PROGRESS not json", "TX_PROGRESS []",
                     'TX_PROGRESS {"type":"file","path":"/unknown.wav","state":"completed","progress":1}',
                     'TX_PROGRESS {"type":"status","progress":NaN}',
                     'TX_PROGRESS {"type":"status","progress":true}',
                     'TX_PROGRESS {"type":"file","path":"/a.wav","state":"completed","progress":0.3}']:
            progress.feed(line)
        self.assertEqual(count, len(self.events))

    def test_cancel_terminates_running_child_and_returns_cancelled(self):
        (self.program/'tx.py').write_text('import time\ntime.sleep(60)\n')
        cancelled=threading.Event()
        timer=threading.Timer(.3,cancelled.set)
        timer.start();self.addCleanup(timer.cancel)
        started=time.monotonic()
        self.assertEqual(130,self.run_gui(cancelled=cancelled))
        self.assertLess(time.monotonic()-started,8)
        self.assertEqual('cancelled',self.events[-1]['type'])
        # Cancellation releases the GUI task lock for the next run.
        with gui.TaskLock(self.app/'.gui-task.lock'):pass

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS process helpers')
    def test_complete_with_caffeinate_does_not_need_process_listing(self):
        helpers = []
        popen = subprocess.Popen
        def start(command, *args, **kwargs):
            child = popen(command, *args, **kwargs)
            if command[0] == '/usr/bin/caffeinate':
                helpers.append(child)
            return child
        with patch.object(gui.shutil, 'which', return_value='/usr/bin/caffeinate'), \
             patch.object(gui.subprocess, 'Popen', side_effect=start), \
             patch.object(gui.subprocess, 'run', side_effect=PermissionError('ps denied')):
            result = gui.run_gui([self.source], app_dir=self.app, user_dir=self.user,
                                 program_dir=self.program, emit=self.emit)
        self.assertEqual(0, result)
        self.assertEqual('complete', self.events[-1]['type'])
        self.assertEqual(1, len(helpers))
        self.assertIsNotNone(helpers[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(helpers[0].pid, 0)

    @unittest.skipUnless(sys.platform == 'darwin', 'libproc is macOS only')
    def test_libproc_fallback_detects_live_descendant_then_confirms_cleanup(self):
        parent = ('import subprocess,sys\n'
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                  'print(p.pid,flush=True)\n')
        process = subprocess.Popen([sys.executable, '-c', parent], stdout=subprocess.PIPE,
                                   text=True, start_new_session=True)
        child = int(process.stdout.readline())
        process.wait(timeout=2)
        try:
            with patch.object(gui.subprocess, 'run', side_effect=PermissionError('ps denied')):
                with self.assertRaisesRegex(ValueError, '尚不能确认'):
                    gui.confirm_group_stopped(process, timeout=.1)
                gui.stop_process_group(process, grace=.1)
                self.assertFalse(gui.libproc_group_has_live_members(process.pid))
            with patch.object(gui.subprocess, 'run', side_effect=PermissionError('ps denied')), \
                 patch.object(gui, 'libproc_group_has_live_members', side_effect=PermissionError('kernel denied')):
                with self.assertRaises(PermissionError):
                    gui.confirm_group_stopped(process, timeout=.1)
        finally:
            process.stdout.close()
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_owned_helper_ignoring_terminate_is_killed_and_reaped(self):
        code = ('import signal,time\n'
                'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                'print("ready",flush=True)\n'
                'time.sleep(60)\n')
        helper = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual('ready', helper.stdout.readline().strip())
            gui.stop_owned_process(helper, grace=.05)
            self.assertEqual(-signal.SIGKILL, helper.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(helper.pid, 0)
        finally:
            helper.stdout.close()
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=1)

    def test_completed_document_rejects_stale_foreign_and_modified_outputs(self):
        self.assertEqual(0,self.run_gui())
        out=self.user/'转写结果';index=next((out/'.批次').glob('*.json'))
        pointer=self.root/'pointer';pointer.write_text(str(index))
        before=json.loads(index.read_text())
        for change in ('old','foreign','bad_sha','partial','bad_entry','boolean_time','nan_time'):
            with self.subTest(change=change):
                batch=json.loads(json.dumps(before))
                if change=='old':batch['started_at']=1
                elif change=='foreign':batch['files'][0]['source']='/other.wav'
                elif change=='bad_sha':batch['merged_markdown']['sha256']='bad'
                elif change=='partial':batch['merged_markdown']['status']='partial'
                elif change=='bad_entry':batch['files']=[None]
                elif change=='boolean_time':batch['started_at']=True
                else:batch['started_at']=float('nan')
                index.write_text(json.dumps(batch))
                with self.assertRaises(ValueError):
                    gui.completed_document(pointer,out,[self.source],time.time())

    def test_runner_cancel_during_install_stops_its_process_group(self):
        runner=self.root/'runner';runner.mkdir()
        real=Path(gui.__file__).with_name('gui_runner.sh')
        (runner/'gui_runner.sh').write_bytes(real.read_bytes())
        (runner/'common.sh').write_text('PKG_DIR="'+str(runner)+'"\n'
            'select_engine() { :; }\nis_installed() { return 1; }\n')
        (runner/'install.sh').write_text('echo install-diagnostic\nsleep 60\n')
        process=subprocess.Popen(['/bin/bash',str(runner/'gui_runner.sh'),str(self.source)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        self.addCleanup(lambda:process.poll() is None and process.kill())
        first=process.stdout.readline()
        self.assertEqual('status',json.loads(first)['type'])
        time.sleep(.1)
        process.send_signal(signal.SIGINT)
        stdout,stderr=process.communicate(timeout=6)
        self.assertEqual(130,process.returncode)
        self.assertEqual('cancelled',json.loads(stdout.strip())['type'])
        self.assertNotIn('install-diagnostic',stdout)

    def test_runner_cancel_escalates_for_installer_descendants_ignoring_signals(self):
        runner=self.root/'stubborn-runner';runner.mkdir()
        (runner/'gui_runner.sh').write_bytes(Path(gui.__file__).with_name('gui_runner.sh').read_bytes())
        (runner/'common.sh').write_text('PKG_DIR="'+str(runner)+'"\n'
            'select_engine() { :; }\nis_installed() { return 1; }\n')
        pidfile=runner/'child.pid'
        child=runner/'child.sh'
        child.write_text('trap "" INT TERM\necho $$ > "'+str(pidfile)+'"\nsleep 60\n')
        (runner/'install.sh').write_text('trap "" INT TERM\n/bin/bash "'+str(child)+'" &\nwait\n')
        process=subprocess.Popen(['/bin/bash',str(runner/'gui_runner.sh'),str(self.source)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        self.addCleanup(lambda:process.poll() is None and process.kill())
        self.assertEqual('status',json.loads(process.stdout.readline())['type'])
        deadline=time.monotonic()+2
        while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(pidfile.exists())
        descendant=int(pidfile.read_text())
        started=time.monotonic();process.send_signal(signal.SIGINT)
        stdout,stderr=process.communicate(timeout=6)
        self.assertEqual(130,process.returncode,stderr)
        self.assertLess(time.monotonic()-started,5)
        self.assertEqual('cancelled',json.loads(stdout.strip())['type'])
        status=subprocess.run(['ps','-o','stat=','-p',str(descendant)],capture_output=True,text=True).stdout.strip()
        self.assertTrue(not status or status.startswith('Z'),status)

    def test_cancel_reaps_four_workers_ignoring_interrupt_and_terminate(self):
        worker_code='import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
        parent_code=('import signal,subprocess,sys,time,json\n'
            'signal.signal(signal.SIGINT,signal.SIG_IGN)\n'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
            'children=[subprocess.Popen([sys.executable,"-c",'+repr(worker_code)+']) for _ in range(4)]\n'
            'time.sleep(.15)\n'
            'print(json.dumps([p.pid for p in children]),flush=True)\n'
            'time.sleep(60)\n')
        cancelled=threading.Event();pids=[]
        def ready(line):
            pids.extend(json.loads(line));cancelled.set()
        started=time.monotonic()
        result=gui.run_process([sys.executable,'-c',parent_code],io.BytesIO(),cancelled,ready)
        self.assertEqual(130,result)
        self.assertEqual(4,len(pids))
        self.assertLess(time.monotonic()-started,8)
        for pid in pids:
            status=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
            self.assertTrue(not status or status.startswith('Z'),(pid,status))


if __name__=='__main__':
    unittest.main()
