import CryptoKit
import Foundation
import Security

// Adapted from Heeler (Apache-2.0): the device's Ed25519 SSH key, Keychain
// storage, and OpenSSH-style host key fingerprints.

enum KeychainError: Error, Equatable {
    case unexpectedStatus(OSStatus)
}

/// Generic-password Keychain items under one service, kept on this device only.
struct Keychain: Sendable {
    let service: String

    func read(_ account: String) throws -> Data? {
        var query = baseQuery(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess: return item as? Data
        case errSecItemNotFound: return nil
        default: throw KeychainError.unexpectedStatus(status)
        }
    }

    func write(_ secret: Data, _ account: String) throws {
        var attributes = baseQuery(account)
        attributes[kSecValueData as String] = secret
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem {
            let update = [kSecValueData as String: secret]
            let updateStatus = SecItemUpdate(baseQuery(account) as CFDictionary, update as CFDictionary)
            guard updateStatus == errSecSuccess else { throw KeychainError.unexpectedStatus(updateStatus) }
        } else if status != errSecSuccess {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    func remove(_ account: String) throws {
        let status = SecItemDelete(baseQuery(account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    private func baseQuery(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

/// The device's SSH identity: an Ed25519 keypair generated on this device.
/// The private key never leaves the Keychain except as a CryptoKit object.
struct DeviceKey: Sendable {
    let privateKey: Curve25519.Signing.PrivateKey

    /// SSH wire-format public key blob: `string "ssh-ed25519"` + `string key`.
    var publicKeyBlob: Data {
        var blob = Data()
        blob.appendSSHString(Data("ssh-ed25519".utf8))
        blob.appendSSHString(privateKey.publicKey.rawRepresentation)
        return blob
    }

    var authorizedKeysLine: String {
        "ssh-ed25519 " + publicKeyBlob.base64EncodedString() + " steer-iphone"
    }

    static func loadOrCreate(in keychain: Keychain) throws -> DeviceKey {
        let account = "device-ed25519"
        if let stored = try keychain.read(account),
            let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: stored)
        {
            return DeviceKey(privateKey: key)
        }
        let key = Curve25519.Signing.PrivateKey()
        try keychain.write(key.rawRepresentation, account)
        return DeviceKey(privateKey: key)
    }
}

extension Data {
    fileprivate mutating func appendSSHString(_ bytes: Data) {
        var length = UInt32(bytes.count).bigEndian
        Swift.withUnsafeBytes(of: &length) { append(contentsOf: $0) }
        append(bytes)
    }
}

/// `SHA256:<base64>` over the host's public key blob, as OpenSSH prints it.
enum HostFingerprint {
    static func display(for blob: Data) -> String {
        "SHA256:" + Data(SHA256.hash(data: blob)).base64EncodedString().trimmingCharacters(in: ["="])
    }
}
