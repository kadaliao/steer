import SwiftUI
import UIKit

struct RootView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        Group {
            if app.hosts.isEmpty {
                NavigationStack {
                    HostEditorView(host: nil, isFirstRun: true)
                }
            } else {
                HomeView()
            }
        }
        .task { await app.connectAll() }
        .alert(
            "确认主机", isPresented: Binding(get: { !app.trustQueue.isEmpty }, set: { _ in }),
            presenting: app.trustQueue.first
        ) { _ in
            Button("取消", role: .cancel) { app.declineTrust() }
            Button("信任并连接") { app.trustPending() }
        } message: { pending in
            Text("第一次连接 \(pending.host.displayName)（\(pending.host.address)）。它的指纹是\n\(pending.fingerprint)\n\n在电脑上运行 ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 可以核对。")
        }
    }
}

/// Add or edit a host: address, port, user, and how to log in.
struct HostEditorView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let host: HostConfig?
    let isFirstRun: Bool

    @State private var name = ""
    @State private var address = ""
    @State private var port = "22"
    @State private var username = ""
    @State private var auth: AuthMethod = .deviceKey
    @State private var password = ""
    @State private var publicKey = ""
    @State private var copied = false

    var body: some View {
        Form {
            if let host, let conn = app.connection(host.id) {
                HostStatusSection(conn: conn)
            }
            if isFirstRun {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("连接你的电脑").font(.title2.bold())
                        Text("Steer 通过 SSH 登录你的电脑，在那里运行 Claude Code 和 Codex。电脑需要打开“远程登录”（macOS：系统设置 › 通用 › 共享）。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    .listRowBackground(Color.clear)
                }
            }
            Section("主机") {
                TextField("地址（IP 或域名）", text: $address)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                TextField("端口", text: $port).keyboardType(.numberPad)
                TextField("用户名", text: $username).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("名称（可选）", text: $name)
            }
            Section {
                Picker("登录方式", selection: $auth) {
                    Text("本机密钥").tag(AuthMethod.deviceKey)
                    Text("密码").tag(AuthMethod.password)
                }
                .pickerStyle(.segmented)
                if auth == .password {
                    SecureField(host == nil ? "密码" : "密码（不改就留空）", text: $password)
                } else {
                    Text(publicKey).font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
                    Button(copied ? "已复制" : "复制添加公钥的命令") {
                        UIPasteboard.general.string = "mkdir -p ~/.ssh && echo '\(publicKey)' >> ~/.ssh/authorized_keys"
                        copied = true
                    }
                }
            } header: {
                Text("登录")
            } footer: {
                if auth == .deviceKey {
                    Text("这台手机生成了自己的密钥，私钥只存在钥匙串里。把上面的命令在电脑终端里运行一次即可。")
                }
            }
            Section {
                Button(host == nil ? "保存并连接" : "保存") { save() }
                    .disabled(address.isEmpty || username.isEmpty || Int(port) == nil)
            }
            if let host, !isFirstRun {
                Section {
                    if host.fingerprint != nil {
                        Button("重置主机指纹") {
                            var updated = host
                            updated.fingerprint = nil
                            app.saveHost(updated, password: nil)
                        }
                    }
                    Button("删除这台主机", role: .destructive) {
                        app.deleteHost(host)
                        dismiss()
                    }
                }
            }
        }
        .navigationTitle(isFirstRun ? "" : (host == nil ? "添加主机" : "编辑主机"))
        .onAppear(perform: fill)
    }

    private func fill() {
        if let host {
            name = host.name
            address = host.address
            port = String(host.port)
            username = host.username
            auth = host.auth
        }
        publicKey = (try? app.deviceKey().authorizedKeysLine) ?? ""
    }

    private func save() {
        var updated = host ?? HostConfig(name: name, address: address, username: username)
        let changedEndpoint = updated.address != address || updated.port != Int(port)
        updated.name = name
        updated.address = address.trimmingCharacters(in: .whitespaces)
        updated.port = Int(port) ?? 22
        updated.username = username.trimmingCharacters(in: .whitespaces)
        updated.auth = auth
        if changedEndpoint { updated.fingerprint = nil }
        app.saveHost(updated, password: auth == .password ? password : nil)
        app.connection(updated.id)?.reconnect()
        if !isFirstRun { dismiss() }
    }
}

/// A host's link state and what runs there.
struct HostStatusSection: View {
    let conn: HostConnection

    var body: some View {
        Section("状态") {
            HStack {
                StatusDot(status: conn.status)
                Text(HostStatusText.text(conn.status)).lineLimit(3)
                Spacer()
                if !conn.isConnected {
                    Button("重连") { conn.reconnect() }
                }
            }
            if let hello = conn.hello {
                LabeledContent("Claude Code", value: hello.agents["claude"]??.version ?? String(localized: "未安装"))
                LabeledContent("Codex", value: hello.agents["codex"]??.version ?? String(localized: "未安装"))
                LabeledContent("主机助手", value: hello.helper)
                LabeledContent("Python", value: hello.python ?? "")
                if hello.platform == "darwin", hello.launchd != "launchd" {
                    Text("助手没能以登录会话身份运行，Claude 可能读不到登录状态。在电脑上登录桌面后重连即可。")
                        .font(.footnote).foregroundStyle(.orange)
                }
            }
        }
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @State private var returnSends = Prefs.returnSends

    var body: some View {
        Form {
            Section {
                ForEach(app.ordered) { conn in
                    NavigationLink {
                        HostEditorView(host: conn.host, isFirstRun: false)
                    } label: {
                        HStack(spacing: 10) {
                            StatusDot(status: conn.status)
                            VStack(alignment: .leading) {
                                Text(conn.name)
                                Text("\(conn.host.username)@\(conn.host.address):\(conn.host.port)")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                NavigationLink("添加主机") { HostEditorView(host: nil, isFirstRun: false) }
            } header: {
                Text("主机")
            } footer: {
                Text("所有主机同时保持连接，首页可以看全部，也可以只看一台。")
            }
            Section {
                Toggle("回车直接发送", isOn: $returnSends)
                    .onChange(of: returnSends) { _, value in Prefs.returnSends = value }
            } footer: {
                Text("关闭时回车换行，用发送按钮发送。")
            }
        }
        .navigationTitle("设置")
    }
}
