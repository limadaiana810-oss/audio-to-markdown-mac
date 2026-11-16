import AppKit
import SwiftUI
import UniformTypeIdentifiers

private let supportedAudioExtensions: Set<String> = [
    "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma", "mp4", "mov", "amr"
]

enum WorkState { case idle, working, complete, failed, cancelled }

enum FileWorkState: String {
    case queued, preparing, running, exporting, completed, failed, stopped

    var sortRank: Int {
        switch self {
        case .preparing, .running, .exporting: return 0
        case .queued: return 1
        case .failed, .stopped: return 2
        case .completed: return 3
        }
    }

    var isActive: Bool { self == .preparing || self == .running || self == .exporting }
}

struct FileProgress {
    var state: FileWorkState = .queued
    var progress: Double?
}

final class OutputLog: @unchecked Sendable {
    private let lock = NSLock()
    private let handle: FileHandle?
    let url: URL?

    init() {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("录音转写/日志", isDirectory: true)
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let candidate = directory.appendingPathComponent("界面_\(formatter.string(from: Date()))_\(UUID().uuidString.prefix(8)).log")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: candidate.path, contents: nil)
            handle = try FileHandle(forWritingTo: candidate)
            url = candidate
        } catch {
            handle = nil
            url = nil
        }
    }

    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        try? handle?.write(contentsOf: data)
    }

    deinit { try? handle?.close() }
}

@MainActor final class TranscriptionModel: ObservableObject {
    @Published var files: [URL] = []
    @Published var state = WorkState.idle
    @Published var fileProgress: [String: FileProgress] = [:]
    @Published var message = ""
    @Published var progress: Double?
    @Published var completedChunks = 0
    @Published var resultURL: URL?
    @Published var resultCount = 0
    @Published var stopping = false
    @Published var notice: String?
    @Published var logURL: URL?
    private var process: Process?
    private var receivedTerminalEvent = false
    var afterStop: (() -> Void)?
    var isWorking: Bool { state == .working }
    var completedFileCount: Int { files.filter { fileState($0).state == .completed }.count }

    func fileState(_ url: URL) -> FileProgress { fileProgress[url.path] ?? FileProgress() }

    var displayedFiles: [URL] {
        // Keep selection order as the source of truth. Only the working queue is reordered.
        guard state != .idle, state != .complete else { return files }
        return files.enumerated().sorted {
            let left = fileState($0.element).state.sortRank
            let right = fileState($1.element).state.sortRank
            return left == right ? $0.offset < $1.offset : left < right
        }.map(\.element)
    }

    func chooseFiles() {
        guard !isWorking else { return }
        let panel = NSOpenPanel()
        panel.title = "选择录音"
        panel.prompt = "添加录音"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = supportedAudioExtensions.compactMap { UTType(filenameExtension: $0) }
        if panel.runModal() == .OK { add(panel.urls) }
    }

    func chooseFolder() {
        guard !isWorking else { return }
        let panel = NSOpenPanel()
        panel.title = "选择录音文件夹"
        panel.prompt = "添加文件夹"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { add(panel.urls) }
    }

