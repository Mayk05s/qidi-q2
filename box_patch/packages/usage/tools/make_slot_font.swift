// Renders the slot names for the low-plastic picture into slot_font.json as smooth alpha bitmaps
// at the size they are shown, so the printer only copies them. Run once on a Mac:
// swift tools/make_slot_font.swift > printer/slot_font.json (from the package folder)
import AppKit

let chars = "0123456789ABCDEFGHR"
let font = NSFont(name: "HelveticaNeue-Bold", size: 23)!
let pad = 4
var glyphs: [String: Any] = [:]
for ch in chars {
    let text = NSAttributedString(string: String(ch), attributes: [.font: font, .foregroundColor: NSColor.white])
    let width = Int(text.size().width.rounded(.up)) + pad * 2
    let height = Int((font.ascender - font.descender).rounded(.up)) + pad * 2
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    text.draw(at: NSPoint(x: CGFloat(pad), y: CGFloat(pad) - font.descender))
    NSGraphicsContext.restoreGraphicsState()
    var alpha: [UInt8] = []
    for row in 0..<height {
        for x in 0..<width {
            alpha.append(UInt8(((rep.colorAt(x: x, y: row)?.alphaComponent ?? 0) * 255).rounded()))
        }
    }
    // Rows go from the top; the baseline sits pad + ascender below the first row.
    glyphs[String(ch)] = ["width": width, "height": height, "advance": Double(text.size().width),
                          "baseline": Double(CGFloat(pad) + font.ascender), "alpha": Data(alpha).base64EncodedString()]
}
let out: [String: Any] = ["pad": pad, "cap": Double(font.capHeight), "glyphs": glyphs]
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
