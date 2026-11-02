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

    init?(_ item: [String: Any]) {
        guard let id = item["id"] as? String, let title = item["title"] as? String else { return nil }
        self.id = id
        self.title = title
        version = item["version"] as? String ?? ""
        icon = item["icon"] as? String ?? "puzzlepiece"
        summary = item["summary"] as? String ?? ""
        details = item["details"] as? String ?? ""
        note = item["note"] as? String ?? ""
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
    @Published var modules: [Feature] = []
    @Published var risks: [String] = []
    @Published var packageVersion = ""
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
        Set(modules.filter { installed.contains($0.id) && installedVersions[$0.id] != $0.version }.map(\.id))
    }

    var hasUpdate: Bool { !installed.isEmpty && (!outdated.isEmpty || printerPackage != packageVersion) }

    var actionTitle: String {
        if installed.isEmpty { return "Установить" }
        if hasChanges { return "Переустановить" }
        return hasUpdate ? "Обновить" : "Переустановить"
    }

    var canRunAction: Bool { canChange && !(installed.isEmpty && chosen.isEmpty) }

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
        if parts.isEmpty { parts.append("Модули будут поставлены заново в той же версии.") }
        parts.append("Klipper перезапустится.")
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

    func apply() {
        startAction(["apply", "--features", chosen.sorted().joined(separator: ",")])
    }

    func diagnose() {
        startAction(["diag"])
    }

    func uninstall() {
        startAction(["apply", "--features", ""])
    }

    func saveConnection(_ connection: Connection) {
        startAction(["save-connection"], input: try? JSONEncoder().encode(connection))
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
                    self.searching = false
                    return
                }
                self.busy = false
                if code != 0 && self.outcome == nil && command != "status" {
                    self.outcome = (false, "Действие остановилось с ошибкой, подробности в журнале.")
                }
                if command != "status" { self.refresh() }
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
            modules = (message["modules"] as? [[String: Any]] ?? []).compactMap(Feature.init)
            risks = message["risks"] as? [String] ?? []
            packageVersion = message["version"] as? String ?? ""
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
            list.insert(FoundPrinter(host: model.host, name: "добавлен вручную"), at: 0)
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
                                Text(printer.name).font(.caption).foregroundStyle(.secondary)
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
                        if current {
                            Label(model.customLogin ? "Вход: свой логин \(model.user)" : "Вход: стандартный QIDI",
                                  systemImage: "person.crop.circle")
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
            Divider()
            ForEach(model.modules) { module in
                row(module.title, "v" + module.version
                    + (model.installed.contains(module.id) ? " · на принтере " + (model.installedVersions[module.id] ?? "?") : ""))
            }
        }
        .font(.callout)
        .padding(16)
        .frame(width: 340)
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

    private func binding(_ id: String) -> Binding<Bool> {
        Binding(get: { model.chosen.contains(id) },
                set: { on in
                    if on { model.chosen.insert(id) } else { model.chosen.remove(id) }
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
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(model.modules) { feature in
                            ModuleRow(feature: feature, installed: model.installed.contains(feature.id),
                                      installedVersion: model.installedVersions[feature.id],
                                      isOn: binding(feature.id))
                                .disabled(!model.canChange)
                        }
                    }
                }

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
                }
            }
            .padding(18)
            .frame(width: 600)
        }
        .frame(height: 680)
        .onAppear { model.refresh() }
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
            Text("Klipper перезапустится. Твои материалы и цвета останутся на принтере.")
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
