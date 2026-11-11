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
        .task { await app.connect() }
        .alert(
            "确认主机", isPresented: Binding(get: { app.pendingTrust != nil }, set: { if !$0 { app.pendingTrust = nil } }),
            presenting: app.pendingTrust
        ) { _ in
            Button("取消", role: .cancel) { app.pendingTrust = nil }
            Button("信任并连接") { app.trustPending() }
        } message: { pending in
            Text("第一次连接 \(pending.host.address)。它的指纹是\n\(pending.fingerprint)\n\n在电脑上运行 ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 可以核对。")
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
        if updated.id == app.activeHost?.id {
            app.disconnect()
            Task { await app.connect() }
        }
        if !isFirstRun { dismiss() }
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @State private var phrases = Prefs.quickPhrases
    @State private var newPhrase = ""
    @State private var returnSends = Prefs.returnSends

    var body: some View {
        Form {
            Section("主机") {
                ForEach(app.hosts) { host in
                    HStack {
                        Button {
                            app.switchHost(host)
                        } label: {
                            HStack {
                                Image(systemName: host.id == app.activeHost?.id ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(Color.accentColor)
                                VStack(alignment: .leading) {
                                    Text(host.displayName).foregroundStyle(.primary)
                                    Text("\(host.username)@\(host.address):\(host.port)").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        Spacer()
                        NavigationLink("") { HostEditorView(host: host, isFirstRun: false) }.frame(width: 30)
                    }
                }
                NavigationLink("添加主机") { HostEditorView(host: nil, isFirstRun: false) }
            }
            if let hello = app.hello {
                Section("当前主机") {
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
            Section {
                ForEach(phrases, id: \.self) { phrase in Text(phrase) }
                    .onDelete { phrases.remove(atOffsets: $0); Prefs.quickPhrases = phrases }
                    .onMove { phrases.move(fromOffsets: $0, toOffset: $1); Prefs.quickPhrases = phrases }
                HStack {
                    TextField("新短语", text: $newPhrase)
                    Button("添加") {
                        let phrase = newPhrase.trimmingCharacters(in: .whitespaces)
                        guard !phrase.isEmpty else { return }
                        phrases.append(phrase)
                        Prefs.quickPhrases = phrases
                        newPhrase = ""
                    }
                }
            } header: {
                Text("快捷短语")
            } footer: {
                Text("输入框上方的一排短语，点一下就直接发出。")
            }
            Section {
                Toggle("回车直接发送", isOn: $returnSends)
                    .onChange(of: returnSends) { _, value in Prefs.returnSends = value }
            } footer: {
                Text("关闭时回车换行，用发送按钮发送。")
            }
        }
        .navigationTitle("设置")
        .toolbar { EditButton() }
    }
}
