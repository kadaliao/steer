import XCTest

/// Drives real conversations against a real host. Needs:
///   TEST_RUNNER_STEER_UI_HOST     user@host:port the Simulator can reach (device key authorized)
///   TEST_RUNNER_STEER_UI_PROJECT  a throwaway project folder on that host
/// Uses the smallest models; each run spends a few cents.
@MainActor
final class ConversationUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        let env = ProcessInfo.processInfo.environment
        guard let host = env["STEER_UI_HOST"], let project = env["STEER_UI_PROJECT"] else {
            throw XCTSkip("STEER_UI_HOST / STEER_UI_PROJECT not set")
        }
        app = XCUIApplication()
        app.launchArguments = [
            "-steerTrustAny", "-steerSeedHost", host,
            "-lastProject", project,
            "-lastModel.claude", "haiku", "-lastMode.claude", "default",
            "-lastModel.codex", "gpt-5.6-luna", "-lastMode.codex", "untrusted",
            "-draft.new", "",
        ]
    }

    private func shot(_ name: String) {
        let screenshot = app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        // Also straight to a folder (TEST_RUNNER_STEER_UI_SHOTS), for runs whose result bundle never closes.
        if let dir = ProcessInfo.processInfo.environment["STEER_UI_SHOTS"] {
            let test = name.isEmpty ? "shot" : name
            let file = URL(fileURLWithPath: dir).appendingPathComponent("\(self.name.split(separator: " ").last?.dropLast() ?? "test")-\(test).png")
            try? screenshot.pngRepresentation.write(to: file)
        }
    }

    /// The whole screen, springboard included (banners are not the app's).
    private func screen(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        add(XCTAttachment(screenshot: screenshot))
        if let dir = ProcessInfo.processInfo.environment["STEER_UI_SHOTS"] {
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(self.name.split(separator: " ").last?.dropLast() ?? "test")-\(name).png"))
        }
    }

    private func waitEnabled(_ element: XCUIElement, timeout: TimeInterval) {
        let predicate = NSPredicate(format: "exists == true AND enabled == true")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: element)
        XCTAssertEqual(XCTWaiter().wait(for: [expectation], timeout: timeout), .completed, "\(element) not enabled")
    }

    private func turnsDone() -> XCUIElementQuery {
        app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH '完成'"))
    }

    private func waitForTurns(_ count: Int, timeout: TimeInterval, approving decision: String = "允许") {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if turnsDone().count >= count { return }
            let allow = app.buttons[decision]
            if allow.exists, allow.isHittable {
                shot("request")
                allow.tap()
            }
            sleep(1)
        }
        XCTFail("turn \(count) did not finish")
    }

    private var composer: XCUIElement { app.descendants(matching: .any)["composer"].firstMatch }

    private func openNewChat() {
        let newChat = app.buttons["新会话"]
        waitEnabled(newChat, timeout: 60)
        shot("list")
        newChat.tap()
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
    }

    private func send(_ text: String) {
        composer.tap()
        composer.typeText(text)
        app.buttons["send"].tap()
    }

    private func startChat(prompt: String) {
        openNewChat()
        composer.tap()
        composer.typeText(prompt)
        shot("new-chat")
        app.buttons["send"].tap()
    }

    func testClaudeConversation() throws {
        app.launchArguments += ["-lastAgent", "claude"]
        app.launch()
        openNewChat()

        // "/" lists the project's commands and skills; the list can be closed.
        composer.tap()
        composer.typeText("/")
        let close = app.buttons["closeSuggestions"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let anyCommand = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '/'")).firstMatch
        XCTAssertTrue(anyCommand.waitForExistence(timeout: 45), "no commands listed")
        shot("commands")
        composer.typeText("comp")
        let compact = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '/compact'")).firstMatch
        XCTAssertTrue(compact.waitForExistence(timeout: 5), "built-in commands missing")
        shot("commands-filtered")
        close.tap()
        XCTAssertFalse(close.waitForExistence(timeout: 2))
        composer.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 5))

        // The keyboard can be put away.
        app.buttons["hideKeyboard"].tap()
        XCTAssertFalse(app.keyboards.firstMatch.waitForExistence(timeout: 2))

        send("Run ls, then add a function cube(x) to calc.py. Keep it minimal. Reply in one short sentence.")
        sleep(6)
        shot("running")
        waitForTurns(1, timeout: 180)
        shot("first-turn")

        // Claude's suggested next message sends in one tap, when it offers one.
        let suggestion = app.buttons["replySuggestion"]
        if suggestion.waitForExistence(timeout: 25) {
            shot("suggestion")
            suggestion.tap()
        } else {
            send("Explain the change in one sentence.")
        }
        waitForTurns(2, timeout: 180)
        shot("second-turn")

        // Open the folded tool steps and the edit's diff.
        let summary = app.buttons.matching(NSPredicate(format: "label CONTAINS '改 1 个文件'")).firstMatch
        if summary.waitForExistence(timeout: 5) {
            summary.tap()
            shot("tools-expanded")
            let edit = app.buttons.matching(NSPredicate(format: "label CONTAINS 'calc.py' AND label CONTAINS '+'")).firstMatch
            if edit.waitForExistence(timeout: 5) {
                edit.tap()
                sleep(1)
                shot("diff")
                app.buttons["完成"].firstMatch.tap()
            }
        }
        // "/clear" starts a new, empty session in the same folder.
        send("/clear")
        let fresh = app.staticTexts["新会话"].firstMatch
        XCTAssertTrue(fresh.waitForExistence(timeout: 20), "/clear did not open a new session")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'cube'")).firstMatch.exists)
        shot("after-clear")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        sleep(2)
        shot("list-after")
    }

    /// Two hosts (the same Mac by two names): the home list shows both and
    /// the title switches between them. Spends no usage.
    func testTwoHosts() throws {
        let env = ProcessInfo.processInfo.environment
        guard let host = env["STEER_UI_HOST"], let at = host.firstIndex(of: "@") else { return }
        let other = host[..<at] + "@localhost" + (host.contains(":") ? ":" + (host.split(separator: ":").last ?? "22") : "")
        app.launchArguments += ["-steerSeedHost", String(other)]
        app.launch()
        let scope = app.buttons["hostScope"]
        XCTAssertTrue(scope.waitForExistence(timeout: 30))
        waitEnabled(app.buttons["新会话"], timeout: 60)
        sleep(3)
        shot("all-hosts")
        scope.tap()
        shot("scope-menu")
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'localhost'")).firstMatch.tap()
        sleep(1)
        shot("one-host")
        scope.tap()
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH '全部主机'")).firstMatch.tap()
    }

    /// The folder browser makes a folder and the new chat picks it. Browsing
    /// starts at home, so this needs a project folder under home; the new
    /// folder goes inside it. Spends no usage.
    func testNewFolder() throws {
        let project = ProcessInfo.processInfo.environment["STEER_UI_PROJECT"] ?? ""
        app.launch()
        openNewChat()
        let chip = app.buttons.matching(identifier: "projectChip").firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 30))
        chip.tap()
        app.buttons["浏览文件夹…"].tap()
        let newFolder = app.buttons["在这里新建文件夹…"]
        // Browsing starts at home: find it in the project path, then walk
        // down. Folder lists load lazily, so scroll each row into view first.
        let parts = project.split(separator: "/").map(String.init)
        XCTAssertTrue(newFolder.waitForExistence(timeout: 20))
        guard let home = parts.lastIndex(where: { app.navigationBars[$0].exists }) else {
            throw XCTSkip("STEER_UI_PROJECT is not under the host's home folder")
        }
        for component in parts[(home + 1)...] {
            let row = app.collectionViews.buttons[component].firstMatch
            for _ in 0..<20 where !(row.exists && row.isHittable) { app.swipeUp() }
            row.tap()
            XCTAssertTrue(app.navigationBars[component].waitForExistence(timeout: 10))
            XCTAssertTrue(newFolder.waitForExistence(timeout: 20))
        }
        newFolder.tap()
        let name = "ui-folder-\(Int(Date().timeIntervalSince1970))"
        let field = app.alerts.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.typeText(name)
        shot("naming")
        app.alerts.buttons["新建并使用"].tap()
        try waitPicked(name, chip: chip)
    }

    /// A typed path that does not exist is offered as a new folder.
    func testTypedPathMakesFolder() throws {
        let project = ProcessInfo.processInfo.environment["STEER_UI_PROJECT"] ?? ""
        app.launch()
        openNewChat()
        let chip = app.buttons.matching(identifier: "projectChip").firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 30))
        chip.tap()
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10))
        search.tap()
        let name = "ui-typed-\(Int(Date().timeIntervalSince1970))"
        search.typeText(project + "/" + name)
        app.buttons.matching(NSPredicate(format: "label BEGINSWITH '使用 '")).firstMatch.tap()
        let create = app.alerts.buttons["新建并使用"]
        XCTAssertTrue(create.waitForExistence(timeout: 20), "missing folder was not offered")
        shot("offer")
        create.tap()
        try waitPicked(name, chip: chip)
    }

    /// The folder browser starts at home and can climb above it.
    func testBrowseAboveHome() throws {
        app.launch()
        openNewChat()
        let chip = app.buttons.matching(identifier: "projectChip").firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 30))
        chip.tap()
        app.buttons["浏览文件夹…"].tap()
        let up = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '上一级'")).firstMatch
        XCTAssertTrue(up.waitForExistence(timeout: 20))
        up.tap()
        // Home is two levels down on macOS and Linux (/Users/x, /home/x).
        XCTAssertTrue(app.buttons["上一级 · /"].waitForExistence(timeout: 20))
        shot("above-home")
    }

    /// The chip shows the new folder, or the test skips when the host still
    /// runs an older helper without `dirs.create`.
    private func waitPicked(_ name: String, chip: XCUIElement) throws {
        let picked = NSPredicate(format: "label CONTAINS %@", name)
        let old = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH '这台主机上的助手还是旧版本'")).firstMatch
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline {
            if picked.evaluate(with: chip) {
                shot("picked")
                return
            }
            if old.exists {
                shot("old-helper")
                throw XCTSkip("the host is still running an older helper")
            }
            sleep(1)
        }
        XCTFail("new folder was not picked")
    }

    func testCodexConversation() throws {
        app.launchArguments += ["-lastAgent", "codex"]
        app.launch()
        openNewChat()

        // "$" lists Codex skills.
        composer.tap()
        composer.typeText("$")
        let close = app.buttons["closeSuggestions"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let skill = app.buttons.matching(NSPredicate(format: "label BEGINSWITH '$'")).firstMatch
        XCTAssertTrue(skill.waitForExistence(timeout: 45), "no skills listed")
        shot("skills")
        close.tap()
        composer.typeText(XCUIKeyboardKey.delete.rawValue)

        send("Add a function square(x) to calc.py. Keep it minimal. Reply in one short sentence.")
        sleep(8)
        shot("running")
        waitForTurns(1, timeout: 240, approving: "本会话都允许")
        shot("first-turn")
    }

    /// Real APNs (sandbox) to the Simulator: a test push from Settings, then a
    /// turn started from the app that ends while the app is in the background.
    /// Tapping its notification opens the session. Needs the helper's APNs key.
    func testPushNotifications() throws {
        app.launchArguments += ["-lastAgent", "claude"]
        app.launch()
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let allow = springboard.buttons.matching(NSPredicate(format: "label IN {'允许', 'Allow'}")).firstMatch
        waitEnabled(app.buttons["新会话"], timeout: 60)
        if allow.waitForExistence(timeout: 10) { allow.tap() }
        sleep(4)

        app.buttons["设置"].tap()
        let test = app.buttons["发一条测试通知"]
        waitEnabled(test, timeout: 30)
        test.tap()
        let banner = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS '测试通知'")).firstMatch
        let found = banner.waitForExistence(timeout: 30)
        screen("test-push")
        XCTAssertTrue(found, "no test notification")
        app.navigationBars.buttons.element(boundBy: 0).tap()

        startChat(prompt: "Reply with the single word pushed.")
        sleep(2)
        XCUIDevice.shared.press(.home)
        let done = springboard.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS '做完了'")).firstMatch
        let arrived = done.waitForExistence(timeout: 120)
        screen("done-banner")
        XCTAssertTrue(arrived, "no turn-finished notification")
        done.tap()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        let reply = app.staticTexts.matching(NSPredicate(format: "label CONTAINS[c] 'pushed'")).firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 20), "the notification did not open the session")
        shot("opened-from-push")
    }
}