    func add(_ urls: [URL]) {
        guard !isWorking else {
            notice = "请等待本次转写结束后再添加录音。"
            return
        }
        var candidates: [URL] = []
        for raw in urls {
            let url = raw.standardizedFileURL.resolvingSymlinksInPath()
            var directory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) else { continue }
            if directory.boolValue {
                let children = (try? FileManager.default.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles])) ?? []
                candidates += children.filter {
                    supportedAudioExtensions.contains($0.pathExtension.lowercased()) &&
                    (try? $0.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true
                }.sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
            } else if supportedAudioExtensions.contains(url.pathExtension.lowercased()) {
                candidates.append(url)
            }
        }
        var seen = Set(files.map { $0.path })
        let additions = candidates.filter { seen.insert($0.path).inserted }
        if additions.isEmpty {
            notice = candidates.isEmpty ? "没有找到支持的录音文件，请选择录音或包含录音的文件夹。" : "这些录音已经在列表中。"
            return
        }
        files += additions
        resetResult()
    }

    func remove(_ url: URL) {
        guard !isWorking else { return }
        files.removeAll { $0 == url }
        resetResult()
    }

    func clear() {
        guard !isWorking else { return }
        files.removeAll()
        resetResult()
    }

    private func resetResult() {
        state = .idle
        resultURL = nil
        resultCount = 0
        message = ""
        notice = nil
        progress = nil
        completedChunks = 0
        fileProgress = [:]
    }

    func start() {
        guard !files.isEmpty, !isWorking else { return }
        guard let resourceRoot = Bundle.main.resourceURL else {
            fail("找不到程序资源，请重新打开应用。")
            return
        }
        let runner = resourceRoot.appendingPathComponent("程序文件/gui_runner.sh")
        guard FileManager.default.fileExists(atPath: runner.path) else {
            fail("应用文件不完整，请重新安装此应用。")
            return
        }
        state = .working
        fileProgress = Dictionary(uniqueKeysWithValues: files.map { ($0.path, FileProgress()) })
        message = "正在准备转写…"
        notice = nil
        progress = nil
        completedChunks = 0
        stopping = false
        resultURL = nil
        receivedTerminalEvent = false
        let job = Process()
        let stdout = Pipe(), stderr = Pipe()
        let log = OutputLog()
        logURL = log.url
        job.executableURL = URL(fileURLWithPath: "/bin/bash")
        job.arguments = [runner.path] + files.map { $0.path }
        job.standardOutput = stdout
        job.standardError = stderr
        job.standardInput = FileHandle.nullDevice
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        job.environment = environment
        let readers = DispatchGroup()
        readers.enter()
        readers.enter()
        job.terminationHandler = { [weak self] finished in
            readers.notify(queue: .main) {
                Task { @MainActor in self?.finish(exitCode: finished.terminationStatus) }
            }
        }
        do {
            try job.run()
            process = job
        } catch {
            readers.leave()
            readers.leave()
            fail("无法启动转写，请重新打开应用后再试。")
            return
        }
        // Each stream has a dedicated reader; stdout is parsed only after a full UTF-8 line arrives.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var buffer = Data()
            while true {
                let data = stdout.fileHandleForReading.availableData
                if data.isEmpty { break }
                log.append(data)
                buffer.append(data)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = Data(buffer[..<newline])
                    buffer.removeSubrange(...newline)
                    DispatchQueue.main.async { self?.consume(line) }
                }
            }
            if !buffer.isEmpty {
                let last = buffer
                DispatchQueue.main.async { self?.consume(last) }
            }
            readers.leave()
        }
        DispatchQueue.global(qos: .utility).async {
            while true {
                let data = stderr.fileHandleForReading.availableData
                if data.isEmpty { break }
                log.append(data)
            }
            readers.leave()
        }
    }

    func consume(_ line: Data) {
        guard let event = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let type = event["type"] as? String else { return }
        guard isWorking else { return }
        switch type {
        case "file":
            guard let path = event["path"] as? String,
                  files.contains(where: { $0.path == path }),
                  let value = event["state"] as? String,
                  let next = FileWorkState(rawValue: value), next != .stopped else { return }
            var item = fileProgress[path] ?? FileProgress()
            // A delayed progress event must not undo a validated file completion.
            if item.state == .completed && next != .completed && next != .failed { return }
            item.state = next
            if next == .completed { item.progress = 1 }
            else if let number = event["progress"] as? Double, number.isFinite {
                item.progress = min(0.99, max(item.progress ?? 0, max(0, number)))
            }
            fileProgress[path] = item
        case "status":
            if let value = event["message"] as? String, !stopping { message = value }
            if let value = event["progress"] as? Double, value.isFinite { progress = min(0.99, max(progress ?? 0, max(0, value))) }
            if let value = event["completed_chunks"] as? Int { completedChunks = value }
        case "complete":
            guard let path = event["path"] as? String,
                  isDocumentAvailable(URL(fileURLWithPath: path)) else {
                receivedTerminalEvent = true
                resultURL = nil
                message = "转写已经结束，但没有找到完整文档。请重新开始生成。"
                return
            }
            resultURL = URL(fileURLWithPath: path)
            resultCount = event["file_count"] as? Int ?? files.count
            message = "正在完成…"
            progress = 0.99
            receivedTerminalEvent = true
            // Completion is shown only after the backend has exited successfully.
        case "error":
            receivedTerminalEvent = true
            message = event["message"] as? String ?? "转写未完成，请重试。"
            if let path = event["log_path"] as? String { logURL = URL(fileURLWithPath: path) }
            resultURL = nil
        case "cancelled":
            receivedTerminalEvent = true
            stopping = true
            resultURL = nil
            message = event["message"] as? String ?? "已停止，下次可以接着处理。"
        default: break
        }
    }

    private func isDocumentAvailable(_ url: URL) -> Bool {
        (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true
    }

    func finish(exitCode: Int32) {
        process = nil
        if exitCode == 0, let document = resultURL, isDocumentAvailable(document) {
            state = .complete
            message = "转写完成"
            progress = 1
            for file in files { fileProgress[file.path] = FileProgress(state: .completed, progress: 1) }
        } else if stopping || exitCode == 130 {
            state = .cancelled
            message = "已停止，再次开始会接着处理。"
            resultURL = nil
        } else {
            state = .failed
            let incompleteExit = resultURL != nil
            resultURL = nil
            if incompleteExit || !receivedTerminalEvent { message = "转写没有完成，请重试。已完成的内容会保留。" }
        }
        if state != .complete {
            for file in files where fileState(file).state.isActive {
                fileProgress[file.path]?.state = state == .cancelled ? .stopped : .failed
            }
        }
        stopping = false
        let completion = afterStop
        afterStop = nil
        completion?()
    }

    private func fail(_ text: String) {
        state = .failed
        message = text
        resultURL = nil
    }

    func stop() {
        guard isWorking, !stopping else { return }
        stopping = true
        message = "正在停止并保存已完成内容…"
        if let process, process.isRunning { process.interrupt() }
    }

    func openResult() {
        if let resultURL { NSWorkspace.shared.open(resultURL) }
    }

    func revealResult() {
        if let resultURL { NSWorkspace.shared.activateFileViewerSelecting([resultURL]) }
    }

    func saveResult() {
        guard let resultURL else { return }
        let panel = NSSavePanel()
        panel.title = "保存转写文档"
        panel.prompt = "保存"
        let date = DateFormatter()
        date.dateFormat = "yyyy-MM-dd"
        panel.nameFieldStringValue = "录音转写_\(date.string(from: Date())).md"
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        do {
            let data = try Data(contentsOf: resultURL)
            try data.write(to: destination, options: .atomic)
            NSWorkspace.shared.activateFileViewerSelecting([destination])
        } catch {
            notice = "文档没有保存成功，请换一个位置再试。"
        }
    }
}

