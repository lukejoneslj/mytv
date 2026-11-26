// Plays a channel with AVPlayer (the engine Apple TV uses) and reports stalls.
// usage: swift avplayer_check.swift http://host:8400/live/1.m3u8 [seconds]
import AVFoundation
import Foundation

let url = URL(string: CommandLine.arguments[1])!
let limit = Double(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "60")!
let item = AVPlayerItem(url: url)
let video = AVPlayerItemVideoOutput(pixelBufferAttributes: nil)
item.add(video)
let player = AVPlayer(playerItem: item)
player.isMuted = true
player.play()
var last = -1.0, stuck = 0, maxStuck = 0, sizes: [String] = []
let t0 = Date()
var firstFrame: Double?
var noFrame = 0
Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
    let now = Date().timeIntervalSince(t0)
    let pos = item.currentTime().seconds
    if item.status == .failed { print("FAILED", item.error ?? ""); exit(1) }
    if firstFrame == nil, pos.isFinite, pos != last, last > 0 { firstFrame = now }
    if firstFrame != nil { if abs(pos - last) < 0.2 { stuck += 1 } else { stuck = 0 }; maxStuck = max(maxStuck, stuck) }
    last = pos
    // size of the frame really on screen
    guard let px = video.copyPixelBuffer(forItemTime: item.currentTime(), itemTimeForDisplay: nil) else {
        if firstFrame != nil { noFrame += 1 }
        if now > limit + 5 { print("no frames"); exit(1) }
        return
    }
    let s = "\(CVPixelBufferGetWidth(px))x\(CVPixelBufferGetHeight(px))"
    if sizes.last != s { sizes.append(s); print(String(format: "%6.1fs picture %@", now, s)) }
    if now > limit {
        let errs = item.errorLog()?.events.map { "\($0.errorStatusCode) \($0.errorComment ?? "")" } ?? []
        print(String(format: "started after %.1fs, longest freeze %ds, seconds without a new frame %d, stalls %d, pictures %@, errors %@", firstFrame ?? -1, maxStuck, noFrame,
                     item.accessLog()?.events.reduce(0) { $0 + $1.numberOfStalls } ?? -1, sizes.joined(separator: " > "), errs.description))
        exit(maxStuck > 3 || firstFrame == nil ? 1 : 0)
    }
}
RunLoop.main.run()
