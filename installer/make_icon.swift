// Draws the app icon: a spool of filament on a rounded orange tile, with a check badge.
// Usage: swift make_icon.swift <out.iconset folder>
import AppKit

let folder = CommandLine.arguments[1]
try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)

func draw(_ size: CGFloat) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = size / 1024
    // macOS icons keep a margin around the tile.
    let tile = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    let shape = NSBezierPath(roundedRect: tile, xRadius: 185 * s, yRadius: 185 * s)
    NSGradient(starting: NSColor(red: 1.0, green: 0.62, blue: 0.20, alpha: 1),
               ending: NSColor(red: 0.90, green: 0.33, blue: 0.08, alpha: 1))!.draw(in: shape, angle: -90)

    let center = NSPoint(x: 512 * s, y: 530 * s)
    func circle(_ r: CGFloat, _ color: NSColor) {
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - r * s, y: center.y - r * s, width: 2 * r * s, height: 2 * r * s)).fill()
    }
    // Spool: white flange, wound filament in rings, dark hub.
    circle(300, .white)
    circle(255, NSColor(red: 0.16, green: 0.18, blue: 0.22, alpha: 1))
    for (i, r) in stride(from: 245, to: 120, by: -26).enumerated() {
        circle(CGFloat(r), i % 2 == 0 ? NSColor(red: 0.20, green: 0.55, blue: 0.95, alpha: 1)
                                       : NSColor(red: 0.13, green: 0.43, blue: 0.82, alpha: 1))
    }
    circle(110, .white)
    circle(52, NSColor(red: 0.95, green: 0.50, blue: 0.14, alpha: 1))

    // Filament leaving the spool towards the printer.
    let strand = NSBezierPath()
    strand.lineWidth = 26 * s
    strand.lineCapStyle = .round
    strand.move(to: NSPoint(x: 740 * s, y: 560 * s))
    strand.curve(to: NSPoint(x: 820 * s, y: 250 * s), controlPoint1: NSPoint(x: 820 * s, y: 520 * s),
                 controlPoint2: NSPoint(x: 760 * s, y: 330 * s))
    NSColor(red: 0.20, green: 0.55, blue: 0.95, alpha: 1).setStroke()
    strand.stroke()

    // Check badge: the patch is on.
    let badge = NSRect(x: 600 * s, y: 160 * s, width: 250 * s, height: 250 * s)
    NSColor.white.setFill()
    NSBezierPath(ovalIn: badge).fill()
    NSColor(red: 0.18, green: 0.70, blue: 0.35, alpha: 1).setFill()
    NSBezierPath(ovalIn: badge.insetBy(dx: 18 * s, dy: 18 * s)).fill()
    let check = NSBezierPath()
    check.lineWidth = 30 * s
    check.lineCapStyle = .round
    check.lineJoinStyle = .round
    check.move(to: NSPoint(x: 668 * s, y: 287 * s))
    check.line(to: NSPoint(x: 712 * s, y: 243 * s))
    check.line(to: NSPoint(x: 790 * s, y: 330 * s))
    NSColor.white.setStroke()
    check.stroke()
    NSGraphicsContext.restoreGraphicsState()
    return rep
}

for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let name = "icon_\(points)x\(points)" + (scale == 2 ? "@2x" : "") + ".png"
    let data = draw(CGFloat(points * scale)).representation(using: .png, properties: [:])!
    try! data.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name))
}
