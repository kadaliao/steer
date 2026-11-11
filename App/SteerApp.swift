import SwiftUI

@main
struct SteerApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var app: AppModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let app = AppModel()
        #if DEBUG
        DebugLaunch.apply(to: app)
        #endif
        AppDelegate.model = app
        _app = State(initialValue: app)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(app)
                .onChange(of: scenePhase) { _, phase in app.scenePhaseChanged(phase) }
        }
    }
}
