# 发 TestFlight

在维护者的 Mac 上发。签名和上传都用 App Store Connect API 密钥
（`~/.appstoreconnect/private_keys/AuthKey_<密钥 ID>.p8`），Xcode 里不用登录账号。

```sh
ASC_KEY_ID=<密钥 ID> ASC_ISSUER_ID=<Issuer ID> scripts/testflight.sh
ASC_KEY_ID=… ASC_ISSUER_ID=… NOTES="这一版改了什么" scripts/testflight.sh
```

脚本会：归档并签名（团队 `HMDAVG9B8J`）→ 导出 → 用 `altool` 上传 →
`scripts/beta.sh` 等 App Store Connect 处理完（5–15 分钟），写上"测试内容"。

- **版本号**：`project.yml` 的 `MARKETING_VERSION`，要发新版本时手动改。
- **构建号**：自动取 App Store Connect 上已有的最大构建号加一；要指定就传 `BUILD_NUMBER=<n>`。
- **谁能装**：内部测试组「内部测试」，第一次运行 `beta.sh` 时建好，团队里的
  App Store Connect 用户都在里面。内部测试不用过 TestFlight 审核，处理完打开
  TestFlight 就能装。
- **加密合规**：Info.plist 里 `ITSAppUsesNonExemptEncryption` 为 `false`，上传后不用再答问卷。

## 第一次之前

已经做过（2026-11-11），App 记录叫 Steer，App ID `6833468787`。要重做时：

1. 套装 ID `com.kadaliao.steer` 用 API 注册（`scripts/asc.sh POST /v1/bundleIds …`）。
2. App 记录只能在网页上建（API 不允许创建 `apps`）：App Store Connect › App › ＋ ›
   新建 App，平台 iOS，名称 Steer，主要语言简体中文，套装 ID `com.kadaliao.steer`，
   SKU `steer-ios`。脚本按套装 ID 找它。

## 调 API

`scripts/asc.sh <方法> <路径> [JSON]`，例如
`scripts/asc.sh GET "/v1/apps?filter[bundleId]=com.kadaliao.steer"`。

开着代理或 VPN 时 `altool` 可能报 TLS 错误（`-9816`）。重传之前先到 App Store
Connect 看构建是不是已经在了。
