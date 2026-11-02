// The installer window. All the work happens in qidi_backend.py; this only shows what it reports.
import AppKit
import Network
import SwiftUI

struct Feature: Identifiable {
    let id: String
    let version: String
    let icon: String
    let title: String
    let summary: String
    let details: String
    let note: String
    let adds: [String]
    let changes: [String]
    let readme: String
    let needs: [String]
    let parts: [String]
    let partIds: [String]
    let hidden: Bool
    let klipper: Bool

    init?(_ item: [String: Any]) {
        guard let id = item["id"] as? String, let title = item["title"] as? String else { return nil }
        self.id = id
        self.title = title
        version = item["version"] as? String ?? ""
        icon = item["icon"] as? String ?? "puzzlepiece"
        summary = item["summary"] as? String ?? ""
        details = item["details"] as? String ?? ""
        note = item["note"] as? String ?? ""
        adds = item["adds"] as? [String] ?? []
        changes = item["changes"] as? [String] ?? []
        readme = item["readme"] as? String ?? ""
        needs = item["needs"] as? [String] ?? []
        parts = item["parts"] as? [String] ?? []
        partIds = item["part_ids"] as? [String] ?? []
        hidden = item["hidden"] as? Bool ?? false
        klipper = item["klipper"] as? Bool ?? false
    }
}

/// A backup of the printer kept on this computer.
struct Backup: Identifiable {
    let id: String
    let time: String
    let features: [String]
    let package: String?
    let firmware: String?

    init?(_ item: [String: Any]) {
        guard let id = item["id"] as? String else { return nil }
        self.id = id
        time = item["time"] as? String ?? id
        features = item["features"] as? [String] ?? []
        package = (item["versions"] as? [String: String])?["package"]
        firmware = item["firmware"] as? String
    }
}

struct FoundPrinter: Identifiable {
    let host: String
    let name: String
    var id: String { host }
}

/// Fields left nil keep what is saved; the backend falls back to the login QIDI ships.
struct Connection: Codable {
    var host: String?
    var user: String?
    var password: String?
    var default_login: Bool?
}

final class LineReader: @unchecked Sendable {
    private var buffer = Data()
    private let onLine: @Sendable (Data) -> Void

    init(onLine: @escaping @Sendable (Data) -> Void) {
        self.onLine = onLine
    }

    func feed(_ chunk: Data) {
        buffer.append(chunk)
        while let end = buffer.firstIndex(of: 10) {
            onLine(buffer.subdata(in: buffer.startIndex..<end))
            buffer.removeSubrange(buffer.startIndex...end)
        }
    }
}

@MainActor
final class Model: ObservableObject {
    @Published var allPackages: [Feature] = []
    var modules: [Feature] { allPackages.filter { !$0.hidden } }
    @Published var risks: [String] = []
    @Published var packageVersion = ""
    @Published var testedFirmware: [String] = []
    @Published var firmware: String?
    @Published var commonAdds: [String] = []
    @Published var commonChanges: [String] = []
    @Published var installedVersions: [String: String] = [:]
    @Published var state = "unknown"
    @Published var host = ""
    @Published var user = ""
    @Published var configured = true
    @Published var customLogin = false
    @Published var networkBlocked = false
    private var askedForNetwork = false
    @Published var installed: Set<String> = []
    @Published var chosen: Set<String> = []
    @Published var found: [FoundPrinter] = []
    @Published var backups: [Backup] = []
    @Published var searching = false
    @Published var logLines: [String] = []
    @Published var busy = false
    @Published var outcome: (ok: Bool, text: String)?

    let backend: String
    let demo: Bool

    init() {
        let root = Bundle.main.object(forInfoDictionaryKey: "QidiProjectRoot") as? String
            ?? FileManager.default.currentDirectoryPath
        backend = root + "/installer/qidi_backend.py"
        demo = CommandLine.arguments.contains("--demo")
    }

    var canChange: Bool { !busy && (state == "ready" || state == "standby") }
    var hasChanges: Bool { chosen != installed }
    var printerPackage: String? { installedVersions["package"] }

    /// Installed modules whose copy on the printer is older than here, or of unknown version.
    var outdated: Set<String> {
        Set(modules.filter { installed.contains($0.id) && (isOld($0) || $0.partIds.contains { isOld(id: $0) }) }
            .map(\.id))
    }

