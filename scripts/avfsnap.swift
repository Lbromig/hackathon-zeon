// avfsnap — list AVFoundation video devices with their uniqueIDs, and grab one frame
// from a device selected BY uniqueID.
//
// Why this tiny tool exists. Every root-free way of reaching a UVC camera on macOS from
// this repo addresses it by an *index* (OpenCV) or by a *name* (ffmpeg), and neither is an
// identity on this bench:
//
//   * Index — macOS renumbers devices on any add/remove/replug, and measured here, even
//     between two consecutive enumerations seconds apart with nothing touched.
//   * Name  — the two D405 arm cameras report the byte-identical name
//     "Intel(R) RealSense(TM) Depth Camera 405  Depth", so a name binds to whichever of
//     them enumerates first.
//   * uniqueID via ffmpeg — ffmpeg's avfoundation input does NOT match uniqueIDs. It
//     silently falls back to the default device instead, which is worse than an error:
//     you get a plausible frame from the wrong camera.
//
// AVFoundation's own API does honour uniqueID (`AVCaptureDevice(uniqueID:)`), and a
// uniqueID embeds the USB locationID, so it is stable per physical port and maps to a USB
// serial via ioreg. That is the only root-free stable key available. librealsense binds by
// serial too, but needs root on macOS to claim the UVC interface.
//
//   swiftc -O scripts/avfsnap.swift -o temp/bin/avfsnap
//   temp/bin/avfsnap list
//   temp/bin/avfsnap grab 0x124300080860b5b out.png [width height]
import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers

// Discovery must name the device types explicitly. `.external` covers the UVC units this
// bench cares about; the Apple types are included ONLY so `list` can show what exists —
// `grab` never opens one unless its uniqueID is passed in by name.
let deviceTypes: [AVCaptureDevice.DeviceType] = [
    .external, .builtInWideAngleCamera, .continuityCamera, .deskViewCamera,
]

func allDevices() -> [AVCaptureDevice] {
    AVCaptureDevice.DiscoverySession(
        deviceTypes: deviceTypes, mediaType: .video, position: .unspecified
    ).devices
}

func listDevices() {
    for d in allDevices() {
        // Tab-separated so the caller can parse it without guessing at column widths;
        // names contain spaces, parentheses and even double spaces.
        print("\(d.uniqueID)\t\(d.modelID)\t\(d.localizedName)")
    }
}

/// Encodes a CGImage to JPEG bytes.
func jpegData(_ image: CGImage, quality: Double) -> Data? {
    let data = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(
        data as CFMutableData, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality:
                                             quality] as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { return nil }
    return data as Data
}

/// Writes frames to stdout as a bare JPEG sequence (MJPEG without the multipart wrapper).
///
/// A JPEG self-delimits (FFD8 ... FFD9), so the reader splits the stream on those markers —
/// the same parse the repo already does for the remote camera driver's MJPEG.
final class FrameStreamer: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let ciContext = CIContext()
    private let quality: Double

    init(quality: Double) { self.quality = quality }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ci = CIImage(cvImageBuffer: pixels)
        guard let cg = ciContext.createCGImage(ci, from: ci.extent),
              let jpeg = jpegData(cg, quality: quality) else { return }
        jpeg.withUnsafeBytes { raw in
            var off = 0
            while off < raw.count {
                let n = write(1, raw.baseAddress!.advanced(by: off), raw.count - off)
                // The consumer went away (backend restarted, worker stopped). Exit rather
                // than spin: the parent owns the lifetime and will respawn if it wants more.
                if n <= 0 { exit(0) }
                off += n
            }
        }
    }
}

/// Collects frames off the capture session and hands back the Nth one.
final class FrameGrabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let wanted: Int
    private var seen = 0
    private let sem = DispatchSemaphore(value: 0)
    private var image: CGImage?
    private let ciContext = CIContext()

    init(settleFrames: Int) { self.wanted = settleFrames }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        seen += 1
        // Keep converting until the settle count is reached: frame 1 off a RealSense is
        // routinely black or saturated before auto-exposure converges, and only the last
        // one is kept.
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ci = CIImage(cvImageBuffer: pixels)
        if let cg = ciContext.createCGImage(ci, from: ci.extent) { image = cg }
        if seen >= wanted, image != nil { sem.signal() }
    }

    func wait(timeout: TimeInterval) -> CGImage? {
        _ = sem.wait(timeout: .now() + timeout)
        return image
    }
}

