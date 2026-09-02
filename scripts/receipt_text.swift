// receipt-text: text extraction and preview rendering for receipt files.
//
// Built by scripts/receipt_text.py into scripts/bin/receipt-text with
// `swiftc -O`. Not meant to be run by hand; the python wrapper owns the
// contract. Usage:
//
//   receipt-text <input> [--preview <png>] [--max-pages N]
//
// Prints exactly one JSON object on stdout and exits 0 whenever it could
// start at all:
//
//   {"text": "...", "method": "pdfkit"|"ocr"|"image-ocr"|"none",
//    "pages": N, "preview": true|false, "error": null|"..."}
//
// PDF: PDFKit page text joined with a blank line (method pdfkit). When the
// trimmed text is shorter than 40 characters the pages are rendered at about
// 1600 px wide (up to --max-pages) and read with Vision OCR (method ocr).
// The preview is page 1 rendered to PNG at 1200 px wide.
// Images: Vision OCR of the file (method image-ocr), never a preview file.
// Anything else: method none, error "unsupported".
// Whenever the final text is empty the method is reported as none.
//
// Read-only on the input. The only file written is the preview PNG.

import AppKit
import Foundation
import ImageIO
import PDFKit
import Vision

let previewWidth: CGFloat = 1200
let ocrWidth: CGFloat = 1600
let minPdfkitChars = 40
let imageExtensions: Set<String> = ["png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "bmp", "heic"]

struct Outcome {
    var text = ""
    var method = "none"
    var pages = 0
    var preview = false
    var error: String? = nil
}

func emit(_ outcome: Outcome) -> Never {
    var final = outcome
    if final.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        final.text = ""
        final.method = "none"
    }
    var object: [String: Any] = [
        "text": final.text,
        "method": final.method,
        "pages": final.pages,
        "preview": final.preview,
    ]
    object["error"] = final.error ?? NSNull()
    let out = FileHandle.standardOutput
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]) {
        out.write(data)
    } else {
        // Unreachable with the types above, kept so the wrapper always sees JSON.
        out.write(Data("{\"text\":\"\",\"method\":\"none\",\"pages\":0,\"preview\":false,\"error\":\"json encoding failed\"}".utf8))
    }
    out.write(Data("\n".utf8))
    exit(0)
}

func fail(_ message: String) -> Never {
    emit(Outcome(error: message))
}

// MARK: - Arguments

var inputPath: String? = nil
var previewPath: String? = nil
var maxPages = 3

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let arg = args.removeFirst()
    switch arg {
    case "--preview":
        guard !args.isEmpty else { fail("--preview needs a path") }
        previewPath = args.removeFirst()
    case "--max-pages":
        guard !args.isEmpty, let n = Int(args.removeFirst()) else { fail("--max-pages needs an integer") }
        maxPages = max(1, n)
    default:
        if arg.hasPrefix("--") { fail("unknown option \(arg)") }
        if inputPath != nil { fail("only one input path is accepted") }
        inputPath = arg
    }
}

guard let inputPath = inputPath else { fail("usage: receipt-text <input> [--preview <png>] [--max-pages N]") }
let inputURL = URL(fileURLWithPath: inputPath)
var isDirectory: ObjCBool = false
guard FileManager.default.fileExists(atPath: inputPath, isDirectory: &isDirectory), !isDirectory.boolValue else {
    fail("not found: \(inputPath)")
}
let ext = inputURL.pathExtension.lowercased()

// MARK: - OCR

struct Piece {
    let text: String
    let box: CGRect
}

/// Runs Vision text recognition and rebuilds reading order: observations are
/// grouped into lines by vertical overlap, lines top to bottom, pieces within
/// a line left to right joined by two spaces.
func recognizeText(_ image: CGImage, orientation: CGImagePropertyOrientation) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["sv-SE", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:])
    try handler.perform([request])
    let observations = request.results ?? []
    var pieces: [Piece] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { continue }
        pieces.append(Piece(text: text, box: observation.boundingBox))
    }
    // Vision boxes are normalised with the origin at the bottom left.
    pieces.sort { $0.box.midY > $1.box.midY }
    var lines: [[Piece]] = []
    for piece in pieces {
        if let last = lines.last, let reference = last.first {
            let tolerance = max(reference.box.height, piece.box.height) * 0.5
            if abs(reference.box.midY - piece.box.midY) < tolerance {
                lines[lines.count - 1].append(piece)
                continue
            }
        }
        lines.append([piece])
    }
    return lines.map { line in
        line.sorted { $0.box.minX < $1.box.minX }.map { $0.text }.joined(separator: "  ")
    }.joined(separator: "\n")
}

