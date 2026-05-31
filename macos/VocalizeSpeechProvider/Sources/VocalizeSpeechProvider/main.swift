import AVFoundation
import Foundation
import NIOCore
import Speech
import Vapor

let providerAPIVersion = "1.0"

struct ProviderCapabilities: Content {
    struct SpeechCapability: Content {
        var realtime: Bool
        var inputEncoding: String?
        var inputSampleRate: Int?
        var outputEncoding: String?
        var outputSampleRate: Int?
        var languages: [String]

        enum CodingKeys: String, CodingKey {
            case realtime
            case inputEncoding = "input_encoding"
            case inputSampleRate = "input_sample_rate"
            case outputEncoding = "output_encoding"
            case outputSampleRate = "output_sample_rate"
            case languages
        }
    }

    struct Permissions: Content {
        var speechRecognition: String
        var microphone: String
        var ttsVoicesAvailable: Int

        enum CodingKeys: String, CodingKey {
            case speechRecognition = "speech_recognition"
            case microphone
            case ttsVoicesAvailable = "tts_voices_available"
        }
    }

    var providerApiVersion: String
    var provider: String
    var realtime: Bool
    var stt: SpeechCapability
    var tts: SpeechCapability
    var permissions: Permissions

    enum CodingKeys: String, CodingKey {
        case providerApiVersion = "provider_api_version"
        case provider
        case realtime
        case stt
        case tts
        case permissions
    }
}

final class ProviderState {
    let encoder = JSONEncoder()

    func capabilities() -> ProviderCapabilities {
        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        let voices = AVSpeechSynthesisVoice.speechVoices()
        let languageCodes = Array(Set(voices.map(\.language))).sorted()

        return ProviderCapabilities(
            providerApiVersion: providerAPIVersion,
            provider: "macos-native",
            realtime: true,
            stt: .init(
                realtime: true,
                inputEncoding: "pcm_s16le",
                inputSampleRate: 16_000,
                outputEncoding: nil,
                outputSampleRate: nil,
                languages: languageCodes
            ),
            tts: .init(
                realtime: true,
                inputEncoding: nil,
                inputSampleRate: nil,
                outputEncoding: "pcm_s16le",
                outputSampleRate: 24_000,
                languages: languageCodes
            ),
            permissions: .init(
                speechRecognition: describeSpeechStatus(speechStatus),
                microphone: describeAVStatus(micStatus),
                ttsVoicesAvailable: voices.count
            )
        )
    }

    func sendJSON(_ ws: WebSocket, _ value: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value)
        else {
            return
        }
        ws.send(String(decoding: data, as: UTF8.self))
    }

    func sendBinary(_ ws: WebSocket, _ bytes: [UInt8]) {
        ws.send(raw: Data(bytes), opcode: .binary)
    }
}

final class SpeechRecognitionSession {
    private let ws: WebSocket
    private let state: ProviderState
    private let request = SFSpeechAudioBufferRecognitionRequest()
    private var task: SFSpeechRecognitionTask?
    private var started = false

    init(ws: WebSocket, state: ProviderState) {
        self.ws = ws
        self.state = state
        request.shouldReportPartialResults = true
    }

    func start(language: String?) {
        guard !started else { return }
        started = true

        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            state.sendJSON(ws, [
                "type": "error",
                "fatal": true,
                "message": "Speech Recognition permission is not authorized"
            ])
            return
        }

        let locale = Locale(identifier: normalizeLanguage(language))
        guard let recognizer = SFSpeechRecognizer(locale: locale),
              recognizer.isAvailable
        else {
            state.sendJSON(ws, [
                "type": "error",
                "fatal": true,
                "message": "Speech recognizer is unavailable for \(locale.identifier)"
            ])
            return
        }

        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }
            if let error {
                self.state.sendJSON(self.ws, [
                    "type": "error",
                    "fatal": true,
                    "message": error.localizedDescription
                ])
                return
            }
            guard let result else { return }
            self.state.sendJSON(self.ws, [
                "type": "transcript",
                "text": result.bestTranscription.formattedString,
                "is_final": result.isFinal,
                "confidence": 0.0,
                "start_time": 0.0,
                "end_time": 0.0,
                "utterance_id": 0,
                "language": locale.identifier
            ])
        }
    }

    func appendPCM16Mono(_ data: ByteBuffer) {
        guard started else { return }
        var copy = data
        guard let bytes = copy.readBytes(length: copy.readableBytes), !bytes.isEmpty else {
            return
        }
        guard let buffer = makePCMBuffer(bytes: bytes, sampleRate: 16_000) else {
            state.sendJSON(ws, [
                "type": "error",
                "fatal": true,
                "message": "invalid pcm_s16le audio frame"
            ])
            return
        }
        request.append(buffer)
    }

    func finishAudio() {
        request.endAudio()
    }

    func stop() {
        request.endAudio()
        task?.cancel()
        task = nil
    }

    private func normalizeLanguage(_ language: String?) -> String {
        switch language?.lowercased() {
        case "zh", "zh-cn", "cmn-hans-cn":
            return "zh-CN"
        case "en", "en-us":
            return "en-US"
        case let value? where !value.isEmpty && value != "auto":
            return value
        default:
            return Locale.current.identifier
        }
    }
}