    /// A package whose copy on the printer is older than here; hidden parts count for the
    /// visible packages that bring them, since updating those copies the parts again.
    private func isOld(_ feature: Feature) -> Bool { installedVersions[feature.id] != feature.version }

    private func isOld(id: String) -> Bool {
        allPackages.first { $0.id == id }.map(isOld) ?? false
    }

    var hasUpdate: Bool { !installed.isEmpty && (!outdated.isEmpty || printerPackage != packageVersion) }

    var isReinstall: Bool { !installed.isEmpty && !hasChanges && outdated.isEmpty }

    var actionTitle: String {
        if installed.isEmpty { return "Установить" }
        if hasChanges { return "Применить" }
        return isReinstall ? "Переустановить" : "Обновить"
    }

    var canRunAction: Bool { canChange && !chosen.isEmpty || canChange && hasChanges }

    /// What the confirmation asks about, module by module.
    var changes: String {
        let names = { [self] (ids: Set<String>) in
            self.modules.filter { ids.contains($0.id) }.map { "• " + $0.title }.joined(separator: "\n")
        }
        var parts: [String] = []
        let added = chosen.subtracting(installed), removed = installed.subtracting(chosen)
        let updated = outdated.intersection(chosen)
        if !added.isEmpty { parts.append("Установить:\n" + names(added)) }
        if !updated.isEmpty { parts.append("Обновить:\n" + names(updated)) }
        if !removed.isEmpty { parts.append("Убрать:\n" + names(removed)) }
        if isReinstall {
            parts.append("Выбранные пакеты будут переустановлены:\n" + names(chosen))
        }
        let restarts = isReinstall ? modules.contains { chosen.contains($0.id) && $0.klipper } : restartsKlipper
        parts.append(restarts ? "Klipper перезапустится." : "Klipper не перезапускается.")
        parts.append("Перед установкой резервная копия принтера сохранится на этом компьютере.")
        return parts.joined(separator: "\n\n")
    }

    func refresh() {
        run(["status"])
    }

    func discover() {
        guard !searching else { return }
        searching = true
        run(["discover"], exclusive: false)
    }

    /// After every action the list of backups may have grown.
    private func afterAction() {
        loadBackups()
    }

    /// All the chosen changes in one run: new and outdated packages are copied, the ones switched
    /// off are removed, and packages that stay as they are are not touched.
    func apply() {
        // Nothing changed and nothing outdated: the chosen packages are put on again.
        let add = isReinstall ? chosen : chosen.subtracting(installed).union(outdated.intersection(chosen))
        let remove = installed.subtracting(chosen)
        startAction(["apply", "--add", add.sorted().joined(separator: ","),
                     "--remove", remove.sorted().joined(separator: ",")])
    }

    func title(_ id: String) -> String {
        allPackages.first { $0.id == id }?.title ?? id
    }

    /// Switching a package on brings the ones it needs; switching it off takes the ones that need it.
    func setChosen(_ id: String, _ on: Bool) {
        if on {
            chosen.insert(id)
            chosen.formUnion(modules.first { $0.id == id }?.needs ?? [])
        } else {
            chosen.remove(id)
            for module in modules where module.needs.contains(id) { chosen.remove(module.id) }
        }
    }

    /// Whether the chosen changes make Klipper restart.
    var restartsKlipper: Bool {
        let touched = chosen.symmetricDifference(installed).union(outdated.intersection(chosen))
        return modules.contains { touched.contains($0.id) && $0.klipper }
    }

    func loadBackups() {
        run(["backups"], exclusive: false)
    }

    func makeBackup() {
        startAction(["backup"])
    }

