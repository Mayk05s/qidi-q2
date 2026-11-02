// Draws the app icon in the style of QIDI's own apps: white line art of a filament spool
// on a flat blue tile.
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
    func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: x * s, y: y * s) }
    // The flat blue tile and white rounded line art of QIDI's own apps.
    let tile = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    NSColor(srgbRed: 62 / 255, green: 81 / 255, blue: 191 / 255, alpha: 1).setFill()
    NSBezierPath(roundedRect: tile, xRadius: 185 * s, yRadius: 185 * s).fill()

    NSColor.white.setStroke()
    func stroke(_ path: NSBezierPath, _ width: CGFloat) {
        path.lineWidth = width * s
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        path.stroke()
    }
    // A spool seen from the side of the Box: two flanges and the wound filament between them.
    let center = p(452, 540)
    for radius in [250.0, 92.0] as [CGFloat] {
        stroke(NSBezierPath(ovalIn: NSRect(x: center.x - radius * s, y: center.y - radius * s,
                                           width: 2 * radius * s, height: 2 * radius * s)), 46)
    }
    for radius in [190.0, 145.0] as [CGFloat] {
        let ring = NSBezierPath()
        ring.appendArc(withCenter: center, radius: radius * s, startAngle: 110, endAngle: 340)
        stroke(ring, 30)
    }
    // The filament leaves the spool and runs out to the right, as into the feeder.
    let strand = NSBezierPath()
    strand.move(to: p(452, 290))
    strand.line(to: p(700, 290))
    strand.curve(to: p(800, 390), controlPoint1: p(760, 290), controlPoint2: p(800, 330))
    strand.line(to: p(800, 560))
    stroke(strand, 46)
    NSGraphicsContext.restoreGraphicsState()
    return rep
}

for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let name = "icon_\(points)x\(points)" + (scale == 2 ? "@2x" : "") + ".png"
    let data = draw(CGFloat(points * scale)).representation(using: .png, properties: [:])!
    try! data.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name))
}