final class TextToSpeechSession {
    private let ws: WebSocket
    private let state: ProviderState
    private var started = false
    private let lock = NSLock()
    private var pendingUtterances = 0
    private var inputClosed = false
    private var closeSent = false

    init(ws: WebSocket, state: ProviderState) {
        self.ws = ws
        self.state = state
    }

    func start() {
        started = true
    }

    func synthesize(text: String, language: String?) {
        guard started else { return }
        addPending()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let pcm = try synthesizeWithSay(text: text, language: language)
                self.state.sendJSON(self.ws, [
                    "type": "audio_start",
                    "sample_rate": 24_000,
                    "encoding": "pcm_s16le",
                    "channels": 1
                ])
                for chunk in pcm.chunked(size: 8192) {
                    self.state.sendBinary(self.ws, Array(chunk))
                }
                self.state.sendJSON(self.ws, ["type": "audio_end"])
            } catch {
                self.state.sendJSON(self.ws, [
                    "type": "error",
                    "fatal": true,
                    "message": error.localizedDescription
                ])
            }
            self.finishPending()
        }
    }

    func finishInput() {
        lock.lock()
        inputClosed = true
        let shouldClose = pendingUtterances == 0 && !closeSent
        if shouldClose {
            closeSent = true
        }
        lock.unlock()
        if shouldClose {
            ws.close(promise: nil)
        }
    }

    func cancel() {
    }

    private func addPending() {
        lock.lock()
        pendingUtterances += 1
        lock.unlock()
    }

    private func finishPending() {
        lock.lock()
        pendingUtterances = max(0, pendingUtterances - 1)
        let shouldClose = inputClosed && pendingUtterances == 0 && !closeSent
        if shouldClose {
            closeSent = true
        }
        lock.unlock()
        if shouldClose {
            ws.close(promise: nil)
        }
    }

    private func normalizeLanguage(_ language: String) -> String {
        switch language.lowercased() {
        case "zh", "zh-cn", "cmn-hans-cn":
            return "zh-CN"
        case "en", "en-us":
            return "en-US"
        default:
            return language
        }
    }
}

enum TTSError: LocalizedError {
    case sayFailed(Int32)
    case audioReadFailed
    case audioConvertFailed

    var errorDescription: String? {
        switch self {
        case .sayFailed(let code):
            return "macOS say failed with exit code \(code)"
        case .audioReadFailed:
            return "failed to read synthesized macOS speech audio"
        case .audioConvertFailed:
            return "failed to convert synthesized speech to pcm_s16le"
        }
    }
}

func synthesizeWithSay(text: String, language: String?) throws -> [UInt8] {
    let tempURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("vocalize-tts-\(UUID().uuidString).aiff")
    defer { try? FileManager.default.removeItem(at: tempURL) }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/say")
    var args = ["-o", tempURL.path]
    if let voice = voiceName(for: language) {
        args += ["-v", voice]
    }
    args.append(text)
    process.arguments = args
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        throw TTSError.sayFailed(process.terminationStatus)
    }
    return try readPCM16Mono(url: tempURL, targetSampleRate: 24_000)
}

func voiceName(for language: String?) -> String? {
    guard let language, !language.isEmpty, language != "auto" else { return nil }
    let normalized: String
    switch language.lowercased() {
    case "zh", "zh-cn", "cmn-hans-cn":
        normalized = "zh-CN"
    case "en", "en-us":
        normalized = "en-US"
    default:
        normalized = language
    }
    return AVSpeechSynthesisVoice.speechVoices()
        .first(where: { $0.language == normalized })?
        .name
}

func readPCM16Mono(url: URL, targetSampleRate: Double) throws -> [UInt8] {
    let file = try AVAudioFile(forReading: url)
    guard let inputBuffer = AVAudioPCMBuffer(
        pcmFormat: file.processingFormat,
        frameCapacity: AVAudioFrameCount(file.length)
    ) else {
        throw TTSError.audioReadFailed
    }
    try file.read(into: inputBuffer)

    guard let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: targetSampleRate,
        channels: 1,
        interleaved: false
    ),
          let converter = AVAudioConverter(
              from: file.processingFormat,
              to: outputFormat
          )
    else {
        throw TTSError.audioConvertFailed
    }

    let ratio = targetSampleRate / file.processingFormat.sampleRate
    let capacity = AVAudioFrameCount(Double(inputBuffer.frameLength) * ratio) + 1024
    guard let outputBuffer = AVAudioPCMBuffer(
        pcmFormat: outputFormat,
        frameCapacity: capacity
    ) else {
        throw TTSError.audioConvertFailed
    }

    var didProvideInput = false
    var conversionError: NSError?
    converter.convert(to: outputBuffer, error: &conversionError) { _, status in
        if didProvideInput {
            status.pointee = .noDataNow
            return nil
        }
        didProvideInput = true
        status.pointee = .haveData
        return inputBuffer
    }
    if conversionError != nil {
        throw TTSError.audioConvertFailed
    }
    return convertToPCM16Mono(outputBuffer)
}