struct CompactIconButton: View {
    let symbol: String
    let label: String
    var prominent = false
    var disabled = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 28, height: 28)
                .foregroundStyle(prominent ? Color(nsColor: .windowBackgroundColor) : Color.primary)
                .background(prominent ? Color.primary : Color.clear, in: RoundedRectangle(cornerRadius: 7))
                .contentShape(RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .opacity(disabled ? 0.3 : 1)
        .disabled(disabled)
        .help(label)
        .accessibilityLabel(label)
    }
}

struct RecordingRow: View {
    let file: URL
    let item: FileProgress
    let canRemove: Bool
    let remove: () -> Void

    private var color: Color {
        switch item.state {
        case .completed: return .secondary
        case .failed: return .orange
        case .preparing, .running, .exporting: return .primary
        default: return .secondary
        }
    }
    private var symbol: String {
        switch item.state {
        case .completed: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.circle"
        case .stopped: return "pause.circle"
        default: return "waveform"
        }
    }
    private var label: String {
        switch item.state {
        case .queued: return "等待"
        case .preparing: return "准备"
        case .exporting: return "整理"
        case .completed: return "完成"
        case .failed: return "未完成"
        case .stopped: return "已停止"
        case .running:
            if let value = item.progress { return "\(Int(value * 100))%" }
            return "转写中"
        }
    }

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(color)
                .frame(width: 19)
                .accessibilityHidden(true)
            VStack(spacing: 6) {
                HStack(spacing: 8) {
                    Text(file.lastPathComponent)
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(item.state == .completed ? Color.secondary : Color.primary)
                        .lineLimit(1).truncationMode(.middle)
                        .help(file.path)
                    Spacer(minLength: 0)
                    Text(label).font(.system(size: 10)).monospacedDigit()
                        .foregroundStyle(color)
                }
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.07))
                        Capsule().fill(color.opacity(item.state == .completed ? 0.65 : 0.8))
                            .frame(width: geometry.size.width * (item.progress ?? 0))
                    }
                }.frame(height: 2)
                .opacity(item.state == .queued ? 0.4 : 1)
                .accessibilityHidden(true)
            }
            if canRemove {
                Button(action: remove) {
                    Image(systemName: "xmark").font(.system(size: 8, weight: .medium))
                        .foregroundStyle(.tertiary).frame(width: 17, height: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("移除录音")
                .accessibilityLabel("移除 \(file.lastPathComponent)")
            }
        }
        .padding(.horizontal, 12)
        .frame(height: 43)
        .accessibilityElement(children: .contain)
        .accessibilityValue(label)
    }
}

struct TranscriptionView: View {
    @ObservedObject var model: TranscriptionModel