    /// A backup archive from anywhere, chosen in the Open panel, goes into the list.
    func importBackup() {
        let panel = NSOpenPanel()
        panel.title = "Выбери файл резервной копии"
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.init(filenameExtension: "gz") ?? .data]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        startAction(["import-backup", "--file", url.path])
    }

    func restore(_ backup: Backup) {
        startAction(["restore", "--backup", backup.id])
    }

    var backupFolder: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/QIDI Q2 Patcher/backups/\(host)")
    }

    func diagnose() {
        startAction(["diag"])
    }

    func uninstall() {
        startAction(["apply", "--remove", installed.sorted().joined(separator: ",")])
    }

    func saveConnection(_ connection: Connection) {
        startAction(["save-connection"], input: try? JSONEncoder().encode(connection))
    }

    /// "Q2 · прошивка 01.01.03.02" from QIDI's "QD_Q2_01.01.03.02".
    var firmwareLine: String {
        guard let firmware else { return "QIDI, прошивка неизвестна" }
        let parts = firmware.split(separator: "_")
        if parts.count >= 3, parts[0] == "QD" {
            return "QIDI \(parts[1]) · прошивка \(parts[2...].joined(separator: "_"))"
        }
        return "прошивка " + firmware
    }

    var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    }

    private var accessProbe: NWConnection?

    /// macOS asks for the Local Network permission only when the app itself reaches a local
    /// device: the backend runs in Python, so the app knocks on the printer once on its own.
    /// After a refusal macOS does not ask again; then only the settings page helps.
    func requestLocalNetwork() {
        accessProbe?.cancel()
        let connection: NWConnection
        if host.isEmpty {
            connection = NWConnection(host: "224.0.0.251", port: 5353, using: .udp)
        } else {
            connection = NWConnection(host: NWEndpoint.Host(host), port: 7125, using: .tcp)
        }
        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready, .failed:
                DispatchQueue.main.async {
                    self?.accessProbe?.cancel()
                    self?.accessProbe = nil
                    self?.refresh()
                }
            default:
                break
            }
        }
        accessProbe = connection
        connection.start(queue: .main)
        if host.isEmpty { connection.send(content: Data([0]), completion: .contentProcessed { _ in }) }
    }

    func openNetworkSettings() {
        let page = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"
        if let url = URL(string: page) { NSWorkspace.shared.open(url) }
    }

    func openPanel() {
        if let url = URL(string: "http://\(host):18770") { NSWorkspace.shared.open(url) }
    }

    private func startAction(_ arguments: [String], input: Data? = nil) {
        logLines = []
        outcome = nil
        run(arguments, input: input)
    }

    /// An exclusive command waits for no other; the printer search runs beside the rest.
    private func run(_ arguments: [String], input: Data? = nil, exclusive: Bool = true) {
        if exclusive {
            guard !busy else { return }
            busy = true
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-B", backend] + (demo ? ["--demo"] : []) + arguments
        let output = Pipe(), stdin = Pipe()
        process.standardOutput = output
        process.standardError = output
        process.standardInput = stdin
        let command = arguments[0]
        process.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            DispatchQueue.main.async {
                guard let self else { return }
                if !exclusive {
                    if command == "discover" { self.searching = false }
                    return
                }
                self.busy = false
                if code != 0 && self.outcome == nil && command != "status" {
                    self.outcome = (false, "Действие остановилось с ошибкой, подробности в журнале.")
                }
                if command != "status" {
                    self.refresh()
                    self.afterAction()
                }
            }
        }
        // The handler is called one chunk at a time, so the reader needs no lock of its own.
        let reader = LineReader { [weak self] line in
            DispatchQueue.main.async { self?.receive(line) }
        }
        output.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            reader.feed(chunk)
        }
        do {
            try process.run()
            if let input { stdin.fileHandleForWriting.write(input) }
            try? stdin.fileHandleForWriting.close()
        } catch {
            if exclusive { busy = false } else { searching = false }
            outcome = (false, "Не удалось запустить установщик: \(error.localizedDescription)")
        }
    }

    private func receive(_ line: Data) {
        guard let message = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let type = message["type"] as? String else {
            if let text = String(data: line, encoding: .utf8), !text.isEmpty { logLines.append(text) }
            return
        }
        switch type {
        case "status":
            allPackages = (message["modules"] as? [[String: Any]] ?? []).compactMap(Feature.init)
            risks = message["risks"] as? [String] ?? []
            packageVersion = message["version"] as? String ?? ""
            testedFirmware = message["tested_firmware"] as? [String] ?? []
            firmware = message["firmware"] as? String
            let common = message["common"] as? [String: [String]] ?? [:]
            commonAdds = common["adds"] ?? []
            commonChanges = common["changes"] ?? []
            installedVersions = message["installed_versions"] as? [String: String] ?? [:]
            state = message["state"] as? String ?? "unknown"
            host = message["host"] as? String ?? ""
            user = message["user"] as? String ?? ""
            configured = message["configured"] as? Bool ?? false
            customLogin = message["custom_login"] as? Bool ?? false
            let wasBlocked = networkBlocked
            networkBlocked = message["network_blocked"] as? Bool ?? false
            if networkBlocked && !wasBlocked && !askedForNetwork {
                askedForNetwork = true
                requestLocalNetwork()
            }
            installed = Set(message["features"] as? [String] ?? [])
            chosen = installed
        case "backups":
            backups = (message["items"] as? [[String: Any]] ?? []).compactMap(Backup.init)
        case "printers":
            found = (message["items"] as? [[String: String]] ?? []).compactMap { item in
                item["host"].map { FoundPrinter(host: $0, name: item["name"] ?? $0) }
            }
        case "log":
            logLines.append(message["text"] as? String ?? "")
        case "done":
            outcome = (message["ok"] as? Bool ?? false, message["text"] as? String ?? "")
        default:
            break
        }
    }
}

