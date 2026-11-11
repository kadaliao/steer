import SwiftUI

@main
struct SteerApp: App {
    @State private var app: AppModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let app = AppModel()
        #if DEBUG
        DebugLaunch.apply(to: app)
        #endif
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