    private var statusTitle: String {
        switch model.state {
        case .idle: return model.files.isEmpty ? "添加录音" : "待转写"
        case .complete: return "转写完成"
        case .failed: return "转写未完成"
        case .cancelled: return "已停止"
        case .working: return model.stopping ? "正在停止" : "总进度"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                CompactIconButton(symbol: "waveform.badge.plus", label: "选择录音", disabled: model.isWorking, action: model.chooseFiles)
                CompactIconButton(symbol: "folder.badge.plus", label: "选择文件夹", disabled: model.isWorking, action: model.chooseFolder)
                if !model.files.isEmpty {
                    Text("\(model.files.count)").font(.system(size: 10)).monospacedDigit()
                        .foregroundStyle(.secondary).padding(.leading, 3)
                        .accessibilityLabel("\(model.files.count) 份录音")
                }
                Spacer()
                CompactIconButton(symbol: "trash", label: "清空录音", disabled: model.isWorking || model.files.isEmpty, action: model.clear)
                if model.isWorking {
                    CompactIconButton(symbol: "stop.fill", label: model.stopping ? "正在停止" : "停止转写", prominent: true, disabled: model.stopping, action: model.stop)
                } else {
                    CompactIconButton(symbol: "play.fill", label: model.state == .cancelled || model.state == .failed ? "继续转写" : "开始转写", prominent: true, disabled: model.files.isEmpty, action: model.start)
                        .keyboardShortcut(.defaultAction)
                }
            }
            .padding(.horizontal, 11).padding(.vertical, 7)
            Divider()
            if model.files.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "waveform").font(.system(size: 25, weight: .ultraLight))
                        .foregroundStyle(.tertiary)
                    Text("添加录音").font(.system(size: 11)).foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(model.displayedFiles, id: \.path) { file in
                            RecordingRow(file: file, item: model.fileState(file), canRemove: !model.isWorking) { model.remove(file) }
                            if file != model.displayedFiles.last {
                                Divider().opacity(0.45).padding(.leading, 40).padding(.trailing, 12)
                            }
                        }
                    }
                }
                .background(Color(nsColor: .controlBackgroundColor).opacity(0.55))
            }
            Divider()
            footer
        }
        .frame(minWidth: 360, minHeight: 280)
        .background(Color(nsColor: .windowBackgroundColor))
        .alert("录音转写", isPresented: Binding(
            get: { model.notice != nil },
            set: { if !$0 { model.notice = nil } }
        )) { Button("好") { model.notice = nil } } message: { Text(model.notice ?? "") }
    }

    private var footer: some View {
        VStack(spacing: 8) {
            HStack(spacing: 7) {
                if model.state == .complete {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.secondary)
                        .font(.system(size: 12))
                } else if model.state == .failed {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                        .font(.system(size: 12))
                }
                Text(statusTitle).font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .help(model.message)
                if model.isWorking || model.state == .cancelled || model.state == .failed {
                    Text("\(model.completedFileCount)/\(model.files.count)")
                        .font(.system(size: 10)).monospacedDigit().foregroundStyle(.tertiary)
                    if let progress = model.progress {
                        Text("\(Int(progress * 100))%")
                            .font(.system(size: 10)).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 0)
                if model.state == .complete {
                    CompactIconButton(symbol: "doc.text", label: "打开 Markdown", action: model.openResult)
                    CompactIconButton(symbol: "square.and.arrow.down", label: "另存为", action: model.saveResult)
                    CompactIconButton(symbol: "folder", label: "在访达中显示", action: model.revealResult)
                } else if model.state == .failed, let logURL = model.logURL {
                    CompactIconButton(symbol: "doc.text.magnifyingglass", label: "查看运行记录") { NSWorkspace.shared.open(logURL) }
                }
            }
            .frame(height: 28)
            if model.isWorking {
                if let progress = model.progress {
                    ProgressView(value: progress).tint(.primary).controlSize(.small)
                } else {
                    ProgressView().progressViewStyle(.linear).tint(.primary).controlSize(.small)
                }
            } else if model.state == .failed {
                Text(model.message).font(.system(size: 10)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 7)
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = TranscriptionModel()
    private var window: NSWindow?
    private var terminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 360),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "录音转写"
        window.minSize = NSSize(width: 360, height: 308)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .visible
        window.collectionBehavior = [.moveToActiveSpace]
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = NSHostingView(rootView: TranscriptionView(model: model))
        window.center()
        self.window = window
        makeMenu()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于录音转写", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出录音转写", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        model.add(filenames.map { URL(fileURLWithPath: $0) })
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        sender.reply(toOpenOrPrint: .success)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        return true
    }

    private func shouldStopForExit() -> Bool {
        let alert = NSAlert()
        alert.messageText = "停止转写并退出？"
        alert.informativeText = "已经完成的内容会保留，下次可以接着处理。"
        alert.addButton(withTitle: "继续转写")
        alert.addButton(withTitle: "停止并退出")
        return alert.runModal() == .alertSecondButtonReturn
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if !model.isWorking { NSApp.terminate(nil); return false }
        guard shouldStopForExit() else { return false }
        model.afterStop = { NSApp.terminate(nil) }
        model.stop()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model.isWorking else { return .terminateNow }
        if terminating { return .terminateCancel }
        guard shouldStopForExit() else { return .terminateCancel }
        terminating = true
        model.afterStop = { sender.reply(toApplicationShouldTerminate: true) }
        model.stop()
        return .terminateLater
    }
}

#if !TRANSCRIBER_TEST
@main struct TranscriberApplication {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.run()
        withExtendedLifetime(delegate) {}
    }
}

#endif