/// A rounded block of the sidebar with a heading.
struct SidebarSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.caption.bold()).foregroundStyle(.secondary).textCase(.uppercase)
            content
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background.opacity(0.6), in: RoundedRectangle(cornerRadius: 12))
    }
}

/// Adds a printer by address or changes the selected one: address and login in one place.
struct PrinterEditor: View {
    @ObservedObject var model: Model
    @Binding var shown: Bool
    let adding: Bool
    @State private var host = ""
    @State private var standardLogin = true
    @State private var user = ""
    @State private var password = ""

    private var valid: Bool {
        !host.isEmpty && (standardLogin || (!user.isEmpty && (!password.isEmpty || model.customLogin)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(adding ? "Добавить принтер" : "Принтер").font(.headline)
            TextField("IP-адрес, например 192.168.1.50", text: $host)
            Toggle("Стандартный вход QIDI", isOn: $standardLogin)
            if standardLogin {
                Text("Логин и пароль, с которыми QIDI поставляет принтер.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Только если на принтере их меняли.")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("Логин", text: $user)
                SecureField(model.customLogin && !adding ? "Пароль (оставить прежний)" : "Пароль", text: $password)
            }
            HStack {
                Spacer()
                Button("Отмена") { shown = false }
                Button(adding ? "Добавить" : "Сохранить") {
                    var connection = Connection(host: host)
                    if standardLogin {
                        connection.default_login = true
                    } else {
                        connection.user = user
                        connection.password = password.isEmpty ? nil : password
                    }
                    model.saveConnection(connection)
                    shown = false
                }
                .buttonStyle(.borderedProminent)
                .disabled(!valid || model.busy)
            }
        }
        .textFieldStyle(.roundedBorder)
        .padding(16)
        .frame(width: 320)
        .onAppear {
            host = adding ? "" : model.host
            standardLogin = adding || !model.customLogin
            user = adding ? "" : (model.customLogin ? model.user : "")
        }
    }
}

struct PrinterSidebar: View {
    @ObservedObject var model: Model
    // Read once, the warning folds away and stays folded on the next start.
    @AppStorage("risksFolded") private var risksFolded = false
    @State private var adding = false
    @State private var editing = false

    private var stateText: (String, Color) {
        switch model.state {
        case "printing", "paused": return ("Идёт печать, менять нельзя", .orange)
        case "ready", "standby": return ("Подключён", .green)
        case "unavailable": return (model.configured ? "Нет связи" : "Принтер не выбран", .red)
        default: return ("Проверяю...", .secondary)
        }
    }

    /// Found printers plus the chosen one when the search did not see it.
    private var printers: [FoundPrinter] {
        var list = model.found
        if !model.host.isEmpty && !list.contains(where: { $0.host == model.host }) {
            list.insert(FoundPrinter(host: model.host, name: ""), at: 0)
        }
        return list
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("QIDI Q2 Patcher").font(.title2.bold())
                Text("для QIDI Q2 и QIDI Box").font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    Circle().fill(stateText.1).frame(width: 8, height: 8)
                    Text(stateText.0).font(.callout).foregroundStyle(.secondary)
                    if model.demo { Text("ДЕМО").font(.caption.bold()).foregroundStyle(.orange) }
                }
            }

