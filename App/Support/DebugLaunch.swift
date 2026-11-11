#if DEBUG
import Foundation

/// Launch arguments for Simulator runs and UI tests. Debug builds only.
///
///   -steerSeedHost user@host:port   add this host (device key login) if missing
///   -steerTrustAny                  trust the host key on first connect
///   -steerPrintKey                  print the device's authorized_keys line
///
/// Plain preferences can be set the usual way, e.g. `-lastProject /path`.
@MainActor
enum DebugLaunch {
    static var arguments: [String] { ProcessInfo.processInfo.arguments }

    static var trustAnyHost: Bool { arguments.contains("-steerTrustAny") }

    static func apply(to app: AppModel) {
        if let index = arguments.firstIndex(of: "-steerSeedHost"), index + 1 < arguments.count {
            let spec = arguments[index + 1]
            let parts = spec.split(separator: "@", maxSplits: 1).map(String.init)
            if parts.count == 2 {
                let hostPort = parts[1].split(separator: ":").map(String.init)
                let address = hostPort[0]
                if !app.hosts.contains(where: { $0.address == address && $0.username == parts[0] }) {
                    var host = HostConfig(name: "Mac", address: address, username: parts[0])
                    host.port = hostPort.count > 1 ? Int(hostPort[1]) ?? 22 : 22
                    app.saveHost(host, password: nil)
                }
            }
        }
        if arguments.contains("-steerPrintKey"), let key = try? app.deviceKey() {
            print("STEER_DEVICE_KEY " + key.authorizedKeysLine)
        }
    }
}
#endif
