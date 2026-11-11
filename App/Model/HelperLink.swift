import Foundation
import HeelerSSH

enum LinkError: LocalizedError, Equatable {
    case needsTrust(fingerprint: String)
    case hostKeyChanged(expected: String, actual: String)
    case unreachable(String)
    case authFailed
    case noPassword
    case pythonMissing
    case helperFailed(String)
    case remote(code: String, message: String)
    case closed
    case timeout

    var errorDescription: String? {
        switch self {
        case .needsTrust: String(localized: "第一次连接这台主机，需要确认它的指纹。")
        case .hostKeyChanged(let expected, let actual):
            String(localized: "主机指纹变了：之前是 \(expected)，现在是 \(actual)。如果你重装过系统或换了机器，在主机设置里重置指纹。")
        case .unreachable(let detail): String(localized: "连不上主机：\(detail)")
        case .authFailed: String(localized: "登录失败：检查用户名，或把本机公钥加到主机的 authorized_keys。")
        case .noPassword: String(localized: "还没有填密码。")
        case .pythonMissing: String(localized: "主机上没有 python3，Steer 的小助手需要它。")
        case .helperFailed(let detail): String(localized: "主机助手没能启动：\(detail)")
        case .remote(_, let message): message
        case .closed: String(localized: "连接已断开。")
        case .timeout: String(localized: "主机没有响应。")
        }
    }
}

enum Credential: Sendable {
    case password(String)
    case key(DeviceKey)
}

