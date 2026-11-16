"""Exercise the native queue/state contract without opening windows or launching jobs."""
import platform
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("xcrun"), "requires macOS Swift compiler")
class NativeUIStateTests(unittest.TestCase):
    def test_file_queue_and_terminal_state_contract(self):
        source = Path(__file__).resolve().parents[1] / "TranscriberApp.swift"
        harness = r'''
import AppKit
import Foundation

@main struct NativeStateChecks {
    @MainActor static func main() throws {
        let files = (1...5).map { URL(fileURLWithPath: "/录音/会议 \($0).wav") }
        func send(_ model: TranscriptionModel, _ value: [String: Any]) throws {
            model.consume(try JSONSerialization.data(withJSONObject: value))
        }
        func file(_ model: TranscriptionModel, _ n: Int, _ state: String, _ progress: Double = 0) throws {
            try send(model, ["type": "file", "path": files[n - 1].path, "state": state, "progress": progress])
        }
        func make() -> TranscriptionModel {
            let model = TranscriptionModel()
            model.files = files
            model.state = .working
            return model
        }
        let model = make()
        for n in 1...4 { try file(model, n, "running", 0.2) }
        precondition(model.displayedFiles == files, "four active files preserve their input order")
        try file(model, 1, "completed", 1)
        precondition(model.displayedFiles == [files[1], files[2], files[3], files[4], files[0]], "the finished file moves below the next waiting file")
        try file(model, 5, "running", 0.1)
        precondition(model.displayedFiles.first == files[1] && model.displayedFiles.last == files[0], "the fifth recording occupies the active queue")
        try file(model, 1, "running", 0.2)
        precondition(model.fileState(files[0]).state == .completed, "late progress cannot undo completed output")
        try file(model, 5, "running", 0.01)
        precondition(model.fileState(files[4]).progress == 0.1, "progress cannot run backward")
        try send(model, ["type": "file", "path": "/other.wav", "state": "completed", "progress": 1])
        precondition(model.fileProgress["/other.wav"] == nil, "a different batch cannot add a row")
        try send(model, ["type": "status", "progress": 1])
        precondition(model.progress == 0.99 && model.state == .working, "100 percent requires successful exit and the result")

        let destination = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".md")
        try Data("# 转写".utf8).write(to: destination)
        defer { try? FileManager.default.removeItem(at: destination) }
        try send(model, ["type": "complete", "path": destination.path, "file_count": 5])
        precondition(model.state == .working, "a completion event alone must not unlock the UI")
        model.finish(exitCode: 0)
        precondition(model.state == .complete && model.progress == 1, "validated success completes the batch")
        precondition(model.displayedFiles == files && model.completedFileCount == 5, "all complete restores original input order")
        try file(model, 1, "failed")
        precondition(model.state == .complete && model.fileState(files[0]).state == .completed, "late events after exit are ignored")

        let interrupted = make()
        try file(interrupted, 1, "completed", 1)
        try file(interrupted, 2, "running", 0.4)
        interrupted.finish(exitCode: 130)
        precondition(interrupted.state == .cancelled && interrupted.resultURL == nil, "interruption cannot show batch success")
        precondition(interrupted.fileState(files[0]).state == .completed && interrupted.fileState(files[1]).state == .stopped, "stop preserves verified file completion and stops active rows")

        let failed = make()
        try file(failed, 2, "running", 0.4)
        try send(failed, ["type": "complete", "path": destination.path])
        failed.finish(exitCode: 2)
        precondition(failed.state == .failed && failed.resultURL == nil && failed.fileState(files[1]).state == .failed, "nonzero exit overrides premature completion")

        let missing = make()
        try send(missing, ["type": "complete", "path": "/missing/transcript.md"])
        precondition(missing.state == .working, "missing output still waits for cleanup")
        missing.finish(exitCode: 0)
        precondition(missing.state == .failed && missing.resultURL == nil, "missing output never counts as complete")
        let removed = make()
        try send(removed, ["type": "complete", "path": destination.path])
        try FileManager.default.removeItem(at: destination)
        removed.finish(exitCode: 0)
        precondition(removed.state == .failed && removed.resultURL == nil, "output removed before process exit cannot count as success")

        let directory = make()
        try send(directory, ["type": "complete", "path": destination.deletingLastPathComponent().path])
        directory.finish(exitCode: 0)
        precondition(directory.state == .failed && directory.resultURL == nil, "a directory is not a Markdown artifact")
        print("Native queue and completion checks passed")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="transcriber-native-") as directory:
            temporary = Path(directory)
            test = temporary / "NativeChecks.swift"
            test.write_text(harness)
            executable = temporary / "NativeChecks"
            build = subprocess.run(
                ["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", "-D", "TRANSCRIBER_TEST",
                 str(source), str(test), "-o", str(executable)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Native queue and completion checks passed", run.stdout)
