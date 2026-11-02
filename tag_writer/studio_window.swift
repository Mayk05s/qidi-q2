// The window of the plastic check: shows a local address, prints the pressed button, exits.
// A closed window answers "cancel". With "passive" it is a small note in the corner that takes no focus.
// Built by install_studio_check.py.
import AppKit
import WebKit

final class Delegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    let window: NSWindow
    let page: URL

    let passive: Bool

    init(page: URL, passive: Bool) {
        self.page = page
        self.passive = passive
        window = NSWindow(contentRect: passive ? NSRect(x: 0, y: 0, width: 600, height: 325) : NSRect(x: 0, y: 0, width: 760, height: 440),
                          styleMask: [.titled, .closable, .fullSizeContentView],
                          backing: .buffered, defer: false)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "answer")
        let view = WKWebView(frame: window.contentView!.bounds, configuration: config)
        view.autoresizingMask = [.width, .height]
        view.setValue(false, forKey: "drawsBackground")
        window.title = "Хватит ли пластика"
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.level = .floating
        window.delegate = self
        window.contentView!.addSubview(view)
        view.load(URLRequest(url: page))
        // Opens where the user is working: on the display with the pointer, in the current Space.
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        if let area = screen?.visibleFrame {
            window.setFrameOrigin(passive
                ? NSPoint(x: area.maxX - window.frame.width - 16, y: area.maxY - window.frame.height - 16)
                : NSPoint(x: area.midX - window.frame.width / 2, y: area.midY - window.frame.height / 2))
        }
        if passive {
            window.orderFrontRegardless()
        } else {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func finish(_ answer: String) {
        print(answer)
        fflush(stdout)
        exit(0)
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        finish(message.body as? String ?? "cancel")
    }

    func windowWillClose(_ notification: Notification) {
        finish("cancel")
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = Delegate(page: URL(string: CommandLine.arguments[1])!, passive: CommandLine.arguments.count > 2)
app.delegate = delegate
app.run()
