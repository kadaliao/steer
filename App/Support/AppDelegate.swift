import UIKit
import UserNotifications
import os

/// Remote notifications: the device token goes to every host's helper, which
/// pushes straight to APNs when a turn started from the phone ends or needs
/// an answer. Tapping a notification opens that session.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    @MainActor static weak var model: AppModel?
    /// The session on screen, readable from any thread (notification callbacks).
    static let visibleChat = OSAllocatedUnfairLock<String?>(initialState: nil)

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        AppDelegate.model?.pushTokenReceived(token)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        #if DEBUG
        print("STEER push registration failed: \(error)")
        #endif
    }

    // The completion-handler forms on purpose: with the async forms Swift
    // calls UIKit's completion handler off the main thread, and UIKit aborts
    // (seen when tapping a notification). Answer on the caller's thread and
    // hop to the main actor only for our own work.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let sid = notification.request.content.userInfo["sid"] as? String
        let viewing = sid != nil && AppDelegate.visibleChat.withLock { $0 } == sid
        completionHandler(viewing ? [] : [.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let sid = info["sid"] as? String
        let host = info["host"] as? String
        Task { @MainActor in AppDelegate.model?.openFromNotification(host: host, sid: sid) }
        completionHandler()
    }
}