/// One SSH connection to a host plus the `steerd.py attach` channel on it.
/// Requests are NDJSON lines with an id; replies carry the same id. Events
/// (no id, an `ev` field) stream out through `events`.
actor HelperLink {
    nonisolated let events: AsyncStream<EventFrame>
    private let eventSink: AsyncStream<EventFrame>.Continuation
    nonisolated let closedSignal: AsyncStream<String>
    private let closedSink: AsyncStream<String>.Continuation

    private var connection: SSHConnection?
    private var channel: SSHExecChannel?
    private var writer: AsyncStream<Data>.Continuation?
    private var nextID = 0
    private var waiters: [Int: CheckedContinuation<Data, Error>] = [:]
    private var buffer = Data()
    private var tasks: [Task<Void, Never>] = []
    private(set) var isClosed = false

    static let helperDir = "$HOME/.local/share/steer"

    init() {
        (events, eventSink) = AsyncStream.makeStream(of: EventFrame.self)
        (closedSignal, closedSink) = AsyncStream.makeStream(of: String.self)
    }

    /// Connects, installs or upgrades the helper when needed, and attaches.
    func start(host: HostConfig, credential: Credential, helperScript: Data, helperVersion: String,
               progress: @Sendable (String) async -> Void) async throws -> HelloInfo {
        await progress(String(localized: "正在连接 \(host.address)…"))
        let ssh: SSHConnection
        do {
            guard let port = UInt16(exactly: host.port) else { throw LinkError.unreachable("port") }
            ssh = try await SSHConnection.connect(to: SSHEndpoint(host: host.address, port: port), timeout: .seconds(15))
        } catch let error as LinkError {
            throw error
        } catch {
            throw LinkError.unreachable(Self.describe(error))
        }
        connection = ssh
        let fingerprint = HostFingerprint.display(for: ssh.hostKey.key)
        guard let trusted = host.fingerprint else {
            try? await ssh.close(timeout: .seconds(2))
            throw LinkError.needsTrust(fingerprint: fingerprint)
        }
        guard trusted == fingerprint else {
            try? await ssh.close(timeout: .seconds(2))
            throw LinkError.hostKeyChanged(expected: trusted, actual: fingerprint)
        }
        do {
            switch credential {
            case .password(let password):
                try await ssh.authenticate(username: host.username, password: password, timeout: .seconds(20))
            case .key(let key):
                try await ssh.authenticate(
                    username: host.username, publicKey: key.publicKeyBlob,
                    signer: { data in try key.privateKey.signature(for: data) }, timeout: .seconds(20))
            }
        } catch {
            try? await ssh.close(timeout: .seconds(2))
            throw LinkError.authFailed
        }

        await progress(String(localized: "检查主机助手…"))
        let installed = try await shell(ssh, """
            command -v python3 >/dev/null 2>&1 || { echo NO_PYTHON; exit 0; }; \
            python3 "\(Self.helperDir)/steerd.py" version 2>/dev/null || echo MISSING
            """)
        let reported = installed.trimmingCharacters(in: .whitespacesAndNewlines)
        if reported == "NO_PYTHON" { throw LinkError.pythonMissing }
        if reported != helperVersion {
            await progress(String(localized: "安装主机助手…"))
            let result = try await ssh.execute(
                Self.sh("""
                    mkdir -p "\(Self.helperDir)" && cat > "\(Self.helperDir)/steerd.py.tmp" && \
                    mv "\(Self.helperDir)/steerd.py.tmp" "\(Self.helperDir)/steerd.py" && echo OK
                    """),
                input: helperScript, timeout: .seconds(30))
            guard String(decoding: result.stdout, as: UTF8.self).contains("OK") else {
                throw LinkError.helperFailed(String(decoding: result.stderr, as: UTF8.self))
            }
            if reported != "MISSING" {
                // An older daemon may still be running; ask it to make way.
                _ = try? await shell(ssh, "python3 \"\(Self.helperDir)/steerd.py\" stop")
            }
        }

        await progress(String(localized: "连接主机助手…"))
        let channel: SSHExecChannel
        do {
            channel = try await ssh.openExec(
                command: Self.sh("exec python3 \"\(Self.helperDir)/steerd.py\" attach"), timeout: .seconds(20))
        } catch {
            throw LinkError.helperFailed(Self.describe(error))
        }
        self.channel = channel
        startPumps(channel)
        let hello: HelloInfo = try await call("hello", ["app": "0.1.0"], timeout: .seconds(40))
        return hello
    }

    private func shell(_ ssh: SSHConnection, _ script: String) async throws -> String {
        do {
            let result = try await ssh.execute(Self.sh(script), timeout: .seconds(30))
            return String(decoding: result.stdout, as: UTF8.self)
        } catch {
            throw LinkError.helperFailed(Self.describe(error))
        }
    }

    /// Wraps a POSIX sh script so it runs the same under fish, zsh or bash
    /// login shells. Scripts must not contain single quotes.
    static func sh(_ script: String) -> String {
        "/bin/sh -c '" + script + "'"
    }

    static func describe(_ error: Error) -> String {
        if let ssh = error as? SSHError {
            switch ssh {
            case .connectionFailed: return String(localized: "网络不通")
            case .timedOut: return String(localized: "超时")
            case .authenticationFailed: return String(localized: "认证失败")
            default: return String(describing: ssh)
            }
        }
        return error.localizedDescription
    }

    private func startPumps(_ channel: SSHExecChannel) {
        let (stream, sink) = AsyncStream.makeStream(of: Data.self)
        writer = sink
        tasks.append(Task { [weak self] in
            for await chunk in stream {
                do {
                    try await channel.write(chunk, timeout: .seconds(20))
                } catch {
                    await self?.shutdown(reason: Self.describe(error))
                    return
                }
            }
        })
        tasks.append(Task { [weak self] in
            while !Task.isCancelled {
                do {
                    guard let data = try await channel.read(maximumBytes: 256 * 1024, timeout: .seconds(30)) else {
                        await self?.shutdown(reason: "eof")
                        return
                    }
                    await self?.ingest(data)
                } catch SSHError.timedOut {
                    continue
                } catch {
                    await self?.shutdown(reason: Self.describe(error))
                    return
                }
            }
        })
        tasks.append(Task { [weak self] in
            // Heartbeat: notice a dead connection even when nothing else is sent.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(20))
                guard let self, await !self.isClosed else { return }
                do {
                    let _: Empty = try await self.call("ping", [:], timeout: .seconds(12))
                } catch {
                    await self.shutdown(reason: "heartbeat")
                    return
                }
            }
        })
    }

    private func ingest(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = Data(buffer[buffer.startIndex..<newline])
            buffer.removeSubrange(buffer.startIndex...newline)
            if !line.isEmpty { handle(line) }
        }
    }

    private struct Head: Decodable {
        var id: Int?
        var ok: Bool?
        var ev: String?
        var error: RemoteError?

        struct RemoteError: Decodable {
            var code: String?
            var message: String?
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try? c.decodeIfPresent(Int.self, forKey: .id)
            ok = try? c.decodeIfPresent(Bool.self, forKey: .ok)
            ev = try? c.decodeIfPresent(String.self, forKey: .ev)
            error = try? c.decodeIfPresent(RemoteError.self, forKey: .error)
        }

        enum CodingKeys: String, CodingKey { case id, ok, ev, error }
    }

    private func handle(_ line: Data) {
        let decoder = JSONDecoder()
        guard let head = try? decoder.decode(Head.self, from: line) else { return }
        if head.ev != nil {
            if let frame = try? decoder.decode(EventFrame.self, from: line) {
                eventSink.yield(frame)
            }
            return
        }
        guard let id = head.id, let waiter = waiters.removeValue(forKey: id) else { return }
        if head.ok == true {
            waiter.resume(returning: line)
        } else {
            waiter.resume(throwing: LinkError.remote(code: head.error?.code ?? "error", message: head.error?.message ?? ""))
        }
    }

    private struct Reply<T: Decodable>: Decodable {
        var result: T
    }

    func call<T: Decodable & Sendable>(_ op: String, _ params: [String: JSONValue] = [:],
                                       timeout: Duration = .seconds(60)) async throws -> T {
        guard !isClosed, let writer else { throw LinkError.closed }
        nextID += 1
        let id = nextID
        var object = params
        object["id"] = .number(Double(id))
        object["op"] = .string(op)
        var data = try JSONEncoder().encode(object)
        data.append(0x0A)
        let timer = Task { [weak self] in
            try? await Task.sleep(for: timeout)
            await self?.fail(id, LinkError.timeout)
        }
        defer { timer.cancel() }
        let line: Data = try await withCheckedThrowingContinuation { continuation in
            waiters[id] = continuation
            writer.yield(data)
        }
        return try JSONDecoder().decode(Reply<T>.self, from: line).result
    }

    private func fail(_ id: Int, _ error: Error) {
        waiters.removeValue(forKey: id)?.resume(throwing: error)
    }

    func shutdown(reason: String) async {
        guard !isClosed else { return }
        isClosed = true
        for task in tasks { task.cancel() }
        tasks = []
        writer?.finish()
        for (_, waiter) in waiters { waiter.resume(throwing: LinkError.closed) }
        waiters = [:]
        eventSink.finish()
        closedSink.yield(reason)
        closedSink.finish()
        let channel = channel
        let connection = connection
        self.channel = nil
        self.connection = nil
        try? await channel?.close(timeout: .seconds(2))
        try? await connection?.close(timeout: .seconds(2))
    }
}
