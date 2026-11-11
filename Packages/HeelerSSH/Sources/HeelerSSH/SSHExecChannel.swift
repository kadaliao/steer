import Foundation

/// A long-lived SSH exec channel with no PTY and a stdout-only byte stream.
///
/// Standard error is discarded rather than merged into protocol output. The
/// remote exit status remains available after stdout reaches orderly EOF.
/// The channel shares the package's session-channel I/O and teardown rules:
/// cancellation and read timeouts leave it usable when the native continuation
/// can be reclaimed, while uncertain teardown invalidates the SSH connection.
public final class SSHExecChannel: Sendable {
    private let id: UInt64
    private let driver: SessionDriver

    init(id: UInt64, driver: SessionDriver) {
        self.id = id
        self.driver = driver
    }

    public func write(_ data: Data, timeout: Duration) async throws {
        try await driver.writePTY(id: id, data: data, timeout: timeout)
    }

    /// Reads byte-preserving standard output, or nil after orderly remote EOF.
    public func read(
        maximumBytes: Int = 16 * 1024,
        timeout: Duration
    ) async throws -> Data? {
        try await driver.readExec(id: id, maximumBytes: maximumBytes, timeout: timeout)
    }

    /// Completes the close handshake and returns the remote status after
    /// `read` has reported EOF.
    public func exitStatus(timeout: Duration) async throws -> Int32 {
        try await driver.ptyExitStatus(id: id, timeout: timeout)
    }

    /// Closes only this channel. Idempotent.
    public func close(timeout: Duration) async throws {
        try await driver.closePTY(id: id, timeout: timeout)
    }

    deinit {
        let id = id
        let driver = driver
        Task { try? await driver.closePTY(id: id, timeout: .seconds(2)) }
    }
}