func err(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

/// Build a running capture session for the device with this uniqueID.
///
/// Returns nil after printing the reason. There is deliberately no fallback to "some other
/// camera": that is the bug this tool exists to prevent.
func openSession(uniqueID: String, width: Int?, height: Int?,
                 delegate: AVCaptureVideoDataOutputSampleBufferDelegate,
                 queueLabel: String) -> (AVCaptureSession, AVCaptureDevice)? {
    guard let device = AVCaptureDevice(uniqueID: uniqueID) else {
        err("no device with uniqueID \(uniqueID)")
        return nil
    }
    let session = AVCaptureSession()
    session.beginConfiguration()
    guard let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
        err("cannot open \(device.localizedName) — another process may hold it")
        return nil
    }
    session.addInput(input)

    let output = AVCaptureVideoDataOutput()
    // Drop late frames rather than queueing them: only the newest frame matters.
    output.alwaysDiscardsLateVideoFrames = true
    output.setSampleBufferDelegate(delegate, queue: DispatchQueue(label: queueLabel))
    guard session.canAddOutput(output) else {
        err("cannot add video output")
        return nil
    }
    session.addOutput(output)

    // Pick the requested resolution from the device's own format list. Leaving it to a
    // session preset lets AVFoundation substitute a nearby size silently — on a D405 the
    // default is 256x144, useless for reading a 20 mm tag.
    //
    // This has to happen INSIDE the begin/commitConfiguration block. Assigning
    // activeFormat is what flips the session to input priority (there is no settable
    // .inputPriority preset on macOS — it is unavailable), but done after commit the
    // running session had already resolved its own format: measured, the D435 RGB module
    // delivered 1920x1080 for a 1280x720 request until this moved up here.
    if let w = width, let h = height {
        let match = device.formats.first { f in
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return Int(d.width) == w && Int(d.height) == h
        }
        if let match, (try? device.lockForConfiguration()) != nil {
            device.activeFormat = match
            device.unlockForConfiguration()
        } else if match == nil {
            err("warning: \(w)x\(h) not offered by \(device.localizedName); using default")
        }
    }
    session.commitConfiguration()
    session.startRunning()
    return (session, device)
}

func grab(uniqueID: String, to path: String, width: Int?, height: Int?) -> Int32 {
    let grabber = FrameGrabber(settleFrames: 15)
    guard let (session, device) = openSession(uniqueID: uniqueID, width: width, height: height,
                                              delegate: grabber, queueLabel: "avfsnap.grab")
    else { return 6 }
    let image = grabber.wait(timeout: 15)
    session.stopRunning()

    guard let image else {
        err("no frame from \(device.localizedName)")
        return 8
    }
    let url = URL(fileURLWithPath: path)
    guard let dest = CGImageDestinationCreateWithURL(
        url as CFURL, UTType.png.identifier as CFString, 1, nil) else { return 9 }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { return 9 }
    print("\(image.width)x\(image.height)\t\(device.localizedName)")
    return 0
}

func stream(uniqueID: String, width: Int?, height: Int?, quality: Double) -> Int32 {
    let streamer = FrameStreamer(quality: quality)
    guard let (_, device) = openSession(uniqueID: uniqueID, width: width, height: height,
                                        delegate: streamer, queueLabel: "avfsnap.stream")
    else { return 6 }
    // Progress goes to stderr: stdout is the JPEG stream and must carry nothing else.
    err("streaming \(device.localizedName) [\(uniqueID)]")
    // Frames are delivered on the session's own queue; park the main thread forever. The
    // process ends when the consumer closes the pipe (see FrameStreamer) or on a signal.
    dispatchMain()
}

let args = CommandLine.arguments
switch args.count >= 2 ? args[1] : "" {
case "list":
    listDevices()
case "grab" where args.count >= 4:
    let w = args.count >= 6 ? Int(args[4]) : nil
    let h = args.count >= 6 ? Int(args[5]) : nil
    exit(grab(uniqueID: args[2], to: args[3], width: w, height: h))
case "stream" where args.count >= 3:
    let w = args.count >= 5 ? Int(args[3]) : nil
    let h = args.count >= 5 ? Int(args[4]) : nil
    let q = args.count >= 6 ? (Double(args[5]) ?? 85) / 100.0 : 0.85
    exit(stream(uniqueID: args[2], width: w, height: h, quality: q))
default:
    print("usage: avfsnap list")
    print("       avfsnap grab   <uniqueID> <out.png> [width height]")
    print("       avfsnap stream <uniqueID> [width height [jpegQuality]]")
    exit(2)
}