func makePCMBuffer(bytes: [UInt8], sampleRate: Double) -> AVAudioPCMBuffer? {
    let frameCount = bytes.count / MemoryLayout<Int16>.size
    guard frameCount > 0,
          let format = AVAudioFormat(
              commonFormat: .pcmFormatInt16,
              sampleRate: sampleRate,
              channels: 1,
              interleaved: false
          ),
          let buffer = AVAudioPCMBuffer(
              pcmFormat: format,
              frameCapacity: AVAudioFrameCount(frameCount)
          ),
          let channel = buffer.int16ChannelData?.pointee
    else {
        return nil
    }

    bytes.withUnsafeBytes { raw in
        if let source = raw.baseAddress?.assumingMemoryBound(to: Int16.self) {
            channel.update(from: source, count: frameCount)
        }
    }
    buffer.frameLength = AVAudioFrameCount(frameCount)
    return buffer
}

func convertToPCM16Mono(_ buffer: AVAudioPCMBuffer) -> [UInt8] {
    let frameCount = Int(buffer.frameLength)
    guard frameCount > 0 else { return [] }
    var out = [UInt8]()
    out.reserveCapacity(frameCount * MemoryLayout<Int16>.size)

    if let int16 = buffer.int16ChannelData?.pointee {
        for idx in 0..<frameCount {
            var sample = int16[idx].littleEndian
            withUnsafeBytes(of: &sample) { out.append(contentsOf: $0) }
        }
        return out
    }

    if let float = buffer.floatChannelData?.pointee {
        for idx in 0..<frameCount {
            let clamped = max(-1.0, min(1.0, float[idx]))
            var sample = Int16(clamped * Float(Int16.max)).littleEndian
            withUnsafeBytes(of: &sample) { out.append(contentsOf: $0) }
        }
    }
    return out
}

extension Array {
    func chunked(size: Int) -> [ArraySlice<Element>] {
        stride(from: 0, to: count, by: size).map {
            self[$0..<Swift.min($0 + size, count)]
        }
    }
}

func parseJSON(_ text: String) -> [String: Any] {
    guard let data = text.data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data),
          let dict = object as? [String: Any]
    else {
        return [:]
    }
    return dict
}

func describeSpeechStatus(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch status {
    case .authorized:
        return "authorized"
    case .denied:
        return "denied"
    case .restricted:
        return "restricted"
    case .notDetermined:
        return "not_determined"
    @unknown default:
        return "unknown"
    }
}

func describeAVStatus(_ status: AVAuthorizationStatus) -> String {
    switch status {
    case .authorized:
        return "authorized"
    case .denied:
        return "denied"
    case .restricted:
        return "restricted"
    case .notDetermined:
        return "not_determined"
    @unknown default:
        return "unknown"
    }
}

let env = Environment(name: "production", arguments: CommandLine.arguments)
let app = try await Application.make(env)

let state = ProviderState()
let port = Int(Environment.get("VOCALIZE_SPEECH_PROVIDER_PORT") ?? "8765") ?? 8765
app.http.server.configuration.hostname = "127.0.0.1"
app.http.server.configuration.port = port

app.get("v1", "capabilities") { _ in
    state.capabilities()
}

app.webSocket("v1", "stt", "stream") { _, ws in
    let session = SpeechRecognitionSession(ws: ws, state: state)

    ws.onText { ws, text in
        let message = parseJSON(text)
        switch message["type"] as? String {
        case "start":
            session.start(language: message["language"] as? String)
        case "end_of_utterance":
            session.finishAudio()
        case "stop":
            session.stop()
            ws.close(promise: nil)
        default:
            break
        }
    }

    ws.onBinary { _, bytes in
        session.appendPCM16Mono(bytes)
    }

    ws.onClose.whenComplete { _ in
        session.stop()
    }
}

app.webSocket("v1", "tts", "stream") { _, ws in
    let session = TextToSpeechSession(ws: ws, state: state)

    ws.onText { ws, text in
        let message = parseJSON(text)
        switch message["type"] as? String {
        case "start":
            session.start()
        case "text":
            session.synthesize(
                text: message["text"] as? String ?? "",
                language: message["language"] as? String
            )
        case "stop":
            session.finishInput()
        default:
            break
        }
    }

    ws.onClose.whenComplete { _ in
        session.cancel()
    }
}

do {
    try await app.execute()
    try await app.asyncShutdown()
} catch {
    try? await app.asyncShutdown()
    throw error
}
