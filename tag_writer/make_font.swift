// Renders the characters used on samples into sample_font.json as fine bitmap runs.
// Run once on a Mac: swift make_font.swift > sample_font.json
import AppKit

let chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    + "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    + "0123456789 .,:+-–/°%()"
let em = 64
let font = NSFont(name: "HelveticaNeue-Bold", size: CGFloat(em))!
let pad = 8, width = em * 2, height = em * 2
let baseline = em / 2
var out: [String: Any] = ["em": em, "ascent": Int(font.ascender.rounded())]
var glyphs: [String: Any] = [:]
for ch in chars {
    let text = NSAttributedString(string: String(ch),
        attributes: [.font: font, .foregroundColor: NSColor.white])
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    text.draw(at: NSPoint(x: pad, y: baseline - Int(-font.descender)))
    NSGraphicsContext.restoreGraphicsState()
    var runs: [[Int]] = []  // [row above baseline, x0, x1], y grows upward
    for row in 0..<height {
        var start = -1
        for x in 0...width {
            let on = x < width && (rep.colorAt(x: x, y: row)?.alphaComponent ?? 0) >= 0.5
            if on && start < 0 { start = x }
            if !on && start >= 0 {
                runs.append([height - row - 1 - baseline + Int(-font.descender), start - pad, x - pad])
                start = -1
            }
        }
    }
    glyphs[String(ch)] = ["advance": Double(text.size().width), "runs": runs]
}
out["glyphs"] = glyphs
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