            SidebarSection(title: "Принтеры") {
                HStack {
                    Text(model.searching ? "Ищу в сети..." : (model.found.isEmpty ? "В сети не найдено" : "Найдены в сети"))
                        .font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    if model.searching {
                        ProgressView().controlSize(.small)
                    } else {
                        Button {
                            model.discover()
                            model.refresh()
                        } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(.borderless)
                            .help("Искать ещё раз")
                    }
                }
                ForEach(printers) { printer in
                    let current = printer.host == model.host
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Image(systemName: "printer").foregroundStyle(current ? .green : .secondary)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(printer.host).font(.callout.monospacedDigit())
                                Text(current ? model.firmwareLine : "принтер Klipper в сети")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if current {
                                Button { editing = true } label: { Image(systemName: "pencil") }
                                    .buttonStyle(.borderless)
                                    .help("Изменить адрес и вход")
                                    .popover(isPresented: $editing, arrowEdge: .trailing) {
                                        PrinterEditor(model: model, shown: $editing, adding: false)
                                    }
                            } else {
                                Button("Выбрать") { model.saveConnection(Connection(host: printer.host)) }
                                    .controlSize(.small)
                                    .disabled(model.busy)
                            }
                        }
                        if current && model.customLogin {
                            Label("Свой вход: \(model.user)", systemImage: "person.crop.circle")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .padding(8)
                    .background(current ? Color.green.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
                }
                Button { adding = true } label: {
                    Label("Добавить принтер", systemImage: "plus.circle").font(.callout)
                }
                .buttonStyle(.borderless)
                .popover(isPresented: $adding, arrowEdge: .trailing) {
                    PrinterEditor(model: model, shown: $adding, adding: true)
                }
            }

            if model.networkBlocked {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Нет доступа к локальной сети", systemImage: "wifi.exclamationmark")
                        .font(.callout.bold())
                        .foregroundStyle(.red)
                    Text("macOS не пускает программу к принтеру. Нажми «Разрешить» и согласись в окне macOS. Если окно не появилось, доступ уже запрещён: включи «QIDI Q2 Patcher» в настройках «Локальная сеть».")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Разрешить") { model.requestLocalNetwork() }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        Button("Открыть настройки") { model.openNetworkSettings() }
                            .controlSize(.small)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
            }

            Spacer(minLength: 0)

            VStack(alignment: .leading, spacing: 6) {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { risksFolded.toggle() }
                } label: {
                    HStack {
                        Label("Риски", systemImage: "exclamationmark.triangle.fill")
                            .font(.callout.bold())
                            .foregroundStyle(.orange)
                        Spacer()
                        Text(risksFolded ? "Показать" : "Свернуть").font(.caption).foregroundStyle(.secondary)
                        Image(systemName: risksFolded ? "chevron.down" : "chevron.up")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if !risksFolded { ForEach(model.risks, id: \.self) { risk in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                        Text(risk).fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                } }
            }
            .padding(12)
            .background(.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        }
        .padding(16)
        .frame(width: 290)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(.quaternary.opacity(0.35))
        .onAppear { model.discover() }
    }
}

struct VersionsView: View {
    @ObservedObject var model: Model

