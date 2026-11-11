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
        ]
    }

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
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

    private func startChat(prompt: String) {
        let newChat = app.buttons["新会话"]
        waitEnabled(newChat, timeout: 60)
        shot("list")
        newChat.tap()
        let composer = app.descendants(matching: .any)["composer"].firstMatch
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        composer.typeText(prompt)
        shot("new-chat")
        app.buttons["send"].tap()
    }

    func testClaudeConversation() throws {
        app.launchArguments += ["-lastAgent", "claude"]
        app.launch()
        startChat(prompt: "Run ls, then add a function cube(x) to calc.py. Keep it minimal. Reply in one short sentence.")
        sleep(6)
        shot("running")
        waitForTurns(1, timeout: 180)
        shot("first-turn")

        // A quick phrase sends immediately.
        app.buttons["解释一下改动"].tap()
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
        app.navigationBars.buttons.element(boundBy: 0).tap()
        sleep(2)
        shot("list-after")
    }

    func testCodexConversation() throws {
        app.launchArguments += ["-lastAgent", "codex"]
        app.launch()
        startChat(prompt: "Add a function square(x) to calc.py. Keep it minimal. Reply in one short sentence.")
        sleep(8)
        shot("running")
        waitForTurns(1, timeout: 240, approving: "本会话都允许")
        shot("first-turn")
    }
}