// MARK: - Rendering

/// Renders a PDF page onto a white RGB bitmap of the given pixel width.
/// PDFKit applies the page rotation and the media box offset itself.
func render(_ page: PDFPage, width: CGFloat) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    let rotation = ((page.rotation % 360) + 360) % 360
    let rotated = rotation == 90 || rotation == 270
    let pageWidth = rotated ? box.height : box.width
    let pageHeight = rotated ? box.width : box.height
    guard pageWidth > 0, pageHeight > 0 else { return nil }
    let scale = width / pageWidth
    let pixelWidth = Int(width.rounded())
    let pixelHeight = max(1, Int((pageHeight * scale).rounded()))
    guard let context = CGContext(
        data: nil,
        width: pixelWidth,
        height: pixelHeight,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else { return nil }
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: pixelWidth, height: pixelHeight))
    context.interpolationQuality = .high
    context.saveGState()
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    context.restoreGState()
    return context.makeImage()
}

func writePNG(_ image: CGImage, to path: String) -> Bool {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
        return false
    }
    CGImageDestinationAddImage(destination, image, nil)
    return CGImageDestinationFinalize(destination)
}

// MARK: - PDF

func extractPDF() -> Never {
    guard let document = PDFDocument(url: inputURL) else { fail("unreadable pdf") }
    if document.isLocked && !document.unlock(withPassword: "") {
        fail("encrypted pdf")
    }
    let count = document.pageCount
    guard count > 0 else { fail("pdf has no pages") }
    var outcome = Outcome()
    var problems: [String] = []

    if let path = previewPath, let first = document.page(at: 0) {
        if let image = render(first, width: previewWidth), writePNG(image, to: path) {
            outcome.preview = true
        } else {
            problems.append("preview render failed")
        }
    }

    var chunks: [String] = []
    for index in 0..<count {
        guard let page = document.page(at: index) else { continue }
        let text = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { chunks.append(text) }
    }
    let joined = chunks.joined(separator: "\n\n")
    if joined.count >= minPdfkitChars {
        outcome.text = joined
        outcome.method = "pdfkit"
        outcome.pages = count
    } else {
        var ocrChunks: [String] = []
        let limit = min(count, maxPages)
        for index in 0..<limit {
            guard let page = document.page(at: index), let image = render(page, width: ocrWidth) else {
                problems.append("page \(index + 1) render failed")
                continue
            }
            do {
                let text = try recognizeText(image, orientation: .up)
                if !text.isEmpty { ocrChunks.append(text) }
            } catch {
                problems.append("page \(index + 1) ocr failed: \(error.localizedDescription)")
            }
        }
        outcome.pages = limit
        if ocrChunks.isEmpty {
            // Keep whatever PDFKit found rather than nothing at all.
            outcome.text = joined
            outcome.method = joined.isEmpty ? "none" : "pdfkit"
            if joined.isEmpty { problems.append("no text found") }
        } else {
            outcome.text = ocrChunks.joined(separator: "\n\n")
            outcome.method = "ocr"
        }
    }
    if !problems.isEmpty { outcome.error = problems.joined(separator: "; ") }
    emit(outcome)
}

// MARK: - Images

func extractImage() -> Never {
    guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
          CGImageSourceGetCount(source) > 0,
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        fail("unreadable image")
    }
    var orientation = CGImagePropertyOrientation.up
    if let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
       let raw = properties[kCGImagePropertyOrientation] as? UInt32,
       let parsed = CGImagePropertyOrientation(rawValue: raw) {
        orientation = parsed
    }
    var outcome = Outcome(method: "image-ocr", pages: 1)
    do {
        outcome.text = try recognizeText(image, orientation: orientation)
        if outcome.text.isEmpty { outcome.error = "no text found" }
    } catch {
        outcome.error = "ocr failed: \(error.localizedDescription)"
    }
    emit(outcome)
}

// MARK: - Dispatch

if ext == "pdf" {
    extractPDF()
} else if imageExtensions.contains(ext) {
    extractImage()
} else {
    fail("unsupported")
}