    private func row(_ name: String, _ value: String) -> some View {
        HStack {
            Text(name)
            Spacer(minLength: 24)
            Text(value).monospacedDigit().foregroundStyle(.secondary)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Версии").font(.headline)
            row("Программа", model.appVersion)
            row("Пакет здесь", model.packageVersion)
            row("Пакет на принтере", model.installed.isEmpty ? "не установлен" : (model.printerPackage ?? "неизвестна"))
            row("Прошивка принтера", model.firmware ?? "неизвестна")
            row("Проверено на прошивке", model.testedFirmware.joined(separator: ", "))
            if let firmware = model.firmware, !model.testedFirmware.contains(firmware) {
                Label("Эта прошивка не проверялась, модули могут не работать", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
            Divider()
            ForEach(model.modules) { module in
                row(module.title, "v" + module.version
                    + (model.installed.contains(module.id) ? " · на принтере " + (model.installedVersions[module.id] ?? "?") : ""))
            }
        }
        .font(.callout)
        .padding(16)
        .frame(width: 380)
    }
}

struct FileList: View {
    let title: String
    let icon: String
    let items: [String]

    var body: some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                Label(title, systemImage: icon).font(.caption.bold()).foregroundStyle(.secondary)
                ForEach(items, id: \.self) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                        Text(item).fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
            .padding(.top, 2)
        }
    }
}

/// Which module the file help is about; nil shows all of them.
struct FilesTopic: Identifiable {
    let id: String?
}

/// The detailed level: which files on the printer each module adds and which stock files it changes.
struct FilesHelp: View {
    @ObservedObject var model: Model
    let only: String?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(only.map { model.title($0) } ?? "Справка по пакетам").font(.title3.bold())
                Spacer()
                Button("Готово") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(model.allPackages.filter { only == nil || $0.id == only }) { module in
                        VStack(alignment: .leading, spacing: 8) {
                            if only == nil {
                                Label(module.title, systemImage: module.hidden ? "shippingbox" : module.icon)
                                    .font(.headline)
                            }
                            ReadmeView(text: module.readme, skipTitle: true)
                            if !module.parts.isEmpty {
                                Text("Вместе с ним ставится: " + module.parts.joined(separator: ", "))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            FileList(title: "Добавляет на принтер", icon: "plus.square", items: module.adds)
                            FileList(title: "Меняет", icon: "pencil.line", items: module.changes)
                        }
                        if only == nil { Divider() }
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        Label("При любой установке", systemImage: "square.stack").font(.headline)
                        FileList(title: "Добавляет", icon: "plus.square", items: model.commonAdds)
                        FileList(title: "Меняет", icon: "pencil.line", items: model.commonChanges)
                    }
                    Text("Проверено на прошивке " + model.testedFirmware.joined(separator: ", ")
                         + ". Файлы box_*.so и прошивка контроллера Box не меняются.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollIndicators(.never)
        }
        .padding(20)
        .frame(width: 560, height: 600)
    }
}

/// A package README, drawn line by line: headings, list items and plain paragraphs.
struct ReadmeView: View {
    let text: String
    let skipTitle: Bool

    private func inline(_ line: String) -> Text {
        Text((try? AttributedString(markdown: line)) ?? AttributedString(line))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(text.components(separatedBy: "\n").enumerated()), id: \.offset) { index, line in
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("# ") {
                    if !skipTitle { Text(trimmed.dropFirst(2)).font(.title3.bold()) }
                } else if trimmed.hasPrefix("## ") {
                    Text(trimmed.dropFirst(3)).font(.headline).padding(.top, 4)
                } else if trimmed.hasPrefix("- ") {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                        inline(String(trimmed.dropFirst(2))).fixedSize(horizontal: false, vertical: true)
                    }
                } else if !trimmed.isEmpty {
                    inline(trimmed)
                        .font(trimmed.hasPrefix("|") ? .system(.caption, design: .monospaced) : .body)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .font(.callout)
        .textSelection(.enabled)
    }
}

struct BackupsView: View {
    @ObservedObject var model: Model
    @Environment(\.dismiss) private var dismiss
    @State private var restoring: Backup?

    private func modules(_ backup: Backup) -> String {
        backup.features.isEmpty ? "ничего не установлено"
            : backup.features.map { model.title($0) }.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Резервные копии").font(.title3.bold())
                Spacer()
                Button("Готово") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            Text("Перед каждой установкой программа сама сохраняет копию всего, что патч может изменить: настройки Klipper, файлы пакетов, панель, кнопку в Fluidd и окна экрана. Копии лежат на этом компьютере. Копию, сделанную раньше и перенесённую в другое место, можно загрузить из файла. Восстановление возвращает принтер к выбранной копии и перезапускает Klipper.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Сделать копию сейчас") { model.makeBackup() }
                    .disabled(!model.canChange)
                Button("Загрузить копию из файла…") { model.importBackup() }
                    .disabled(model.busy || model.host.isEmpty)
                Button("Показать в Finder") { NSWorkspace.shared.open(model.backupFolder) }
                    .disabled(model.backups.isEmpty)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
            }
            if let outcome = model.outcome {
                Label(outcome.text, systemImage: outcome.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(outcome.ok ? .green : .red)
                    .font(.callout)
            }
            ScrollView {
                VStack(spacing: 8) {
                    if model.backups.isEmpty {
                        Text("Копий пока нет.").foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    ForEach(model.backups) { backup in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(backup.time).font(.headline.monospacedDigit())
                                    if backup.id.hasSuffix("_imported") { Badge(text: "из файла", color: .blue) }
                                }
                                Text(modules(backup)).font(.callout).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                Text("Пакет " + (backup.package.map { "v" + $0 } ?? "без версии")
                                     + " · " + (backup.firmware ?? "прошивка неизвестна"))
                                    .font(.caption).foregroundStyle(.tertiary)
                            }
                            Spacer()
                            Button("Восстановить") { restoring = backup }
                                .buttonStyle(.borderedProminent)
                                .tint(.orange)
                                .disabled(!model.canChange)
                        }
                        .padding(10)
                        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
                    }
                }
            }
            .scrollIndicators(.never)
        }
        .padding(20)
        .frame(width: 520, height: 520)
        .onAppear { model.loadBackups() }
        .alert(item: $restoring) { backup in
            Alert(title: Text("Вернуть принтер к копии \(backup.time)?"),
                  message: Text("Будет: " + modules(backup) + ". Всё, что патч добавил после этой копии, удалится. Klipper перезапустится."),
                  primaryButton: .destructive(Text("Восстановить")) { model.restore(backup) },
                  secondaryButton: .cancel(Text("Отмена")))
        }
    }
}

struct Badge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2.bold())
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(color)
            .background(color.opacity(0.16), in: Capsule())
    }
}

struct ModuleRow: View {
    let feature: Feature
    let installed: Bool
    let installedVersion: String?
    let showFiles: (String) -> Void
    @Binding var isOn: Bool
    @State private var expanded = false

    private var outdated: Bool { installed && installedVersion != feature.version }

    /// What happens to this module on the next install, shown by the colour of the card.
    private var change: (text: String, color: Color)? {
        if isOn && !installed { return ("будет установлен", .blue) }
        if !isOn && installed { return ("будет удалён", .red) }
        if isOn && outdated { return ("будет обновлён", .orange) }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Image(systemName: feature.icon)
                    .font(.title3)
                    .foregroundStyle(.tint)
                    .frame(width: 26)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(feature.title).font(.headline)
                        Text("v" + feature.version).font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
                    }
                    Text(feature.summary).font(.callout).foregroundStyle(.secondary)
                    HStack(spacing: 6) {
                        if installed && !outdated { Badge(text: "установлен", color: .green) }
                        if outdated {
                            Badge(text: "на принтере " + (installedVersion.map { "v" + $0 } ?? "старая версия"),
                                  color: .orange)
                        }
                        if let change { Badge(text: change.text, color: change.color) }
                    }
                }
                Spacer()
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() }
                } label: {
                    Label("Подробнее", systemImage: expanded ? "chevron.up" : "chevron.down")
                        .labelStyle(.titleAndIcon)
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                Toggle("", isOn: $isOn).toggleStyle(.switch).labelsHidden()
            }
            if expanded {
                VStack(alignment: .leading, spacing: 6) {
                    Text(feature.details).fixedSize(horizontal: false, vertical: true)
                    if !feature.note.isEmpty {
                        Label(feature.note, systemImage: "checklist").foregroundStyle(.secondary)
                    }
                    Button("Справка и что меняется на принтере") { showFiles(feature.id) }
                        .buttonStyle(.link)
                        .font(.caption)
                }
                .font(.callout)
                .padding(.leading, 38)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background((change?.color ?? (installed ? .green : .gray)).opacity(change == nil && !installed ? 0.06 : 0.09),
                    in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder((change?.color ?? .green).opacity(change != nil ? 0.7 : (installed ? 0.35 : 0)), lineWidth: 1.5))
    }
}

struct ContentView: View {
    @StateObject private var model = Model()
    @State private var confirmApply = false
    @State private var confirmRemove = false
    @State private var showVersions = false
    @State private var filesFor: FilesTopic?
    @State private var showBackups = false

    private func binding(_ id: String) -> Binding<Bool> {
        Binding(get: { model.chosen.contains(id) },
                set: { on in
                    model.setChosen(id, on)
                })
    }

    var body: some View {
        HStack(spacing: 0) {
            PrinterSidebar(model: model)
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Модули").font(.title2.bold())
                    Spacer()
                    Button { showBackups = true } label: {
                        Label("Восстановить", systemImage: "clock.arrow.circlepath")
                    }
                    .buttonStyle(.bordered)
                    .tint(.orange)
                    .foregroundStyle(.orange)
                    .help("Резервные копии и восстановление")
                    Button { filesFor = FilesTopic(id: nil) } label: { Image(systemName: "questionmark.circle") }
                        .buttonStyle(.borderless)
                        .help("Справка по пакетам")
                    Button("v" + model.packageVersion) { showVersions = true }
                        .buttonStyle(.borderless)
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .help("Версии")
                        .popover(isPresented: $showVersions, arrowEdge: .bottom) { VersionsView(model: model) }
                }
                if model.hasUpdate {
                    HStack {
                        Image(systemName: "arrow.down.circle.fill")
                        Text("На принтере пакет " + (model.printerPackage.map { "v" + $0 } ?? "неизвестной версии")
                             + ", здесь v" + model.packageVersion + ". Можно обновить модулей: \(max(model.outdated.count, 1)).")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
                }
                // A scroll bar that takes room would shift the cards whenever a description opens.
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(model.modules) { feature in
                            ModuleRow(feature: feature, installed: model.installed.contains(feature.id),
                                      installedVersion: model.installedVersions[feature.id],
                                      showFiles: { filesFor = FilesTopic(id: $0) },
                                      isOn: binding(feature.id))
                                .disabled(!model.canChange)
                        }
                    }
                }
                .scrollIndicators(.never)

                if let outcome = model.outcome {
                    Label(outcome.text, systemImage: outcome.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(outcome.ok ? .green : .red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !model.logLines.isEmpty {
                    ScrollView {
                        Text(model.logLines.joined(separator: "\n"))
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                    }
                    .frame(height: 100)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
                }

                if model.isReinstall && !model.busy {
                    Text("Выбранные пакеты будут переустановлены")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                HStack {
                    Button("Диагностика") { model.diagnose() }
                        .disabled(!model.canChange || model.installed.isDisjoint(with: ["bambu", "nfc", "spools", "usage"]))
                    Button("Открыть панель") { model.openPanel() }
                        .disabled(model.host.isEmpty
                                  || model.installed.isDisjoint(with: ["bambu", "nfc", "usage", "guide"]))
                    Button("Удалить всё", role: .destructive) { confirmRemove = true }
                        .disabled(!model.canChange || model.installed.isEmpty)
                    Spacer()
                    if model.busy { ProgressView().controlSize(.small) }
                    Button(model.actionTitle) { confirmApply = true }
                        .buttonStyle(.borderedProminent)
                        .tint(model.actionTitle == "Обновить" ? .orange : .accentColor)
                        .disabled(!model.canRunAction)
                        .help(model.isReinstall ? "Выбранные пакеты будут переустановлены" : "")
                        .fixedSize()
                }
            }
            .padding(18)
            .frame(width: 600)
        }
        .frame(height: 760)
        .onAppear { model.refresh() }
        .sheet(item: $filesFor) { topic in FilesHelp(model: model, only: topic.id) }
        .sheet(isPresented: $showBackups) { BackupsView(model: model) }
        .alert(model.actionTitle + "?", isPresented: $confirmApply) {
            Button(model.actionTitle) { model.apply() }
            Button("Отмена", role: .cancel) {}
        } message: {
            Text(model.changes)
        }
        .alert("Удалить всё с принтера?", isPresented: $confirmRemove) {
            Button("Удалить", role: .destructive) { model.uninstall() }
            Button("Отмена", role: .cancel) {}
        } message: {
            Text("Все пакеты будут удалены. Klipper перезапустится. Твои материалы и цвета останутся на принтере.")
        }
    }
}

@main
struct QidiInstallerApp: App {
    var body: some Scene {
        WindowGroup("QIDI Q2 Patcher") { ContentView() }
            .windowResizability(.contentSize)
    }
}
